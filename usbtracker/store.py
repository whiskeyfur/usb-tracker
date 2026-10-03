"""SQLite persistence: the device roster, the event log, the power samples.

History outlives the process, so a device unplugged last week is still in the
tree as a shadow when the app starts again.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

DEFAULT_DB = os.path.join(
    os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
    "usb-tracker", "history.db",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    key           TEXT PRIMARY KEY,
    busid         TEXT,
    parent_key    TEXT,
    vid           TEXT,
    pid           TEXT,
    serial        TEXT,
    product       TEXT,
    manufacturer  TEXT,
    class_hint    TEXT,
    speed_mbps    REAL,
    max_power_ma  INTEGER,
    version       TEXT,
    is_hub        INTEGER DEFAULT 0,
    self_powered  INTEGER DEFAULT 0,
    first_seen    REAL,
    last_seen     REAL,
    present       INTEGER DEFAULT 0,
    connects      INTEGER DEFAULT 0,
    disconnects   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    key    TEXT NOT NULL,
    kind   TEXT NOT NULL,
    detail TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_key_ts ON events (key, ts DESC);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);

CREATE TABLE IF NOT EXISTS samples (
    ts         REAL NOT NULL,
    key        TEXT NOT NULL,
    est_ma     REAL NOT NULL,
    budget_ma  REAL NOT NULL,
    subtree_ma REAL NOT NULL,
    status     TEXT DEFAULT '',
    urb_rate   REAL DEFAULT 0,
    rx_bps     REAL DEFAULT 0,
    tx_bps     REAL DEFAULT 0,
    alloc_bps  REAL DEFAULT 0,
    sub_bps    REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS samples_key_ts ON samples (key, ts);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""

# Columns added after the first release; existing databases get them on open.
MIGRATIONS = {
    "samples": (("rx_bps", "REAL DEFAULT 0"), ("tx_bps", "REAL DEFAULT 0"),
                ("alloc_bps", "REAL DEFAULT 0"), ("sub_bps", "REAL DEFAULT 0")),
    "devices": (("counter_source", "TEXT DEFAULT ''"),),
}

# Event kinds. Anything not in here still stores fine; this is for display.
KIND_ATTACH = "attach"
KIND_CONNECT = "reconnect"
KIND_DISCONNECT = "disconnect"
KIND_SUSPEND = "suspend"
KIND_RESUME = "resume"
KIND_CONFIG = "config"
KIND_DRIVER = "driver"
KIND_OVERCURRENT = "over-current"
KIND_RESET = "reset"
KIND_ERROR = "bus error"
KIND_FLAP = "flap"
KIND_BANDWIDTH = "bandwidth"
KIND_OUTAGE = "outage"        # several devices vanished together
KIND_STALL = "stall"          # the tracker itself stopped getting time
KIND_CONTROLLER = "controller"  # the host controller complained
KIND_SESSION = "session"


@dataclass
class DeviceRow:
    key: str
    busid: str
    parent_key: str | None
    vid: str
    pid: str
    serial: str
    product: str
    manufacturer: str
    class_hint: str
    speed_mbps: float
    max_power_ma: int
    version: str
    is_hub: bool
    self_powered: bool
    counter_source: str
    first_seen: float
    last_seen: float
    present: bool
    connects: int
    disconnects: int

    @property
    def label(self) -> str:
        return self.product or self.class_hint or f"{self.vid}:{self.pid}"


def _row_to_device(row: sqlite3.Row) -> DeviceRow:
    return DeviceRow(
        key=row["key"], busid=row["busid"] or "", parent_key=row["parent_key"],
        vid=row["vid"] or "", pid=row["pid"] or "", serial=row["serial"] or "",
        product=row["product"] or "", manufacturer=row["manufacturer"] or "",
        class_hint=row["class_hint"] or "", speed_mbps=row["speed_mbps"] or 0.0,
        max_power_ma=row["max_power_ma"] or 0, version=row["version"] or "",
        is_hub=bool(row["is_hub"]), self_powered=bool(row["self_powered"]),
        counter_source=(row["counter_source"] if "counter_source" in row.keys()
                        else "") or "",
        first_seen=row["first_seen"] or 0.0, last_seen=row["last_seen"] or 0.0,
        present=bool(row["present"]), connects=row["connects"] or 0,
        disconnects=row["disconnects"] or 0,
    )


class Store:
    """One connection, owned by whichever thread created it."""

    def __init__(self, path: str = DEFAULT_DB, read_only: bool = False):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=10000")
        if not read_only:
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self) -> None:
        """Add columns a newer version expects, leaving existing rows intact."""
        for table, columns in MIGRATIONS.items():
            have = {r["name"] for r in
                    self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in columns:
                if name not in have:
                    self.db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    # ---- writes ---------------------------------------------------------

    def upsert_device(self, *, key: str, busid: str, parent_key: str | None,
                      vid: str, pid: str, serial: str, product: str,
                      manufacturer: str, class_hint: str, speed_mbps: float,
                      max_power_ma: int, version: str, is_hub: bool,
                      self_powered: bool, ts: float, present: bool,
                      counter_source: str = "") -> bool:
        """Insert or refresh a device. Returns True if it was brand new."""
        cur = self.db.execute("SELECT key FROM devices WHERE key = ?", (key,))
        is_new = cur.fetchone() is None
        if is_new:
            self.db.execute(
                """INSERT INTO devices (key, busid, parent_key, vid, pid, serial,
                       product, manufacturer, class_hint, speed_mbps, max_power_ma,
                       version, is_hub, self_powered, first_seen, last_seen, present,
                       counter_source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, busid, parent_key, vid, pid, serial, product, manufacturer,
                 class_hint, speed_mbps, max_power_ma, version, int(is_hub),
                 int(self_powered), ts, ts, int(present), counter_source))
        else:
            self.db.execute(
                """UPDATE devices SET busid=?, parent_key=?, product=?,
                       manufacturer=?, class_hint=?, speed_mbps=?, max_power_ma=?,
                       version=?, is_hub=?, self_powered=?, last_seen=?, present=?,
                       counter_source=?
                   WHERE key=?""",
                (busid, parent_key, product, manufacturer, class_hint, speed_mbps,
                 max_power_ma, version, int(is_hub), int(self_powered), ts,
                 int(present), counter_source, key))
        return is_new

    def mark_absent(self, key: str, ts: float) -> None:
        self.db.execute("UPDATE devices SET present=0, last_seen=? WHERE key=?",
                        (ts, key))

    def bump_counter(self, key: str, column: str) -> None:
        if column not in ("connects", "disconnects"):
            raise ValueError(column)
        self.db.execute(
            f"UPDATE devices SET {column} = COALESCE({column}, 0) + 1 WHERE key = ?",
            (key,))

    def add_event(self, ts: float, key: str, kind: str, detail: str = "") -> None:
        self.db.execute("INSERT INTO events (ts, key, kind, detail) VALUES (?,?,?,?)",
                        (ts, key, kind, detail))

    def add_sample(self, ts: float, key: str, est_ma: float, budget_ma: float,
                   subtree_ma: float, status: str, urb_rate: float,
                   rx_bps: float = 0.0, tx_bps: float = 0.0,
                   alloc_bps: float = 0.0, sub_bps: float = 0.0) -> None:
        self.db.execute(
            """INSERT INTO samples (ts, key, est_ma, budget_ma, subtree_ma, status,
                   urb_rate, rx_bps, tx_bps, alloc_bps, sub_bps)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (ts, key, est_ma, budget_ma, subtree_ma, status, urb_rate,
             rx_bps, tx_bps, alloc_bps, sub_bps))

    def commit(self) -> None:
        self.db.commit()

    def forget(self, key: str) -> None:
        """Drop a device and its history entirely."""
        self.db.execute("DELETE FROM devices WHERE key=?", (key,))
        self.db.execute("DELETE FROM events WHERE key=?", (key,))
        self.db.execute("DELETE FROM samples WHERE key=?", (key,))
        self.db.commit()

    def forget_absent(self) -> int:
        cur = self.db.execute("SELECT key FROM devices WHERE present=0")
        keys = [r["key"] for r in cur.fetchall()]
        for key in keys:
            self.forget(key)
        return len(keys)

    def prune(self, keep_days: float = 7.0) -> int:
        cutoff = time.time() - keep_days * 86400
        cur = self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        deleted = cur.rowcount or 0
        self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.commit()
        return deleted

    # ---- reads ----------------------------------------------------------

    def devices(self) -> list[DeviceRow]:
        cur = self.db.execute("SELECT * FROM devices")
        return [_row_to_device(r) for r in cur.fetchall()]

    def samples(self, key: str, since: float | None = None) -> list[tuple]:
        if since is None:
            cur = self.db.execute(
                """SELECT ts, est_ma, budget_ma, subtree_ma, status, urb_rate,
                          rx_bps, tx_bps, alloc_bps, sub_bps
                   FROM samples WHERE key=? ORDER BY ts""", (key,))
        else:
            cur = self.db.execute(
                """SELECT ts, est_ma, budget_ma, subtree_ma, status, urb_rate,
                          rx_bps, tx_bps, alloc_bps, sub_bps
                   FROM samples WHERE key=? AND ts>=? ORDER BY ts""", (key, since))
        return [tuple(r) for r in cur.fetchall()]

    def events(self, key: str | None = None, since: float | None = None,
               limit: int = 500) -> list[tuple]:
        sql = "SELECT ts, key, kind, detail FROM events"
        where, args = [], []
        if key is not None:
            where.append("key = ?")
            args.append(key)
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if where:
            sql += " WHERE " + " AND ".join(where)
        # id breaks ties so events logged in the same poll keep their order
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [tuple(r) for r in self.db.execute(sql, args).fetchall()]

    def outages(self, since: float | None = None, limit: int = 200) -> list[tuple]:
        """Correlated outage events, newest first."""
        sql = "SELECT ts, key, kind, detail FROM events WHERE kind = ?"
        args: list = [KIND_OUTAGE]
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        return [tuple(r) for r in self.db.execute(sql, args).fetchall()]

    def event_counts(self, key: str) -> dict[str, int]:
        cur = self.db.execute(
            "SELECT kind, COUNT(*) n FROM events WHERE key=? GROUP BY kind", (key,))
        return {r["kind"]: r["n"] for r in cur.fetchall()}

    def db_size(self) -> int:
        try:
            return os.path.getsize(self.path)
        except OSError:
            return 0

    def samples_bucketed(self, key: str, since: float, bucket: float) -> list[tuple]:
        """Averaged samples for long windows, so a day of history still draws fast."""
        if bucket <= 0:
            return self.samples(key, since)
        cur = self.db.execute(
            """SELECT MIN(ts), AVG(est_ma), MAX(budget_ma), AVG(subtree_ma),
                      '', AVG(urb_rate), AVG(rx_bps), AVG(tx_bps),
                      MAX(alloc_bps), AVG(sub_bps)
                 FROM samples WHERE key=? AND ts>=?
                 GROUP BY CAST((ts - ?) / ? AS INTEGER)
                 ORDER BY 1""",
            (key, since, since, bucket))
        return [tuple(r) for r in cur.fetchall()]

    def first_sample_ts(self, key: str) -> float | None:
        row = self.db.execute("SELECT MIN(ts) t FROM samples WHERE key=?",
                              (key,)).fetchone()
        return row["t"] if row and row["t"] is not None else None
