"""Tests for check_staleness output-name reporting."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from check import check_staleness


def _touch(path, age_seconds=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    if age_seconds:
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
        # Propagate mtime to parent dir so glob on the parent matches by age.
        os.utime(path.parent, (mtime, mtime))


def test_no_matches_returns_none_name(tmp_path):
    is_stale, detail, latest_name = check_staleness(
        str(tmp_path), "DD*", stale_timeout=60
    )
    assert is_stale is False
    assert "no output files" in detail
    assert latest_name is None


def test_returns_latest_basename_when_fresh(tmp_path):
    _touch(tmp_path / "DD0000" / "snap", age_seconds=1000)
    _touch(tmp_path / "DD0001" / "snap", age_seconds=10)
    is_stale, _detail, latest_name = check_staleness(
        str(tmp_path), "DD*", stale_timeout=60
    )
    assert is_stale is False
    assert latest_name == "DD0001"


def test_returns_latest_basename_when_stale(tmp_path):
    _touch(tmp_path / "DD0050" / "snap", age_seconds=60 * 120)  # 2h old
    is_stale, _detail, latest_name = check_staleness(
        str(tmp_path), "DD*", stale_timeout=60
    )
    assert is_stale is True
    assert latest_name == "DD0050"


def test_restart_snapshot_filter_applies_to_latest_name(tmp_path):
    _touch(tmp_path / "DD0000", age_seconds=10)  # newest by mtime but excluded
    _touch(tmp_path / "DD0051", age_seconds=100)
    is_stale, _detail, latest_name = check_staleness(
        str(tmp_path), "DD*", stale_timeout=60, restart_snapshot="DD0050"
    )
    assert is_stale is False
    assert latest_name == "DD0051"
