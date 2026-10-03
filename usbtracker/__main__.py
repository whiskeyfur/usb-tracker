"""Entry point: GUI by default, plus headless recording and one-shot listing."""

from __future__ import annotations

import argparse
import signal
import sys
import time

from . import store
from .monitor import Monitor, _bps


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="usb-tracker",
        description="Track USB topology, estimated power draw, and drops.")
    parser.add_argument("--db", default=store.DEFAULT_DB,
                        help=f"history database (default: {store.DEFAULT_DB})")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="seconds between polls (default: 2)")
    parser.add_argument("--keep-days", type=float, default=7.0,
                        help="discard samples older than this (default: 7)")
    parser.add_argument("--no-kmsg", action="store_true",
                        help="skip kernel-log scraping for resets and faults")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true",
                      help="record in the terminal, no window")
    mode.add_argument("--list", action="store_true",
                      help="print the current tree once and exit")
    mode.add_argument("--events", action="store_true",
                      help="print the recorded event log and exit")
    return parser


def _monitor_for_cli(args) -> Monitor:
    mon = Monitor(db_path=args.db, interval=args.interval,
                  use_kmsg=not args.no_kmsg, prune_days=args.keep_days)
    mon._store = store.Store(args.db)
    mon._rows = {d.key: d for d in mon._store.devices()}
    mon._adopt_stale_rows()
    return mon


def cmd_list(args) -> int:
    mon = _monitor_for_cli(args)
    mon.tick()
    time.sleep(min(1.0, args.interval))
    snap = mon.tick()

    def walk(key: str, depth: int) -> None:
        node = snap.nodes[key]
        mark = " " if node.present else "~"
        name = node.label if node.present else f"({node.label})"
        tree = f"{'  ' * depth}{name}"
        traffic = (_bps(node.total_bps) if node.measured
                   else f"({_bps(node.alloc_bps)})" if node.alloc_bps else "–")
        print(f"{mark} {tree:<40.40}"
              f"{node.est_ma:7.1f} /{node.budget_ma:6.0f} mA "
              f"{node.subtree_ma:8.1f} {traffic:>12} "
              f"{node.urb_rate:7.0f} {node.status or 'lost':<10}"
              f"{node.busid}")
        for child in node.children:
            walk(child, depth + 1)

    print(f"{'':2}{'device':<40}{'draw / budget':>18} {'subtree':>8} "
          f"{'traffic':>12} {'URB/s':>7} {'state':<10}port")
    for root in snap.roots:
        walk(root, 0)
    print(f"\n{snap.live_count} connected, {snap.lost_count} remembered but gone, "
          f"{snap.total_ma:.0f} mA estimated in total, "
          f"{_bps(snap.total_alloc_bps)} of bus bandwidth reserved"
          + (f", {_bps(snap.total_bps)} measured" if snap.total_bps else ""))
    print("Traffic in parentheses is reserved bandwidth: no byte counter "
          "exists for that device.")
    mon._store.close()
    return 0


def cmd_events(args) -> int:
    db = store.Store(args.db)
    rows = db.events(limit=200)
    if not rows:
        print("No events recorded yet.")
        return 0
    names = {d.key: d.label for d in db.devices()}
    for ts, key, kind, detail in reversed(rows):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        who = names.get(key, key or "—")
        print(f"{stamp}  {kind:<12} {who:<34.34} {detail}")
    db.close()
    return 0


def cmd_daemon(args) -> int:
    mon = _monitor_for_cli(args)
    stop = False

    def handler(_sig, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    print(f"Recording to {args.db} every {args.interval:g}s. Ctrl-C to stop.")
    last_count = None
    cutoff = 0.0
    first = True
    while not stop:
        snap = mon.tick()
        if (snap.live_count, snap.lost_count) != last_count:
            last_count = (snap.live_count, snap.lost_count)
            print(f"{time.strftime('%H:%M:%S')}  {snap.live_count} connected, "
                  f"{snap.lost_count} gone, {snap.total_ma:.0f} mA estimated, "
                  f"{_bps(snap.total_alloc_bps)} reserved")
        fresh = [e for e in mon._store.events(limit=200) if e[0] > cutoff]
        cutoff = snap.ts
        if first:
            # The opening tick logs an attach for everything already plugged
            # in; the count above already says that.
            first = False
        else:
            names = {d.key: d.label for d in mon._store.devices()}
            for ts, key, kind, detail in reversed(fresh):
                print(f"{time.strftime('%H:%M:%S', time.localtime(ts))}  "
                      f"{kind:<12} {names.get(key, key or '—'):<30.30} {detail}")
        time.sleep(args.interval)
    mon._store.close()
    print("Stopped.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        return cmd_list(args)
    if args.events:
        return cmd_events(args)
    if args.daemon:
        return cmd_daemon(args)
    try:
        from .app import run
    except Exception as exc:                      # missing PyGObject or no display
        print(f"Cannot start the window: {exc}\n"
              "Install PyGObject with GTK 4 (Debian/Ubuntu: "
              "apt install python3-gi gir1.2-gtk-4.0), or record headless with "
              "--daemon.", file=sys.stderr)
        return 1
    return run(args.db, args.interval, not args.no_kmsg, args.keep_days)


if __name__ == "__main__":
    sys.exit(main())
