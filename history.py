"""
BatoWatch history: SQLite, three tiers of fixed size.

  raw     every sample (10 s)       kept KEEP_RAW    (24 h)
  minute  1-min min/avg/max         kept KEEP_MINUTE (7 days)
  hour    1-h min/avg/max           kept KEEP_HOUR   (1 year)

Written to be gentle on SD cards and SSDs:
  - one row per moment, with every value packed together (4-byte floats) in
    the order of a "layout" (the list of metrics it holds), instead of one
    row per value; rows are keyed by time, so writes only ever append;
  - samples wait in memory and go to disk every 5 minutes in one
    transaction (about 12 MB written a day for ~50 sensors); at most 5
    minutes of graphs are lost on a power cut. Alerts and other events are
    written at once;
  - old rows are deleted from the front as they age, so the file stays the
    same size.

The same class backs hardware.db (sensors) and network.db (per-device
traffic), each with an events table for alerts, game sessions and
connections.
"""
import array
import logging
import os
import sqlite3
import threading
import time

LOG = logging.getLogger("batowatch")
POINTS = 500                     # roughly how many points a graph gets
# Small pages: every save rewrites whole pages (new rows, averages, bookkeeping),
# so 1 KB pages write a quarter of what SQLite's usual 4 KB pages do
PAGE_SIZE = 1024
PRUNE_EVERY = 3600               # s between trimming old rows
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "1y": 365 * 86400}

SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                                    unit TEXT, label TEXT);
CREATE TABLE IF NOT EXISTS layouts (id INTEGER PRIMARY KEY, ids TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS raw    (t INTEGER PRIMARY KEY, layout INTEGER NOT NULL, data BLOB);
CREATE TABLE IF NOT EXISTS minute (t INTEGER PRIMARY KEY, layout INTEGER NOT NULL, data BLOB);
CREATE TABLE IF NOT EXISTS hour   (t INTEGER PRIMARY KEY, layout INTEGER NOT NULL, data BLOB);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                                   t0 INTEGER NOT NULL, t1 INTEGER, key TEXT,
                                   text TEXT, value REAL);
CREATE INDEX IF NOT EXISTS events_i ON events (kind, t0);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def pack(values):
    return array.array("f", values).tobytes()


def unpack(blob):
    a = array.array("f")
    a.frombytes(blob)
    return a


