"""Portfolio P&L uses wallet truth, not summed virtual books."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from models import Market, Position
from pnl import compute_portfolio_pnl, conservative_metrics


def _trader_with_open(name: str, entry: float, count: int):
    pos = Position(
        ticker="KXNBAGAME-T",
        title="Test Game",
        side="yes",
        entry_price=entry,
        count=count,
        sport="nba",
    )
    return SimpleNamespace(
        name=name,
        summary=lambda: {
            "realized_pl": 10.0,
            "settled_pl": 0,
            "traded_pl": 10.0,
        },
        _positions=SimpleNamespace(get_all=lambda: [pos]),
    )


def test_portfolio_net_from_equity():
    kalshi = MagicMock()
    kalshi._cache = [Market(
        ticker="KXNBAGAME-T",
        title="Test",
        sport="nba",
        yes_ask=0.20,
        no_ask=0.82,
        volume=1000,
    )]
    traders = [_trader_with_open("a", 0.30, 100)]
    # cost 30, mkt value 20, unrealized -10; balance 9970 after buy
    pnl = compute_portfolio_pnl(traders, kalshi, 9970.0, 10000.0, trade_log=[])
    assert pnl["unrealized_pl"] == -10.0
    assert pnl["equity"] == 9990.0
    assert pnl["portfolio_net_pl"] == -10.0
    assert pnl["virtual_realized_sum"] == 10.0


def test_conservative_metrics_take_lower_with_haircut():
    out = conservative_metrics(
        wallet_realized_pl=100.0,
        unrealized_pl=-20.0,
        portfolio_net_pl=80.0,
        suspicious_positive_profit=15.0,
    )
    assert out["conservative_wallet_realized_pl"] == 85.0
    assert out["conservative_expected_net_pl"] == 65.0
    assert out["conservative_portfolio_net_pl"] == 65.0
