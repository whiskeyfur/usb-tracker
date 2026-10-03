"""Group kernel-log USB events into outages and say what they have in common.

The live tracker can only see what happens while it runs. The kernel ring
buffer already holds this boot's history, so this reads the same shapes out
of it and gives an answer immediately.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from . import store
from .kmsg import KernelLog
from .monitor import _common_ancestor, _dur

BURST_WINDOW = 12.0        # events this close together are one disturbance
MIN_DEVICES = 3


@dataclass
class Burst:
    start: float = 0.0
    end: float = 0.0
    gone: list[str] = field(default_factory=list)
    back: list[str] = field(default_factory=list)
    errors: list[tuple[float, str, str]] = field(default_factory=list)

    @property
    def devices(self) -> list[str]:
        seen = list(dict.fromkeys(self.gone + self.back))
        return seen

    @property
    def scope(self) -> str | None:
        return _common_ancestor(self.devices)

    @property
    def ancestor_dropped(self) -> bool:
        return self.scope in self.gone if self.scope else False

    @property
    def span(self) -> float:
        return max(0.0, self.end - self.start)

    @property
    def recovered(self) -> bool:
        return len(self.back) >= max(1, int(len(self.gone) * 0.8))


def collect(backfill_seconds: float = 30 * 86400) -> tuple[list[Burst], bool, str]:
    """Read the whole ring buffer and group it. Returns (bursts, ok, error)."""
    log = KernelLog(backfill_seconds=backfill_seconds)
    entries = log.poll()
    if not log.available:
        return ([], False, log.error or "the kernel log could not be read")
    entries.sort(key=lambda e: e[0])

    bursts: list[Burst] = []
    current: Burst | None = None
    for ts, busid, kind, detail in entries:
        if current is not None and ts - current.end > BURST_WINDOW:
            bursts.append(current)
            current = None
        if current is None:
            current = Burst(start=ts, end=ts)
        current.end = ts
        if kind == store.KIND_DISCONNECT:
            current.gone.append(busid)
        elif kind == store.KIND_CONNECT:
            current.back.append(busid)
        else:
            current.errors.append((ts, kind, detail))
    if current is not None:
        bursts.append(current)

    return ([b for b in bursts if len(b.devices) >= MIN_DEVICES], True, "")


def _load_note(db, scope: str | None, when: float) -> str:
    """Pull the recorded downstream draw for a burst, if the database has it."""
    if db is None or not scope:
        return ""
    try:
        key = db.key_for_busid(scope)
        if not key:
            return ""
        last, peak = db.load_before(key, when)
    except Exception:
        return ""
    if last is None or last <= 0:
        return ""
    note = f" carrying {last:.0f} mA downstream"
    if peak is not None and peak > last * 1.15:
        note += f" (peak {peak:.0f} mA)"
    return note


def report(bursts: list[Burst], boot_cutoff: float | None = None,
           db=None) -> list[str]:
    """Human-readable findings, as lines."""
    out: list[str] = []
    if not bursts:
        out.append("No multi-device USB disturbances found in the kernel log.")
        return out

    # The first burst after boot is just the machine enumerating everything.
    real = [b for b in bursts
            if boot_cutoff is None or b.start > boot_cutoff]

    out.append(f"{len(real)} multi-device disturbance"
               f"{'' if len(real) == 1 else 's'} in the kernel log"
               f"{' (the initial boot enumeration is excluded)' if len(real) != len(bursts) else ''}:")
    out.append("")
    for burst in real:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(burst.start))
        scope = burst.scope or "several controllers"
        shape = ("the hub itself dropped first" if burst.ancestor_dropped
                 else "everything behind it dropped" if burst.scope
                 else "spanned controllers -- system-wide")
        out.append(f"  {when}  {len(burst.devices):2} devices, "
                   f"{burst.span:.1f}s, under {scope} -- {shape}"
                   + _load_note(db, burst.scope, burst.start))
        if burst.errors:
            for _ts, kind, detail in burst.errors[:3]:
                out.append(f"      kernel said: {kind}: {detail}")
        if not burst.recovered and burst.gone:
            out.append(f"      {len(burst.gone) - len(burst.back)} device(s) "
                       f"did not come back")

    if not real:
        return out

    out.append("")
    scopes = [b.scope for b in real if b.scope]
    common = set(scopes)
    if len(common) == 1 and len(real) > 1:
        scope = scopes[0]
        hub_first = sum(1 for b in real if b.ancestor_dropped)
        out.append(f"Every one of them is under {scope}.")
        if hub_first == len(real):
            out.append(f"In each case {scope} dropped first and took its tree "
                       f"with it, which is the signature of that hub losing "
                       f"power or its upstream link -- not a host-controller "
                       f"or kernel fault.")
        out.append("")
        out.append("Worth checking, roughly in order of likelihood:")
        out.append(f"  - the hub's own power supply, if it is a powered hub")
        out.append(f"  - the cable between {scope} and the port it plugs into")
        out.append(f"  - what the devices behind it draw in total (usb-tracker")
        out.append(f"    shows this: select the hub and tick 'Including downstream')")
        out.append(f"  - the hub running hot, if the drops cluster after it has")
        out.append(f"    been busy")
    elif not any(b.errors for b in real):
        out.append("No kernel error accompanies these drops, which points at "
                   "power or a connection rather than software.")
    return out


def anomaly_free_note() -> str:
    return ("Nothing in the kernel log yet. Leave the tracker running "
            "(python3 -m usbtracker --daemon) and it will record the next one.")
