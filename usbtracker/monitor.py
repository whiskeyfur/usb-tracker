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


@dataclass
class Snapshot:
    ts: float = 0.0
    nodes: dict[str, Node] = field(default_factory=dict)
    roots: list[str] = field(default_factory=list)
    total_ma: float = 0.0
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
        for key, dev in live.items():
            prev = self._prev.get(key)
            est[key] = dev.estimate_ma(prev)

        subtree = self._subtree_totals(live, busid_to_key, est)

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
                self_powered=dev.self_powered, ts=now, present=True)

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
        for key, prev in self._prev.items():
            if key in live:
                continue
            st.add_event(now, key, store.KIND_DISCONNECT,
                         f"gone from {prev.port_path}")
            st.bump_counter(key, "disconnects")
            st.mark_absent(key, now)

        # --- samples
        for key, dev in live.items():
            st.add_sample(now, key, est[key], float(dev.max_power_ma),
                          subtree.get(key, est[key]), dev.runtime_status,
                          dev.urb_rate(self._prev.get(key), dt))

        # --- kernel log extras
        if self.kmsg is not None:
            for kts, busid, kind, detail in self.kmsg.poll():
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

        snap = self._build_snapshot(now, live, busid_to_key, est, subtree)
        with self._lock:
            self.snapshot = snap
        if self.on_snapshot is not None:
            self.on_snapshot(snap)
        return snap

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
                        est: dict[str, float]) -> dict[str, float]:
        """Own draw plus everything downstream, deepest first."""
        order = sorted(live, key=lambda k: len(live[k].busid), reverse=True)
        totals = {k: est.get(k, 0.0) for k in live}
        for key in order:
            parent_busid = live[key].parent_busid
            pkey = busid_to_key.get(parent_busid or "")
            if pkey:
                totals[pkey] = totals.get(pkey, 0.0) + totals[key]
        return totals

    def _build_snapshot(self, now: float, live: dict[str, UsbDevice],
                        busid_to_key: dict[str, str], est: dict[str, float],
                        subtree: dict[str, float]) -> Snapshot:
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
                urb_rate=dev.urb_rate(self._prev.get(key), self.interval) if dev else 0.0,
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
        if self.kmsg is not None:
            snap.kmsg_ok = self.kmsg.available
            snap.kmsg_error = self.kmsg.error
        snap.db_bytes = st.db_size()
        return snap


def _dur(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"
