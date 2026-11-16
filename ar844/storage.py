from bisect import insort
from contextlib import closing
from dataclasses import dataclass
import sqlite3

from ar844.device import Mode, Sample

PERIODS = {5: "noise_data_5min", 15: "noise_data_15min",
           30: "noise_data_30min", 60: "noise_data_1h"}
PAGE_SIZE = 200
CHART_BINS = 600
METRICS = {
    "max": ("峰值", "max_db"),
    "min": ("最小值", "min_db"),
    "avg": ("平均值", "avg_db"),
    "max_rise": ("最大单步涨幅", "max_rise"),
    "p10": ("P10（底噪）", "p10"),
    "p90": ("P90", "p90"),
    "above_floor": ("峰值高出底噪", "max_db - p10"),
}


class RollupConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.windows = {}


def connect(path, *, read_only=False):
    if read_only:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    else:
        conn = sqlite3.connect(path, timeout=3, factory=RollupConnection)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass(slots=True)
class Window:
    start: int
    ordered_db: list
    total: float = 0.0
    peak_ms: int = 0
    last_ms: int | None = None
    last_db: float = 0.0
    max_rise: float | None = None

    def add(self, ts, db):
        if not self.ordered_db or db > self.ordered_db[-1]:
            self.peak_ms = ts
        if self.last_ms is not None:
            rise = max(0.0, round(db - self.last_db, 10))
            self.max_rise = max(self.max_rise or 0.0, rise)
        insort(self.ordered_db, db)
        self.total += db
        self.last_ms = ts
        self.last_db = db

    def percentile(self, fraction):
        rank = (len(self.ordered_db) - 1) * fraction
        index = int(rank)
        lower = self.ordered_db[index]
        upper = self.ordered_db[min(index + 1, len(self.ordered_db) - 1)]
        return lower + (upper - lower) * (rank - index)


def _write_window(conn, minutes, mode, window):
    table = PERIODS[minutes]
    count = len(window.ordered_db)
    conn.execute(f"""
        INSERT INTO {table}
            (start_ms, end_ms, max_db, peak_ms, sample_count, speed, weight, run,
             min_db, avg_db, max_rise, p10, p90)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (start_ms, weight, speed, run) DO UPDATE SET
            max_db=excluded.max_db, peak_ms=excluded.peak_ms, sample_count=excluded.sample_count,
            min_db=excluded.min_db, avg_db=excluded.avg_db, max_rise=excluded.max_rise,
            p10=excluded.p10, p90=excluded.p90
    """, (window.start, window.start + minutes * 60_000, window.ordered_db[-1],
          window.peak_ms, count, mode.speed, mode.weight, mode.run, window.ordered_db[0],
          window.total / count, window.max_rise, window.percentile(0.1), window.percentile(0.9)))


def _backfill(conn, cancelled):
    windows = {}
    rows = conn.execute("""
        SELECT time_ms, db, speed, weight, run FROM noise_data_raw ORDER BY time_ms, id
    """)
    for row in rows:
        if cancelled():
            raise InterruptedError("统计升级已取消")
        ts, db, speed, weight, run = row
        mode = Mode(speed, weight, run)
        for minutes in PERIODS:
            duration = minutes * 60_000
            start = ts // duration * duration
            key = minutes, mode
            window = windows.get(key)
            if window is None or window.start != start:
                if window is not None:
                    _write_window(conn, minutes, mode, window)
                window = windows[key] = Window(start, [])
            window.add(ts, db)
    for (minutes, mode), window in windows.items():
        _write_window(conn, minutes, mode, window)


