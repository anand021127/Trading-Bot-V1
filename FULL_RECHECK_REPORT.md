# Full system recheck — setup, wiring, pipeline, frontend, security

Independent sweeps (not one point): static analysis (pyflakes-class + compile with warnings-as-errors), frontend↔backend API
contract (every UI call vs the OpenAPI route table; typed responses vs TS interfaces; the real Copilot-context payload vs the
panel), a smoke test of every parameter-free GET (51 endpoints), a mutating-endpoint auth audit, environment/deploy configuration
diff, dependency and dead-code scans, logging/DB/calendar/startup review, and the test suite re-run under shifted system clocks
(Dec 2026 and Mar 2027). Everything below was reproduced before it was fixed and has a regression test.

| # | Area | Finding | Fix |
|---|---|---|---|
| 1 | Crash | `gate_chain._human_summary` used an undefined `detail` → Copilot "why didn't we trade" raised `NameError` for a NO_SIGNAL record without V8-D diagnostics. | Fixed. |
| 2 | API | `GET /api/ai/status` returned **500 on every call** (read settings fields that do not exist). | Fixed. |
| 3 | Security | **Upstox token webhook accepted a token from any caller** (its only check, `client_id`, was skipped when the payload omitted it). | `client_id` required to match; optional `UPSTOX_WEBHOOK_SECRET`. |
| 4 | Security | 7 state-changing endpoints stayed open even with `CONTROL_TOKEN` set (`PUT /api/universe/`, diagnostics runners, instrument refresh, scan-now, alert test, Upstox auth request). | Protected; a test enumerates every mutating route. |
| 5 | Wiring | The frontend never sent the control token, and any 401 was shown as "Upstox token expired". | Header interceptor, Settings → Control token card, separate 401 handling + toast. |
| 6 | Wiring | Two duplicate `buildWsUrl()` copies disagreed with the REST base URL (WebSocket silently unreachable on Vercel without `VITE_BACKEND_URL`). | One shared function. |
| 7 | Wiring | Copilot panel read `errors.recent` / `websocket.state`; the backend sends `recent_error_lines` / `connection_status`. | Panel uses real names; log history never pins the warning banner. |
| 8 | Time bomb | Holiday tables end in 2026; from 1 Jan 2027 the calendar fails closed **and `/api/overview` + price endpoints returned HTTP 500**. | `EXCHANGE_HOLIDAYS_<YYYY>` operator override, warnings from October (health, log, Copilot), display paths degrade to "market closed". Trading stays fail-closed. |
| 9 | Deploy | `render.yaml` never pointed `DATABASE_PATH` at its persistent disk (DB wiped each deploy), stale architecture notes; `vercel.json` used `npm install`. | Fixed (both vercel files identical, tested). |
| 10 | Setup | 26 settings were undocumented; `COPILOT_LLM_TIMEOUT_SECONDS` defaulted to 8 s (too short for a local CPU model — the "Copilot timed out" you saw). | Documented in `.env.example`; default 45 s. |
| 11 | UI honesty | "0/20 trades" while V8-D stops at 3/day. | Effective cap shown with its breakdown. |
| 12 | **Capital vs lot size** | ₹20,000 cannot fund even one lot under V8-D's caps: every valid setup is rejected "0 lots" (2 of 171 real backtest trades fundable). | Detected from the live chain; explicit rejection text, red dashboard/Copilot warning, minimum equity per symbol. |
| 13 | Backtest honesty | P&L alone hid that V8-D's real backtest is a significant loser. | `NEGATIVE_EXPECTANCY / NO_DEMONSTRATED_EDGE / POSITIVE_EXPECTANCY` verdict with a bootstrap interval, shown in the dashboard. |
| 14 | Code hygiene | Unreachable code after `return`, duplicate dict key, dead frontend call, invalid `\l` escape, stray BOM. | Fixed. |
| 15 | Test suite | Fixed calendar dates in fixtures (expiry literals) and the holiday table made tests fail as the clock advanced (found by running the suite at Dec 2026 and Mar 2027). | Relative dates + a test-process holiday stand-in; verified at both shifted clocks. |

## Verified, no defect
All other UI→API calls match a route and method; typed responses match their TS interfaces; no dead pages/routes/components/hooks;
every documented env var is read; requirements cover every third-party import; DB uses WAL + busy-timeout + idempotent migrations;
logs never contain raw tokens and the trade log rotates; missing core env vars refuse to start with an explicit message; equity is
refreshed every scan; exits deliberately bypass the entry pipeline so a risk rule can never trap an open position.

## Left as-is (noted, not changed — protected or by design)
* `PaperTradingRuntime` swallows a failed `db.delete_position` after a closed exit (no log); `exit_signal` there is an unused local.
* `CONTROL_TOKEN` unset = open (documented; set it on any public host). The Copilot chat endpoints stay open by design (observation only).
* The strategy docstring still describes older thresholds; the code is what runs.
