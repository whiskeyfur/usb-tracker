"""GTK4 interface: device tree on the left, power graph and history on the right."""

from __future__ import annotations

import math
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from . import power, store  # noqa: E402
from .monitor import Monitor, Snapshot  # noqa: E402

APP_ID = "dev.local.usbtracker"

# Tree model columns.
C_KEY, C_NAME, C_DRAW, C_BUDGET, C_TRAFFIC, C_STATUS, C_DROPS, C_DOT, \
    C_DOTCOLOR, C_FG, C_FGSET, C_STYLE, C_WEIGHT = range(13)

RANGES: list[tuple[str, float]] = [
    ("5 min", 300), ("15 min", 900), ("1 hour", 3600),
    ("6 hours", 21600), ("24 hours", 86400), ("All", 0),
]

# Line and marker colours, chosen to read on light and dark themes alike.
COL_EST = (0.21, 0.52, 0.89)
COL_BUDGET = (0.57, 0.25, 0.67)
COL_SUBTREE = (0.90, 0.38, 0.00)
COL_DROP = (0.88, 0.11, 0.14)
COL_BACK = (0.18, 0.76, 0.49)
COL_WARN = (0.96, 0.65, 0.14)

DOT_ACTIVE = "#2ec27e"
DOT_SUSPENDED = "#e5a50a"
DOT_LOST = "#77767b"
DOT_FAULT = "#e01b24"
GHOST_FG = "#8b8a8f"

EVENT_COLOURS = {
    store.KIND_ATTACH: "#2ec27e",
    store.KIND_CONNECT: "#2ec27e",
    store.KIND_DISCONNECT: "#e01b24",
    store.KIND_FLAP: "#e01b24",
    store.KIND_OVERCURRENT: "#e01b24",
    store.KIND_ERROR: "#e01b24",
    store.KIND_RESET: "#e5a50a",
    store.KIND_SUSPEND: "#9a9996",
    store.KIND_RESUME: "#3584e4",
    store.KIND_CONFIG: "#9141ac",
    store.KIND_BANDWIDTH: "#9141ac",
    store.KIND_DRIVER: "#1c71d8",
    store.KIND_SESSION: "#77767b",
}

CSS = b"""
.dim { opacity: 0.62; }
.title-strong { font-weight: 700; font-size: 1.05rem; }
.mono { font-family: monospace; }
.pane-head { padding: 6px 10px; }
.card-head { border-bottom: 1px solid alpha(currentColor, 0.12); }
.readout { font-family: monospace; font-size: 0.92rem; }
"""


def fmt_ma(value: float) -> str:
    if value <= 0:
        return "–"
    if value < 10:
        return f"{value:.1f} mA"
    return f"{value:.0f} mA"


def fmt_bps(value: float, style: str = "full") -> str:
    """Bytes per second, scaled.

    "full" reads as prose ("12.0 MB/s"), "axis" drops the rate for tick
    labels ("12.0 MB"), "compact" fits a narrow table column ("12M").
    """
    if value <= 0:
        return "0" if style != "full" else "–"
    for unit, letter in (("B", "B"), ("kB", "k"), ("MB", "M"), ("GB", "G")):
        if value < 1024 or unit == "GB":
            digits = 0 if value >= 100 or unit == "B" else 1
            if style == "compact":
                return f"{value:.{digits}f}{letter}"
            tail = unit if style == "axis" else f"{unit}/s"
            return f"{value:.{digits}f} {tail}"
        value /= 1024
    return f"{value:.1f} TB/s"


def fmt_ago(ts: float, now: float | None = None) -> str:
    if not ts:
        return "never"
    delta = max(0.0, (now or time.time()) - ts)
    if delta < 60:
        return f"{delta:.0f}s ago"
    if delta < 3600:
        return f"{delta / 60:.0f}m ago"
    if delta < 86400:
        return f"{delta / 3600:.1f}h ago"
    return f"{delta / 86400:.1f}d ago"


def nice_ceiling(value: float) -> float:
    """Round a y-axis maximum up to something a person would label."""
    if value <= 0:
        return 10.0
    exp = math.floor(math.log10(value))
    base = 10 ** exp
    for mult in (1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10):
        if value <= mult * base:
            return mult * base
    return 10 * base


COL_URB = (0.11, 0.60, 0.56)

METRIC_POWER = "power"
METRIC_BANDWIDTH = "bandwidth"