def initialize(conn, cancelled=lambda: False):
    conn.execute("PRAGMA journal_mode=WAL")
    with conn:
        conn.execute("BEGIN")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS noise_data_raw (
                id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL, db REAL NOT NULL,
                speed TEXT NOT NULL, weight TEXT NOT NULL, run TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS noise_raw_time ON noise_data_raw(time_ms)")
        conn.execute("CREATE INDEX IF NOT EXISTS noise_raw_weight_time ON noise_data_raw(weight, time_ms)")
        for table in PERIODS.values():
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                    max_db REAL NOT NULL, peak_ms INTEGER NOT NULL,
                    sample_count INTEGER NOT NULL,
                    speed TEXT NOT NULL, weight TEXT NOT NULL, run TEXT NOT NULL,
                    PRIMARY KEY (start_ms, weight, speed, run)
                ) WITHOUT ROWID
            """)
        if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
            for table in PERIODS.values():
                for column in ("min_db", "avg_db", "max_rise", "p10", "p90"):
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} REAL")
                conn.execute(f"DELETE FROM {table}")
            _backfill(conn, cancelled)
            conn.execute("PRAGMA user_version=1")
    conn.windows.clear()


def _load_window(conn, start, end, mode):
    window = Window(start, [])
    rows = conn.execute("""
        SELECT time_ms, db FROM noise_data_raw
        WHERE weight=? AND speed=? AND run=? AND time_ms>=? AND time_ms<?
        ORDER BY time_ms, id
    """, (mode.weight, mode.speed, mode.run, start, end))
    for ts, db in rows:
        window.add(ts, db)
    return window


def save(conn, sample):
    ts, db, mode = sample.time_ms, sample.db, sample.mode
    try:
        with conn:
            conn.execute("""
                INSERT INTO noise_data_raw(time_ms, db, speed, weight, run)
                VALUES (?, ?, ?, ?, ?)
            """, (ts, db, mode.speed, mode.weight, mode.run))
            for minutes in PERIODS:
                duration = minutes * 60_000
                start = ts // duration * duration
                key = minutes, mode
                window = conn.windows.get(key)
                if window is None or window.start != start or ts < window.last_ms:
                    # On restart or out-of-order input, rebuild from the authoritative raw samples.
                    window = conn.windows[key] = _load_window(conn, start, start + duration, mode)
                else:
                    window.add(ts, db)
                _write_window(conn, minutes, mode, window)
    except Exception:
        conn.windows.clear()
        raise


def recent(conn, start_ms, limit):
    rows = conn.execute("""
        SELECT time_ms, db, speed, weight, run FROM noise_data_raw
        WHERE time_ms >= ? ORDER BY time_ms DESC, id DESC LIMIT ?
    """, (start_ms, limit)).fetchall()
    return [Sample(row["time_ms"], row["db"], Mode(row["speed"], row["weight"], row["run"]))
            for row in reversed(rows)]


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    start: int
    end: int
    minutes: int = 0
    weight: str = ""
    page: int = 0
    metric: str = "max"


@dataclass(frozen=True, slots=True)
class HistoryResult:
    count: int
    rows: list
    chart: list


def history(path, request, cancelled, *, include_chart=True):
    if request.end <= request.start or request.page < 0:
        raise ValueError("请选择有效的查询时间范围")
    period = request.minutes
    args = [request.start, request.end]
    weight_filter = ""
    if request.weight:
        weight_filter = "AND weight = ?"
        args.append(request.weight)

    if period:
        table = PERIODS[period]
        _, metric = METRICS[request.metric]
        # Select whole buckets that overlap the requested time range.
        args[0] = request.start // (period * 60_000) * (period * 60_000)
        source = f"""
            SELECT start_ms AS ts, end_ms, max_db AS db, peak_ms, sample_count,
                   min_db, avg_db, max_rise, p10, p90, max_db - p10 AS above_floor,
                   ({metric}) AS plot_db,
                   speed, weight, run, 0 AS id FROM {table}
            WHERE start_ms >= ? AND start_ms < ? {weight_filter}
        """
    else:
        source = f"""
            SELECT time_ms AS ts, NULL AS end_ms, db, db AS plot_db, time_ms AS peak_ms,
                   1 AS sample_count, speed, weight, run, id FROM noise_data_raw
            WHERE time_ms >= ? AND time_ms < ? {weight_filter}
        """

    order_by = "ts, weight, speed, run, id"
    with closing(connect(path, read_only=True)) as conn:
        conn.execute("PRAGMA temp_store=FILE")
        conn.execute("PRAGMA cache_size=-8192")
        conn.set_progress_handler(cancelled, 2_000)
        conn.execute("BEGIN")
        count = conn.execute(f"SELECT COUNT(*) FROM ({source})", args).fetchone()[0]
        rows = conn.execute(f"""
            SELECT * FROM ({source}) ORDER BY {order_by} LIMIT ? OFFSET ?
        """, [*args, PAGE_SIZE, request.page * PAGE_SIZE]).fetchall()
        chart = []
        if include_chart:
            if count <= 2 * CHART_BINS:
                chart = conn.execute(
                    f"SELECT * FROM ({source}) WHERE plot_db IS NOT NULL ORDER BY {order_by}", args
                ).fetchall()
            else:
                width = max(1, (request.end - args[0] + CHART_BINS - 1) // CHART_BINS)
                chart = conn.execute(f"""
                    WITH bins AS (
                        SELECT *, (ts - ?) / ? AS bucket FROM ({source}) WHERE plot_db IS NOT NULL
                    ),
                    ranked AS (
                        SELECT *,
                            ROW_NUMBER() OVER (PARTITION BY bucket, weight, speed, run
                                               ORDER BY plot_db, ts, id) AS low,
                            ROW_NUMBER() OVER (PARTITION BY bucket, weight, speed, run
                                               ORDER BY plot_db DESC, ts, id) AS high
                        FROM bins
                    )
                    SELECT * FROM ranked WHERE low = 1 OR high = 1 ORDER BY {order_by}
                """, [args[0], width, *args]).fetchall()
        return HistoryResult(count, rows, chart)
