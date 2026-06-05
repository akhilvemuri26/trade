import asyncio
import datetime
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import httpx
from rapidfuzz import process as fuzz_process

from kalshi_client import (
    KalshiClient,
    market_side_labels,
    parse_settlement,
    settlement_payout_dollars,
)
from models import Market, Opportunity, OpportunityType, OrderResult, Position, RunEvent, ScoreEvent, Sport
from positions import PositionStore
from strategies.exit_rules import (
    choose_exit,
    compute_lock_profit,
    compute_sell_profit,
    entry_cost,
)
from strategy_config import MAX_HOURS_TO_RESOLUTION, MAX_TOTAL_RISK
from paths import LOGS_DIR, POSITIONS_DIR, RESULTS_DIR, ensure_data_dirs
from simulation_store import record_trade

logger = logging.getLogger(__name__)

EXECUTOR_URL = os.getenv("EXECUTOR_URL", "http://localhost:8100")
FUZZY_THRESHOLD = 70


async def _call_executor(endpoint: str, payload: dict, paper=None) -> OrderResult:
    """Route order to in-process paper executor or HTTP executor service."""
    ticker = payload.get("ticker", "")
    side = payload.get("side", "yes")
    count = payload.get("count", 0)
    price = payload.get("price", 0)
    strategy = payload.get("strategy", "")
    title = payload.get("title", "")
    profit = payload.get("profit")
    exit_reason = payload.get("exit_reason", "")
    entry_price = payload.get("entry_price")

    if paper is not None:
        try:
            if endpoint == "/order/buy":
                data = paper.buy(ticker, side, count, price, strategy=strategy, title=title)
            elif endpoint == "/order/sell":
                data = paper.sell(
                    ticker, side, count, price,
                    strategy=strategy, title=title, profit=profit,
                    exit_reason=exit_reason, entry_price=entry_price,
                )
            elif endpoint == "/order/lock":
                data = paper.lock(
                    ticker, side, count, price,
                    strategy=strategy, title=title, profit=profit,
                    exit_reason=exit_reason, entry_price=entry_price,
                )
            else:
                return OrderResult(
                    success=False, ticker=ticker, side="", price=0, count=0,
                    error=f"Unknown endpoint {endpoint}",
                )
            return OrderResult(**data)
        except Exception as e:
            return OrderResult(
                success=False, ticker=ticker, side="", price=0, count=0, error=str(e),
            )

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{EXECUTOR_URL}{endpoint}", json=payload)
            resp.raise_for_status()
            return OrderResult(**resp.json())
    except Exception as e:
        return OrderResult(
            success=False, ticker=ticker, side="", price=0, count=0, error=str(e),
        )


def _find_market(markets: list[Market], team: str, sport: str) -> Optional[Market]:
    candidates = [m for m in markets if m.sport == sport]
    if not candidates:
        return None
    titles = [m.title for m in candidates]
    result = fuzz_process.extractOne(team, titles, score_cutoff=FUZZY_THRESHOLD)
    if not result:
        return None
    _, _, idx = result
    return candidates[idx]