class HistoryGraph(Gtk.DrawingArea):
    """Line graph of draw or bandwidth, with absence bands and event markers."""

    def __init__(self):
        super().__init__()
        self.set_draw_func(self._draw)
        self.set_content_height(220)
        self.set_hexpand(True)
        self.set_vexpand(True)

        self.samples: list[tuple] = []
        self.events: list[tuple] = []
        self.t_start = time.time() - 300
        self.t_end = time.time()
        self.show_budget = True
        self.show_subtree = False
        self.show_urb = False
        self.metric = METRIC_POWER
        self.title = ""
        self.hover_x: float | None = None
        self.on_readout = None

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    # -- interaction

    def _on_motion(self, _ctrl, x, _y):
        self.hover_x = x
        self.queue_draw()

    def _on_leave(self, _ctrl):
        self.hover_x = None
        if self.on_readout:
            self.on_readout("")
        self.queue_draw()

    def set_data(self, samples, events, t_start, t_end):
        self.samples = samples
        self.events = events
        self.t_start = t_start
        self.t_end = t_end
        self.queue_draw()

    # -- drawing helpers

    def _getters(self):
        """(measured, declared, subtree) accessors for the current metric."""
        if self.metric == METRIC_BANDWIDTH:
            return (lambda s: (s[6] or 0.0) + (s[7] or 0.0),
                    lambda s: s[8] or 0.0,
                    lambda s: s[9] or 0.0)
        return (lambda s: s[1], lambda s: s[2], lambda s: s[3])

    def _fmt_value(self, value: float) -> str:
        if self.metric == METRIC_BANDWIDTH:
            return fmt_bps(value)
        return fmt_ma(value)

    def _fmt_tick(self, value: float, ymax: float) -> str:
        if self.metric == METRIC_BANDWIDTH:
            return fmt_bps(value, "axis")
        return f"{value:.0f}" if ymax >= 10 else f"{value:.1f}"

    def _unit(self) -> str:
        return "B/s" if self.metric == METRIC_BANDWIDTH else "mA"

    def _fg(self) -> tuple[float, float, float]:
        try:
            c = self.get_color()
            return (c.red, c.green, c.blue)
        except Exception:
            return (0.5, 0.5, 0.5)

    def _text(self, cr, x, y, text, size=11.0, align="left", alpha=1.0,
              rgb=None, bold=False):
        cr.save()
        cr.select_font_face("Sans", 0, 1 if bold else 0)
        cr.set_font_size(size)
        r, g, b = rgb if rgb else self._fg()
        cr.set_source_rgba(r, g, b, alpha)
        ext = cr.text_extents(text)
        if align == "right":
            x -= ext.width
        elif align == "center":
            x -= ext.width / 2
        cr.move_to(x, y)
        cr.show_text(text)
        cr.restore()

    def _draw(self, _area, cr, width, height, _data=None):
        fr, fg, fb = self._fg()
        if not self.samples:
            msg = self.title or "Select a device to see its history"
            self._text(cr, width / 2, height / 2, msg, 12.5, "center", 0.55)
            return

        left, right, top, bottom = 62, (54 if self.show_urb else 16), 24, 26
        pw = max(10, width - left - right)
        ph = max(10, height - top - bottom)

        t1 = self.t_end
        t0 = self.t_start
        if t1 - t0 <= 0:
            t0 = t1 - 60
        span = t1 - t0

        measured, declared, subtree = self._getters()
        vals = [measured(s) for s in self.samples]
        if self.show_budget:
            vals += [declared(s) for s in self.samples]
        if self.show_subtree:
            vals += [subtree(s) for s in self.samples]
        ymax = nice_ceiling(max(vals + [1.0]) * 1.08)
        urb_max = nice_ceiling(max([s[5] or 0.0 for s in self.samples] + [1.0]) * 1.1)

        def sx(ts: float) -> float:
            return left + (ts - t0) / span * pw

        def sy(ma: float) -> float:
            return top + ph - (max(0.0, min(ma, ymax)) / ymax) * ph

        # plot frame
        cr.set_source_rgba(fr, fg, fb, 0.05)
        cr.rectangle(left, top, pw, ph)
        cr.fill()

        # absence bands, from the event log
        for start, end in self._absence_spans(t0, t1):
            x0, x1 = sx(start), sx(end)
            cr.set_source_rgba(*COL_DROP, 0.10)
            cr.rectangle(x0, top, max(1.0, x1 - x0), ph)
            cr.fill()

        # horizontal grid + y labels
        steps = 4
        for i in range(steps + 1):
            value = ymax * i / steps
            y = sy(value)
            cr.set_source_rgba(fr, fg, fb, 0.14 if i else 0.3)
            cr.set_line_width(1.0)
            cr.move_to(left, y + 0.5)
            cr.line_to(left + pw, y + 0.5)
            cr.stroke()
            self._text(cr, left - 8, y + 3.5, self._fmt_tick(value, ymax), 10.0,
                       "right", 0.7)
            if self.show_urb:
                self._text(cr, left + pw + 8, y + 3.5,
                           f"{urb_max * i / steps:.0f}", 10.0, "left", 0.6,
                           rgb=COL_URB)
        self._text(cr, left - 8, top - 10, self._unit(), 9.5, "right", 0.55)
        if self.show_urb:
            self._text(cr, left + pw + 8, top - 10, "URB/s", 9.5, "left", 0.7,
                       rgb=COL_URB)

        # vertical grid + time labels
        for ts, label in self._time_ticks(t0, t1):
            x = sx(ts)
            cr.set_source_rgba(fr, fg, fb, 0.12)
            cr.move_to(x + 0.5, top)
            cr.line_to(x + 0.5, top + ph)
            cr.stroke()
            self._text(cr, x, top + ph + 15, label, 10.0, "center", 0.7)

        # event markers
        for ts, _key, kind, _detail in self.events:
            if not (t0 <= ts <= t1) or kind == store.KIND_SESSION:
                continue
            colour = {
                store.KIND_DISCONNECT: COL_DROP,
                store.KIND_FLAP: COL_DROP,
                store.KIND_OVERCURRENT: COL_DROP,
                store.KIND_ERROR: COL_DROP,
                store.KIND_CONNECT: COL_BACK,
                store.KIND_ATTACH: COL_BACK,
                store.KIND_RESET: COL_WARN,
            }.get(kind)
            if colour is None:
                continue
            x = sx(ts)
            cr.set_source_rgba(*colour, 0.85)
            cr.set_line_width(1.4)
            cr.move_to(x + 0.5, top)
            cr.line_to(x + 0.5, top + ph)
            cr.stroke()
            cr.arc(x + 0.5, top + 4, 2.6, 0, 2 * math.pi)
            cr.fill()

        gap = self._gap_threshold()

        if self.show_urb:
            def uy(value: float) -> float:
                return top + ph - (max(0.0, min(value, urb_max)) / urb_max) * ph
            self._line(cr, [(s[0], s[5] or 0.0) for s in self.samples], sx, uy,
                       COL_URB, gap, width=1.2)
        if self.show_budget:
            self._line(cr, [(s[0], declared(s)) for s in self.samples], sx, sy,
                       COL_BUDGET, gap, width=1.3, dash=[4.0, 3.0])
        if self.show_subtree:
            self._line(cr, [(s[0], subtree(s)) for s in self.samples], sx, sy,
                       COL_SUBTREE, gap, width=1.6)
        self._line(cr, [(s[0], measured(s)) for s in self.samples], sx, sy, COL_EST,
                   gap, width=2.0, fill_to=top + ph)

        self._crosshair(cr, left, top, pw, ph, t0, span, sx, sy)

    def _line(self, cr, points, sx, sy, rgb, gap, width=2.0, dash=None,
              fill_to=None):
        segments, current = [], []
        prev_ts = None
        for ts, value in points:
            if prev_ts is not None and ts - prev_ts > gap:
                segments.append(current)
                current = []
            current.append((ts, value))
            prev_ts = ts
        segments.append(current)

        for seg in segments:
            if len(seg) < 1:
                continue
            if fill_to is not None and len(seg) > 1:
                cr.save()
                cr.set_source_rgba(*rgb, 0.14)
                cr.move_to(sx(seg[0][0]), fill_to)
                for ts, value in seg:
                    cr.line_to(sx(ts), sy(value))
                cr.line_to(sx(seg[-1][0]), fill_to)
                cr.close_path()
                cr.fill()
                cr.restore()
            cr.save()
            cr.set_source_rgb(*rgb)
            cr.set_line_width(width)
            cr.set_line_join(1)
            if dash:
                cr.set_dash(dash)
            if len(seg) == 1:
                ts, value = seg[0]
                cr.arc(sx(ts), sy(value), width, 0, 2 * math.pi)
                cr.fill()
            else:
                cr.move_to(sx(seg[0][0]), sy(seg[0][1]))
                for ts, value in seg[1:]:
                    cr.line_to(sx(ts), sy(value))
                cr.stroke()
            cr.restore()

    def _crosshair(self, cr, left, top, pw, ph, t0, span, sx, sy):
        if self.hover_x is None or not (left <= self.hover_x <= left + pw):
            return
        ts = t0 + (self.hover_x - left) / pw * span
        measured, declared, subtree = self._getters()
        nearest = min(self.samples, key=lambda s: abs(s[0] - ts))
        if abs(nearest[0] - ts) > self._gap_threshold():
            if self.on_readout:
                self.on_readout(time.strftime("%H:%M:%S", time.localtime(ts))
                                + "  ·  no data")
            return
        x, y = sx(nearest[0]), sy(measured(nearest))
        fr, fg, fb = self._fg()
        cr.set_source_rgba(fr, fg, fb, 0.45)
        cr.set_line_width(1.0)
        cr.set_dash([2.0, 2.0])
        cr.move_to(x + 0.5, top)
        cr.line_to(x + 0.5, top + ph)
        cr.stroke()
        cr.set_dash([])
        cr.set_source_rgb(*COL_EST)
        cr.arc(x, y, 3.4, 0, 2 * math.pi)
        cr.fill()
        if self.on_readout:
            if self.metric == METRIC_BANDWIDTH:
                parts = [time.strftime("%H:%M:%S", time.localtime(nearest[0])),
                         f"in {fmt_bps(nearest[6] or 0.0)}",
                         f"out {fmt_bps(nearest[7] or 0.0)}",
                         f"reserved {self._fmt_value(declared(nearest))}"]
            else:
                parts = [time.strftime("%H:%M:%S", time.localtime(nearest[0])),
                         f"draw {self._fmt_value(measured(nearest))}",
                         f"budget {self._fmt_value(declared(nearest))}"]
            if self.show_subtree:
                parts.append(f"subtree {self._fmt_value(subtree(nearest))}")
            if self.show_urb:
                parts.append(f"{nearest[5] or 0.0:.0f} URB/s")
            if nearest[4]:
                parts.append(nearest[4])
            self.on_readout("  ·  ".join(parts))

    def _gap_threshold(self) -> float:
        if len(self.samples) > 2:
            deltas = sorted(self.samples[i + 1][0] - self.samples[i][0]
                            for i in range(min(len(self.samples) - 1, 60)))
            typical = deltas[len(deltas) // 2] if deltas else 2.0
            return max(4.0, typical * 3.5)
        return 8.0

    def _absence_spans(self, t0: float, t1: float) -> list[tuple[float, float]]:
        """Grey bands for the stretches a device was missing."""
        spans: list[tuple[float, float]] = []
        ordered = sorted((e for e in self.events
                          if e[2] in (store.KIND_DISCONNECT, store.KIND_CONNECT,
                                      store.KIND_ATTACH)), key=lambda e: e[0])
        open_at: float | None = None
        for ts, _key, kind, _detail in ordered:
            if kind == store.KIND_DISCONNECT and open_at is None:
                open_at = ts
            elif kind in (store.KIND_CONNECT, store.KIND_ATTACH) and open_at is not None:
                if ts > t0 and open_at < t1:
                    spans.append((max(open_at, t0), min(ts, t1)))
                open_at = None
        if open_at is not None and open_at < t1:
            spans.append((max(open_at, t0), t1))
        return spans

    def _time_ticks(self, t0: float, t1: float) -> list[tuple[float, str]]:
        span = t1 - t0
        for step in (10, 30, 60, 300, 600, 1800, 3600, 7200, 21600, 43200, 86400):
            if span / step <= 7:
                break
        fmt = "%H:%M:%S" if step < 60 else ("%H:%M" if span <= 86400 else "%a %H:%M")
        ticks = []
        first = math.ceil(t0 / step) * step
        ts = first
        while ts <= t1:
            ticks.append((ts, time.strftime(fmt, time.localtime(ts))))
            ts += step
        return ticks


class TrackerWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, monitor: Monitor, reader: store.Store):
        super().__init__(application=app, title="USB Tracker")
        self.monitor = monitor
        self.reader = reader
        self.snapshot = Snapshot()
        self.selected_key: str | None = None
        self.iters: dict[str, Gtk.TreeIter] = {}
        self.expanded: set[str] = set()
        self.first_fill = True
        self.set_default_size(1240, 780)

        self._build_header()
        self._build_body()
        # Without this the filter entry takes focus on open and swallows
        # whatever the user types next.
        self.set_focus(self.tree)
        GLib.idle_add(lambda: (self.set_focus(self.tree), False)[1])
        self.connect("close-request", self._on_close)

    # ---- chrome ---------------------------------------------------------

    def _build_header(self):
        header = Gtk.HeaderBar()
        self.title_label = Gtk.Label(label="USB Tracker")
        self.title_label.add_css_class("title-strong")
        self.subtitle_label = Gtk.Label(label="starting…")
        self.subtitle_label.add_css_class("dim")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.title_label)
        box.append(self.subtitle_label)
        header.set_title_widget(box)

        self.pause_button = Gtk.ToggleButton(icon_name="media-playback-pause-symbolic",
                                             tooltip_text="Pause polling")
        self.pause_button.connect("toggled", self._on_pause)
        header.pack_start(self.pause_button)

        self.interval_spin = Gtk.SpinButton.new_with_range(0.5, 30.0, 0.5)
        self.interval_spin.set_value(self.monitor.interval)
        self.interval_spin.set_tooltip_text("Seconds between polls")
        self.interval_spin.connect("value-changed", self._on_interval)
        header.pack_start(self.interval_spin)

        menu = Gio.Menu()
        menu.append("Forget lost devices", "win.forget-lost")
        menu.append("Forget this device", "win.forget-one")
        menu.append("Export selected history (CSV)", "win.export")
        menu.append("How power is estimated", "win.about-power")
        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(button)

        for name, handler in (("forget-lost", self._act_forget_lost),
                              ("forget-one", self._act_forget_one),
                              ("export", self._act_export),
                              ("about-power", self._act_about_power),
                              ("autosuspend-allow", self._act_autosuspend_allow),
                              ("autosuspend-prevent", self._act_autosuspend_prevent),
                              ("copy-udev", self._act_copy_udev),
                              ("copy-info", self._act_copy_info)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)

        self.set_titlebar(header)

    def _build_body(self):
        outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_position(556)
        outer.set_shrink_start_child(False)
        outer.set_shrink_end_child(False)
        outer.set_start_child(self._build_tree_pane())
        outer.set_end_child(self._build_detail_pane())
        self.set_child(outer)

    def _build_tree_pane(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        head = Gtk.Box(spacing=8)
        head.add_css_class("pane-head")
        head.add_css_class("card-head")
        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter devices")
        self.filter_entry.set_hexpand(True)
        self.filter_entry.connect("search-changed", lambda *_: self._refill_tree())
        head.append(self.filter_entry)
        self.show_lost = Gtk.ToggleButton(label="Lost", active=True,
                                          tooltip_text="Show devices that are gone")
        self.show_lost.connect("toggled", lambda *_: self._refill_tree())
        head.append(self.show_lost)
        box.append(head)

        self.model = Gtk.TreeStore(str, str, str, str, str, str, str, str, str,
                                   str, bool, int, int)
        self.tree = Gtk.TreeView(model=self.model, headers_visible=True,
                                 enable_tree_lines=True)
        self.tree.set_tooltip_column(-1)
        self.tree.get_selection().connect("changed", self._on_select)

        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        right_click.connect("pressed", self._on_right_click)
        self.tree.add_controller(right_click)
        self.context_menu = Gtk.PopoverMenu.new_from_model(self._context_model())
        self.context_menu.set_parent(self.tree)
        self.context_menu.set_has_arrow(False)
        self.tree.connect("row-expanded", self._on_expand, True)
        self.tree.connect("row-collapsed", self._on_expand, False)

        dot = Gtk.CellRendererText(xalign=0.5)
        col = Gtk.TreeViewColumn("", dot, text=C_DOT, foreground=C_DOTCOLOR)
        col.set_fixed_width(26)
        col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        self.tree.append_column(col)

        name = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col = Gtk.TreeViewColumn("Device", name, markup=C_NAME, foreground=C_FG,
                                 style=C_STYLE, weight=C_WEIGHT)
        col.add_attribute(name, "foreground-set", C_FGSET)
        col.set_expand(True)
        col.set_min_width(150)
        self.tree.append_column(col)
        self.tree.set_expander_column(col)

        for title, column, width, align in (("Draw", C_DRAW, 54, 1.0),
                                            ("Budget", C_BUDGET, 56, 1.0),
                                            ("Traffic", C_TRAFFIC, 62, 1.0),
                                            ("State", C_STATUS, 88, 0.0),
                                            ("Drops", C_DROPS, 46, 1.0)):
            renderer = Gtk.CellRendererText(xalign=align, family="monospace")
            col = Gtk.TreeViewColumn(title, renderer, text=column, foreground=C_FG,
                                     style=C_STYLE)
            col.add_attribute(renderer, "foreground-set", C_FGSET)
            col.set_fixed_width(width)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            self.tree.append_column(col)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.tree)
        box.append(scroller)

        self.tree_footer = Gtk.Label(xalign=0.0, label="")
        self.tree_footer.add_css_class("dim")
        self.tree_footer.add_css_class("pane-head")
        box.append(self.tree_footer)
        return box

    def _build_detail_pane(self) -> Gtk.Widget:
        pane = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        pane.set_position(430)
        pane.set_shrink_start_child(False)
        pane.set_shrink_end_child(False)

        # --- top: power graph
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        head = Gtk.Box(spacing=10)
        head.add_css_class("pane-head")
        head.add_css_class("card-head")
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.device_label = Gtk.Label(xalign=0.0, label="No device selected")
        self.device_label.add_css_class("title-strong")
        self.device_sub = Gtk.Label(xalign=0.0, label="")
        self.device_sub.add_css_class("dim")
        self.device_sub.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(self.device_label)
        titles.append(self.device_sub)
        head.append(titles)

        switcher = Gtk.Box(css_classes=["linked"], valign=Gtk.Align.CENTER)
        self.metric_buttons: dict[str, Gtk.ToggleButton] = {}
        for metric, label in ((METRIC_POWER, "Power"),
                              (METRIC_BANDWIDTH, "Bandwidth")):
            button = Gtk.ToggleButton(label=label,
                                      active=metric == METRIC_POWER)
            button.connect("toggled", self._on_metric, metric)
            switcher.append(button)
            self.metric_buttons[metric] = button
        head.append(switcher)

        self.range_drop = Gtk.DropDown.new_from_strings([r[0] for r in RANGES])
        self.range_drop.set_selected(2)
        self.range_drop.set_tooltip_text("Time window")
        self.range_drop.connect("notify::selected", lambda *_: self._refresh_graph())
        head.append(self.range_drop)
        top.append(head)

        self.graph = HistoryGraph()
        self.graph.on_readout = self._set_readout
        top.append(self.graph)

        legend = Gtk.Box(spacing=14)
        legend.add_css_class("pane-head")
        self.budget_toggle = Gtk.CheckButton(label="Declared budget", active=True)
        self.budget_toggle.connect("toggled", self._on_series_toggle)
        self.subtree_toggle = Gtk.CheckButton(label="Including downstream",
                                              active=False)
        self.subtree_toggle.connect("toggled", self._on_series_toggle)
        self.urb_toggle = Gtk.CheckButton(label="Transfers (URB/s)", active=False)
        self.urb_toggle.connect("toggled", self._on_series_toggle)
        self.primary_swatch = self._swatch(COL_EST, "Estimated draw")
        legend.append(self.primary_swatch)
        legend.append(self.budget_toggle)
        legend.append(self.subtree_toggle)
        legend.append(self.urb_toggle)
        self.readout = Gtk.Label(xalign=1.0, hexpand=True, label="")
        self.readout.add_css_class("readout")
        self.readout.add_css_class("dim")
        legend.append(self.readout)
        top.append(legend)
        self._sync_legend()
        pane.set_start_child(top)

        # --- bottom: history
        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        hhead = Gtk.Box(spacing=10)
        hhead.add_css_class("pane-head")
        hhead.add_css_class("card-head")
        self.history_label = Gtk.Label(xalign=0.0, hexpand=True, label="History")
        self.history_label.add_css_class("title-strong")
        hhead.append(self.history_label)
        self.all_events = Gtk.ToggleButton(label="All devices",
                                           tooltip_text="Show events from every device")
        self.all_events.connect("toggled", lambda *_: self._refresh_history())
        hhead.append(self.all_events)
        bottom.append(hhead)

        self.events_model = Gtk.ListStore(str, str, str, str, bool)
        self.events_view = Gtk.TreeView(model=self.events_model)
        for title, column, width, mono in (("Time", 0, 150, True),
                                           ("Event", 1, 110, False),
                                           ("Detail", 2, 0, False)):
            renderer = Gtk.CellRendererText(family="monospace" if mono else "",
                                            ellipsize=Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, renderer, text=column, foreground=3)
            col.add_attribute(renderer, "foreground-set", 4)
            if width:
                col.set_fixed_width(width)
                col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            else:
                col.set_expand(True)
            self.events_view.append_column(col)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.events_view)
        bottom.append(scroller)
        pane.set_end_child(bottom)
        return pane

    def _swatch(self, rgb, label: str) -> Gtk.Widget:
        box = Gtk.Box(spacing=6)
        box.label_widget = None
        area = Gtk.DrawingArea(content_width=14, content_height=14,
                               valign=Gtk.Align.CENTER)

        def draw(_a, cr, w, h, _d=None):
            cr.set_source_rgb(*rgb)
            cr.set_line_width(2.4)
            cr.move_to(1, h / 2)
            cr.line_to(w - 1, h / 2)
            cr.stroke()

        area.set_draw_func(draw)
        box.append(area)
        box.label_widget = Gtk.Label(label=label)
        box.append(box.label_widget)
        return box

    # ---- snapshot plumbing ---------------------------------------------

    def on_snapshot(self, snap: Snapshot) -> None:
        GLib.idle_add(self._apply_snapshot, snap, priority=GLib.PRIORITY_DEFAULT_IDLE)

    def _apply_snapshot(self, snap: Snapshot) -> bool:
        self.snapshot = snap
        self._refill_tree()
        self._refresh_graph()
        self._refresh_history()
        self._update_header()
        return False

    def _update_header(self):
        snap = self.snapshot
        bits = [f"{snap.live_count} connected"]
        if snap.lost_count:
            bits.append(f"{snap.lost_count} lost")
        bits.append(f"{fmt_ma(snap.total_ma)} estimated across all buses")
        if snap.total_alloc_bps:
            bits.append(f"{fmt_bps(snap.total_alloc_bps)} bus bandwidth reserved")
        if snap.total_bps:
            bits.append(f"{fmt_bps(snap.total_bps)} measured")
        if snap.paused:
            bits.append("paused")
        if not snap.kmsg_ok:
            bits.append("kernel log unavailable")
        if self.monitor.last_error:
            bits.append(self.monitor.last_error)
        self.subtitle_label.set_label("  ·  ".join(bits))
        self.tree_footer.set_label(
            f"polling every {snap.interval:g}s  ·  history {snap.db_bytes / 1024:.0f} KiB")

    # ---- tree -----------------------------------------------------------

    def _visible(self, key: str) -> bool:
        node = self.snapshot.nodes.get(key)
        if node is None:
            return False
        if node.lost and not self.show_lost.get_active():
            return False
        needle = self.filter_entry.get_text().strip().lower()
        if not needle:
            return True
        if self._matches(node, needle):
            return True
        return any(self._visible(child) for child in node.children)

    def _matches(self, node, needle: str) -> bool:
        haystack = " ".join((node.label, node.class_hint, node.busid, node.vid,
                             node.pid, node.serial, node.manufacturer,
                             " ".join(node.drivers))).lower()
        return needle in haystack

    def _refill_tree(self):
        snap = self.snapshot
        selection = self.tree.get_selection()
        model, sel_iter = selection.get_selected()
        keep = model.get_value(sel_iter, C_KEY) if sel_iter else self.selected_key

        self.model.clear()
        self.iters.clear()

        def add(key: str, parent: Gtk.TreeIter | None):
            if not self._visible(key):
                return
            node = snap.nodes[key]
            it = self.model.append(parent, self._row(node))
            self.iters[key] = it
            for child in node.children:
                add(child, it)

        for root in snap.roots:
            add(root, None)

        for key, it in self.iters.items():
            if self.first_fill or key in self.expanded:
                self.tree.expand_to_path(self.model.get_path(it))

        if self.iters:
            self.first_fill = False
        if keep and keep in self.iters:
            path = self.model.get_path(self.iters[keep])
            self.tree.expand_to_path(path)
            selection.select_path(path)
            self.selected_key = keep

    def _row(self, node) -> list:
        lost = node.lost
        name = GLib.markup_escape_text(node.label)
        detail = node.class_hint or ""
        if node.busid:
            detail = f"{detail} · {node.busid}" if detail else node.busid
        markup = f"{name}  <span size='small' alpha='55%'>{GLib.markup_escape_text(detail)}</span>"

        if lost:
            dot_colour = DOT_LOST
            state = fmt_ago(node.last_seen, self.snapshot.ts or None)
            draw = budget = "–"
        else:
            if node.over_current or node.errors:
                dot_colour = DOT_FAULT
            elif node.status == "suspended":
                dot_colour = DOT_SUSPENDED
            else:
                dot_colour = DOT_ACTIVE
            state = node.status or "–"
            if node.est_ma <= 0:
                draw = "–"
            else:
                draw = (f"{node.est_ma:.0f}" if node.est_ma >= 10
                        else f"{node.est_ma:.1f}")
            budget = f"{node.budget_ma:.0f}" if node.budget_ma else "–"

        traffic = "–" if lost else self._traffic_text(node)
        drops = str(node.disconnects) if node.disconnects else "–"
        if node.resets or node.errors:
            drops += "!"

        return [node.key, markup, draw, budget, traffic, state, drops,
                "●" if not lost else "○", dot_colour,
                GHOST_FG, lost, int(Pango.Style.ITALIC) if lost else
                int(Pango.Style.NORMAL), 400]

    def _context_model(self) -> Gio.Menu:
        menu = Gio.Menu()
        suspend = Gio.Menu()
        suspend.append("Allow autosuspend", "win.autosuspend-allow")
        suspend.append("Prevent autosuspend (keep powered)",
                       "win.autosuspend-prevent")
        suspend.append("Copy udev rule to make it stick", "win.copy-udev")
        menu.append_section(None, suspend)

        clipboard = Gio.Menu()
        clipboard.append("Copy device details", "win.copy-info")
        clipboard.append("Export history (CSV)", "win.export")
        menu.append_section(None, clipboard)

        forget = Gio.Menu()
        forget.append("Forget this device", "win.forget-one")
        menu.append_section(None, forget)
        return menu

    def _on_right_click(self, gesture, _n_press, x, y):
        if self._show_context_menu(x, y):
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)

    def _show_context_menu(self, x: float, y: float) -> bool:
        hit = self.tree.get_path_at_pos(int(x), int(y))
        if hit is None:
            return False
        path = hit[0]
        self.tree.get_selection().select_path(path)
        node = self.snapshot.nodes.get(self.selected_key or "")

        # Only offer what this row can actually do.
        live = node is not None and node.present
        allowed = bool(node and node.autosuspend_allowed)
        self._set_action_enabled("autosuspend-allow", live and not allowed)
        self._set_action_enabled("autosuspend-prevent", live and allowed)
        self._set_action_enabled("copy-udev", node is not None and bool(node.vid))
        self._set_action_enabled("copy-info", node is not None)
        self._set_action_enabled("export", node is not None)
        self._set_action_enabled("forget-one", node is not None and not live)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self.context_menu.set_pointing_to(rect)
        self.context_menu.popup()
        return True

    def _set_action_enabled(self, name: str, enabled: bool) -> None:
        action = self.lookup_action(name)
        if action is not None:
            action.set_enabled(enabled)

    def _act_autosuspend_allow(self, *_):
        self._change_autosuspend(True)

    def _act_autosuspend_prevent(self, *_):
        self._change_autosuspend(False)

    def _change_autosuspend(self, allow: bool):
        """Hand the change to pkexec off the UI thread; it shows its own dialog."""
        node = self.snapshot.nodes.get(self.selected_key or "")
        if node is None or not node.present:
            self._toast("That device is not connected.")
            return
        busid = node.busid
        self._toast("Waiting for authentication…" if not power.writable(busid)
                    else "Applying…")

        def work():
            result = power.set_autosuspend(busid, allow)
            GLib.idle_add(self._autosuspend_done, result)

        threading.Thread(target=work, daemon=True).start()

    def _autosuspend_done(self, result) -> bool:
        self._toast(result.message)
        if result.ok:
            self.monitor.poke()
        return False

    def _act_copy_udev(self, *_):
        node = self.snapshot.nodes.get(self.selected_key or "")
        if node is None or not node.vid:
            self._toast("No device selected.")
            return
        rule = power.udev_rule(node.vid, node.pid, node.autosuspend_allowed,
                               node.label)
        self._to_clipboard(
            rule, "udev rule copied — save it as "
                  "/etc/udev/rules.d/99-usb-power.rules")

    def _act_copy_info(self, *_):
        node = self.snapshot.nodes.get(self.selected_key or "")
        if node is None:
            self._toast("No device selected.")
            return
        lines = [
            f"{node.label}",
            f"  id            {node.vid}:{node.pid}",
            f"  serial        {node.serial or '—'}",
            f"  manufacturer  {node.manufacturer or '—'}",
            f"  class         {node.class_hint}",
            f"  port          {node.busid}",
            f"  speed         {node.speed_label}",
            f"  power         {fmt_ma(node.est_ma)} estimated of "
            f"{fmt_ma(node.budget_ma)} declared",
            f"  bandwidth     {fmt_bps(node.alloc_bps)} reserved"
            + (f", {fmt_bps(node.total_bps)} measured ({node.counter_source})"
               if node.measured else ""),
            f"  transfers     {node.urb_rate:.0f} URB/s",
            f"  autosuspend   {'allowed' if node.autosuspend_allowed else 'prevented'}"
            + (f", delay {node.autosuspend_ms} ms"
               if node.autosuspend_ms >= 0 else ""),
            f"  drivers       {', '.join(node.drivers) or '—'}",
            f"  drops         {node.disconnects} "
            f"({node.connects} connects, {node.resets} resets)",
        ]
        self._to_clipboard("\n".join(lines) + "\n", "Device details copied.")

    def _to_clipboard(self, text: str, message: str):
        try:
            value = GObject.Value(str, text)
            self.get_clipboard().set_content(
                Gdk.ContentProvider.new_for_value(value))
        except Exception as exc:
            self._toast(f"Could not copy: {exc}")
            return
        self._toast(message)

    def _traffic_text(self, node) -> str:
        """Measured throughput, or the reserved figure in parentheses."""
        if node.measured:
            return fmt_bps(node.total_bps, "compact") if node.total_bps else "0"
        if node.alloc_bps > 0:
            return f"({fmt_bps(node.alloc_bps, 'compact')})"
        return "–"

    def _on_expand(self, _tree, it, _path, expanded: bool):
        key = self.model.get_value(it, C_KEY)
        if expanded:
            self.expanded.add(key)
        else:
            self.expanded.discard(key)

    def _on_select(self, selection):
        model, it = selection.get_selected()
        self.selected_key = model.get_value(it, C_KEY) if it else None
        self._refresh_detail_header()
        self._refresh_graph()
        self._refresh_history()

    # ---- right-hand side ------------------------------------------------

    def _refresh_detail_header(self):
        node = self.snapshot.nodes.get(self.selected_key or "")
        if node is None:
            self.device_label.set_label("No device selected")
            self.device_sub.set_label("Pick a device in the tree.")
            return
        suffix = "" if node.present else "  (lost)"
        self.device_label.set_label(node.label + suffix)

        bits = [f"{node.vid}:{node.pid}", node.class_hint or "unknown class"]
        if node.busid:
            bits.append(node.busid)
        if node.speed_label:
            bits.append(node.speed_label)
        if node.serial:
            bits.append(f"serial {node.serial}")
        if node.manufacturer:
            bits.append(node.manufacturer)
        if node.drivers:
            bits.append("drivers: " + ", ".join(node.drivers))
        if node.self_powered:
            bits.append("self-powered")
        if node.present:
            bits.append(f"now {fmt_ma(node.est_ma)} of {fmt_ma(node.budget_ma)}")
            if node.is_hub:
                bits.append(f"downstream total {fmt_ma(node.subtree_ma)}")
            if node.alloc_bps:
                bits.append(f"reserves {fmt_bps(node.alloc_bps)} of "
                            f"{fmt_bps(node.link_bps)} link")
            if node.measured:
                bits.append(f"in {fmt_bps(node.rx_bps)} / out {fmt_bps(node.tx_bps)}"
                            f" ({node.counter_source})")
            if node.urb_rate:
                bits.append(f"{node.urb_rate:.0f} URB/s")
            bits.append("autosuspend allowed" if node.autosuspend_allowed
                        else "autosuspend prevented")
        else:
            bits.append(f"last seen {fmt_ago(node.last_seen, self.snapshot.ts or None)}")
        bits.append(f"{node.connects} connects / {node.disconnects} drops")
        if node.resets:
            bits.append(f"{node.resets} resets")
        if node.over_current:
            bits.append(f"over-current count {node.over_current}")
        self.device_sub.set_label("  ·  ".join(bits))
        wants_subtree = bool(node.is_hub or node.subtree_ma > node.est_ma)
        if self.subtree_toggle.get_visible() != wants_subtree:
            self.subtree_toggle.set_visible(wants_subtree)

    def _window_seconds(self) -> float:
        return RANGES[self.range_drop.get_selected()][1]

    def _refresh_graph(self):
        key = self.selected_key
        self._refresh_detail_header()
        if not key:
            self.graph.title = "Select a device to see its history"
            now = time.time()
            self.graph.set_data([], [], now - 300, now)
            return

        now = self.snapshot.ts or time.time()
        window = self._window_seconds()
        if window:
            since = now - window
        else:
            first = self.reader.first_sample_ts(key)
            since = first if first else now - 300
            window = max(60.0, now - since)

        bucket = 0.0 if window <= 2 * 3600 else window / 900.0
        samples = (self.reader.samples_bucketed(key, since, bucket) if bucket
                   else self.reader.samples(key, since))
        events = self.reader.events(key, since=None, limit=3000)

        # A window far wider than the history on hand leaves the line crushed
        # against the right edge; start the axis near the data instead.
        t_start = since
        if samples and samples[0][0] > since + (now - since) * 0.2:
            t_start = samples[0][0] - max(5.0, (now - samples[0][0]) * 0.06)

        self.graph.show_budget = self.budget_toggle.get_active()
        self.graph.show_subtree = self.subtree_toggle.get_active()
        self.graph.show_urb = (self.urb_toggle.get_active()
                               and self.graph.metric == METRIC_BANDWIDTH)
        node = self.snapshot.nodes.get(key)
        self.graph.title = ("No samples recorded yet for this device"
                           if node is None or not samples else "")
        self.graph.set_data(samples, events, t_start, now)

    def _on_metric(self, button, metric):
        if not button.get_active():
            # keep one of the two always selected
            if not any(b.get_active() for b in self.metric_buttons.values()):
                button.set_active(True)
            return
        for name, other in self.metric_buttons.items():
            if name != metric and other.get_active():
                other.set_active(False)
        self.graph.metric = metric
        self._sync_legend()
        self._refresh_graph()

    def _sync_legend(self):
        bandwidth = self.graph.metric == METRIC_BANDWIDTH
        self.primary_swatch.label_widget.set_label(
            "Measured throughput" if bandwidth else "Estimated draw")
        self.budget_toggle.set_label(
            "Reserved bandwidth" if bandwidth else "Declared budget")
        self.urb_toggle.set_visible(bandwidth)

    def _on_series_toggle(self, _btn):
        self._refresh_graph()

    def _set_readout(self, text: str):
        self.readout.set_label(text)

    def _refresh_history(self):
        key = None if self.all_events.get_active() else self.selected_key
        rows = self.reader.events(key, limit=400)
        self.events_model.clear()
        label_all = self.all_events.get_active()
        for ts, ekey, kind, detail in rows:
            node = self.snapshot.nodes.get(ekey)
            text = detail
            if label_all and node is not None:
                text = f"{node.label}: {detail}" if detail else node.label
            colour = EVENT_COLOURS.get(kind, "")
            self.events_model.append([
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                kind, text, colour or GHOST_FG, bool(colour)])
        if label_all:
            self.history_label.set_label(f"History · all devices ({len(rows)})")
        elif key:
            node = self.snapshot.nodes.get(key)
            name = node.label if node else key
            self.history_label.set_label(f"History · {name} ({len(rows)})")
        else:
            self.history_label.set_label("History")

    # ---- actions --------------------------------------------------------

    def _on_pause(self, button):
        self.monitor.set_paused(button.get_active())
        button.set_icon_name("media-playback-start-symbolic" if button.get_active()
                             else "media-playback-pause-symbolic")
        self._update_header()

    def _on_interval(self, spin):
        self.monitor.set_interval(spin.get_value())

    def _act_forget_lost(self, *_):
        count = self.reader.forget_absent()
        self._toast(f"Forgot {count} lost device{'' if count == 1 else 's'}.")
        self.monitor.poke()

    def _act_forget_one(self, *_):
        key = self.selected_key
        if not key:
            self._toast("No device selected.")
            return
        node = self.snapshot.nodes.get(key)
        if node is not None and node.present:
            self._toast("That device is still connected; unplug it first.")
            return
        self.reader.forget(key)
        self.selected_key = None
        self._toast("Device forgotten.")
        self.monitor.poke()

    def _act_export(self, *_):
        key = self.selected_key
        if not key:
            self._toast("No device selected.")
            return
        node = self.snapshot.nodes.get(key)
        safe = "".join(c if c.isalnum() or c in "-_." else "_"
                       for c in (node.label if node else key))[:50]
        path = GLib.build_filenamev([GLib.get_home_dir(),
                                     f"usb-tracker-{safe}.csv"])
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("timestamp,iso_time,est_ma,budget_ma,subtree_ma,status,urb_rate\n")
                for ts, est, budget, subtree, status, urb in self.reader.samples(key):
                    fh.write(f"{ts:.3f},{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts))},"
                             f"{est:.2f},{budget:.0f},{subtree:.2f},{status},{urb:.2f}\n")
                fh.write("\n# events\ntimestamp,iso_time,kind,detail\n")
                for ts, _k, kind, detail in reversed(self.reader.events(key, limit=100000)):
                    fh.write(f"{ts:.3f},{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(ts))},"
                             f"{kind},\"{detail}\"\n")
        except OSError as exc:
            self._toast(f"Export failed: {exc}")
            return
        self._toast(f"Wrote {path}")

    def _act_about_power(self, *_):
        text = (
            "Linux exposes no actual current measurement for USB devices, so the "
            "draw shown here is an estimate, not a reading.\n\n"
            "• <b>Declared budget</b> is bMaxPower from the device's active "
            "configuration: the most it is allowed to pull from the bus.\n"
            "• <b>Estimated draw</b> weights that budget by the share of each "
            "interval the device actually spent powered up, taken from the "
            "kernel's runtime-PM counters (runtime_active_time and "
            "runtime_suspended_time). A suspended device is floored at the "
            "spec's 2.5 mA.\n"
            "• <b>Including downstream</b> adds every device behind a hub, which "
            "is how you spot a hub budgeted past what its port can supply.\n\n"
            "Drops, resets and over-current trips are real: they come from "
            "sysfs appearing and disappearing, the port's over_current_count, "
            "and the kernel ring buffer.\n\n"
            "<b>Autosuspend</b> can be changed from the right-click menu. "
            "The kernel owns that setting, so the change goes through polkit "
            "and you will be asked to authenticate. It lasts until the device "
            "is replugged or the machine reboots — copy the udev rule from the "
            "same menu to make it stick. "
            + ("The polkit helper is installed, so one authentication covers "
               "a run of changes." if power.helper_installed() else
               "Running packaging/install-helper.sh narrows what is authorised "
               "and stops polkit asking every single time.")
        )
        dialog = Gtk.AlertDialog(message="How power is estimated", detail=text)
        dialog.show(self)

    def _toast(self, message: str):
        self.subtitle_label.set_label(message)

    def _on_close(self, *_):
        self.monitor.stop(join=False)
        return False


class TrackerApp(Gtk.Application):
    def __init__(self, db_path: str, interval: float, use_kmsg: bool,
                 prune_days: float):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.db_path = db_path
        self.interval = interval
        self.use_kmsg = use_kmsg
        self.prune_days = prune_days
        self.monitor: Monitor | None = None
        self.window: TrackerWindow | None = None

    def do_startup(self):
        Gtk.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)

    def do_activate(self):
        if self.window is not None:
            self.window.present()
            return
        self.monitor = Monitor(db_path=self.db_path, interval=self.interval,
                               use_kmsg=self.use_kmsg, prune_days=self.prune_days)
        reader = store.Store(self.db_path)
        self.window = TrackerWindow(self, self.monitor, reader)
        self.monitor.on_snapshot = self.window.on_snapshot
        self.monitor.start()
        self.window.present()

    def do_shutdown(self):
        if self.monitor is not None:
            self.monitor.stop(join=False)
        Gtk.Application.do_shutdown(self)


def run(db_path: str, interval: float, use_kmsg: bool, prune_days: float,
        argv: list[str] | None = None) -> int:
    app = TrackerApp(db_path, interval, use_kmsg, prune_days)
    return app.run(argv or [])
