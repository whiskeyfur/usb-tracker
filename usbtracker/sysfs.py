"""Read the USB topology out of /sys/bus/usb/devices.

Everything here is a plain read of sysfs: no root, no libusb, no udev. One
call to scan() returns a snapshot of every USB device the kernel currently
knows about, with the counters needed to estimate power draw and spot drops.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

SYS_USB = "/sys/bus/usb/devices"

ROOT_RE = re.compile(r"^usb(\d+)$")
DEV_RE = re.compile(r"^(\d+)-(\d+(?:\.\d+)*)$")
IFACE_RE = re.compile(r"^(?:\d+)-(?:\d+(?:\.\d+)*):(\d+)\.(\d+)$")

# Suspended devices are allowed 2.5 mA by the spec; used to floor the estimate.
SUSPEND_MA = 2.5

CLASS_NAMES = {
    0x00: "per-interface",
    0x01: "audio",
    0x02: "communications",
    0x03: "HID",
    0x05: "physical",
    0x06: "image",
    0x07: "printer",
    0x08: "mass storage",
    0x09: "hub",
    0x0A: "CDC data",
    0x0B: "smart card",
    0x0D: "content security",
    0x0E: "video",
    0x0F: "healthcare",
    0x10: "audio/video",
    0x11: "billboard",
    0x12: "USB-C bridge",
    0xDC: "diagnostic",
    0xE0: "wireless",
    0xEF: "miscellaneous",
    0xFE: "application",
    0xFF: "vendor-specific",
}


def _text(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _int(path: str, default: int = 0) -> int:
    raw = _text(path)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _hex(path: str, default: int = 0) -> int:
    raw = _text(path)
    if not raw:
        return default
    try:
        return int(raw, 16)
    except ValueError:
        return default


def _ma(path: str) -> int:
    """bMaxPower reads as e.g. '100mA' (older kernels: '2' in 2 mA units)."""
    raw = _text(path).lower()
    if not raw:
        return 0
    if raw.endswith("ma"):
        raw = raw[:-2]
    try:
        return int(float(raw))
    except ValueError:
        return 0


def _speed_label(mbps: float) -> str:
    for limit, name in ((1.5, "low"), (12, "full"), (480, "high"), (5000, "super"),
                        (10000, "super+"), (20000, "super+ x2"), (40000, "USB4")):
        if abs(mbps - limit) < 0.01:
            return name
    return ""


@dataclass
class Interface:
    name: str
    number: int
    cls: int
    subclass: int
    protocol: int
    driver: str

    @property
    def class_name(self) -> str:
        return CLASS_NAMES.get(self.cls, f"class 0x{self.cls:02x}")


@dataclass
class UsbDevice:
    busid: str                  # sysfs name: "usb3", "3-4.1.2"
    key: str = ""               # identity that survives a replug
    parent_busid: str | None = None
    is_root_hub: bool = False

    vid: str = ""
    pid: str = ""
    serial: str = ""
    product: str = ""
    manufacturer: str = ""
    version: str = ""

    dev_class: int = 0
    speed_mbps: float = 0.0
    max_power_ma: int = 0
    self_powered: bool = False
    configuration: int = 0
    num_interfaces: int = 0
    max_children: int = 0
    removable: str = ""
    connect_type: str = ""
    port_location: str = ""

    runtime_status: str = ""
    active_time_ms: int = 0
    suspended_time_ms: int = 0
    connected_ms: int = 0
    autosuspend_ms: int = -1
    control: str = ""
    urbnum: int = 0
    over_current: int = 0

    interfaces: list[Interface] = field(default_factory=list)

    # ---- presentation helpers -------------------------------------------

    @property
    def is_hub(self) -> bool:
        return self.dev_class == 0x09 or self.max_children > 0

    @property
    def label(self) -> str:
        name = self.product or ""
        if not name:
            name = self.class_hint
        if self.is_root_hub:
            name = name or "Root hub"
            return f"{name} (bus {self.busid.removeprefix('usb')})"
        return name or f"{self.vid}:{self.pid}"

    @property
    def class_hint(self) -> str:
        """Device class, falling back to the interface classes."""
        if self.dev_class not in (0x00, 0xEF) or not self.interfaces:
            return CLASS_NAMES.get(self.dev_class, f"class 0x{self.dev_class:02x}")
        seen: list[str] = []
        for iface in self.interfaces:
            if iface.class_name not in seen:
                seen.append(iface.class_name)
        return " + ".join(seen[:3])

    @property
    def speed_label(self) -> str:
        label = _speed_label(self.speed_mbps)
        mbps = self.speed_mbps
        shown = f"{mbps:g} Mb/s" if mbps < 1000 else f"{mbps / 1000:g} Gb/s"
        return f"{shown} ({label}-speed)" if label else shown

    @property
    def port_path(self) -> str:
        """Human port path: bus 3, port 4.1.2."""
        match = DEV_RE.match(self.busid)
        if match:
            return f"bus {match.group(1)} port {match.group(2)}"
        match = ROOT_RE.match(self.busid)
        return f"bus {match.group(1)} controller" if match else self.busid

    def drivers(self) -> list[str]:
        return sorted({i.driver for i in self.interfaces if i.driver})

    def active_fraction(self, prev: "UsbDevice | None") -> float:
        """Share of the interval the device spent powered up (0.0-1.0).

        Derived from the runtime-PM counters, which is real kernel data rather
        than a guess. Falls back to the instantaneous status on the first
        sample or when the counters do not move.
        """
        if prev is not None:
            d_active = self.active_time_ms - prev.active_time_ms
            d_susp = self.suspended_time_ms - prev.suspended_time_ms
            total = d_active + d_susp
            if total > 0 and d_active >= 0 and d_susp >= 0:
                return max(0.0, min(1.0, d_active / total))
        if self.runtime_status == "suspended":
            return 0.0
        if self.runtime_status == "active":
            return 1.0
        return 1.0

    def estimate_ma(self, prev: "UsbDevice | None") -> float:
        """Estimated draw from the upstream bus, in mA at 5 V.

        bMaxPower is a *declared ceiling*, not a measurement -- sysfs exposes
        no actual current. Weighting it by the runtime-PM duty cycle is the
        closest honest approximation available without inline hardware.
        """
        budget = self.max_power_ma
        if budget <= 0:
            return 0.0
        frac = self.active_fraction(prev)
        return budget * frac + SUSPEND_MA * (1.0 - frac)

    def urb_rate(self, prev: "UsbDevice | None", dt: float) -> float:
        if prev is None or dt <= 0 or self.urbnum < prev.urbnum:
            return 0.0
        return (self.urbnum - prev.urbnum) / dt


def _parent_busid(name: str) -> str | None:
    match = DEV_RE.match(name)
    if not match:
        return None            # root hub or controller
    bus, path = match.groups()
    segments = path.split(".")
    if len(segments) == 1:
        return f"usb{bus}"
    return f"{bus}-{'.'.join(segments[:-1])}"


def identity_key(dev: UsbDevice) -> str:
    """Stable identity across a replug.

    A serial number is the only thing that follows a device between ports, so
    prefer it. Without one, the port path is the identity -- two identical
    serial-less devices are genuinely indistinguishable otherwise.
    """
    if dev.serial:
        return f"{dev.vid}:{dev.pid}:{dev.serial}"
    return f"{dev.vid}:{dev.pid}@{dev.busid}"


def _read_interfaces(path: str) -> list[Interface]:
    out: list[Interface] = []
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return out
    for entry in entries:
        if not IFACE_RE.match(entry):
            continue
        ipath = os.path.join(path, entry)
        if not os.path.isdir(ipath):
            continue
        driver = ""
        try:
            driver = os.path.basename(os.readlink(os.path.join(ipath, "driver")))
        except OSError:
            pass
        out.append(Interface(
            name=entry,
            number=_hex(os.path.join(ipath, "bInterfaceNumber")),
            cls=_hex(os.path.join(ipath, "bInterfaceClass")),
            subclass=_hex(os.path.join(ipath, "bInterfaceSubClass")),
            protocol=_hex(os.path.join(ipath, "bInterfaceProtocol")),
            driver=driver,
        ))
    return out


def read_device(busid: str) -> UsbDevice | None:
    path = os.path.join(SYS_USB, busid)
    if not os.path.isdir(path):
        return None

    def p(*parts: str) -> str:
        return os.path.join(path, *parts)

    dev = UsbDevice(busid=busid)
    dev.is_root_hub = bool(ROOT_RE.match(busid))
    dev.parent_busid = _parent_busid(busid)

    dev.vid = _text(p("idVendor"))
    dev.pid = _text(p("idProduct"))
    dev.serial = _text(p("serial"))
    dev.product = _text(p("product"))
    dev.manufacturer = _text(p("manufacturer"))
    dev.version = _text(p("version"))

    dev.dev_class = _hex(p("bDeviceClass"))
    try:
        dev.speed_mbps = float(_text(p("speed")) or 0)
    except ValueError:
        dev.speed_mbps = 0.0
    dev.max_power_ma = _ma(p("bMaxPower"))
    dev.self_powered = bool(_hex(p("bmAttributes")) & 0x40)
    dev.configuration = _int(p("bConfigurationValue"))
    dev.num_interfaces = _int(p("bNumInterfaces"))
    dev.max_children = _int(p("maxchild"))
    dev.removable = _text(p("removable"))
    dev.connect_type = _text(p("port", "connect_type"))
    dev.port_location = _text(p("port", "location"))
    dev.over_current = _int(p("port", "over_current_count"))

    dev.runtime_status = _text(p("power", "runtime_status"))
    dev.active_time_ms = _int(p("power", "runtime_active_time"))
    dev.suspended_time_ms = _int(p("power", "runtime_suspended_time"))
    dev.connected_ms = _int(p("power", "connected_duration"))
    dev.autosuspend_ms = _int(p("power", "autosuspend_delay_ms"), -1)
    dev.control = _text(p("power", "control"))
    dev.urbnum = _int(p("urbnum"))

    dev.interfaces = _read_interfaces(path)
    dev.key = identity_key(dev)
    return dev


def scan() -> dict[str, UsbDevice]:
    """Snapshot every USB device, keyed by sysfs bus id."""
    try:
        names = os.listdir(SYS_USB)
    except OSError:
        return {}
    devices: dict[str, UsbDevice] = {}
    for name in names:
        if ":" in name:          # an interface, collected with its device
            continue
        if not (ROOT_RE.match(name) or DEV_RE.match(name)):
            continue
        dev = read_device(name)
        if dev is not None:
            devices[name] = dev
    return devices


def sort_key(busid: str) -> tuple:
    """Order siblings the way a port list reads: 1, 2, 10 -- not 1, 10, 2."""
    match = ROOT_RE.match(busid)
    if match:
        return (int(match.group(1)),)
    match = DEV_RE.match(busid)
    if match:
        bus, path = match.groups()
        return (int(bus),) + tuple(int(x) for x in path.split("."))
    return (9999, busid)
