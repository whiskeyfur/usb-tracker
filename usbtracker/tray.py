"""Panel icon: a StatusNotifierItem that watches what the recorder records.

GTK 3, deliberately. The only tray library installed on this kind of desktop
is AyatanaAppIndicator3, which is GTK 3, and a single process cannot load
GTK 3 and GTK 4 at once. So the panel icon is its own process: it reads the
same database read-only, never writes and never polls sysfs itself, and
launches the GTK 4 window as a separate program when asked.

Under StatusNotifier there is no plain "icon was clicked" event -- the panel
opens the menu instead -- so the thing you most want is the first menu item,
not a click handler.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import GLib, Gtk                             # noqa: E402

from . import store                                             # noqa: E402
from .summary import ago, summarise                                  # noqa: E402

APP_ID = "usb-tracker"
ICON = "drive-removable-media-usb"
ICON_ATTENTION = "dialog-warning"

# One icon per panel. An abstract socket is the tidiest lock on Linux: the
# name disappears with the process, so there is no stale file to clean up
# after a crash.
LOCK_NAME = "\0usb-tracker-tray"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def take_lock() -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.bind(LOCK_NAME)
    except OSError:
        sock.close()
        return None
    return sock


class Tray:
    def __init__(self, db_path: str, interval: float, unit: str,
                 show_label: bool) -> None:
        self.db_path = db_path
        self.interval = max(0.25, interval)
        self.unit = unit
        self.show_label = show_label
        self.db: store.Store | None = None
        self.window: subprocess.Popen | None = None
        # Set once the icon is being torn down, so a poll already in flight
        # does not come back and touch a destroyed indicator.
        self.closing = False

        self.indicator = AppIndicator.Indicator.new(
            APP_ID, ICON, AppIndicator.IndicatorCategory.HARDWARE)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.indicator.set_title("USB Tracker")
        self.indicator.set_attention_icon_full(ICON_ATTENTION, "USB outage")

        self.menu = Gtk.Menu()
        self.items: dict[str, Gtk.MenuItem] = {}
        self._build_menu()
        self.indicator.set_menu(self.menu)
        # Middle-click goes straight to the window; left-click belongs to the
        # panel, which uses it to open the menu.
        self.indicator.set_secondary_activate_target(self.items["open"])

    # ---- menu -----------------------------------------------------------

    def _build_menu(self) -> None:
        # The primary action comes first: there is no click event to put it on.
        self.items["open"] = self._item("Open USB Tracker", self._on_open)
        self.menu.append(self.items["open"])

        self.menu.append(Gtk.SeparatorMenuItem())

        self.items["counts"] = self._label_item("reading…")
        self.menu.append(self.items["counts"])
        self.items["recorder"] = self._label_item("")
        self.menu.append(self.items["recorder"])

        self.items["outage"] = self._label_item("")
        self.menu.append(self.items["outage"])
        self.outage_menu = Gtk.Menu()
        self.items["outages"] = Gtk.MenuItem(label="Recent outages")
        self.items["outages"].set_submenu(self.outage_menu)
        self.menu.append(self.items["outages"])

        self.menu.append(Gtk.SeparatorMenuItem())

        self.items["restart"] = self._item(f"Restart {self.unit.split('.')[0]}",
                                           self._on_restart)
        self.menu.append(self.items["restart"])
        self.menu.append(self._item("Quit panel icon", self._on_quit))
        self.menu.show_all()

    def _item(self, label: str, handler) -> Gtk.MenuItem:
        item = Gtk.MenuItem(label=label)
        item.connect("activate", handler)
        return item

    def _label_item(self, label: str) -> Gtk.MenuItem:
        """A line of status, not a thing to click."""
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(False)
        return item

    # ---- state ----------------------------------------------------------

    def _store(self) -> store.Store | None:
        """Open on first use and reopen if the file was replaced."""
        if self.db is None:
            if not os.path.exists(self.db_path):
                return None
            try:
                self.db = store.Store(self.db_path, read_only=True)
            except Exception:
                return None
        return self.db

    def poll(self) -> bool:
        if self.closing:
            return False
        try:
            self._refresh()
        except Exception as exc:
            # A tray that pops dialogs at you is worse than one that is briefly
            # wrong, and this runs every few seconds.
            self.items["counts"].set_label(f"{type(exc).__name__}: {exc}"[:60])
            if self.db is not None:
                self.db.close()
                self.db = None
        return True

    def _refresh(self) -> None:
        db = self._store()
        if db is None:
            self.items["counts"].set_label("no history database yet")
            self.items["recorder"].set_label(f"expected at {self.db_path}")
            self._set_outages([])
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            return

        view = summarise(db, time.time(), self.interval)
        self.items["counts"].set_label(view.counts)
        self.items["recorder"].set_label(f"{self.unit}: {self._unit_state()}")
        self._set_outages(view.outages)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ATTENTION
                                  if view.alert else
                                  AppIndicator.IndicatorStatus.ACTIVE)
        if self.show_label:
            self.indicator.set_label("" if view.stale else str(view.live), "88")

    def _set_outages(self, starts: list[tuple[float, str]]) -> None:
        count = len(starts)
        self.items["outage"].set_label(
            "no outages in 24 h" if not count else
            f"{count} outage{'' if count == 1 else 's'} in 24 h")
        for child in self.outage_menu.get_children():
            self.outage_menu.remove(child)
        if not starts:
            item = Gtk.MenuItem(label="nothing in the last 24 hours")
            item.set_sensitive(False)
            self.outage_menu.append(item)
        now = time.time()
        for ts, text in starts[:8]:
            item = Gtk.MenuItem(label=f"{ago(now - ts)} — {text}"[:110])
            item.set_sensitive(False)
            self.outage_menu.append(item)
        self.items["outages"].set_sensitive(bool(starts))
        self.outage_menu.show_all()

    def _unit_state(self) -> str:
        try:
            done = subprocess.run(
                ["systemctl", "--user", "is-active", self.unit],
                capture_output=True, text=True, timeout=5)
            return done.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    # ---- actions --------------------------------------------------------

    def _on_open(self, *_) -> None:
        if self.window is not None and self.window.poll() is None:
            return                      # one is already up
        try:
            self.window = subprocess.Popen(
                [sys.executable, "-m", "usbtracker", "--db", self.db_path,
                 "--interval", f"{self.interval:g}"],
                cwd=REPO_ROOT, start_new_session=True)
        except OSError as exc:
            self.items["counts"].set_label(f"cannot start the window: {exc}"[:60])

    def _on_restart(self, *_) -> None:
        try:
            subprocess.Popen(["systemctl", "--user", "restart", self.unit])
        except OSError:
            pass

    def _on_quit(self, *_) -> None:
        self.shutdown()

    def shutdown(self) -> bool:
        # Order matters: stop answering the panel before dropping the icon, or
        # a poll in flight refreshes an indicator that is already gone.
        self.closing = True
        try:
            self.indicator.set_status(AppIndicator.IndicatorStatus.PASSIVE)
        except Exception:
            pass
        if self.db is not None:
            self.db.close()
            self.db = None
        Gtk.main_quit()
        return False


def run(db_path: str, interval: float, unit: str = "usb-tracker.service",
        show_label: bool = False) -> int:
    lock = take_lock()
    if lock is None:
        print("A panel icon is already running.", file=sys.stderr)
        return 1

    tray = Tray(db_path, interval, unit, show_label)
    # A marker in the journal, so service logs starting empty means something.
    print(f"Panel icon up, reading {db_path} every {max(2.0, interval):g}s.",
          flush=True)
    tray.poll()
    GLib.timeout_add_seconds(max(2, int(interval)), tray.poll)
    for sig in (signal.SIGINT, signal.SIGTERM):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, tray.shutdown)
    Gtk.main()
    print("Panel icon stopped.", flush=True)
    return 0