class VirtualTrader:
    """
    Autonomous trader for one strategy config.
    Auto-enters on underdog signals, auto-exits on runs.
    Logs every event extensively to a per-strategy log file.
    """

    def __init__(self, config: dict, kalshi: KalshiClient, paper=None):
        self.name = config["name"]
        self._config = config
        self._kalshi = kalshi
        self._paper = paper
        ensure_data_dirs()
        self._positions = PositionStore(POSITIONS_DIR / f"{self.name}.json")
        self._trades: list[dict] = []
        self._events_log: list[dict] = []
        self._start_time = time.time()
        self._scan_count = 0
        self._score_events_seen = 0
        self._runs_detected = 0
        self._entry_attempts = 0
        self._exit_attempts = 0

        self._max_risk = config.get("max_risk", 50)
        self._max_positions = config.get("max_positions", 3)
        self._underdog_max = config.get("underdog_max_price", 0.45)
        # Phase 6 entry filters + re-entry guard (run 6).
        self._min_entry_price = config.get("min_entry_price", 0.0)
        self._min_hours_to_resolution = config.get("min_hours_to_resolution", 0.0)
        self._one_entry_per_game = config.get("one_entry_per_game", False)
        self._entered_tickers: set[str] = set()
        # Absolute live scores accumulated from the ESPN feed (process_score),
        # used for best-effort in-game features at entry (Track B edge model).
        self._current_score: dict[str, int] = {}
        self._last_score_ts: dict[str, float] = {}
        self._sports = set(config.get("sports", []))
        self._enter_all_markets = config.get("enter_all_markets", False)
        self._ignore_global_risk_cap = config.get("ignore_global_risk_cap", False)
        self._use_run_exits = config.get("use_run_exits", True)
        self._exit_mode = config.get("exit_mode", "hybrid")

        # ML entry gate (Run 4). When enabled, skip candidates whose predicted
        # P(profitable swing) is below model_min_prob. See ml/predict.py.
        self._use_model_gate = config.get("use_model_gate", False)
        self._model_min_prob = config.get("model_min_prob", 0.0)
        self._model_path = config.get("model_path")
        self._model_skips = 0
        self._trade_lock = asyncio.Lock()
        self._closing_tickers: set[str] = set()

        # Per-trader run thresholds (NOT shared with other traders)
        self._run_pts = config.get("run_pts", 8)
        self._run_opp = config.get("run_opp", 2)
        self._run_window = config.get("run_window", 180)
        self._run_cooldown = config.get("run_cooldown", 120)

        # Per-trader run tracking state
        self._score_history: dict[str, list[tuple[float, int]]] = {}
        self._last_run_ts: dict[str, float] = {}

        # Set up per-strategy file logger
        ensure_data_dirs()
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self._log_path = LOGS_DIR / f"{self.name}_{ts}.log"
        self._file_logger = logging.getLogger(f"trader.{self.name}")
        self._file_logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler(self._log_path)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        self._file_logger.addHandler(fh)

        # Log the full config at startup
        self._file_logger.info("=" * 60)
        self._file_logger.info("STRATEGY: %s", self.name)
        self._file_logger.info("CONFIG: %s", json.dumps(config, indent=2))
        self._file_logger.info("=" * 60)

    def _log_event(self, event_type: str, data: dict):
        entry = {
            "timestamp": time.time(),
            "iso": datetime.datetime.now().isoformat(),
            "strategy": self.name,
            "event": event_type,
            **data,
        }
        self._events_log.append(entry)
        self._file_logger.info("[%s] %s", event_type, json.dumps(data, default=str))

    def _side_context(self, title: str, ticker: str, side: str = "yes") -> dict:
        labels = market_side_labels(title, ticker)
        selected = labels["yes_label"] if side == "yes" else labels["no_label"]
        return {
            "yes_label": labels["yes_label"],
            "no_label": labels["no_label"],
            "yes_ticker_code": labels["yes_ticker_code"],
            "selected_team": selected,
            "selected_side": side,
        }

    def rehydrate_entered_tickers(self) -> None:
        """Rebuild the re-entry guard set from replayed ledger trades after a
        restart/resume, so run 6 doesn't re-enter games already traded this run."""
        self._entered_tickers = {
            t.get("ticker") for t in self._trades
            if t.get("action") == "buy" and t.get("ticker")
        }

    def _ingame_state(self, market: Market, side_ctx: dict) -> dict:
        """Best-effort live score snapshot for the market's teams at entry.

        Uses scores accumulated from the ESPN feed (process_score), fuzzy-matching
        the market's team labels to feed team names. Never raises -- on any miss it
        returns nulls so entry logging/execution is unaffected. Feeds Track B.
        """
        state = {
            "entry_score_diff": None,
            "entry_team_score": None,
            "entry_opp_score": None,
            "entry_is_live": False,
            "entry_seconds_since_score": None,
        }
        try:
            prefix = f"{(market.sport or '').lower()}:"
            keyed = {
                k[len(prefix):]: v for k, v in self._current_score.items()
                if k.startswith(prefix)
            }
            if not keyed:
                return state
            names = list(keyed.keys())

            def _match(label):
                if not label:
                    return None
                r = fuzz_process.extractOne(label, names, score_cutoff=FUZZY_THRESHOLD)
                return r[0] if r else None

            team = _match(side_ctx.get("selected_team") or side_ctx.get("yes_label"))
            opp = _match(side_ctx.get("no_label"))
            if team is not None:
                state["entry_team_score"] = keyed[team]
                state["entry_is_live"] = True
                lt = self._last_score_ts.get(prefix + team)
                if lt:
                    state["entry_seconds_since_score"] = round(time.time() - lt)
            if opp is not None:
                state["entry_opp_score"] = keyed[opp]
            if team is not None and opp is not None:
                state["entry_score_diff"] = keyed[team] - keyed[opp]
        except Exception:
            pass
        return state

    def _detect_run(self, event: ScoreEvent) -> Optional[dict]:
        """Per-trader run detection with isolated thresholds."""
        key = f"{event.sport.value}:{event.team}"
        opp_key = f"{event.sport.value}:{event.opponent}"
        now = event.timestamp

        if key not in self._score_history:
            self._score_history[key] = []
        if opp_key not in self._score_history:
            self._score_history[opp_key] = []

        self._score_history[key].append((now, event.delta))

        # Prune outside window
        self._score_history[key] = [
            (ts, pts) for ts, pts in self._score_history[key]
            if now - ts <= self._run_window
        ]
        self._score_history[opp_key] = [
            (ts, pts) for ts, pts in self._score_history[opp_key]
            if now - ts <= self._run_window
        ]

        team_scored = sum(pts for _, pts in self._score_history[key])
        opp_scored = sum(pts for _, pts in self._score_history[opp_key])

        if team_scored >= self._run_pts and opp_scored <= self._run_opp:
            last = self._last_run_ts.get(key, 0)
            if now - last < self._run_cooldown:
                return None
            self._last_run_ts[key] = now
            earliest = min(ts for ts, _ in self._score_history[key])
            return {
                "team": event.team,
                "opponent": event.opponent,
                "sport": event.sport.value,
                "points_scored": team_scored,
                "opponent_scored": opp_scored,
                "window_seconds": now - earliest,
            }
        return None

    def current_risk(self) -> float:
        """Total dollars at risk across all open positions for this strategy."""
        return sum(p.entry_price * p.count for p in self._positions.get_all())

    async def check_entries(self, markets: list[Market], all_traders: list = None):
        self._scan_count += 1
        candidates_found = 0

        open_count = len(self._positions.get_all())
        if open_count >= self._max_positions:
            self._log_event("SCAN_COMPLETE", {
                "scan_number": self._scan_count,
                "markets_in_scope": 0,
                "open_positions": open_count,
                "skipped_reason": "max_positions reached",
            })
            return

        min_volume = self._config.get("min_volume", 0)

        for market in markets:
            if market.sport not in self._sports:
                continue
            if market.volume < min_volume:
                continue
            if self._positions.has_position(market.ticker):
                continue
            if self._one_entry_per_game and market.ticker in self._entered_tickers:
                # Re-entry guard (Phase 6): one position per game per strategy.
                # Kills the stop -> re-buy -> stop churn that lost ~$5k in run 5.
                continue
            if market.yes_ask < self._min_entry_price:
                continue
            if market.yes_ask > self._underdog_max or market.yes_ask <= 0:
                continue
            if market.resolves_at is None:
                continue
            hours_to_resolve = (market.resolves_at - time.time()) / 3600
            if hours_to_resolve <= 0 or hours_to_resolve > MAX_HOURS_TO_RESOLUTION:
                continue
            if hours_to_resolve < self._min_hours_to_resolution:
                # Avoid near-decided games (Phase 6: <3h entries were ~-49% ROI).
                continue

            model_prob = None
            if self._use_model_gate:
                try:
                    from ml.predict import score as _model_score
                    # predict_proba is CPU-bound and blocks the event loop;
                    # run it in a thread so the dashboard/health server stays
                    # responsive during scans.
                    model_prob = await asyncio.to_thread(
                        _model_score, market, self._config, self._model_path
                    )
                except Exception as exc:
                    self._file_logger.warning(f"model gate scoring failed: {exc}")
                    model_prob = None
                if model_prob is not None and model_prob < self._model_min_prob:
                    self._model_skips += 1
                    self._log_event("ENTRY_SKIPPED_MODEL", {
                        "ticker": market.ticker,
                        "model_prob": round(model_prob, 4),
                        "model_min_prob": self._model_min_prob,
                    })
                    continue

            candidates_found += 1

            if not self._enter_all_markets:
                if len(self._positions.get_all()) >= self._max_positions:
                    break

            count = int(self._max_risk / market.yes_ask)
            if count < 1:
                continue

            trade_cost = market.yes_ask * count
            global_risk = sum(t.current_risk() for t in all_traders) if all_traders else self.current_risk()
            if not self._ignore_global_risk_cap and global_risk + trade_cost > MAX_TOTAL_RISK:
                self._log_event("ENTRY_BLOCKED_GLOBAL_RISK", {
                    "ticker": market.ticker,
                    "trade_cost": round(trade_cost, 2),
                    "global_risk": round(global_risk, 2),
                    "limit": MAX_TOTAL_RISK,
                })
                continue

            self._entry_attempts += 1
            self._log_event("ENTRY_SIGNAL", {
                "ticker": market.ticker,
                "title": market.title,
                "sport": market.sport,
                "volume": market.volume,
                "yes_ask": market.yes_ask,
                "no_ask": market.no_ask,
                "hours_to_resolution": round(hours_to_resolve, 2),
                "resolves_at": datetime.datetime.fromtimestamp(
                    market.resolves_at, tz=datetime.timezone.utc,
                ).isoformat(),
                "suggested_count": count,
                "suggested_risk": round(market.yes_ask * count, 2),
                "model_prob": round(model_prob, 4) if model_prob is not None else None,
                "config": {
                    "underdog_max_price": self._underdog_max,
                    "max_risk": self._max_risk,
                    "min_volume": min_volume,
                    "max_hours_to_resolution": MAX_HOURS_TO_RESOLUTION,
                    "use_model_gate": self._use_model_gate,
                    "model_min_prob": self._model_min_prob,
                },
            })

            result = await _call_executor("/order/buy", {
                "ticker": market.ticker,
                "side": "yes",
                "count": count,
                "price": market.yes_ask,
                "strategy": self.name,
                "title": market.title,
            }, paper=self._paper)

            if result.success:
                self._positions.add(Position(
                    ticker=result.ticker,
                    title=market.title,
                    side="yes",
                    entry_price=result.price,
                    count=result.count,
                    sport=market.sport or "",
                ))
                now_ts = time.time()
                hours_to_resolve = (
                    (market.resolves_at - now_ts) / 3600
                    if market.resolves_at else None
                )
                trade = {
                    "action": "buy",
                    "ticker": result.ticker,
                    "title": market.title,
                    "sport": market.sport,
                    "side": "yes",
                    "price": result.price,
                    "count": result.count,
                    "cost": round(result.price * result.count, 2),
                    "timestamp": now_ts,
                    # Entry-time market state, persisted for later entry-quality
                    # analysis (are we overpaying into wide/thin markets or
                    # entering near-decided games?). Live score/clock is not on
                    # this code path -- it would need the team-keyed score feed
                    # bridged to the market ticker (tracked as a follow-up).
                    "entry_yes_ask": round(market.yes_ask, 4),
                    "entry_no_ask": round(market.no_ask, 4),
                    "entry_yes_bid": round(market.yes_bid, 4),
                    "entry_no_bid": round(market.no_bid, 4),
                    "entry_spread": round(market.yes_ask - market.yes_bid, 4),
                    "entry_market_width": round(market.yes_ask + market.no_ask - 1.0, 4),
                    "entry_volume": float(market.volume or 0.0),
                    "resolves_at": market.resolves_at,
                    "hours_to_resolution": (
                        round(hours_to_resolve, 3) if hours_to_resolve is not None else None
                    ),
                }
                side_ctx = self._side_context(market.title, result.ticker, "yes")
                trade.update(side_ctx)
                trade.update(self._ingame_state(market, side_ctx))
                self._entered_tickers.add(result.ticker)
                self._trades.append(trade)
                self._log_event("TRADE_EXECUTED", trade)
                record_trade(
                    strategy=self.name,
                    action="buy",
                    ticker=result.ticker,
                    exit_mode=self._exit_mode,
                    extra=trade,
                )
            else:
                self._log_event("TRADE_FAILED", {
                    "ticker": market.ticker,
                    "error": result.error,
                })

        self._log_event("SCAN_COMPLETE", {
            "scan_number": self._scan_count,
            "markets_in_scope": candidates_found,
            "open_positions": len(self._positions.get_all()),
        })

    async def check_exits(
        self,
        markets_by_ticker: dict[str, Market],
        kalshi: KalshiClient,
    ):
        """Price-swing exits on open positions (poll Kalshi quotes)."""
        for position in list(self._positions.get_all()):
            market = markets_by_ticker.get(position.ticker)
            if market is None:
                market = await kalshi.get_market(position.ticker)
            if not market:
                continue

            current_yes = market.yes_ask
            current_yes_bid = market.yes_bid
            if current_yes_bid <= 0:
                continue
            current_no = market.no_ask
            sell_profit = compute_sell_profit(position, current_yes_bid)
            lock_profit = compute_lock_profit(position, current_no)
            cost = entry_cost(position)
            max_gain = (1.0 - position.entry_price) * position.count
            hold_seconds = time.time() - position.timestamp

            action, exit_reason = choose_exit(
                self._config, sell_profit, lock_profit, cost, max_gain=max_gain,
            )

            self._log_event("EXIT_EVALUATION", {
                "ticker": position.ticker,
                "entry_price": position.entry_price,
                "current_yes": current_yes,
                "current_yes_bid": current_yes_bid,
                "current_no": current_no,
                "sell_profit": round(sell_profit, 2),
                "lock_profit": round(lock_profit, 2),
                "exit_mode": self._exit_mode,
                "chosen_action": action or "hold",
                "exit_reason": exit_reason,
                "hold_seconds": round(hold_seconds, 1),
            })

            if not action:
                continue

            self._exit_attempts += 1
            await self._execute_exit(
                position, action, current_yes_bid, current_no,
                hold_seconds, exit_reason, trigger="price_swing",
            )

    async def _execute_exit(
        self,
        position: Position,
        action: str,
        current_yes_bid: float,
        current_no: float,
        hold_seconds: float,
        exit_reason: str,
        trigger: str = "price_swing",
        run: Optional[dict] = None,
    ):
        async with self._trade_lock:
            if position.ticker in self._closing_tickers:
                self._log_event("EXIT_SKIP_ALREADY_CLOSING", {"ticker": position.ticker})
                return
            # Position may already be closed by another scanner.
            if not self._positions.has_position(position.ticker):
                self._log_event("EXIT_SKIP_NOT_OPEN", {"ticker": position.ticker})
                return
            self._closing_tickers.add(position.ticker)
        try:
            await self._execute_exit_unlocked(
                position, action, current_yes_bid, current_no,
                hold_seconds, exit_reason, trigger=trigger, run=run,
            )
        finally:
            async with self._trade_lock:
                self._closing_tickers.discard(position.ticker)

    async def _execute_exit_unlocked(
        self,
        position: Position,
        action: str,
        current_yes_bid: float,
        current_no: float,
        hold_seconds: float,
        exit_reason: str,
        trigger: str = "price_swing",
        run: Optional[dict] = None,
    ):
        if action == "sell":
            actual_profit = (current_yes_bid - position.entry_price) * position.count
            result = await _call_executor("/order/sell", {
                "ticker": position.ticker,
                "side": position.side,
                "count": position.count,
                "price": current_yes_bid,
                "strategy": self.name,
                "title": position.title,
                "profit": round(actual_profit, 2),
                "exit_reason": exit_reason,
                "entry_price": position.entry_price,
            }, paper=self._paper)
            if not result.success:
                self._log_event("EXIT_FAILED", {"error": result.error})
                return
            actual_profit = (result.price - position.entry_price) * result.count
            self._positions.remove(position.ticker)
            trade = {
                "action": "sell",
                "ticker": position.ticker,
                "title": position.title,
                "sport": position.sport,
                "side": position.side,
                "entry_price": position.entry_price,
                "exit_price": result.price,
                "count": result.count,
                "profit": round(actual_profit, 2),
                "hold_time_seconds": round(hold_seconds, 1),
                "exit_type": "sell",
                "exit_reason": exit_reason,
                "trigger": trigger,
                "timestamp": time.time(),
            }
            trade.update(self._side_context(position.title, position.ticker, position.side))
            if run:
                trade["run_that_triggered"] = run
            self._trades.append(trade)
            self._log_event("EXIT_EXECUTED", trade)
            record_trade(
                strategy=self.name,
                action="sell",
                ticker=position.ticker,
                exit_mode=self._exit_mode,
                exit_reason=exit_reason,
                profit=actual_profit,
                extra=trade,
            )
        elif action == "lock":
            locked_profit = (1.0 - position.entry_price - current_no) * position.count
            result = await _call_executor("/order/lock", {
                "ticker": position.ticker,
                "side": "no",
                "count": position.count,
                "price": current_no,
                "strategy": self.name,
                "title": position.title,
                "profit": round(locked_profit, 2),
                "exit_reason": exit_reason,
                "entry_price": position.entry_price,
            }, paper=self._paper)
            if not result.success:
                self._log_event("EXIT_FAILED", {"error": result.error})
                return
            locked_profit = (1.0 - position.entry_price - result.price) * position.count
            self._positions.remove(position.ticker)
            trade = {
                "action": "lock",
                "ticker": position.ticker,
                "title": position.title,
                "sport": position.sport,
                "side": "no",
                "entry_price": position.entry_price,
                "lock_price": result.price,
                "count": result.count,
                "profit": round(locked_profit, 2),
                "hold_time_seconds": round(hold_seconds, 1),
                "exit_type": "lock",
                "exit_reason": exit_reason,
                "trigger": trigger,
                "timestamp": time.time(),
            }
            trade.update(self._side_context(position.title, position.ticker, "no"))
            if run:
                trade["run_that_triggered"] = run
            self._trades.append(trade)
            self._log_event("EXIT_EXECUTED", trade)
            record_trade(
                strategy=self.name,
                action="lock",
                ticker=position.ticker,
                exit_mode=self._exit_mode,
                exit_reason=exit_reason,
                profit=locked_profit,
                extra=trade,
            )

    async def process_score(self, event: ScoreEvent, kalshi: KalshiClient):
        if event.sport.value not in self._sports:
            return

        self._score_events_seen += 1
        # Track absolute current scores for entry-time in-game features (Track B).
        skey = f"{event.sport.value}:{event.team}"
        self._current_score[skey] = event.new_score
        self._last_score_ts[skey] = event.timestamp
        self._log_event("SCORE_UPDATE", {
            "sport": event.sport.value,
            "team": event.team,
            "opponent": event.opponent,
            "old_score": event.old_score,
            "new_score": event.new_score,
            "delta": event.delta,
        })

        run = self._detect_run(event)
        if not run:
            return

        if not self._use_run_exits:
            return

        self._runs_detected += 1
        self._log_event("RUN_DETECTED", {
            **run,
            "config": {
                "run_pts": self._run_pts,
                "run_opp": self._run_opp,
                "run_window": self._run_window,
            },
        })

        markets = await kalshi.get_sports_markets()
        market = _find_market(markets, run["team"], run["sport"])
        if not market:
            self._log_event("RUN_NO_MARKET", {"team": run["team"], "sport": run["sport"]})
            return

        refreshed = await kalshi.get_market(market.ticker)
        if not refreshed:
            return

        position = self._positions.get(refreshed.ticker)
        if not position:
            self._log_event("RUN_NO_POSITION", {"ticker": refreshed.ticker})
            return

        current_yes = refreshed.yes_ask
        current_yes_bid = refreshed.yes_bid
        if current_yes_bid <= 0:
            self._log_event("EXIT_SKIP_NO_BID", {"ticker": position.ticker})
            return
        current_no = refreshed.no_ask
        sell_profit = (current_yes_bid - position.entry_price) * position.count
        lock_profit = (1.0 - position.entry_price - current_no) * position.count
        hold_seconds = time.time() - position.timestamp

        self._log_event("EXIT_EVALUATION", {
            "ticker": position.ticker,
            "entry_price": position.entry_price,
            "current_yes": current_yes,
            "current_yes_bid": current_yes_bid,
            "current_no": current_no,
            "sell_profit": round(sell_profit, 2),
            "lock_profit": round(lock_profit, 2),
            "hold_seconds": round(hold_seconds, 1),
            "position_count": position.count,
        })

        cost = entry_cost(position)
        max_gain = (1.0 - position.entry_price) * position.count
        action, exit_reason = choose_exit(
            self._config, sell_profit, lock_profit, cost, max_gain=max_gain,
        )
        if not action:
            self._log_event("EXIT_SKIP_UNPROFITABLE", {
                "ticker": position.ticker,
                "sell_profit": round(sell_profit, 2),
                "lock_profit": round(lock_profit, 2),
            })
            return

        self._exit_attempts += 1
        await self._execute_exit(
            position, action, current_yes_bid, current_no,
            hold_seconds, exit_reason, trigger="espn_run", run=run,
        )

    async def settle_open_positions(self, kalshi: KalshiClient) -> int:
        """Settle any open positions whose markets have resolved on Kalshi."""
        if self._paper is None:
            return 0
        settled_count = 0
        for position in list(self._positions.get_all()):
            raw = await kalshi.get_market_raw(position.ticker)
            if not raw:
                continue
            info = parse_settlement(raw)
            if not info:
                continue
            payout = settlement_payout_dollars(
                position.side,
                position.count,
                position.entry_price,
                info.result,
            )
            self._paper.settle(
                position.ticker,
                position.side,
                position.count,
                payout,
                position.entry_price,
                strategy=self.name,
                title=position.title,
                result=info.result,
            )
            profit = round(payout - position.entry_price * position.count, 2)
            hold_seconds = time.time() - position.timestamp
            self._positions.remove(position.ticker)
            trade = {
                "action": "settle",
                "ticker": position.ticker,
                "title": position.title,
                "sport": position.sport,
                "side": position.side,
                "entry_price": position.entry_price,
                "payout": round(payout, 2),
                "count": position.count,
                "profit": profit,
                "result": info.result,
                "resolution_source": info.resolution_source,
                "hold_time_seconds": round(hold_seconds, 1),
                "exit_type": "settle",
                "timestamp": time.time(),
            }
            trade.update(self._side_context(position.title, position.ticker, position.side))
            self._trades.append(trade)
            self._log_event("SETTLEMENT_EXECUTED", trade)
            record_trade(
                strategy=self.name,
                action="settle",
                ticker=position.ticker,
                exit_mode=self._exit_mode,
                profit=profit,
                extra=trade,
            )
            settled_count += 1
        return settled_count

    def save_results(self) -> Path:
        RESULTS_DIR.mkdir(exist_ok=True)
        ts = datetime.datetime.fromtimestamp(self._start_time).strftime("%Y%m%d_%H%M%S")
        path = RESULTS_DIR / f"{self.name}_{ts}.json"

        output = {
            "strategy": self.name,
            "config": self._config,
            "run_started": datetime.datetime.fromtimestamp(self._start_time).isoformat(),
            "run_ended": datetime.datetime.now().isoformat(),
            "duration_seconds": round(time.time() - self._start_time, 1),
            "stats": self.summary(),
            "counters": {
                "market_scans": self._scan_count,
                "score_events_seen": self._score_events_seen,
                "runs_detected": self._runs_detected,
                "entry_attempts": self._entry_attempts,
                "exit_attempts": self._exit_attempts,
            },
            "trades": self._trades,
            "events": self._events_log,
        }

        path.write_text(json.dumps(output, indent=2, default=str))
        return path

    def summary(self) -> dict:
        buys = [t for t in self._trades if t.get("action") == "buy"]
        exits = [t for t in self._trades if t.get("action") in ("sell", "lock", "settle")]
        traded = [t for t in exits if t.get("action") in ("sell", "lock")]
        settled = [t for t in exits if t.get("action") == "settle"]
        total_exits = len(exits)
        wins = sum(1 for t in exits if t.get("profit", 0) > 0)
        losses = sum(1 for t in exits if t.get("profit", 0) <= 0)
        realized_pl = sum(t.get("profit", 0) for t in exits)
        traded_pl = sum(t.get("profit", 0) for t in traded)
        settled_pl = sum(t.get("profit", 0) for t in settled)
        total_risked = sum(t.get("entry_price", 0) * t.get("count", 0) for t in exits)
        open_positions = len(self._positions.get_all())
        sells = [t for t in exits if t.get("action") == "sell"]
        locks = [t for t in exits if t.get("action") == "lock"]
        hold_times = [
            t["hold_time_seconds"] for t in exits
            if t.get("hold_time_seconds") is not None
        ]
        avg_hold = sum(hold_times) / len(hold_times) if hold_times else 0
        entries_count = len(buys)

        return {
            "name": self.name,
            "exit_mode": self._exit_mode,
            "entries": entries_count,
            "exits": total_exits,
            "sell_exits": len(sells),
            "lock_exits": len(locks),
            "settle_exits": len(settled),
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total_exits * 100) if total_exits > 0 else 0,
            "realized_pl": round(realized_pl, 2),
            "traded_pl": round(traded_pl, 2),
            "settled_pl": round(settled_pl, 2),
            "total_pl": round(realized_pl, 2),
            "avg_pl": round(realized_pl / total_exits, 2) if total_exits > 0 else 0,
            "profit_per_entry": round(realized_pl / entries_count, 2) if entries_count > 0 else 0,
            "avg_hold_seconds": round(avg_hold, 1),
            "total_risked": round(total_risked, 2),
            "roi": round(realized_pl / total_risked * 100, 2) if total_risked > 0 else 0,
            "open_positions": open_positions,
        }
