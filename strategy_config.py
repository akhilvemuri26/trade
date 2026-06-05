# Paper sim — Run 6: re-entry guard + entry filters (underdog moneylines)
#
# Why this changed from Run 5:
#   Run 5 added stop-losses but lost -$5.2k in 17h. The cohort comparison + optimizer
#   (ml/compare.py, ml/optimize.py) showed ~96% of that loss was a CHURN LOOP: after a
#   stop-out the strategy re-bought the same sub-$0.40 game and stopped out again
#   (hold_nostop = 16 buys vs hold_stop30 = 118 buys on the same games). Collapsing to
#   one position per game would have cut run 5 from -$5,175 to -$202.
#
#   Walk-forward (fit runs 2-3, test 4-5) also showed entry filters help: a price floor
#   ~0.20 lifted test ROI -30% -> -22%, and near-resolution entries (<3h) were ~-49% ROI
#   vs >12h positive. So Run 6:
#     (1) RE-ENTRY GUARD  - one_entry_per_game: one position per game per strategy.
#     (2) ENTRY FILTERS   - min_entry_price (skip deep longshots), min_hours_to_resolution
#                           (skip near-decided games).
#     (3) IN-GAME LOGGING - best-effort live score_diff/is_live logged at entry for the
#                           Track B edge model (virtual_trader._ingame_state).
#
# Every slice was still net-negative (the entry has ~no edge), so Run 6 is a data-
# collection run to confirm the guard/filters get us to ~break-even AND to gather the
# in-game features needed to build a real edge model. The ML gate stays OFF.
import os

# Stake frozen at the value the backtest used ($25); do not drift mid-experiment.
_MAX_RISK = float(os.getenv("MAX_RISK_PER_TRADE", "25"))

MAX_TOTAL_RISK = int(os.getenv("MAX_TOTAL_RISK", "50000"))
MAX_HOURS_TO_RESOLUTION = 24

# Run 6 entry filters + guard (env-overridable; defaults from the optimizer).
_MIN_ENTRY_PRICE = float(os.getenv("MIN_ENTRY_PRICE", "0.20"))
_MIN_HOURS_TO_RESOLUTION = float(os.getenv("MIN_HOURS_TO_RESOLUTION", "3"))
_ONE_ENTRY_PER_GAME = os.getenv("ONE_ENTRY_PER_GAME", "1") == "1"

# Model gate defaults OFF. Kept env-configurable so the model can be re-enabled
# later (after it is relabeled to predict EV / game outcome).
_USE_MODEL_GATE = os.getenv("USE_MODEL_GATE", "0") == "1"
_MODEL_MIN_PROB = float(os.getenv("MODEL_MIN_PROB", "0.6"))
_MODEL_PATH = os.getenv("MODEL_PATH") or None

# Game-winner moneylines only: KXNBAGAME, KXMLBGAME, KXNHLGAME (kalshi_client).

_EXPERIMENT_BASE = {
    "moneyline_only": True,
    "underdog_max_price": 0.40,
    "min_entry_price": _MIN_ENTRY_PRICE,
    "min_hours_to_resolution": _MIN_HOURS_TO_RESOLUTION,
    "one_entry_per_game": _ONE_ENTRY_PER_GAME,
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


# Exit-policy cohorts (all guarded + base-filtered, so no churn) -------------- #
#   hold        - control: hold winners to settlement, no stop.
#   hold_stopNN - hold winners, cut losers at NN% of cost.
#   tpAA_stopNN - take profit after capturing AA% of the move to $1, + stop.
_EXIT_COHORTS = [
    _strat("hold", exit_mode="hold"),
    _strat("hold_stop50", exit_mode="hold", stop_loss_pct=0.50),
    _strat("hold_stop30", exit_mode="hold", stop_loss_pct=0.30),
    _strat("tp70_stop50", exit_mode="take_profit", take_profit_frac=0.70, stop_loss_pct=0.50),
]

# Filter-isolation cohorts (same hold exit; vary the entry filter) ------------ #
#   hold_strict   - tighter filters (price >= 0.25, >= 6h to resolution).
#   hold_nofilter - guard ON but NO price/time filter -> isolates the filter's value.
_FILTER_COHORTS = [
    _strat("hold_strict", exit_mode="hold", min_entry_price=0.25, min_hours_to_resolution=6.0),
    _strat("hold_nofilter", exit_mode="hold", min_entry_price=0.0, min_hours_to_resolution=0.0),
]

STRATEGIES = _EXIT_COHORTS + _FILTER_COHORTS
