"""Tests for ``custom_components.ezviz_hp7.stats``."""

from __future__ import annotations

import logging

from custom_components.ezviz_hp7.stats import ActivityStats


def test_counters_default_to_zero() -> None:
    s = ActivityStats()
    assert s.cloud_logins == 0
    assert s.cloud_polls == 0
    assert s.aes_cache_hits == 0
    assert s.aes_cache_misses == 0
    assert s.lan_sessions_started == 0
    assert s.mjpeg_sessions == 0
    assert s.errors_mjpeg == 0


def test_uptime_is_non_negative() -> None:
    s = ActivityStats()
    assert s.uptime_seconds() >= 0


def test_counters_increment() -> None:
    s = ActivityStats()
    s.cloud_logins += 1
    s.aes_cache_hits += 5
    s.errors_mjpeg += 2
    assert s.cloud_logins == 1
    assert s.aes_cache_hits == 5
    assert s.errors_mjpeg == 2


def test_log_summary_emits_a_line(caplog) -> None:
    """``log_summary`` must produce one INFO-level line — that's the
    contract the periodic background task in ``__init__.py`` relies
    on so an admin can ``grep "EZVIZ HP7 stats"`` on the log."""
    s = ActivityStats()
    s.cloud_polls = 240
    s.aes_cache_hits = 8
    s.lan_session_total_bytes = 1024 * 1024
    s.lan_session_total_seconds = 10.0
    with caplog.at_level(logging.INFO, logger="custom_components.ezviz_hp7.stats"):
        s.log_summary()
    summary_lines = [m for m in caplog.messages if "EZVIZ HP7 stats" in m]
    assert len(summary_lines) == 1
    line = summary_lines[0]
    assert "cloud_polls" in line
    assert "aes_cache_hits" in line
    # Computed fields — the units the dashboard cares about.
    assert "lan_total_MB" in line
    assert "lan_avg_KBps" in line


def test_log_summary_skips_zero_counters(caplog) -> None:
    """Keep the line short by omitting counters that haven't moved."""
    s = ActivityStats()
    s.cloud_polls = 1
    with caplog.at_level(logging.INFO, logger="custom_components.ezviz_hp7.stats"):
        s.log_summary()
    line = next(m for m in caplog.messages if "EZVIZ HP7 stats" in m)
    assert "cloud_polls" in line
    # ``mjpeg_sessions`` defaulted to 0 → must be filtered out.
    assert "'mjpeg_sessions'" not in line
