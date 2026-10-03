"""The polling loop: diff successive sysfs snapshots into events and samples."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import store, sysfs
from .kmsg import KernelLog
from .store import DeviceRow, Store
from .sysfs import UsbDevice

# A disconnect followed by a reconnect inside this window is a flap, not a
# deliberate unplug -- usually a marginal cable, a hub browning out, or a
# port losing contact.
FLAP_WINDOW = 20.0

# When a hub loses power, or a controller falls over, devices do not drop one
# at a time -- a cluster goes within a second or two and comes back together.
# Reporting that as one attributed event is far more use than fourteen
# unrelated disconnects.
OUTAGE_MIN_DEVICES = 3
OUTAGE_WINDOW = 8.0          # drops this close together are the same event
OUTAGE_GIVE_UP = 180.0       # after this, treat it as an unplug, not an outage
OUTAGE_RECOVERED = 0.8       # share of devices back before calling it over

# A poll this much later than asked for means the tracker itself was starved.
STALL_FACTOR = 3.0
STALL_FLOOR = 2.0


@dataclass
class Node:
    """One row of the tree: live device, remembered device, or both."""

    key: str
    label: str = ""
    busid: str = ""
    parent_key: str | None = None
    present: bool = False
    class_hint: str = ""
    vid: str = ""
    pid: str = ""
    serial: str = ""
    manufacturer: str = ""
    version: str = ""
    speed_label: str = ""
    is_hub: bool = False
    self_powered: bool = False
    budget_ma: float = 0.0
    est_ma: float = 0.0
    subtree_ma: float = 0.0
    status: str = ""
    urb_rate: float = 0.0
    rx_bps: float = 0.0
    tx_bps: float = 0.0
    alloc_bps: float = 0.0
    sub_bps: float = 0.0
    link_bps: float = 0.0
    counter_source: str = ""
    control: str = ""            # "auto" = may autosuspend, "on" = kept awake
    autosuspend_ms: int = -1
    first_seen: float = 0.0
    last_seen: float = 0.0
    connects: int = 0
    disconnects: int = 0
    resets: int = 0
    errors: int = 0
    over_current: int = 0
    drivers: tuple[str, ...] = ()
    interfaces: tuple[str, ...] = ()
    children: list[str] = field(default_factory=list)
    device: UsbDevice | None = None

    @property
    def lost(self) -> bool:
        return not self.present

    @property
    def total_bps(self) -> float:
        return self.rx_bps + self.tx_bps

    @property
    def autosuspend_allowed(self) -> bool:
        return self.control != "on"

    @property
    def measured(self) -> bool:
        """True when the kernel counts this device's bytes for real."""
        return bool(self.counter_source)


@dataclass
class Snapshot:
    ts: float = 0.0
    nodes: dict[str, Node] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    outages_24h: int = 0
    last_outage: str = ""
    outage_open: bool = False
    total_ma: float = 0.0
    total_bps: float = 0.0
    total_alloc_bps: float = 0.0
    live_count: int = 0
    lost_count: int = 0
    interval: float = 2.0
    kmsg_ok: bool = True
    kmsg_error: str = ""
    db_path: str = ""
    db_bytes: int = 0
    paused: bool = False


