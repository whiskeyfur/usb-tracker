"""Reading and changing a device's runtime power-management setting.

`power/control` is owned by root, so changing it means asking polkit. The
app never escalates on its own: a menu item the user picked leads to pkexec,
which puts up its own authentication dialog, and the user answers it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

from .sysfs import SYS_USB

# Exactly the shapes sysfs uses, so nothing else can reach the command line.
BUSID_RE = re.compile(r"^(?:usb\d{1,3}|\d{1,3}-\d{1,3}(?:\.\d{1,3})*)$")

CONTROL_AUTO = "auto"      # the kernel may autosuspend this device
CONTROL_ON = "on"          # keep it powered; no autosuspend

PKEXEC = shutil.which("pkexec")

# Installed by packaging/install-helper.sh. When present, polkit authorises
# this one narrow operation (and remembers it for the session) instead of
# authorising a root shell every time.
HELPER = "/usr/libexec/usb-tracker/usb-tracker-power-helper"
POLICY = "/usr/share/polkit-1/actions/dev.local.usbtracker.policy"


def helper_installed() -> bool:
    return os.access(HELPER, os.X_OK) and os.path.exists(POLICY)


@dataclass
class Result:
    ok: bool
    message: str
    cancelled: bool = False


def control_path(busid: str) -> str:
    if not BUSID_RE.match(busid):
        raise ValueError(f"not a USB bus id: {busid!r}")
    return os.path.join(SYS_USB, busid, "power", "control")


def read_control(busid: str) -> str:
    try:
        with open(control_path(busid)) as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


def writable(busid: str) -> bool:
    try:
        return os.access(control_path(busid), os.W_OK)
    except ValueError:
        return False


def pkexec_command(busid: str, allow: bool) -> list[str]:
    """The command polkit will run as root, with the bus id already validated."""
    path = control_path(busid)                     # validates busid
    value = CONTROL_AUTO if allow else CONTROL_ON
    pkexec = PKEXEC or "pkexec"
    if helper_installed():
        return [pkexec, HELPER, busid, value]
    # Fallback: no helper installed, so ask pkexec for a one-off write. This
    # authorises a root shell, so polkit will ask every single time.
    return [pkexec, "/bin/sh", "-c", f"printf %s {value} > {path}"]


def set_autosuspend(busid: str, allow: bool) -> Result:
    """Allow or prevent runtime suspend for one device.

    Tries a plain write first — some systems loosen the permissions — and
    otherwise goes through pkexec, which puts up its own authentication
    dialog. The setting lasts until the device is replugged or the machine
    reboots; udev_rule() is the durable form.
    """
    try:
        path = control_path(busid)
    except ValueError as exc:
        return Result(False, str(exc))

    value = CONTROL_AUTO if allow else CONTROL_ON
    verb = "Autosuspend allowed" if allow else "Autosuspend prevented"

    try:
        with open(path, "w") as fh:
            fh.write(value)
        return Result(True, f"{verb} for this device.")
    except PermissionError:
        pass
    except OSError as exc:
        return Result(False, f"Could not change it: {exc}")

    if not PKEXEC:
        return Result(False, "Needs root, and pkexec is not installed. "
                             f"Run: echo {value} | sudo tee {path}")
    try:
        proc = subprocess.run(pkexec_command(busid, allow),
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(False, f"Could not run pkexec: {exc}")

    if proc.returncode == 0:
        hint = ("" if helper_installed() else
                "  Run packaging/install-helper.sh to stop being asked every time.")
        return Result(True, f"{verb} for this device." + hint)
    if proc.returncode in (126, 127):
        return Result(False, "Authentication dismissed; nothing changed.",
                      cancelled=True)
    detail = (proc.stderr or "").strip().splitlines()
    return Result(False, detail[-1] if detail else
                  f"pkexec exited with {proc.returncode}.")


def udev_rule(vid: str, pid: str, allow: bool, label: str = "") -> str:
    """A rule that reapplies the setting on every plug and every boot.

    Writing power/control only lasts until the device is replugged, so this
    is the durable form of the same change.
    """
    value = CONTROL_AUTO if allow else CONTROL_ON
    comment = f"# {label}\n" if label else ""
    return (f'{comment}ACTION=="add", SUBSYSTEM=="usb", '
            f'ATTR{{idVendor}}=="{vid}", ATTR{{idProduct}}=="{pid}", '
            f'TEST=="power/control", ATTR{{power/control}}="{value}"\n')
