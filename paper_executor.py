import time
from typing import Callable, Optional

_persist_hook: Optional[Callable[[], None]] = None
_ledger_hook: Optional[Callable[[dict], None]] = None


def set_persist_hooks(on_persist: Callable[[], None], on_ledger: Callable[[dict], None]):
    global _persist_hook, _ledger_hook
    _persist_hook = on_persist
    _ledger_hook = on_ledger


def _after_trade(ledger_extra: Optional[dict] = None):
    if _persist_hook:
        _persist_hook()
    if _ledger_hook and ledger_extra:
        _ledger_hook(ledger_extra)


class PaperExecutor:
    """Simulates order fills in-memory. Drop-in replacement for the real executor."""

    def __init__(self, initial_balance: float = 10_000.0):
        self._balance = initial_balance
        self._initial_balance = initial_balance
        self._trade_log: list[dict] = []

    def _log(
        self,
        action: str,
        ticker: str,
        side: str,
        count: int,
        price: float,
        strategy: str = "",
        title: str = "",
        *,
        payout: float | None = None,
        profit: float | None = None,
        exit_reason: str = "",
        entry_price: float | None = None,
    ):
        entry = {
            "timestamp": time.time(),
            "action": action,
            "ticker": ticker,
            "title": title,
            "strategy": strategy,
            "side": side,
            "count": count,
            "price": price,
            "cost": round(price * count, 4),
            "balance_after": round(self._balance, 2),
        }
        if payout is not None:
            entry["payout"] = round(payout, 2)
        if profit is not None:
            entry["profit"] = round(profit, 2)
        if exit_reason:
            entry["exit_reason"] = exit_reason
        if entry_price is not None:
            entry["entry_price"] = round(entry_price, 4)
        self._trade_log.append(entry)

    def _result(self, success, ticker, side, price, count, error=""):
        return {
            "success": success,
            "ticker": ticker,
            "side": side,
            "price": price,
            "count": count,
            "error": error,
        }

    def buy(
        self,
        ticker: str,
        side: str,
        count: int,
        price: float,
        strategy: str = "",
        title: str = "",
    ) -> dict:
        cost = price * count
        if self._balance < cost:
            return self._result(False, ticker, side, price, count, "Insufficient paper balance")
        self._balance -= cost
        self._log("buy", ticker, side, count, price, strategy=strategy, title=title)
        _after_trade({
            "strategy": strategy,
            "action": "buy",
            "ticker": ticker,
            "side": side,
            "price": price,
            "count": count,
        })
        return self._result(True, ticker, side, price, count)

    def sell(
        self,
        ticker: str,
        side: str,
        count: int,
        price: float,
        strategy: str = "",
        title: str = "",
        profit: float | None = None,
        exit_reason: str = "",
        entry_price: float | None = None,
    ) -> dict:
        self._balance += price * count
        self._log(
            "sell", ticker, side, count, price,
            strategy=strategy, title=title, profit=profit,
            exit_reason=exit_reason, entry_price=entry_price,
        )
        _after_trade({
            "strategy": strategy,
            "action": "sell",
            "ticker": ticker,
            "profit": profit,
        })
        return self._result(True, ticker, side, price, count)

    def lock(
        self,
        ticker: str,
        side: str,
        count: int,
        price: float,
        strategy: str = "",
        title: str = "",
        profit: float | None = None,
        exit_reason: str = "",
        entry_price: float | None = None,
    ) -> dict:
        cost = price * count
        if self._balance < cost:
            return self._result(False, ticker, side, price, count, "Insufficient paper balance")
        self._balance -= cost
        self._balance += float(count)
        self._log(
            "lock", ticker, side, count, price,
            strategy=strategy, title=title, profit=profit,
            exit_reason=exit_reason, entry_price=entry_price,
        )
        _after_trade({
            "strategy": strategy,
            "action": "lock",
            "ticker": ticker,
            "profit": profit,
        })
        return self._result(True, ticker, side, price, count)

    def settle(
        self,
        ticker: str,
        side: str,
        count: int,
        payout: float,
        entry_price: float,
        strategy: str = "",
        title: str = "",
        result: str = "",
    ) -> dict:
        self._balance += payout
        profit = payout - entry_price * count
        effective_price = payout / count if count else 0.0
        self._log(
            "settle",
            ticker,
            side,
            count,
            effective_price,
            strategy=strategy,
            title=title,
            payout=payout,
            profit=profit,
        )
        _after_trade({
            "strategy": strategy,
            "action": "settle",
            "ticker": ticker,
            "profit": profit,
            "result": result,
        })
        return self._result(True, ticker, side, effective_price, count)

    def get_balance(self) -> float:
        return round(self._balance, 2)

    def get_initial_balance(self) -> float:
        return self._initial_balance

    def get_trade_log(self) -> list[dict]:
        return self._trade_log