class History:
    def __init__(self, path, keep):
        """keep: {'raw': s, 'minute': s, 'hour': s}"""
        self.path, self.keep = path, dict(keep)
        self.lock = threading.Lock()
        self.pending = {}            # t -> {metric name: value}, not yet in `recent`
        # The last day of full-detail readings lives in memory, not on disk:
        # writing it every few minutes was most of BatoWatch's disk writes.
        # It's saved to the raw table on a clean stop and read back at start;
        # after a power cut only this detail is lost, the minute averages stay.
        self.recent = []             # [(t, layout id, packed values)], oldest first
        self.ids = {}
        self.layouts = {}            # layout id -> tuple of metric ids
        self.layout_ids = {}         # tuple of metric ids -> layout id
        self.db = None
        self.open()

    # -- opening, and starting over when the file is damaged -------------------
    def open(self):
        """Only a damaged file is set aside. A locked one (BatoWatch still
        stopping, holding it while it saves) is waited for; other errors (disk
        full, no permission) stop BatoWatch, and the service starts it again."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        deadline = time.time() + 60
        while True:
            try:
                self._connect()
                ok = self.db.execute("PRAGMA quick_check").fetchone()[0]
                if ok != "ok":
                    raise sqlite3.DatabaseError(ok)
                return
            except sqlite3.OperationalError as e:
                self._close_quietly()
                if ("locked" in str(e) or "busy" in str(e)) and time.time() < deadline:
                    time.sleep(2)
                    continue
                raise
            except sqlite3.DatabaseError as e:
                self._start_over(e)
                return

    def _close_quietly(self):
        try:
            if self.db:
                self.db.close()
        except sqlite3.Error:
            pass
        self.db = None

    def _connect(self):
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        # Only BatoWatch opens these files, so SQLite can keep its WAL index in
        # memory instead of a -shm file on disk that every save rewrites
        self.db.execute("PRAGMA locking_mode=EXCLUSIVE")
        # page size is fixed when a database is made; an older one is rebuilt
        # once with small pages (a few MB, a second or two)
        if self.db.execute("PRAGMA page_size").fetchone()[0] != PAGE_SIZE:
            new = self.db.execute("PRAGMA page_count").fetchone()[0] == 0
            if not new:
                self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute(f"PRAGMA page_size={PAGE_SIZE}")
            if not new:
                self.db.execute("VACUUM")
                LOG.info(f"history: {os.path.basename(self.path)} rebuilt with small pages")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        # don't zero deleted rows on disk (some SQLite builds do by default): more writes for nothing
        self.db.execute("PRAGMA secure_delete=OFF")
        self.db.executescript(SCHEMA)
        self.ids = {name: i for i, name in self.db.execute("SELECT id, name FROM metrics")}
        self.layouts, self.layout_ids = {}, {}
        for i, ids in self.db.execute("SELECT id, ids FROM layouts"):
            key = tuple(int(x) for x in ids.split(",")) if ids else ()
            self.layouts[i], self.layout_ids[key] = key, i
        since = int(time.time()) - self.keep["raw"]
        self.recent = list(self.db.execute("SELECT t, layout, data FROM raw WHERE t >= ? "
                                           "ORDER BY t", (since,)))

    def _start_over(self, err):
        try:
            if self.db:
                self.db.close()
        except sqlite3.Error:
            pass
        broken = self.path[:-3] + ".broken" if self.path.endswith(".db") else self.path + ".broken"
        for suffix in ("", "-wal", "-shm"):
            try:
                os.replace(self.path + suffix, broken + suffix)
            except OSError:
                pass
        LOG.error(f"history: {os.path.basename(self.path)} was damaged ({err}); kept it as "
                  f"{os.path.basename(broken)} and started a new one")
        self._connect()

    # -- ids -------------------------------------------------------------------
    # A new id is saved at once (rare: a new sensor or a new set of them) and
    # only remembered once saved, so a save that fails later can't leave
    # readings pointing at an id the file doesn't have
    def metric_id(self, name, unit=None, label=None):
        i = self.ids.get(name)
        if i is None:
            cur = self.db.execute("INSERT OR IGNORE INTO metrics (name, unit, label) VALUES "
                                  "(?, ?, ?)", (name, unit, label))
            i = cur.lastrowid if cur.rowcount else self.db.execute(
                "SELECT id FROM metrics WHERE name=?", (name,)).fetchone()[0]
            self.db.commit()
            self.ids[name] = i
        return i

    def layout_id(self, key):
        i = self.layout_ids.get(key)
        if i is None:
            ids = ",".join(map(str, key))
            self.db.execute("INSERT OR IGNORE INTO layouts (ids) VALUES (?)", (ids,))
            i = self.db.execute("SELECT id FROM layouts WHERE ids=?", (ids,)).fetchone()[0]
            self.db.commit()
            self.layouts[i], self.layout_ids[key] = key, i
        return i

    # -- writing ---------------------------------------------------------------
    def add(self, t, values, units=None, labels=None):
        """values: {metric name: number}. Kept in memory until flush()."""
        units, labels = units or {}, labels or {}
        with self.lock:
            row = self.pending.setdefault(int(t), {})
            for name, v in values.items():
                if v is None:
                    continue
                row[name] = (float(v), units.get(name), labels.get(name))

    def flush(self, now=None):
        now = int(now or time.time())
        with self.lock:
            rows, self.pending = self.pending, {}
            try:
                with self.db:
                    last = self.recent[-1][0] if self.recent else 0
                    for t in sorted(rows):
                        vals = {self.metric_id(n, u, lb): v for n, (v, u, lb) in rows[t].items()}
                        key = tuple(sorted(vals))
                        row = (t, self.layout_id(key), pack(vals[i] for i in key))
                        if t > last:
                            self.recent.append(row)
                            last = t
                    self._roll(now)
                    self._prune(now)
            except sqlite3.OperationalError as e:
                # locked, disk full, a read error: not damage. Keep the readings
                # (the in-memory day has them) and try again at the next save
                LOG.warning(f"history: couldn't save ({e}); will retry")
            except sqlite3.DatabaseError as e:
                self._start_over(e)

    def _meta(self, k, default=None):
        r = self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return int(r[0]) if r else default

    def _set_meta(self, k, v):
        self.db.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))

    def get_flag(self, k):
        """A one-time marker (e.g. a migration that has run), saved with the next flush."""
        with self.lock:
            return self._meta(k, 0) == 1

    def set_flag(self, k):
        with self.lock:
            self._set_meta(k, 1)

    def device_names(self):
        """{ip: latest name} of every device in the history."""
        with self.lock:
            rows = self.db.execute("SELECT key, text FROM events WHERE kind='device' "
                                   "ORDER BY t0").fetchall()
        names = {}
        for k, t in rows:
            if k and (t and t != k or k not in names):
                names[k] = t or k     # the latest real name beats a bare IP
        return names

    def _recent_between(self, start, end):
        return [r for r in self.recent if start <= r[0] < end]

    def _aggregate(self, src, start, end, step, triples):
        """Rows of `src` ("raw": the in-memory day, or a table) in [start, end)
        -> one row per `step` seconds with min/avg/max per metric."""
        groups = {}
        rows = self._recent_between(start, end) if src == "raw" else self.db.execute(
            f"SELECT t, layout, data FROM {src} WHERE t >= ? AND t < ? ORDER BY t", (start, end))
        for t, lay, blob in rows:
            ids, vals = self.layouts.get(lay, ()), unpack(blob)
            g = groups.setdefault(t - t % step, {})
            if triples:
                for n, i in enumerate(ids):
                    lo, avg, hi = vals[3 * n], vals[3 * n + 1], vals[3 * n + 2]
                    s = g.get(i)
                    if s is None:
                        g[i] = [lo, avg, hi, 1]
                    else:
                        s[0], s[1], s[2], s[3] = min(s[0], lo), s[1] + avg, max(s[2], hi), s[3] + 1
            else:
                for i, v in zip(ids, vals):
                    s = g.get(i)
                    if s is None:
                        g[i] = [v, v, v, 1]
                    else:
                        s[0], s[1], s[2], s[3] = min(s[0], v), s[1] + v, max(s[2], v), s[3] + 1
        out = []
        for t, g in sorted(groups.items()):
            key = tuple(sorted(g))
            flat = []
            for i in key:
                lo, total, hi, n = g[i]
                flat += (lo, total / n, hi)
            out.append((t, self.layout_id(key), pack(flat)))
        return out

    def _roll(self, now):
        """Average finished minutes into `minute`, finished hours into `hour`."""
        done_min = self._meta("minute_done", 0)
        upto_min = now - now % 60                    # minutes before this are finished
        if upto_min > done_min:
            start = done_min or upto_min - self.keep["raw"]
            rows = self._aggregate("raw", start, upto_min, 60, False)
            self.db.executemany("INSERT OR REPLACE INTO minute (t, layout, data) VALUES (?, ?, ?)",
                                rows)
            self._set_meta("minute_done", upto_min)
        done_hour = self._meta("hour_done", 0)
        upto_hour = now - now % 3600
        if upto_hour > done_hour:
            start = done_hour or upto_hour - self.keep["minute"]
            rows = self._aggregate("minute", start, upto_hour, 3600, True)
            self.db.executemany("INSERT OR REPLACE INTO hour (t, layout, data) VALUES (?, ?, ?)",
                                rows)
            self._set_meta("hour_done", upto_hour)

    def _prune(self, now):
        last = self._meta("pruned", 0)
        if now - last < PRUNE_EVERY:
            return
        cut = now - self.keep["raw"]
        if self.recent and self.recent[0][0] < cut:
            self.recent = [r for r in self.recent if r[0] >= cut]
        for table in ("minute", "hour"):
            self.db.execute(f"DELETE FROM {table} WHERE t < ?", (now - self.keep[table],))
        self.db.execute("DELETE FROM events WHERE t0 < ?", (now - self.keep["hour"],))
        self._set_meta("pruned", now)
        # Give freed pages back now and then, so the file really stays small
        if now - self._meta("vacuumed", now) > 7 * 86400:
            self._set_meta("vacuumed", now)
            self.db.commit()
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.execute("VACUUM")
        elif self._meta("vacuumed") is None:
            self._set_meta("vacuumed", now)

    # -- events (alerts, game sessions, connections) -----------------------------
    # Events aren't saved on their own: they go to disk with the next flush(),
    # like the readings (a save per connection was most of the disk writes)
    def event_start(self, kind, t0, key, text, value=None):
        with self.lock:
            return self.db.execute("INSERT INTO events (kind, t0, key, text, value) "
                                   "VALUES (?, ?, ?, ?, ?)",
                                   (kind, int(t0), key, text, value)).lastrowid

    def event_end(self, event_id, t1, value=None):
        with self.lock:
            if value is None:
                self.db.execute("UPDATE events SET t1=? WHERE id=?", (int(t1), event_id))
            else:
                self.db.execute("UPDATE events SET t1=?, value=? WHERE id=?",
                                (int(t1), value, event_id))

    def close_open_events(self, now):
        """Events left open by a crash or power cut end at the last write."""
        with self.lock:
            with self.db:
                self.db.execute("UPDATE events SET t1=? WHERE t1 IS NULL", (int(now),))

    def events(self, kinds, since, limit=200):
        q = ",".join("?" * len(kinds))
        with self.lock:
            rows = self.db.execute(
                f"SELECT kind, t0, t1, key, text, value FROM events WHERE kind IN ({q}) "
                f"AND (t1 IS NULL OR t1 >= ?) ORDER BY t0 DESC LIMIT ?",
                (*kinds, int(since), limit)).fetchall()
        return [{"kind": k, "t0": a, "t1": b, "key": key, "text": tx, "value": v}
                for k, a, b, key, tx, v in rows]

    # -- reading ---------------------------------------------------------------
    def series(self, names, range_name, now=None):
        """{name: [[t, min, avg, max], ...]} for a graph, about POINTS points."""
        now = int(now or time.time())
        span = RANGES.get(range_name, 86400)
        since = now - span
        bucket = max(1, span // POINTS)
        if span <= self.keep["raw"] and span <= 6 * 3600:
            table = "raw"                       # the in-memory day
        elif span <= self.keep["minute"]:
            table = "minute"
        else:
            table = "hour"
        triples = table != "raw"
        out = {n: {} for n in names}
        with self.lock:
            want = {self.ids[n]: n for n in names if n in self.ids}
            if want:
                pos_cache = {}
                rows = self._recent_between(since, now + 1) if table == "raw" else self.db.execute(
                    f"SELECT t, layout, data FROM {table} WHERE t >= ? ORDER BY t", (since,))
                for t, lay, blob in rows:
                    pos = pos_cache.get(lay)
                    if pos is None:
                        ids = self.layouts.get(lay, ())
                        pos = pos_cache[lay] = [(n, want[i]) for n, i in enumerate(ids)
                                                if i in want]
                    if not pos:
                        continue
                    vals = unpack(blob)
                    b = t - t % bucket
                    for n, name in pos:
                        if triples:
                            lo, avg, hi = vals[3 * n], vals[3 * n + 1], vals[3 * n + 2]
                        else:
                            lo = avg = hi = vals[n]
                        self._into(out[name], b, lo, avg, hi)
            if table == "raw":
                # Points not written yet are in memory; include them for live graphs
                for t, row in self.pending.items():
                    if t < since:
                        continue
                    for name in names:
                        if name in row:
                            v = row[name][0]
                            self._into(out[name], t - t % bucket, v, v, v)
        return {name: [[t, round(s[0], 2), round(s[1] / s[3], 2), round(s[2], 2)]
                       for t, s in sorted(pts.items())] for name, pts in out.items()}

    @staticmethod
    def _into(pts, b, lo, avg, hi):
        s = pts.get(b)
        if s is None:
            pts[b] = [lo, avg, hi, 1]
        else:
            s[0], s[1], s[2], s[3] = min(s[0], lo), s[1] + avg, max(s[2], hi), s[3] + 1

    def metrics(self):
        with self.lock:
            return [{"name": n, "unit": u, "label": lb} for n, u, lb in
                    self.db.execute("SELECT name, unit, label FROM metrics ORDER BY name")]

    def size(self):
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += os.stat(self.path + suffix).st_size
            except OSError:
                pass
        return total

    def save_recent(self):
        """Keep the in-memory day across a restart (called on a clean stop)."""
        with self.lock:
            try:
                with self.db:
                    self.db.execute("DELETE FROM raw")
                    self.db.executemany("INSERT OR REPLACE INTO raw (t, layout, data) "
                                        "VALUES (?, ?, ?)", self.recent)
            except sqlite3.Error as e:
                LOG.warning(f"history: couldn't save the last day's detail: {e}")

    def close(self):
        self.save_recent()
        with self.lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
