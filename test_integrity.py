"""Integrity tests for replay, settlement parsing, and pricing marks."""

from pathlib import Path
from types import SimpleNamespace
import asyncio
import sys
import types

if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from kalshi_client import market_side_labels, parse_settlement
from models import Market, Position
from pnl import mark_price_for_position
import simulation_store
from run_server import _resolution_integrity


def test_parse_settlement_falls_back_to_winning_outcome():
    info = parse_settlement({
        "ticker": "KXNBAGAME-TEST",
        "status": "finalized",
        "result": "",
        "winning_outcome": "yes",
    })
    assert info is not None
    assert info.result == "yes"
    assert info.resolution_source == "winning_outcome"


def test_market_side_labels_parses_moneyline_title():
    labels = market_side_labels(
        "New York M vs Miami Winner?",
        "KXMLBGAME-26MAY231610NYMMIA-NYM",
    )
    assert labels["yes_label"] == "New York M"
    assert labels["no_label"] == "Miami"
    assert labels["yes_ticker_code"] == "NYM"


def test_mark_price_prefers_bid_for_liquidation():
    pos = Position(
        ticker="KXNBAGAME-T",
        title="Test",
        side="yes",
        entry_price=0.25,
        count=10,
        sport="nba",
    )
    cache = [Market(
        ticker="KXNBAGAME-T",
        title="Test",
        yes_ask=0.90,
        no_ask=0.11,
        yes_bid=0.60,
        no_bid=0.10,
        sport="nba",
    )]
    assert mark_price_for_position(pos, cache) == 0.60


def test_rebuild_strategy_trades_from_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "trades.jsonl"
    ledger.write_text(
        '{"strategy":"s1","action":"buy","ticker":"T1","timestamp":1}\n'
        '{"strategy":"s1","action":"sell","ticker":"T1","timestamp":2,"profit":1.2}\n',
    )
    monkeypatch.setattr(simulation_store, "LEDGER_PATH", Path(ledger))
    trader = SimpleNamespace(name="s1", _trades=[])
    replayed = simulation_store.rebuild_strategy_trades_from_ledger([trader])
    assert replayed["s1"] == 2
    assert len(trader._trades) == 2
    assert trader._trades[-1]["action"] == "sell"


class _FakeKalshiYes:
    async def get_market_raw(self, ticker):
        return {"status": "finalized", "result": "yes", "ticker": ticker}


def test_resolution_integrity_detects_settle_payout_mismatch(monkeypatch):
    rows = [{
        "strategy": "s1",
        "action": "settle",
        "ticker": "KXNBAGAME-T",
        "side": "yes",
        "result": "yes",
        "count": 10,
        "entry_price": 0.4,
        "payout": 10.0,
        "profit": 1.0,  # wrong, should be 6.0
    }]
    monkeypatch.setattr("run_server.load_ledger_trades", lambda limit: rows)
    out = asyncio.run(_resolution_integrity(_FakeKalshiYes(), limit=50))
    assert len(out["settle_payout_mismatches"]) == 1


def test_lock_not_suspicious_when_resolved_opposite_hedge(monkeypatch):
    # A lock hedges to a guaranteed payout, so the game resolving on the side
    # opposite the hedge is EXPECTED. Regression test for the old side != result
    # heuristic that false-flagged every won game as suspicious (108 of them in
    # run 4, wrongly deducting $1,951 from the conservative P&L).
    rows = [{
        "strategy": "swing_lock_15usd",
        "action": "lock",
        "ticker": "KXMLBGAME-T",
        "side": "no",
        "count": 100,
        "entry_price": 0.30,
        "lock_price": 0.55,
        "profit": 15.0,  # exactly (1 - 0.30 - 0.55) * 100
    }]
    monkeypatch.setattr("run_server.load_ledger_trades", lambda limit: rows)
    out = asyncio.run(_resolution_integrity(_FakeKalshiYes(), limit=50))
    assert out["suspicious_exits"] == []
    assert out["suspicious_positive_profit"] == 0.0
    assert out["lock_profit_mismatches"] == []


def test_lock_flagged_when_profit_exceeds_locked(monkeypatch):
    # The real failure mode for a lock: booking more than the hedge guarantees.
    rows = [{
        "strategy": "swing_lock_15usd",
        "action": "lock",
        "ticker": "KXMLBGAME-T",
        "side": "no",
        "count": 100,
        "entry_price": 0.30,
        "lock_price": 0.55,
        "profit": 40.0,  # booked $40 but the lock only guarantees $15
    }]
    monkeypatch.setattr("run_server.load_ledger_trades", lambda limit: rows)
    out = asyncio.run(_resolution_integrity(_FakeKalshiYes(), limit=50))
    assert len(out["lock_profit_mismatches"]) == 1
    assert out["lock_profit_mismatches"][0]["delta"] == 25.0
