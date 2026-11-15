from contextlib import closing
from dataclasses import dataclass
import sqlite3

from ar844.device import Mode, Sample

PERIODS = {5: "noise_data_5min", 15: "noise_data_15min",
           30: "noise_data_30min", 60: "noise_data_1h"}
PAGE_SIZE = 200
CHART_BINS = 600


def connect(path, *, read_only=False):
    if read_only:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    else:
        conn = sqlite3.connect(path, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


def initialize(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS noise_data_raw (
            id INTEGER PRIMARY KEY, time_ms INTEGER NOT NULL, db REAL NOT NULL,
            speed TEXT NOT NULL, weight TEXT NOT NULL, run TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS noise_raw_time ON noise_data_raw(time_ms);
        CREATE INDEX IF NOT EXISTS noise_raw_weight_time ON noise_data_raw(weight, time_ms);
    """)
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


def save(conn, sample):
    ts, db, mode = sample.time_ms, sample.db, sample.mode
    with conn:
        conn.execute("""
            INSERT INTO noise_data_raw(time_ms, db, speed, weight, run)
            VALUES (?, ?, ?, ?, ?)
        """, (ts, db, mode.speed, mode.weight, mode.run))
        for minutes, table in PERIODS.items():
            duration = minutes * 60_000
            start = ts // duration * duration
            conn.execute(f"""
                INSERT INTO {table}
                    (start_ms, end_ms, max_db, peak_ms, sample_count, speed, weight, run)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT (start_ms, weight, speed, run) DO UPDATE SET
                    sample_count = sample_count + 1,
                    peak_ms = CASE
                        WHEN excluded.max_db > max_db THEN excluded.peak_ms
                        WHEN excluded.max_db = max_db THEN MIN(peak_ms, excluded.peak_ms)
                        ELSE peak_ms END,
                    max_db = MAX(max_db, excluded.max_db)
            """, (start, start + duration, db, ts, mode.speed, mode.weight, mode.run))


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
        # Select whole buckets that overlap the requested time range.
        args[0] = request.start // (period * 60_000) * (period * 60_000)
        source = f"""
            SELECT start_ms AS ts, end_ms, max_db AS db, peak_ms, sample_count,
                   speed, weight, run, 0 AS id FROM {table}
            WHERE start_ms >= ? AND start_ms < ? {weight_filter}
        """
    else:
        source = f"""
            SELECT time_ms AS ts, NULL AS end_ms, db, time_ms AS peak_ms,
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
                    f"SELECT * FROM ({source}) ORDER BY {order_by}", args
                ).fetchall()
            else:
                width = max(1, (request.end - args[0] + CHART_BINS - 1) // CHART_BINS)
                chart = conn.execute(f"""
                    WITH bins AS (
                        SELECT *, (ts - ?) / ? AS bucket FROM ({source})
                    ),
                    ranked AS (
                        SELECT *,
                            ROW_NUMBER() OVER (PARTITION BY bucket, weight, speed, run
                                               ORDER BY db, ts, id) AS low,
                            ROW_NUMBER() OVER (PARTITION BY bucket, weight, speed, run
                                               ORDER BY db DESC, ts, id) AS high
                        FROM bins
                    )
                    SELECT * FROM ranked WHERE low = 1 OR high = 1 ORDER BY {order_by}
                """, [args[0], width, *args]).fetchall()
        return HistoryResult(count, rows, chart)
