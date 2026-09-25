# Trade Metadata Model — ONE Common Contract (Paper → Live → Backtest)

## What this is

A single trade metadata contract, implemented once in
`backend/domain/trade_metadata.py` and persisted in **one** schema (the
existing `trades` table), used identically by all three execution modes:

| Mode     | Where metadata is captured                                        |
|----------|-------------------------------------------------------------------|
| PAPER    | `paper_runtime._place` — authoritative contract fields from the signal + **actual simulated fill** (`order.avg_price × order.filled_qty`); exit via `_execute_exit` |
| LIVE     | `trading_engine.execute_multi_signal` — broker-resolved `selected_contract` (strike/option_type/expiry/lot_size/instrument_key) + **actual broker fill**; exit via `_close_position` |
| BACKTEST | `backtest.engine.BacktestTrade` — the actual historical contract resolved by `options_data_layer` / `historical_contract_resolver` |

There is **no** Paper schema, Live schema, or Backtest schema — one table,
one set of columns, one normalizer.

## The canonical fields

```
underlying_symbol   option_type (CE|PE)   strike_price   expiry
instrument_key      entry_price           exit_price     quantity (EXECUTED)
lot_size            capital_used          entry_timestamp  exit_timestamp
strategy            status                trade_id / order_id / signal_id
```

DB columns (trades table): the canonical names, with the timestamps stored
under the historical column names `entry_time` / `exit_time` (the API layer
additionally exposes `entry_timestamp` / `exit_timestamp` aliases, so
consumers of the contract read one field name everywhere).

## capital_used — the only definition

```
capital_used = entry_price × executed_quantity
```

- entry_price is the **actual executed fill price** (never the requested premium
  when the fill differs).
- quantity is the **actually executed quantity** (partial fills count only what
  filled — never requested/theoretical quantity).
- It is NEVER max allocation, theoretical risk, configured allocation
  percentage, or account capital.
- Fees/charges stay separate: gross P&L, brokerage, STT and net P&L are their
  own columns and are never folded into capital_used.
- Invalid fill data (price ≤ 0, quantity ≤ 0, non-numeric) → capital_used
  stays NULL. The system never invents a capital figure.

## Historical rows — "N/A / Historical metadata unavailable"

The migration is purely additive (`ALTER TABLE ADD COLUMN`, PRAGMA-guarded,
idempotent, restart-safe). Rows written before this contract keep NULL for the
new columns. The UI renders exactly:

```
N/A / Historical metadata unavailable
```

Nothing is backfilled, guessed, or synthesized.

## Invariants (all covered by `backend/tests/test_trade_metadata_contract.py`)

1. Paper entry persists all fields; duplicate execution creates no second row.
2. Paper restart rehydrates strike/option_type/expiry/qty/lot/entry price/capital/trade_id (positions.extra) and exits still update the original row.
3. Exit writes only exit-side columns — entry metadata is preserved untouched.
4. Partial fill → capital_used uses executed quantity only.
5. Missing strike → recorded NULL (renders N/A); missing/invalid lot_size or
   expiry → the pre-trade contract gate refuses execution (fail-safe, no
   invented metadata).
6. quantity = 0 / non-lot-multiple quantity → refused before any order.
7. Invalid exit price (≤ 0) → exit refused, row untouched.
8. A stored capital_used that contradicts the executed fill data is recomputed
   from the fill data (never trusted over actual execution).
9. Live mode without a usable broker token executes nothing (paper→live safety unchanged).
10. Backtest results are unchanged — the model only *exposes* what the engine
    already traded (added: capital_used, status, strike_price, entry/exit_timestamp fields in `to_dict`).

## API surface

- `GET /api/trades` — rows now carry the full metadata model (+ summary).
  Also fixes a latent bug: the endpoint previously serialized `Trade`
  dataclasses through `dict(row)`, which silently returned `{}` for every
  trade. All fields now reach the frontend.
- `GET /api/trades/{trade_id}` — raw row incl. metadata.
- `GET /api/trades/export/csv` — new columns: underlying_symbol, option_type,
  strike_price, expiry, instrument_key, lot_size, capital_used, order_id,
  signal_id.
- `GET /api/positions` and `GET /api/overview` open_positions — open
  positions surface the same contract identity (from `positions.extra`,
  persisted at entry) + capital_used.
- `GET /api/paper/positions` — same fields for the paper ledger positions.
- `backend/cli/node_bridge.py cmd_trades()` — same field set for the node
  gateway.
