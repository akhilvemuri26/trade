"""Portfolio P&L aggregation for dashboard and CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kalshi_client import KalshiClient
    from virtual_trader import VirtualTrader


def mark_price_for_position(pos, kalshi_cache: list) -> float | None:
    for m in kalshi_cache:
        if m.ticker == pos.ticker:
            if pos.side == "yes":
                if m.yes_bid > 0:
                    return m.yes_bid
                if m.yes_ask > 0:
                    return m.yes_ask
            else:
                if m.no_bid > 0:
                    return m.no_bid
                if m.no_ask > 0:
                    return m.no_ask
            return None
    return None


def position_market_value(pos, current_price: float | None) -> float:
    if current_price is None:
        return 0.0
    return current_price * pos.count


def compute_portfolio_pnl(
    traders: list[VirtualTrader],
    kalshi: KalshiClient,
    balance: float,
    initial_balance: float,
    trade_log: list | None = None,
) -> dict:
    """
    Portfolio metrics for a shared paper wallet running parallel virtual strategies.

    Wallet truth: portfolio_net_pl = equity - initial_balance.
    virtual_realized_sum sums each strategy's closed-book P&L (can exceed wallet
    gains when many strategies exit the same market independently).
    """
    virtual_realized = 0.0
    settled_pl = 0.0
    traded_pl = 0.0
    open_market_value = 0.0
    open_cost_basis = 0.0
    unrealized_pl = 0.0
    marked_positions = 0
    unmarked_positions = 0
    unmarked_cost_basis = 0.0

    for trader in traders:
        s = trader.summary()
        virtual_realized += s["realized_pl"]
        settled_pl += s.get("settled_pl", 0)
        traded_pl += s.get("traded_pl", 0)
        for pos in trader._positions.get_all():
            cost = pos.entry_price * pos.count
            open_cost_basis += cost
            cp = mark_price_for_position(pos, kalshi._cache)
            mv = position_market_value(pos, cp)
            open_market_value += mv
            if cp is not None:
                marked_positions += 1
                unrealized_pl += (cp - pos.entry_price) * pos.count
            else:
                unmarked_positions += 1
                unmarked_cost_basis += cost

    wallet_closed_pl = 0.0
    if trade_log:
        wallet_closed_pl = sum(
            t.get("profit", 0) for t in trade_log if t.get("profit") is not None
        )

    virtual_realized = round(virtual_realized, 2)
    settled_pl = round(settled_pl, 2)
    traded_pl = round(traded_pl, 2)
    unrealized_pl = round(unrealized_pl, 2)
    open_cost_basis = round(open_cost_basis, 2)
    open_market_value = round(open_market_value, 2)
    wallet_closed_pl = round(wallet_closed_pl, 2)
    equity = round(balance + open_market_value, 2)
    portfolio_net_pl = round(equity - initial_balance, 2)
    expected_net_pl = round(wallet_closed_pl + unrealized_pl - unmarked_cost_basis, 2)
    reconciliation_delta = round(portfolio_net_pl - expected_net_pl, 2)

    return {
        # Wallet (source of truth for the shared $10k account)
        "portfolio_net_pl": portfolio_net_pl,
        "equity": equity,
        "wallet_realized_pl": wallet_closed_pl,
        "unrealized_pl": unrealized_pl,
        "open_market_value": open_market_value,
        "open_cost_basis": open_cost_basis,
        "cash_deployed": round(initial_balance - balance, 2),
        "expected_net_pl": expected_net_pl,
        "reconciliation_delta": reconciliation_delta,
        "marked_positions": marked_positions,
        "unmarked_positions": unmarked_positions,
        "unmarked_cost_basis": round(unmarked_cost_basis, 2),
        # Virtual strategy books (for A/B comparison — not additive to wallet)
        "virtual_realized_sum": virtual_realized,
        "settled_pl": settled_pl,
        "traded_pl": traded_pl,
        # Backward-compatible aliases
        "realized_pl": wallet_closed_pl,
        "net_pl": portfolio_net_pl,
        "total_pl": wallet_closed_pl,
    }


def conservative_metrics(
    *,
    wallet_realized_pl: float,
    unrealized_pl: float,
    portfolio_net_pl: float,
    suspicious_positive_profit: float = 0.0,
) -> dict:
    """
    Apply conservative floors: when uncertain/discrepant, choose lower outcome.
    """
    suspicious = max(0.0, float(suspicious_positive_profit or 0.0))
    conservative_wallet = round(wallet_realized_pl - suspicious, 2)
    conservative_expected = round(conservative_wallet + unrealized_pl, 2)
    conservative_portfolio = round(min(portfolio_net_pl, conservative_expected), 2)
    conservative_delta = round(conservative_portfolio - conservative_expected, 2)
    return {
        "suspicious_positive_profit": round(suspicious, 2),
        "conservative_wallet_realized_pl": conservative_wallet,
        "conservative_expected_net_pl": conservative_expected,
        "conservative_portfolio_net_pl": conservative_portfolio,
        "conservative_reconciliation_delta": conservative_delta,
    }
