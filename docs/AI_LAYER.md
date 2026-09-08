# AI Decision-Filter Layer — Final Report

## 1. Existing architecture discovered
```
Upstox WS v3 (protobuf) → market_data/historical.py, live_feed.py
  → Indicators (ema/rsi/atr/vwap/choppiness/volume)
  → MultiStrategyEngine (EMATrendStrategy, ORBStrategy, OptionPremiumStrategy, V8DStrategy)
  → OptionPremiumStrategy uses ConfidenceScorer → SetupScoreResult (0-100 score, factor breakdown)
  → trading_engine.execute_multi_signal() → RiskManager → PositionSizer → OrderManager → TradeLogger
  → BacktestEngine replays the same MultiStrategyEngine over historical candles
```
A V8-D shadow-mode system (`paper/v8d_shadow_mode.py`) already existed and was used as the pattern for the AI shadow logger.

## 2. Files changed
- `backend/strategy/signal.py` — added `setup_name`, `factor_scores` fields to `StrategySignal` (additive, default-preserving)
- `backend/strategy/strategies/option_premium.py` — populate those two fields from `SetupScoreResult` at all 3 signal-construction sites
- `backend/strategy/trading_engine.py` — added `self.ai_predictor`, and an AI-filter check in `execute_multi_signal()` between signal generation and `RiskManager.can_take_trade()`
- `backend/backtest/engine.py` — added `ai_mode` param (`disabled`/`shadow`/`filter`), `BacktestResult` fields (`ai_mode`, `ai_signals_evaluated`, `ai_signals_filtered`, `ai_shadow_log_sample`), and the per-candidate AI check in the signal loop
- `backend/api/routers/__init__.py`, `backend/api/main.py` — registered new read-only `ai_router`
- `.env.example` — added `AI_ENABLED`, `AI_MODE`, `AI_MIN_CONFIDENCE`, `AI_MIN_TRADE_PROBABILITY`, `AI_FAIL_OPEN`, `AI_MODELS_DIR` (all default to off/safe)

## 3. Files added
- `backend/ai/__init__.py`, `features.py`, `dataset.py`, `model.py`, `walk_forward.py`, `registry.py`, `config.py`, `predictor.py`, `shadow_logger.py`
- `backend/api/routers/ai.py` — read-only `GET /api/ai/status`
- `backend/tests/test_ai_layer.py` — 12 tests (feature schema, no-lookahead, chronological ordering, disabled pass-through, fail-open/fail-closed, mode safety)
- `models/ai/<version>/model.pkl` + `metadata.json`, `models/ai/latest.json` — one trained model saved for reproducibility (explicitly marked not-for-live in its metadata)
- This report

## 4. Files intentionally untouched
`RiskManager`, `PositionSizer`, `OrderManager`, `ExitManager`, `TrailingStopManager`, all other strategies (`EMATrendStrategy`, `ORBStrategy`, `V8DStrategy`), the database schema, and every existing API route.

## 5. AI architecture
`Existing Strategy Signal → AI feature extraction (from the same SetupScoreResult data the strategy already computed) → calibrated probability → AI decision (allow/filter) → [existing] RiskManager → PositionSizer → Execution`. The AI never touches risk limits, sizing, or order placement, and is fully bypassed when `AI_ENABLED=false`.

## 6. Features used
23 features — `setup_confidence`, direction, 4 factor scores (trend/momentum/vwap/volume), RSI, choppiness, volume ratio, 3 rate-of-change windows, EMA20 slope, distance-from-EMA20-in-ATR, overextension flag, close-vs-VWAP%, close-vs-EMA50%, ATR%, minutes-since-open, day-of-week, and 3 setup-type flags. All derived only from `candles[:t+1]` and the strategy's own already-computed indicators.

## 7. Target definition
`1` if a 2R target (3× entry-ATR) is hit before a 1.5×-ATR stop within 48 bars (4h), else `0` — mirrors `EMATrendStrategy`'s own stop/target convention exactly, applied to the underlying (see §13 scope note).

