"""Tests for trade normalization and filtering/sorting helpers."""

import sys
import types

if "aiohttp" not in sys.modules:
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.web = types.SimpleNamespace()
    sys.modules["aiohttp"] = aiohttp_stub

from run_server import _apply_trade_filters_and_sort, _normalize_trades


def test_normalize_trades_adds_stable_fields():
    rows = [{
        "action": "sell",
        "ticker": "KXNBAGAME-TEST-AAA",
        "title": "Alpha vs Beta Winner?",
        "side": "yes",
        "strategy": "swing_sell_5pct",
        "entry_price": 0.2,
        "exit_price": 0.3,
        "profit": 10.0,
        "count": 100,
        "timestamp": 1000,
    }]
    out = _normalize_trades(rows)[0]
    assert out["selected_team"] == "Alpha"
    assert out["entry_price_norm"] == 0.2
    assert out["exit_price_norm"] == 0.3
    assert out["display_price"] == 0.3
    assert out["display_profit"] == 10.0
    assert out["trade_date"] is not None


def test_apply_trade_filters_and_sort_filters_and_orders():
    rows = _normalize_trades([
        {
            "action": "sell",
            "ticker": "A",
            "title": "A vs B Winner?",
            "side": "yes",
            "strategy": "s1",
            "entry_price": 0.2,
            "exit_price": 0.4,
            "profit": 20.0,
            "timestamp": 2000,
            "anomaly_flag": False,
        },
        {
            "action": "lock",
            "ticker": "B",
            "title": "C vs D Winner?",
            "side": "no",
            "strategy": "s2",
            "entry_price": 0.3,
            "lock_price": 0.5,
            "profit": -5.0,
            "timestamp": 1000,
            "anomaly_flag": True,
        },
    ])
    filtered = _apply_trade_filters_and_sort(rows, {
        "strategy": "s2",
        "sort": "profit",
        "order": "asc",
        "anomalies_only": "1",
    })
    assert len(filtered) == 1
    assert filtered[0]["strategy"] == "s2"
    assert filtered[0]["anomaly_flag"] is True
