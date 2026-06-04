# Paper sim — Run 5: exit-policy redesign (underdog moneylines)
#
# Why this changed from the Run 4 swing-exit grid:
#   Backtest on runs 2-3 (true Kalshi settlement outcomes) showed the entry is
#   ~fairly priced (underdogs won 30.6% vs 32.6% implied), but the swing-exit
#   rule clipped 342/346 winning positions early, leaving ~$14.6k on the table,
#   while losers rode to a full-premium loss with no stop. Same trades:
#       actual swing-exits, no stop ........ -$24.0k
#       hold winners to settlement ......... -$13.3k
#       stop-loss @50% + hold winners ...... +$2.8k
#
# Run 5 therefore (a) stops clipping winners and (b) cuts the losing tail, and
# runs two cohorts side by side to compare on live data:
#   HOLD cohort:        never take profit (full winner), stop-loss cuts losers.
#   TAKE_PROFIT cohort: sell once we've captured most of the move to $1, + stop.
#
# The ML entry gate is OFF for Run 5: it optimized P(profitable swing) (~win
# rate, anti-correlated with EV here) and fed the winner-clipping. ml/ is kept
# in place for a future EV-based relabel once an entry edge is demonstrated.
import os

# Stake frozen at the value the backtest used ($25); do not drift mid-experiment.
_MAX_RISK = float(os.getenv("MAX_RISK_PER_TRADE", "25"))

MAX_TOTAL_RISK = int(os.getenv("MAX_TOTAL_RISK", "50000"))
MAX_HOURS_TO_RESOLUTION = 24

# Model gate defaults OFF for Run 5. Kept env-configurable so the model can be
# re-enabled later (after it is relabeled to predict EV / game outcome).
_USE_MODEL_GATE = os.getenv("USE_MODEL_GATE", "0") == "1"
_MODEL_MIN_PROB = float(os.getenv("MODEL_MIN_PROB", "0.6"))
_MODEL_PATH = os.getenv("MODEL_PATH") or None

# Game-winner moneylines only: KXNBAGAME, KXMLBGAME, KXNHLGAME (kalshi_client).

_EXPERIMENT_BASE = {
    "moneyline_only": True,
    "underdog_max_price": 0.40,
    "max_risk": _MAX_RISK,
    "max_positions": 200,
    "min_volume": 0,
    "enter_all_markets": True,
    "ignore_global_risk_cap": True,
    "use_run_exits": False,
    "sports": ["nba", "mlb", "nhl"],
    "use_model_gate": _USE_MODEL_GATE,
    "model_min_prob": _MODEL_MIN_PROB,
    "model_path": _MODEL_PATH,
}


def _strat(name: str, **overrides) -> dict:
    return {**_EXPERIMENT_BASE, "name": name, **overrides}


# --- HOLD cohort: capture the full winner, cut losers at a stop ---------------
# stop_loss_pct = exit once unrealized loss reaches this fraction of cost.
# hold_nostop is the control (pure buy-and-hold-to-settlement) to isolate the
# stop-loss contribution.
_HOLD = [
    _strat("hold_nostop", exit_mode="hold"),
    _strat("hold_stop30", exit_mode="hold", stop_loss_pct=0.30),
    _strat("hold_stop50", exit_mode="hold", stop_loss_pct=0.50),
    _strat("hold_stop70", exit_mode="hold", stop_loss_pct=0.70),
]

# --- TAKE_PROFIT cohort: keep most of the winner, cut losers ------------------
# take_profit_frac = sell once we've captured this fraction of the move from
# entry to a $1 settlement (price-independent across entry prices).
_TAKE_PROFIT = [
    _strat("tp60_stop50", exit_mode="take_profit", take_profit_frac=0.60, stop_loss_pct=0.50),
    _strat("tp80_stop50", exit_mode="take_profit", take_profit_frac=0.80, stop_loss_pct=0.50),
    _strat("tp80_stop30", exit_mode="take_profit", take_profit_frac=0.80, stop_loss_pct=0.30),
]

STRATEGIES = _HOLD + _TAKE_PROFIT