class Monitor:
    """Polls sysfs on a background thread and records what changed.

    Owns its own SQLite connection; the UI gets snapshots through a callback
    and reads history with a separate connection.
    """

    def __init__(self, db_path: str = store.DEFAULT_DB, interval: float = 2.0,
                 use_kmsg: bool = True, prune_days: float = 7.0,
                 on_snapshot=None, scan_fn=None):
        self.db_path = db_path
        self.scan_fn = scan_fn or sysfs.scan
        self.interval = max(0.25, interval)
        self.prune_days = prune_days
        self.on_snapshot = on_snapshot
        self.kmsg = KernelLog() if use_kmsg else None

        self._store: Store | None = None
        self._prev: dict[str, UsbDevice] = {}
        self._prev_ts = 0.0
        self._rows: dict[str, DeviceRow] = {}
        self._recent_drops: list[tuple[float, str, str]] = []
        self._outage: dict | None = None
        self._skip_stall = True
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._paused = False
        self._lock = threading.Lock()
        self.snapshot = Snapshot(interval=self.interval, db_path=db_path)
        self.last_error = ""

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="usb-monitor",
                                        daemon=True)
        self._thread.start()

    def stop(self, join: bool = True) -> None:
        self._stop.set()
        self._wake.set()
        if join and self._thread is not None:
            self._thread.join(timeout=5.0)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        self._skip_stall = True      # a pause is not a stall
        self._wake.set()

    def set_interval(self, interval: float) -> None:
        self.interval = max(0.25, interval)
        self._wake.set()

    def poke(self) -> None:
        self._wake.set()

    def _run(self) -> None:
        self._store = Store(self.db_path)
        self._rows = {d.key: d for d in self._store.devices()}
        self._adopt_stale_rows()
        last_prune = 0.0
        while not self._stop.is_set():
            try:
                if not self._paused:
                    self.tick()
                    if time.time() - last_prune > 3600:
                        self._store.prune(self.prune_days)
                        last_prune = time.time()
            except Exception as exc:                  # keep the thread alive
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._wake.wait(self.interval)
            self._wake.clear()
        self._store.close()

    def _adopt_stale_rows(self) -> None:
        """Devices left marked present by a previous run are not live now."""
        now = time.time()
        stale = [r for r in self._rows.values() if r.present]
        if stale:
            live_keys = {d.key for d in self.scan_fn().values()}
            for row in stale:
                if row.key in live_keys:
                    continue
                self._store.mark_absent(row.key, row.last_seen or now)
                self._store.add_event(now, row.key, store.KIND_DISCONNECT,
                                      "gone since the tracker last ran")
                self._store.bump_counter(row.key, "disconnects")
                row.present = False
                row.disconnects += 1
        self._store.add_event(now, "", store.KIND_SESSION, "tracking started")
        self._store.commit()

    # ---- one poll -------------------------------------------------------

    def tick(self) -> Snapshot:
        assert self._store is not None
        st = self._store
        now = time.time()
        dt = (now - self._prev_ts) if self._prev_ts else self.interval

        live_by_busid = self.scan_fn()

        # Resolve key collisions (identical serials on two ports) by port.
        live: dict[str, UsbDevice] = {}
        for busid in sorted(live_by_busid, key=sysfs.sort_key):
            dev = live_by_busid[busid]
            key = dev.key
            if key in live:
                key = f"{key}@{busid}"
                dev.key = key
            live[key] = dev

        busid_to_key = {d.busid: k for k, d in live.items()}

        est: dict[str, float] = {}
        flow: dict[str, tuple[float, float]] = {}
        alloc: dict[str, float] = {}
        urb: dict[str, float] = {}
        for key, dev in live.items():
            prev = self._prev.get(key)
            est[key] = dev.estimate_ma(prev)
            flow[key] = dev.throughput(prev, dt)
            alloc[key] = dev.reserved_bps
            # computed here, while _prev still holds the previous poll
            urb[key] = dev.urb_rate(prev, dt)

        subtree = self._subtree_totals(live, busid_to_key, est)
        sub_bps = self._subtree_totals(
            live, busid_to_key, {k: sum(v) for k, v in flow.items()})

        # --- appearances and changes
        for key, dev in live.items():
            prev = self._prev.get(key)
            row = self._rows.get(key)
            parent_key = busid_to_key.get(dev.parent_busid or "")
            is_new = st.upsert_device(
                key=key, busid=dev.busid, parent_key=parent_key, vid=dev.vid,
                pid=dev.pid, serial=dev.serial, product=dev.product,
                manufacturer=dev.manufacturer, class_hint=dev.class_hint,
                speed_mbps=dev.speed_mbps, max_power_ma=dev.max_power_ma,
                version=dev.version, is_hub=dev.is_hub,
                self_powered=dev.self_powered, ts=now, present=True,
                counter_source=dev.counter_source)

            if is_new:
                st.add_event(now, key, store.KIND_ATTACH,
                             f"{dev.port_path}, {dev.speed_label}, "
                             f"budget {dev.max_power_ma} mA")
                st.bump_counter(key, "connects")
            elif row is not None and not row.present:
                gap = now - (row.last_seen or now)
                st.add_event(now, key, store.KIND_CONNECT,
                             f"back on {dev.port_path} after {_dur(gap)}")
                st.bump_counter(key, "connects")
                if gap <= FLAP_WINDOW:
                    st.add_event(now, key, store.KIND_FLAP,
                                 f"dropped and returned within {_dur(gap)}")

            if prev is not None:
                self._diff_device(st, now, key, prev, dev)

        # --- disappearances
        dropped: list[tuple[str, str]] = []
        for key, prev in self._prev.items():
            if key in live:
                continue
            st.add_event(now, key, store.KIND_DISCONNECT,
                         f"gone from {prev.port_path}")
            st.bump_counter(key, "disconnects")
            st.mark_absent(key, now)
            dropped.append((key, prev.busid))

        self._check_stall(st, now)
        self._note_drops(st, now, dropped)
        self._check_recovery(st, now, live)

        # --- samples
        for key, dev in live.items():
            rx, tx = flow[key]
            st.add_sample(now, key, est[key], float(dev.max_power_ma),
                          subtree.get(key, est[key]), dev.runtime_status,
                          urb[key],
                          rx_bps=rx, tx_bps=tx, alloc_bps=alloc[key],
                          sub_bps=sub_bps.get(key, rx + tx))

        # --- kernel log extras
        if self.kmsg is not None:
            pci_to_key = {d.serial: k for k, d in live.items()
                          if d.is_root_hub and d.serial}
            for kts, busid, kind, detail in self.kmsg.poll():
                if busid.startswith("pci:"):
                    key = pci_to_key.get(busid[4:], "")
                    detail = f"{detail} [{busid[4:]}]" if not key else detail
                else:
                    key = busid_to_key.get(busid, "")
                    if not key:
                        key = self._last_key_for_busid(busid)
                if kind in (store.KIND_DISCONNECT, store.KIND_CONNECT):
                    # sysfs polling already covers these; keep only the
                    # diagnoses polling cannot see.
                    continue
                st.add_event(kts, key, kind, detail if key else
                             f"{detail} [{busid}]")

        st.commit()
        self._rows = {d.key: d for d in st.devices()}
        self._prev = live
        self._prev_ts = now

        snap = self._build_snapshot(now, live, busid_to_key, est, subtree,
                                    flow, alloc, sub_bps, urb)
        with self._lock:
            self.snapshot = snap
        if self.on_snapshot is not None:
            self.on_snapshot(snap)
        return snap

    def _check_stall(self, st: Store, now: float) -> None:
        """Notice when the tracker itself stopped getting scheduled."""
        if not self._prev_ts:
            return
        if self._skip_stall:
            self._skip_stall = False
            return
        gap = now - self._prev_ts
        if gap - self.interval > max(STALL_FLOOR, self.interval * STALL_FACTOR):
            st.add_event(now, "", store.KIND_STALL,
                         f"no polling for {_dur(gap)} -- machine busy, asleep, "
                         f"or the tracker was stopped; USB events in that "
                         f"window may be missing")

    def _note_drops(self, st: Store, now: float,
                    dropped: list[tuple[str, str]]) -> None:
        if dropped:
            self._recent_drops.extend((now, key, busid) for key, busid in dropped)
        cutoff = now - OUTAGE_WINDOW
        self._recent_drops = [d for d in self._recent_drops if d[0] >= cutoff]

        if self._outage is not None:
            # a cascade still unfolding belongs to the outage already open
            for _ts, key, busid in self._recent_drops:
                self._outage["keys"].add(key)
                if busid not in self._outage["busids"]:
                    self._outage["busids"].append(busid)
            return

        if len(self._recent_drops) < OUTAGE_MIN_DEVICES:
            return

        keys = {key for _ts, key, _b in self._recent_drops}
        busids = [b for _ts, _k, b in self._recent_drops]
        scope = _common_ancestor(busids)
        self._outage = {
            "start": min(ts for ts, _k, _b in self._recent_drops),
            "keys": set(keys),
            "busids": list(dict.fromkeys(busids)),
            "scope": scope,
        }
        detail = self._describe_outage(len(keys), scope, busids)
        detail += self._load_clause(st, scope, self._outage["start"])
        st.add_event(now, "", store.KIND_OUTAGE, detail)

    def _describe_outage(self, count: int, scope: str | None,
                         busids: list[str]) -> str:
        if scope is None:
            buses = {b.split("-")[0] for b in busids if "-" in b}
            return (f"{count} devices across {len(buses)} controllers went away "
                    f"at once -- this looks system-wide")
        label = self._label_for_busid(scope)
        named = f"{scope} ({label})" if label else scope
        if scope.startswith("usb"):
            return (f"{count} devices went away with the whole controller "
                    f"{named}")
        if scope in busids:
            return (f"{count} devices went away with the hub at {named} -- "
                    f"the hub dropped first and took everything behind it")
        return f"{count} devices behind {named} went away together"

    def _load_clause(self, st: Store, scope: str | None, when: float) -> str:
        """What the hub was carrying just before it went, if we sampled it."""
        if not scope:
            return ""
        key = self._key_for_busid(scope) or st.key_for_busid(scope)
        if not key:
            return ""
        last, peak = st.load_before(key, when)
        if last is None or last <= 0:
            return ""
        clause = f", carrying {last:.0f} mA downstream at the time"
        if peak is not None and peak > last * 1.15:
            clause += f" (peaking at {peak:.0f} mA in the preceding minute)"
        return clause

    def _key_for_busid(self, busid: str) -> str:
        for key, dev in self._prev.items():
            if dev.busid == busid:
                return key
        return ""

    def _label_for_busid(self, busid: str) -> str:
        for dev in self._prev.values():
            if dev.busid == busid:
                return dev.label
        for row in self._rows.values():
            if row.busid == busid:
                return row.label
        return ""

    def _check_recovery(self, st: Store, now: float,
                        live: dict[str, UsbDevice]) -> None:
        if self._outage is None:
            return
        keys = self._outage["keys"]
        back = {k for k in keys if k in live}
        elapsed = now - self._outage["start"]
        if len(back) >= max(1, int(len(keys) * OUTAGE_RECOVERED)):
            scope = self._outage["scope"] or "several controllers"
            st.add_event(now, "", store.KIND_OUTAGE,
                         f"back after {_dur(elapsed)} -- {len(back)} of "
                         f"{len(keys)} devices returned on {scope}")
            self._outage = None
        elif elapsed > OUTAGE_GIVE_UP:
            st.add_event(now, "", store.KIND_OUTAGE,
                         f"{len(keys) - len(back)} of {len(keys)} devices never "
                         f"came back after {_dur(elapsed)} -- treating it as an "
                         f"unplug rather than an outage")
            self._outage = None

    def _last_key_for_busid(self, busid: str) -> str:
        for row in self._rows.values():
            if row.busid == busid:
                return row.key
        return ""

    def _diff_device(self, st: Store, now: float, key: str,
                     prev: UsbDevice, dev: UsbDevice) -> None:
        if prev.runtime_status != dev.runtime_status:
            if dev.runtime_status == "suspended":
                st.add_event(now, key, store.KIND_SUSPEND, "autosuspended")
            elif dev.runtime_status == "active" and prev.runtime_status:
                st.add_event(now, key, store.KIND_RESUME, "resumed")
        if prev.max_power_ma != dev.max_power_ma:
            st.add_event(now, key, store.KIND_CONFIG,
                         f"power budget {prev.max_power_ma} -> "
                         f"{dev.max_power_ma} mA")
        if prev.configuration != dev.configuration:
            st.add_event(now, key, store.KIND_CONFIG,
                         f"configuration {prev.configuration} -> "
                         f"{dev.configuration}")
        if prev.speed_mbps != dev.speed_mbps:
            st.add_event(now, key, store.KIND_CONFIG,
                         f"link speed {prev.speed_label} -> {dev.speed_label}")
        if dev.over_current > prev.over_current:
            st.add_event(now, key, store.KIND_OVERCURRENT,
                         f"port over-current count {prev.over_current} -> "
                         f"{dev.over_current}")
        before, after = prev.reserved_bps, dev.reserved_bps
        if abs(after - before) > max(64.0, before * 0.02):
            st.add_event(now, key, store.KIND_BANDWIDTH,
                         f"reserved bus bandwidth {_bps(before)} -> "
                         f"{_bps(after)}")
        if prev.control != dev.control and prev.control and dev.control:
            st.add_event(now, key, store.KIND_CONFIG,
                         "autosuspend prevented (kept powered)"
                         if dev.control == "on" else "autosuspend allowed")
        old_drivers, new_drivers = prev.drivers(), dev.drivers()
        if old_drivers != new_drivers:
            gone = [d for d in old_drivers if d not in new_drivers]
            came = [d for d in new_drivers if d not in old_drivers]
            bits = []
            if came:
                bits.append("bound " + ", ".join(came))
            if gone:
                bits.append("released " + ", ".join(gone))
            st.add_event(now, key, store.KIND_DRIVER, "; ".join(bits))

    def _subtree_totals(self, live: dict[str, UsbDevice],
                        busid_to_key: dict[str, str],
                        values: dict[str, float]) -> dict[str, float]:
        """A device's own value plus everything downstream, deepest first."""
        order = sorted(live, key=lambda k: len(live[k].busid), reverse=True)
        totals = {k: values.get(k, 0.0) for k in live}
        for key in order:
            parent_busid = live[key].parent_busid
            pkey = busid_to_key.get(parent_busid or "")
            if pkey:
                totals[pkey] = totals.get(pkey, 0.0) + totals[key]
        return totals

    def _build_snapshot(self, now: float, live: dict[str, UsbDevice],
                        busid_to_key: dict[str, str], est: dict[str, float],
                        subtree: dict[str, float],
                        flow: dict[str, tuple[float, float]],
                        alloc: dict[str, float],
                        sub_bps: dict[str, float],
                        urb: dict[str, float]) -> Snapshot:
        snap = Snapshot(ts=now, interval=self.interval, db_path=self.db_path,
                        paused=self._paused)
        st = self._store
        assert st is not None
        counts = {}
        for key in self._rows:
            counts[key] = st.event_counts(key)

        for key, row in self._rows.items():
            dev = live.get(key)
            kinds = counts.get(key, {})
            node = Node(
                key=key,
                label=dev.label if dev else (row.label or key),
                busid=dev.busid if dev else row.busid,
                parent_key=(busid_to_key.get(dev.parent_busid or "") if dev
                            else row.parent_key),
                present=dev is not None,
                class_hint=dev.class_hint if dev else row.class_hint,
                vid=row.vid, pid=row.pid, serial=row.serial,
                manufacturer=row.manufacturer, version=row.version,
                speed_label=dev.speed_label if dev else
                            (f"{row.speed_mbps:g} Mb/s" if row.speed_mbps else ""),
                is_hub=dev.is_hub if dev else row.is_hub,
                self_powered=dev.self_powered if dev else row.self_powered,
                budget_ma=float(dev.max_power_ma if dev else row.max_power_ma),
                est_ma=est.get(key, 0.0),
                subtree_ma=subtree.get(key, 0.0),
                status=dev.runtime_status if dev else "",
                urb_rate=urb.get(key, 0.0),
                rx_bps=flow.get(key, (0.0, 0.0))[0],
                tx_bps=flow.get(key, (0.0, 0.0))[1],
                alloc_bps=alloc.get(key, 0.0),
                sub_bps=sub_bps.get(key, 0.0),
                link_bps=dev.link_bps if dev else 0.0,
                counter_source=dev.counter_source if dev else row.counter_source,
                control=dev.control if dev else "",
                autosuspend_ms=dev.autosuspend_ms if dev else -1,
                first_seen=row.first_seen, last_seen=row.last_seen,
                connects=row.connects, disconnects=row.disconnects,
                resets=kinds.get(store.KIND_RESET, 0),
                errors=kinds.get(store.KIND_ERROR, 0) +
                       kinds.get(store.KIND_OVERCURRENT, 0),
                over_current=dev.over_current if dev else 0,
                drivers=tuple(dev.drivers()) if dev else (),
                interfaces=tuple(
                    f"{i.name}  {i.class_name}" + (f"  [{i.driver}]" if i.driver else "")
                    for i in dev.interfaces) if dev else (),
                device=dev,
            )
            snap.nodes[key] = node

        # Link children; a node whose parent is unknown becomes a root.
        for key, node in snap.nodes.items():
            parent = snap.nodes.get(node.parent_key or "")
            if parent is not None and parent.key != key:
                parent.children.append(key)
            else:
                snap.roots.append(key)

        def order(k: str) -> tuple:
            n = snap.nodes[k]
            return (0 if n.present else 1,) + sysfs.sort_key(n.busid)

        snap.roots.sort(key=order)
        for node in snap.nodes.values():
            node.children.sort(key=order)

        snap.live_count = sum(1 for n in snap.nodes.values() if n.present)
        snap.lost_count = len(snap.nodes) - snap.live_count
        snap.total_ma = sum(est.get(k, 0.0) for k in live)
        snap.total_bps = sum(sum(flow.get(k, (0.0, 0.0))) for k in live)
        snap.total_alloc_bps = sum(alloc.get(k, 0.0) for k in live)
        if self.kmsg is not None:
            snap.kmsg_ok = self.kmsg.available
            snap.kmsg_error = self.kmsg.error
        snap.db_bytes = st.db_size()
        recent = st.outages(since=now - 86400, limit=100)
        starts = [r for r in recent if "back after" not in r[3]
                  and "never came back" not in r[3]]
        snap.outages_24h = len(starts)
        snap.last_outage = starts[0][3] if starts else ""
        snap.outage_open = self._outage is not None
        return snap


def _common_ancestor(busids: list[str]) -> str | None:
    """The deepest point every dropped device hangs off.

    Fourteen devices going at once is not fourteen faults; it is one fault at
    whatever they share. None means they spanned controllers.
    """
    paths = [b for b in busids if "-" in b]
    if not paths:
        roots = {b for b in busids}
        return roots.pop() if len(roots) == 1 else None
    buses = {b.split("-", 1)[0] for b in paths}
    if len(buses) != 1 or len(buses) != len({b.split("-", 1)[0] for b in busids}):
        return None
    bus = buses.pop()
    segments = [b.split("-", 1)[1].split(".") for b in paths]
    common: list[str] = []
    for parts in zip(*segments):
        if len(set(parts)) == 1:
            common.append(parts[0])
        else:
            break
    return f"{bus}-{'.'.join(common)}" if common else f"usb{bus}"


def _bps(value: float) -> str:
    for unit in ("B/s", "kB/s", "MB/s"):
        if value < 1024 or unit == "MB/s":
            return f"{value:.0f} {unit}" if value >= 10 else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB/s"


def _dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