## 8. Model selected and why
`HistGradientBoostingClassifier` / `RandomForestClassifier` / `LogisticRegression` were all fit per fold; the one with the best validation AUC (ties broken by Brier score) was kept, isotonic-calibrated on the validation split via `sklearn.frozen.FrozenEstimator`. Winner varied by fold (random_forest, hist_gradient_boosting, logistic_regression each won at least one fold) — no single model dominated, consistent with a weak underlying signal (§13).

## 9. Training methodology
Chronological, expanding-window: train on all months before the validation month, calibrate on the validation month, test on the next unseen month. Model selection happens only on validation AUC — never on test.

## 10. Walk-forward methodology
5 folds, test months Aug–Dec 2024. Fold N: train = Jan..(N-2), val = month (N-1), test = month N. Each fold trains and calibrates its own model from scratch.

## 11–12. Baseline vs AI-filtered results (corrected methodology — see §14)
De-duplicated to one open position at a time (matching `RiskManager`'s gating), R-multiples on the underlying (not real premium — §13):

| | Trades | Win rate | PF | Net R | Max DD | Max consec. losses |
|---|---|---|---|---|---|---|
| **Baseline** (existing strategy, confidence ≥ 70) | 136 | 32.4% | 0.96 | −4R | −21R | 9 |
| **AI-filtered** (also requires calibrated P ≥ 0.65) | 4 | 50.0% | 2.00 | +2R | −1R | 1 |

## 13. Whether AI actually improved the strategy: **No — and the AI-filtered row above is not a real result, it's an artifact of a 4-trade sample.**
Aggregate numbers alone would look like a win for AI. They aren't — see the diagnostic:

**Accepted-vs-rejected breakdown** (evaluating the AI's opinion on all 136 baseline-eligible trades):
| | n | Win rate | Mean AI probability | Mean existing confidence |
|---|---|---|---|---|
| AI accepted (P≥0.65) | 1 | 0% | 0.77 | 94.0 |
| AI rejected (P<0.65) | 135 | 32.6% | 0.33 | 84.7 |

The rejected group's win rate (32.6%) is statistically indistinguishable from the baseline's overall win rate (32.4%). **The AI isn't separating good trades from bad ones — it's rejecting almost everything (135/136) uniformly**, because validation AUC hovered around 0.55–0.77 (mostly close to 0.60, barely above random) and a ~33% true win rate rarely produces calibrated probabilities above a 0.65 bar. The one "accepted" trade lost. The apparent 2.0 profit factor is 2 wins out of 4 total trades — not a sample anyone should trust.

**Root-cause check, run at your request:** across the full 13,035-row dataset (not just the 136-trade backtest sample), win rate by existing-`ConfidenceScorer`-bucket is flat — 31–38% from confidence 65 through 99, with no monotonic trend (checked at both 4h and 2h horizons, same result). **The existing confidence score itself shows no measurable relationship to forward outcome in this underlying-move proxy.** An ML model trained substantially on the same underlying indicators can't manufacture separable edge that isn't there in the base signal — this is very likely why AUC tops out around 0.6–0.77 rather than something meaningfully higher.

**What was checked and ruled out** (per your request to verify alignment before trusting the result):
- Instrument/timeframe: single instrument (NIFTY50), 5-min bars, consistent across train/val/test — no mismatch.
- Feature/training-target alignment: dataset built directly from `ConfidenceScorer.evaluate()`, the same class `OptionPremiumStrategy` (the only production consumer) calls — no separate/diverging code path.
- AI filtering point: confirmed to sit exactly between signal generation and `RiskManager`, matching your specified architecture.
- **Found and fixed a real bug**: the first walk-forward run evaluated a "trade" on every 5-min bar a setup stayed active, producing 4,515 baseline "trades" over 5 months — 83% of those rows sat in runs of ≥5 consecutive duplicate-outcome bars, meaning the same real market move was counted many times. Rebuilt with non-overlapping trade selection (one open position at a time, matching `RiskManager`), giving the 136-trade baseline above (~27/month, a plausible intraday-options trade frequency). This fix changed baseline PF from 0.90→0.96 and, more importantly, corrected the AI comparison from a large-N misleading result (36 trades, PF 0.57) to a small-N one that's honestly too thin to trust in either direction — which is why the accepted-vs-rejected diagnostic on the full 136-trade population is the number to actually rely on, not the top-line "AI-filtered" row.

## 14. Max drawdown / profit factor / win-rate / trade-count comparison
See tables in §12. Read them alongside §13 — the top-line AI-filtered row is not a reliable comparison at n=4.

## 15. Remaining weaknesses
- Underlying-move proxy, not real option premium P&L (full-year premium history isn't in this repo — only a handful of Oct-2024 sample days in `real_data/options_cache`). Real option economics (theta, IV changes, liquidity/slippage) could change the picture and weren't tested.
- Single instrument (NIFTY50), single year (2024) — not tested on BANKNIFTY/FINNIFTY/SENSEX or other years.
- `ConfidenceScorer`'s own score shows no forward-predictive relationship in this proxy — if that holds up under a real-premium backtest too, it's worth investigating independent of the AI layer.
- Validation set sizes vary a lot by fold (289 to 1267 rows) since months have very different signal counts — later folds have more stable calibration than earlier ones.
- `/api/ai/status` route registration was added but its live mounting wasn't independently re-verified after the last edit (see follow-up items below) — treat as provisional and check `GET /api/ai/status` after startup before relying on it.
- Frontend `AIStatusPanel` was not built in this session.

## 16–22. How to run
```bash
# Train + walk-forward validate (writes a report dict; wire to a script/CLI as needed)
python3 -c "
import json
from backend.ai.walk_forward import run_walk_forward
with open('real_data/NIFTY50_2024_5min.json') as f:
    candles = json.load(f)
result = run_walk_forward(candles, symbol='NIFTY50')
print(json.dumps(result, indent=2))
"

# Train + save one model to the registry
python3 -c "
from backend.ai.dataset import build_dataset, rows_to_xy
from backend.ai.model import select_best_model
from backend.ai.registry import save_model
import json
with open('real_data/NIFTY50_2024_5min.json') as f:
    candles = json.load(f)
rows, _ = build_dataset(candles, symbol='NIFTY50')
# ... split by month, fit, then save_model(trained, training_months=..., validation_month=..., test_months=...)
"

# Run the AI layer's own tests
python3 run_all_tests.py         # full suite (project convention)
python3 pytest.py backend/tests/test_ai_layer.py -v   # just the AI layer

# Enable shadow mode (observe only, never blocks trades)
# in .env: AI_ENABLED=true, AI_MODE=shadow
# Decisions land in logs/ai_shadow_log.csv

# Disable entirely (default) — identical behavior to before this layer existed
# in .env: AI_ENABLED=false
```

## 23. Recommendation
**Keep `AI_ENABLED=false` (the shipped default).** The diagnostics above show the model isn't discriminating winning trades from losing ones at the current feature set / target definition / instrument — it's applying a threshold that happens to reject almost everything. This is not a wiring problem (verified: feature pipeline, training target, instrument/timeframe, and filter insertion point all match the real strategy path) — it's a genuine "this doesn't work yet" result, reported as instructed rather than manufactured into a false positive. Do not raise/lower `AI_MIN_TRADE_PROBABILITY` to chase a better-looking number on this same test data — that would be exactly the test-set tuning the brief prohibits. If you want to pursue this further, the highest-value next step is probably investigating why `ConfidenceScorer`'s own score doesn't separate outcomes in this proxy, independent of the AI layer, and/or validating against real option premium data instead of the underlying-move proxy.
