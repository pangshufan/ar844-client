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
