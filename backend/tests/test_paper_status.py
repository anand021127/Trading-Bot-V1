"""Tests for compute_paper_status — real trade-log-derived readiness (item #7)."""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest

from backend.paper.status_calculator import compute_paper_status, pair_trades, read_trade_events


@pytest.fixture()
def temp_log_dir(monkeypatch):
    d = Path(tempfile.gettempdir()) / f"test_logs_{uuid.uuid4().hex}"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LOGS_DIR", str(d))
    yield d


def _write_events(log_dir: Path, events: list) -> None:
    path = log_dir / "trades.log"
    with open(path, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")


class TestReadAndPair:
    def test_no_log_file_returns_empty(self, temp_log_dir) -> None:
        assert read_trade_events() == []

    def test_pairs_entry_and_exit_by_trade_id(self, temp_log_dir) -> None:
        _write_events(temp_log_dir, [
            {"event": "ENTRY", "trade_id": "t1", "symbol": "HDFCBANK",
             "mode": "paper", "ts": "2026-01-05T09:30:00", "trend_bias": "BULLISH"},
            {"event": "EXIT", "trade_id": "t1", "net_pnl": 500.0,
             "exit_reason": "TARGET_HIT", "mode": "paper", "ts": "2026-01-05T10:00:00"},
        ])
        trades = pair_trades(read_trade_events(mode_filter="paper"))
        assert len(trades) == 1
        assert trades[0]["is_closed"] is True
        assert trades[0]["net_pnl"] == 500.0

    def test_entry_without_exit_is_still_open(self, temp_log_dir) -> None:
        _write_events(temp_log_dir, [
            {"event": "ENTRY", "trade_id": "t2", "symbol": "TCS",
             "mode": "paper", "ts": "2026-01-05T09:30:00"},
        ])
        trades = pair_trades(read_trade_events(mode_filter="paper"))
        assert trades[0]["is_closed"] is False
        assert trades[0]["net_pnl"] is None

    def test_mode_filter_excludes_live_trades(self, temp_log_dir) -> None:
        _write_events(temp_log_dir, [
            {"event": "ENTRY", "trade_id": "t3", "symbol": "TCS",
             "mode": "live", "ts": "2026-01-05T09:30:00"},
        ])
        assert read_trade_events(mode_filter="paper") == []

    def test_malformed_line_is_skipped_not_fatal(self, temp_log_dir) -> None:
        path = temp_log_dir / "trades.log"
        with open(path, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({"event": "ENTRY", "trade_id": "t4", "mode": "paper", "ts": "2026-01-01"}) + "\n")
        events = read_trade_events(mode_filter="paper")
        assert len(events) == 1


class TestComputePaperStatus:
    def test_no_trades_yet_gives_honest_zero_state(self, temp_log_dir) -> None:
        status = compute_paper_status()
        assert status["days_active"] == 0
        assert status["is_ready"] is False
        assert status["data_source"] == "no_trades_yet"
        assert status["checklist"]["win_rate_ok"]["pass"] is False

    def test_winning_trades_compute_real_metrics(self, temp_log_dir) -> None:
        events = []
        for i in range(5):
            events.append({"event": "ENTRY", "trade_id": f"t{i}", "symbol": "HDFCBANK",
                            "mode": "paper", "ts": f"2026-01-{i+1:02d}T09:30:00", "trend_bias": "BULLISH"})
            events.append({"event": "EXIT", "trade_id": f"t{i}", "net_pnl": 500.0,
                            "exit_reason": "TARGET_HIT", "mode": "paper", "ts": f"2026-01-{i+1:02d}T10:00:00"})
        _write_events(temp_log_dir, events)

        status = compute_paper_status()
        assert status["days_active"] == 5
        assert status["checklist"]["win_rate_ok"]["value"] == 100.0
        assert status["checklist"]["win_rate_ok"]["pass"] is True
        assert status["data_source"] == "real_trade_log"
        assert len(status["daily_history"]) == 5

    def test_mixed_wins_and_losses_gives_realistic_win_rate(self, temp_log_dir) -> None:
        events = [
            {"event": "ENTRY", "trade_id": "w1", "symbol": "A", "mode": "paper", "ts": "2026-01-01T09:30:00"},
            {"event": "EXIT", "trade_id": "w1", "net_pnl": 300.0, "mode": "paper", "ts": "2026-01-01T10:00:00"},
            {"event": "ENTRY", "trade_id": "l1", "symbol": "B", "mode": "paper", "ts": "2026-01-01T11:00:00"},
            {"event": "EXIT", "trade_id": "l1", "net_pnl": -200.0, "mode": "paper", "ts": "2026-01-01T11:30:00"},
        ]
        _write_events(temp_log_dir, events)
        status = compute_paper_status()
        assert status["checklist"]["win_rate_ok"]["value"] == 50.0
        assert status["closed_trades"] == 2

    def test_never_fabricates_orb_or_choppiness_filter_checks(self, temp_log_dir) -> None:
        """These checks require data we don't have from the trade log alone
        — must stay honestly False/None, not a fake green check."""
        status = compute_paper_status()
        assert status["checklist"]["orb_filter_ok"]["value"] is None
        assert status["checklist"]["orb_filter_ok"]["pass"] is False
