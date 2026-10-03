"""Best-effort kernel-log scraping for events sysfs cannot show.

Polling sysfs sees a device vanish, but not *why*: a port reset, an
over-current trip and a failed enumeration all look the same from outside.
The kernel ring buffer names them. Unavailable when kernel.dmesg_restrict is
set, in which case the tracker simply runs without these extras.
"""

from __future__ import annotations

import re
import subprocess
import time

from . import store

TS_RE = re.compile(r"^\[\s*(\d+\.\d+)\]\s+(.*)$")

# (pattern, event kind, summary). Group names feed the summary text.
PATTERNS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^usb (?P<bus>\S+): USB disconnect, device number (?P<num>\d+)"),
     store.KIND_DISCONNECT, "kernel saw disconnect (device {num})"),
    (re.compile(r"^usb (?P<bus>\S+): new (?P<speed>\S+)-speed USB device number "
                r"(?P<num>\d+)"),
     store.KIND_CONNECT, "kernel enumerated {speed}-speed device {num}"),
    (re.compile(r"^usb (?P<bus>\S+): reset (?P<speed>\S+)-speed USB device number "
                r"(?P<num>\d+)"),
     store.KIND_RESET, "port reset ({speed}-speed, device {num})"),
    (re.compile(r"^usb usb(?P<hub>\d+)-port(?P<port>\d+): over-current condition"),
     store.KIND_OVERCURRENT, "over-current on bus {hub} port {port}"),
    (re.compile(r"^hub (?P<bus>\S+?):[\d.]+: over-current condition on port "
                r"(?P<port>\d+)"),
     store.KIND_OVERCURRENT, "over-current on port {port}"),
    (re.compile(r"^usb (?P<bus>\S+): device descriptor read/(?P<phase>\S+), "
                r"error (?P<err>-?\d+)"),
     store.KIND_ERROR, "descriptor read failed ({phase}, error {err})"),
    (re.compile(r"^usb (?P<bus>\S+): device not accepting address (?P<num>\d+), "
                r"error (?P<err>-?\d+)"),
     store.KIND_ERROR, "not accepting address (error {err})"),
    (re.compile(r"^usb (?P<bus>\S+): unable to enumerate USB device"),
     store.KIND_ERROR, "enumeration failed"),
    (re.compile(r"^usb (?P<bus>\S+): device-level power management is disabled"),
     store.KIND_ERROR, "device-level power management disabled"),

    # Host-controller trouble. These name a PCI address rather than a bus id;
    # the monitor maps it back through the root hubs' serial numbers.
    (re.compile(r"^xhci_hcd (?P<pci>\S+): xHCI host controller not responding"),
     store.KIND_CONTROLLER, "host controller stopped responding"),
    (re.compile(r"^xhci_hcd (?P<pci>\S+): HC died"),
     store.KIND_CONTROLLER, "host controller died, kernel is cleaning up"),
    (re.compile(r"^xhci_hcd (?P<pci>\S+): Host halt failed"),
     store.KIND_CONTROLLER, "host controller halt failed"),
    (re.compile(r"^xhci_hcd (?P<pci>\S+): Host (?:not accessible|controller not "
                r"halted), reset failed"),
     store.KIND_CONTROLLER, "host controller reset failed"),
    (re.compile(r"^xhci_hcd (?P<pci>\S+): Timeout while waiting for (?P<what>.+)"),
     store.KIND_CONTROLLER, "controller timeout waiting for {what}"),
    (re.compile(r"^usb usb(?P<hub>\d+)-port(?P<port>\d+): Cannot enable"),
     store.KIND_ERROR, "port would not enable (kernel suspects the cable)"),
    (re.compile(r"^usb (?P<bus>\S+?)-port(?P<port>\d+): Cannot enable"),
     store.KIND_ERROR, "port {port} would not enable (kernel suspects the cable)"),
    (re.compile(r"^hub (?P<bus>\S+?):[\d.]+: hub_ext_port_status failed "
                r"\(err = (?P<err>-?\d+)\)"),
     store.KIND_ERROR, "hub stopped answering port status (error {err})"),
    (re.compile(r"^hub (?P<bus>\S+?):[\d.]+: activate --> (?P<err>-?\d+)"),
     store.KIND_ERROR, "hub failed to activate (error {err})"),
]


def boot_time() -> float:
    try:
        with open("/proc/uptime") as fh:
            return time.time() - float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


class KernelLog:
    """Incremental reader over `dmesg` output."""

    def __init__(self, backfill_seconds: float = 300.0):
        self.available = True
        self.error = ""
        self._last_kts = -1.0
        self._backfill = backfill_seconds

    def _run(self) -> list[str]:
        try:
            proc = subprocess.run(["dmesg"], capture_output=True, text=True,
                                  timeout=5, errors="replace")
        except (OSError, subprocess.SubprocessError) as exc:
            self.available = False
            self.error = str(exc)
            return []
        if proc.returncode != 0:
            self.available = False
            self.error = (proc.stderr or "dmesg failed").strip().splitlines()[-1:][0] \
                if proc.stderr else "dmesg unavailable"
            return []
        return proc.stdout.splitlines()

    def poll(self) -> list[tuple[float, str, str, str]]:
        """Return new (wall_ts, busid, kind, detail) entries since the last poll."""
        if not self.available:
            return []
        lines = self._run()
        if not lines:
            return []
        boot = boot_time()
        first_pass = self._last_kts < 0
        floor = (time.time() - boot - self._backfill) if first_pass else self._last_kts
        out: list[tuple[float, str, str, str]] = []
        high_water = self._last_kts

        for line in lines:
            if "usb" not in line and "hub" not in line:
                continue
            m = TS_RE.match(line)
            if not m:
                continue
            kts = float(m.group(1))
            if kts > high_water:
                high_water = kts
            if kts <= floor:
                continue
            msg = m.group(2)
            for pattern, kind, template in PATTERNS:
                hit = pattern.match(msg)
                if not hit:
                    continue
                fields = hit.groupdict()
                busid = fields.get("bus") or ""
                if not busid and fields.get("pci"):
                    busid = f"pci:{fields['pci']}"
                elif not busid and "hub" in fields:
                    # "usb usb3-port4" style: the port names a child of the root hub.
                    busid = f"{fields['hub']}-{fields['port']}"
                elif busid and "port" in fields and kind == store.KIND_OVERCURRENT:
                    busid = f"{busid}.{fields['port']}"
                detail = template.format(**fields)
                out.append((boot + kts, busid, kind, detail))
                break

        self._last_kts = high_water
        return out
