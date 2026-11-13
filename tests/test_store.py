import time

import pytest

from store import Store


def test_path_escape_rejected(tmp_path):
    s = Store(root=tmp_path / "d", min_free_mb=0)
    with pytest.raises(ValueError):
        s._path("..", "evil.db")


def test_init_and_settle_idempotent(tmp_path):
    s = Store(root=tmp_path / "d", min_free_mb=0)
    s.init()
    try:
        assert s.settle("2026-11-13", "daily") is True
        assert s.settle("2026-11-13", "daily") is False
        assert s.stats()["settlements"] == 1
    finally:
        s.close()


def test_event_cap_drops_lowest(tmp_path):
    s = Store(root=tmp_path / "d", min_free_mb=0)
    s.init()
    try:
        for weight in (5.0, 1.0):
            s.add_event("2026-11-13", "gossip", weight=weight, max_per_day=2)
        s.add_event("2026-11-13", "gossip", weight=9.0, max_per_day=2)
        assert s.stats()["events"] == 2
        rows = s._query("SELECT weight FROM events ORDER BY weight")
        assert [r[0] for r in rows] == [5.0, 9.0]
    finally:
        s.close()


def test_trim_events(tmp_path):
    s = Store(root=tmp_path / "d", min_free_mb=0)
    s.init()
    try:
        s.add_event("old", "k", payload="{}")
        s._write("UPDATE events SET created_at = ?", (time.time() - 100 * 86400,))
        assert s.trim_events(keep_days=90) == 1
        assert s.stats()["events"] == 0
    finally:
        s.close()
