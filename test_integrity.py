"""Integrity tests for replay, settlement parsing, and pricing marks."""

from pathlib import Path
from types import SimpleNamespace
import pytest
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


@pytest.mark.asyncio
async def test_resolution_integrity_detects_settle_payout_mismatch(monkeypatch):
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

    class FakeKalshi:
        async def get_market_raw(self, ticker):
            return {"status": "finalized", "result": "yes", "ticker": ticker}

    out = await _resolution_integrity(FakeKalshi(), limit=50)
    assert len(out["settle_payout_mismatches"]) == 1
