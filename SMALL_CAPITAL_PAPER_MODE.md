# Small-capital paper profile (₹5,000)

This profile is for **paper simulation only**. It configures the example environment
with `TRADING_CAPITAL=5000`; the V8-D risk controls remain unchanged:

- Maximum account risk per trade: **2.5%**
- Maximum capital allocation per trade: **18%**
- Whole option lots only; no fractional lots and no bypass of risk gates

## Important limitation

A ₹5,000 account allows at most ₹900 allocation under the 18% cap and ₹125
risk under the 2.5% cap. If the cheapest eligible whole option lot exceeds either
limit, V8-D must reject the setup as `0 lots`. That is an affordability constraint,
not a scanner failure. The bot may scan normally and still take no trades.

Do **not** loosen the risk caps, fabricate fills, or use a virtual fractional lot
to make the dashboard show trades. This profile does not promise trades or profit.

## Apply it

1. Keep `TRADING_MODE=paper`.
2. Set `TRADING_CAPITAL=5000` in the actual environment used by the worker.
3. If the Settings page has a saved capital value in SQLite, update it there too:
   the persisted Settings value can override environment defaults.
4. Restart the API and paper worker, then verify the effective capital in Settings
   and check the funding diagnostic for the current option-chain premiums/lot sizes.

This is a configuration/example profile, not a recommendation to trade live with
₹5,000. Validate all settings before any deployment.
