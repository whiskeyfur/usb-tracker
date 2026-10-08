"""What a glanceable view of the recorder's output says.

Kept apart from tray.py on purpose. The panel icon has to be GTK 3, the
window is GTK 4, and one process cannot load both -- so anything worth
testing, or worth reusing anywhere else, lives here where it imports no
toolkit at all.

Nothing here writes. The one judgement it makes is whether the recorder is
still running: the `present` flags in the database are only ever as fresh as
the last poll, so without that check a dead recorder would leave the icon
cheerfully reporting a device count from hours ago.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The recorder is presumed dead once its last sample is older than this
# multiple of its poll interval, with a floor for very fast intervals.
STALE_AFTER = 6.0
STALE_FLOOR = 20.0

# Treat an outage as worth drawing attention to for this long.
ALERT_FOR = 600.0


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h ago"
    return f"{seconds / 86400:.0f} days ago"


@dataclass
class Summary:
    counts: str = ""
    outage: str = ""
    outages: list[tuple[float, str]] = field(default_factory=list)
    live: int = 0
    gone: int = 0
    stale: bool = True
    alert: bool = False


def summarise(db, now: float, interval: float) -> Summary:
    """Read the database once and say what the icon should show.

    `db` is any store.Store; nothing here needs it to be writable.
    """
    devices = db.devices()
    live = sum(1 for d in devices if d.present)
    gone = len(devices) - live
    last = db.last_sample_ts()
    stale = last is None or (now - last) > max(STALE_AFTER * interval,
                                               STALE_FLOOR)

    if stale:
        when = "never" if last is None else ago(now - last)
        counts = f"not recording · last sample {when}"
    else:
        counts = f"{live} connected" + (f" · {gone} gone" if gone else "")

    # The recovery half of an outage is logged as a second outage event; it is
    # not a second outage.
    starts = [(ts, " ".join(detail.split()))
              for ts, _key, _kind, detail in db.outages(since=now - 86400,
                                                        limit=12)
              if "back after" not in detail and "never came back" not in detail]
    if starts:
        outage = f"{len(starts)} outage{'' if len(starts) == 1 else 's'} in 24 h"
    else:
        outage = "no outages in 24 h"

    recent = bool(starts) and (now - starts[0][0]) < ALERT_FOR
    return Summary(counts=counts, outage=outage, outages=starts, live=live,
                   gone=gone, stale=stale, alert=recent or stale)
