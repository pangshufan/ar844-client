from contextlib import closing
import sqlite3

import pytest

from ar844.device import Mode, Sample
from ar844.storage import CHART_BINS, PAGE_SIZE, PERIODS, HistoryRequest, connect, history, initialize, save


def test_bucket_boundaries_settings_and_peak_time(tmp_path):
    path = tmp_path / "noise.sqlite3"
    start = 18_000_000
    a = Mode("FAST", "A", "NORMAL")
    c = Mode()
    with closing(connect(path)) as conn:
        initialize(conn)
        save(conn, Sample(start - 1, 88.0, a))
        save(conn, Sample(start + 2100, 65.5, a))
        save(conn, Sample(start + 1000, 65.5, a))
        save(conn, Sample(start + 3400, 72.0, c))
        save(conn, Sample(start + 3600, 51.0, a))
        save(conn, Sample(start + 3_600_000, 99.0, a))
    for minutes in PERIODS:
        rows = history(path, HistoryRequest(start + 500, start + 4000, minutes), lambda: False).rows
        assert [(r["weight"], r["db"], r["peak_ms"], r["sample_count"]) for r in rows] == [
            ("A", 65.5, start + 1000, 3), ("C", 72.0, start + 3400, 1)
        ]
        assert rows[0]["ts"] == start
        assert rows[0]["end_ms"] == start + minutes * 60_000
    assert history(path, HistoryRequest(start, start + 3600, weight="A"), lambda: False).count == 2


def test_raw_and_rollups_commit_atomically(tmp_path):
    path = tmp_path / "noise.sqlite3"
    with closing(connect(path)) as conn:
        initialize(conn)
        conn.execute("""
            CREATE TRIGGER reject_hour BEFORE INSERT ON noise_data_1h
            BEGIN SELECT RAISE(ABORT, 'write failed'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError):
            save(conn, Sample(1000, 52.0, Mode()))
        for table in ("noise_data_raw", *PERIODS.values()):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_large_query_keeps_peaks_and_paginates_full_data(tmp_path):
    path = tmp_path / "noise.sqlite3"
    total = 6000
    with closing(connect(path)) as conn:
        initialize(conn)
        with conn:
            conn.executemany("""
                INSERT INTO noise_data_raw(time_ms, db, speed, weight, run)
                VALUES (?, ?, 'FAST', 'C', 'NORMAL')
            """, ((i * 1000, 128 if i == 3113 else 60) for i in range(total)))
        first = history(path, HistoryRequest(0, total * 1000), lambda: False)
        second = history(path, HistoryRequest(0, total * 1000, page=1), lambda: False,
                         include_chart=False)
    assert first.count == second.count == total
    assert len(first.chart) <= 2 * CHART_BINS
    assert max(row["db"] for row in first.chart) == 128
    assert first.rows[-1]["ts"] == (PAGE_SIZE - 1) * 1000
    assert second.rows[0]["ts"] == PAGE_SIZE * 1000
    assert second.chart == []


def test_extended_statistics_use_chronological_adjacency_and_interpolated_percentiles(tmp_path):
    path = tmp_path / "noise.sqlite3"
    start = 18_000_000
    with closing(connect(path)) as conn:
        initialize(conn)
        for offset, db in ((-1, 30), (1000, 42), (3000, 81), (2000, 62)):
            save(conn, Sample(start + offset, db, Mode()))
        save(conn, Sample(start + 2500, 100, Mode(weight="A")))
    for period in PERIODS:
        request = HistoryRequest(start, start + 5000, period, metric="max_rise")
        result = history(path, request, lambda: False)
        a, c = result.rows
        assert a["max_rise"] is None
        assert a["p10"] == a["p90"] == 100
        assert c["db"] == 81 and c["min_db"] == 42
        assert c["avg_db"] == pytest.approx(185 / 3)
        assert c["max_rise"] == 20  # 42 → 62 → 81, ordered by timestamp.
        assert c["p10"] == pytest.approx(46)
        assert c["p90"] == pytest.approx(77.2)
        assert c["above_floor"] == pytest.approx(35)
        assert [point["plot_db"] for point in result.chart] == [20]


def test_existing_database_backfills_and_can_resume_saving(tmp_path):
    path = tmp_path / "old.sqlite3"
    with closing(sqlite3.connect(path)) as old:
        old.executescript("""
            CREATE TABLE noise_data_raw (
                id INTEGER PRIMARY KEY, time_ms INTEGER, db REAL, speed TEXT, weight TEXT, run TEXT
            );
            CREATE TABLE noise_data_5min (
                start_ms INTEGER, end_ms INTEGER, max_db REAL, peak_ms INTEGER,
                sample_count INTEGER, speed TEXT, weight TEXT, run TEXT,
                PRIMARY KEY(start_ms, weight, speed, run)
            ) WITHOUT ROWID;
            INSERT INTO noise_data_raw VALUES (1, 1000, 40, 'FAST', 'C', 'NORMAL');
            INSERT INTO noise_data_raw VALUES (2, 2000, 60, 'FAST', 'C', 'NORMAL');
            INSERT INTO noise_data_5min VALUES (0, 300000, 60, 2000, 2, 'FAST', 'C', 'NORMAL');
        """)
    with closing(connect(path)) as conn:
        with pytest.raises(InterruptedError):
            initialize(conn, lambda: True)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
        initialize(conn)
        initialize(conn)
        row = conn.execute("SELECT min_db, avg_db, max_rise, p10, p90 FROM noise_data_5min").fetchone()
        assert tuple(row) == (40, 50, 20, 42, 58)
        conn.execute("""
            CREATE TRIGGER reject_sample BEFORE UPDATE ON noise_data_1h
            BEGIN SELECT RAISE(ABORT, 'write failed'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError):
            save(conn, Sample(3000, 95, Mode()))
        conn.execute("DROP TRIGGER reject_sample")
        save(conn, Sample(4000, 65, Mode()))
        assert conn.execute("SELECT COUNT(*) FROM noise_data_raw").fetchone()[0] == 3
        max_db, count, rise = conn.execute(
            "SELECT max_db, sample_count, max_rise FROM noise_data_5min"
        ).fetchone()
        assert (max_db, count, rise) == (65, 3, 20)
