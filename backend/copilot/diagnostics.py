"""run_full_diagnostics(): inspects the ACTUAL running system, reusing
backend/health/health_monitor.py's HealthMonitor (already tracks
component status/heartbeats/errors for whatever's registered) rather
than re-implementing per-component checks. Each row follows the spec's
exact shape: COMPONENT / STATUS / EVIDENCE / PROBLEM / SEVERITY /
RECOMMENDED_ACTION. If something can't be verified, the row says so —
it never gets silently skipped or guessed as healthy.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List


@dataclass
class DiagnosticRow:
    component: str
    status: str        # "OK" | "DEGRADED" | "ERROR" | "UNKNOWN"
    evidence: str
    problem: str
    severity: str       # "NONE" | "LOW" | "MEDIUM" | "HIGH"
    recommended_action: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _row_from_health_component(name: str, comp: Any) -> DiagnosticRow:
    try:
        d = comp.to_dict() if hasattr(comp, "to_dict") else dict(comp)
    except Exception as e:
        return DiagnosticRow(name, "UNKNOWN", "", f"Could not read component state: {e}", "MEDIUM",
                              "Investigate why this component's status can't be read.")
    status_raw = str(d.get("status", "unknown")).upper()
    status = status_raw if status_raw in ("OK", "DEGRADED", "ERROR") else "UNKNOWN"
    problem = "" if status == "OK" else (d.get("last_error") or f"Component reported status={status_raw}.")
    severity = {"OK": "NONE", "DEGRADED": "MEDIUM", "ERROR": "HIGH", "UNKNOWN": "MEDIUM"}[status]
    action = "" if status == "OK" else "Check component logs and recent errors; restart if the process supports it."
    return DiagnosticRow(name, status, evidence=str(d), problem=problem, severity=severity, recommended_action=action)


def run_full_diagnostics(tools: Any) -> Dict[str, Any]:
    rows: List[DiagnosticRow] = []

    # ── Everything HealthMonitor already tracks ──────────────────────
    if tools.health_monitor is not None:
        try:
            snap = tools.health_monitor.snapshot()
            for name, comp in (snap.get("components") or {}).items():
                rows.append(_row_from_health_component(name, comp))
        except Exception as e:
            rows.append(DiagnosticRow("health_monitor", "ERROR", "", f"snapshot() raised: {e}", "HIGH",
                                       "HealthMonitor itself is failing — check its wiring in main.py."))
    else:
        rows.append(DiagnosticRow("health_monitor", "UNKNOWN", "", "No HealthMonitor attached to this process.",
                                   "MEDIUM", "Cannot verify component health without HealthMonitor — attach it."))

    # ── Database ──────────────────────────────────────────────────────
    if tools.db_manager is None:
        rows.append(DiagnosticRow("database", "UNKNOWN", "", "No DatabaseManager attached.", "MEDIUM",
                                   "Attach a DatabaseManager to the Copilot's tool layer."))
    else:
        try:
            tools.db_manager.get_open_positions()
            rows.append(DiagnosticRow("database", "OK", "get_open_positions() succeeded.", "", "NONE", ""))
        except Exception as e:
            rows.append(DiagnosticRow("database", "ERROR", "", f"Query failed: {e}", "HIGH",
                                       "Check the SQLite file path/permissions and recent schema migrations."))

    # ── Risk manager ──────────────────────────────────────────────────
    if tools.risk_manager is None:
        rows.append(DiagnosticRow("risk_manager", "UNKNOWN", "", "No RiskManager attached.", "HIGH",
                                   "Cannot verify risk state — attach RiskManager before allowing any trading."))
    else:
        try:
            status = tools.risk_manager.get_status()
            rows.append(DiagnosticRow("risk_manager", "OK", str(status), "", "NONE", ""))
        except Exception as e:
            rows.append(DiagnosticRow("risk_manager", "ERROR", "", f"get_status() failed: {e}", "HIGH",
                                       "RiskManager is not queryable — treat as unsafe to trade until fixed."))

    # ── WebSocket / live data ────────────────────────────────────────
    if tools.ws_client is None:
        rows.append(DiagnosticRow("websocket", "UNKNOWN", "", "No WebSocket client attached to this process.",
                                   "MEDIUM", "Expected in backtest-only contexts; if live trading is expected, investigate why ws_client is None."))
    else:
        try:
            connected = bool(getattr(tools.ws_client, "is_connected", False))
            feed_status = getattr(tools.ws_client, "market_data_status", "UNKNOWN")
            ok = connected and feed_status == "LIVE"
            rows.append(DiagnosticRow(
                "websocket", "OK" if ok else ("DEGRADED" if connected else "ERROR"),
                f"is_connected={connected}, market_data_status={feed_status}",
                "" if ok else f"WebSocket connected={connected}, feed_status={feed_status}.",
                "NONE" if ok else ("MEDIUM" if connected else "HIGH"),
                "" if ok else "Check Upstox auth token validity, subscriptions, and network connectivity.",
            ))
        except Exception as e:
            rows.append(DiagnosticRow("websocket", "ERROR", "", f"Status check raised: {e}", "HIGH",
                                       "WebSocket client is in a bad state — check logs and consider restarting it."))

    # ── Strategy engine / scanner ───────────────────────────────────
    if tools.engine is None:
        rows.append(DiagnosticRow("strategy_engine", "UNKNOWN", "", "No TradingEngine attached.", "HIGH",
                                   "Cannot verify strategy engine status."))
    else:
        rows.append(DiagnosticRow("strategy_engine", "OK", "TradingEngine instance present.", "", "NONE", ""))

    # ── PositionSizer ────────────────────────────────────────────────
    sizer = getattr(tools.engine, "position_sizer", None)
    if sizer is None:
        rows.append(DiagnosticRow("position_sizer", "UNKNOWN", "", "No PositionSizer attached.", "HIGH",
                                   "Cannot verify sizing without a PositionSizer instance."))
    else:
        try:
            test_qty = sizer.calculate(entry_price=100.0, stop_loss_price=95.0)
            rows.append(DiagnosticRow("position_sizer", "OK",
                                       f"capital={getattr(sizer, 'capital', '?')}, risk_per_trade={getattr(sizer, 'risk_per_trade', '?')}, "
                                       f"test_calculate(100,95)={test_qty}",
                                       "", "NONE", ""))
        except Exception as e:
            rows.append(DiagnosticRow("position_sizer", "ERROR", "", f"calculate() raised on a sane test input: {e}", "HIGH",
                                       "PositionSizer is misconfigured — check capital/risk_per_trade."))

    # ── OrderManager ─────────────────────────────────────────────────
    om = getattr(tools.engine, "order_manager", None)
    if om is None:
        rows.append(DiagnosticRow("order_manager", "UNKNOWN", "", "No OrderManager attached.", "HIGH",
                                   "Cannot verify order execution safety without an OrderManager instance."))
    else:
        paper_mode = getattr(om, "paper_mode", None)
        rows.append(DiagnosticRow(
            "order_manager", "OK" if paper_mode is not None else "UNKNOWN",
            f"paper_mode={paper_mode}", "" if paper_mode else "OrderManager is NOT in paper_mode — real orders are possible.",
            "NONE" if paper_mode else "HIGH",
            "" if paper_mode else "Confirm this is intentional before any live-adjacent testing.",
        ))

    # ── Underlying market data / candle freshness ───────────────────
    if tools.engine is None or getattr(tools.engine, "client", None) is None:
        rows.append(DiagnosticRow("market_data", "UNKNOWN", "", "No broker client attached.", "MEDIUM",
                                   "Cannot verify underlying candle freshness."))
    else:
        try:
            candles_resp = tools.get_live_candles("NIFTY50", limit=5)
            if candles_resp.get("available"):
                age = candles_resp.get("data_age_seconds")
                stale = age is not None and age > 900  # >15 min during a live session is suspicious
                rows.append(DiagnosticRow(
                    "market_data", "DEGRADED" if stale else "OK",
                    f"last_candle_timestamp={candles_resp.get('last_candle_timestamp')}, age_seconds={age}",
                    "Latest underlying candle looks stale." if stale else "", "MEDIUM" if stale else "NONE",
                    "Check the historical-candle endpoint / market hours." if stale else "",
                ))
            else:
                rows.append(DiagnosticRow("market_data", "ERROR", "", candles_resp.get("reason", ""), "MEDIUM",
                                           "Underlying candle fetch is failing."))
        except Exception as e:
            rows.append(DiagnosticRow("market_data", "ERROR", "", f"Candle freshness check raised: {e}", "MEDIUM", ""))

    # ── Option premium candle freshness ─────────────────────────────
    if tools.engine is None or getattr(tools.engine, "client", None) is None:
        rows.append(DiagnosticRow("option_premiums", "UNKNOWN", "", "No broker client attached.", "MEDIUM",
                                   "Cannot verify option premium data."))
    else:
        try:
            chain_resp = tools.get_option_chain("NIFTY50")
            if chain_resp.get("available") and chain_resp.get("contracts"):
                sample = chain_resp["contracts"][0]
                key = sample.get("instrument_key")
                if key:
                    premium_candles = tools.engine.client.get_historical_candles(key, "5minute", limit=5)
                    ok = bool(premium_candles)
                    rows.append(DiagnosticRow("option_premiums", "OK" if ok else "DEGRADED",
                                               f"sample_instrument={key}, candles_returned={len(premium_candles) if premium_candles else 0}",
                                               "" if ok else "No premium candles returned for a sample contract.",
                                               "NONE" if ok else "MEDIUM", "" if ok else "Check premium candle endpoint."))
                else:
                    rows.append(DiagnosticRow("option_premiums", "UNKNOWN", "", "Chain contract had no instrument_key to sample.", "LOW", ""))
            else:
                rows.append(DiagnosticRow("option_premiums", "UNKNOWN", "", "No option chain available to sample a premium series from.", "MEDIUM", ""))
        except Exception as e:
            rows.append(DiagnosticRow("option_premiums", "ERROR", "", f"Premium candle check raised: {e}", "MEDIUM", ""))

    if tools.scanner is None:
        rows.append(DiagnosticRow("scanner", "UNKNOWN", "", "No LiveScanner attached to this process.", "LOW",
                                   "Expected if the scanner isn't running in this context."))
    else:
        try:
            health = tools.scanner.health_report() if hasattr(tools.scanner, "health_report") else {}
            healthy = bool(health.get("is_healthy", health.get("is_running", True)))
            rows.append(DiagnosticRow("scanner", "OK" if healthy else "DEGRADED", str(health),
                                       "" if healthy else "Scanner is attached but not healthy/running.",
                                       "NONE" if healthy else "MEDIUM",
                                       "" if healthy else "Check why the scanner's background loop stopped or stalled."))
        except Exception as e:
            rows.append(DiagnosticRow("scanner", "ERROR", "", f"health_report() failed: {e}", "MEDIUM",
                                       "Scanner instance is in a bad state."))

    # ── Option chain / premium freshness (Copilot-specific, Phase 14) ──
    client = getattr(tools.engine, "client", None)
    if client is None:
        rows.append(DiagnosticRow("option_chain", "UNKNOWN", "", "No broker client attached.", "MEDIUM",
                                   "Cannot verify option chain access without a broker client."))
    else:
        try:
            expiry = client.get_nearest_expiry("NIFTY50")
            if not expiry:
                rows.append(DiagnosticRow("option_chain", "DEGRADED", "get_nearest_expiry returned nothing.",
                                           "No upcoming expiry resolved for NIFTY50.", "MEDIUM",
                                           "Check instrument master data / expiry calendar."))
            else:
                chain = client.get_option_chain("NIFTY50", expiry)
                ok = bool(chain)
                rows.append(DiagnosticRow(
                    "option_chain", "OK" if ok else "DEGRADED",
                    f"expiry={expiry}, contracts={len(chain) if chain else 0}",
                    "" if ok else f"Option chain for NIFTY50 {expiry} came back empty.",
                    "NONE" if ok else "MEDIUM",
                    "" if ok else "Check the option-chain endpoint and instrument master sync.",
                ))
        except Exception as e:
            rows.append(DiagnosticRow("option_chain", "ERROR", "", f"Live check raised: {e}", "MEDIUM",
                                       "Option chain fetch is failing — check broker auth/connectivity."))

    # ── Copilot's own config ──────────────────────────────────────────
    try:
        from backend.copilot.config import load_copilot_settings
        cp_settings = load_copilot_settings()
        rows.append(DiagnosticRow(
            "copilot", "OK", f"enabled={cp_settings.enabled}, mode={cp_settings.mode}, llm_backend={cp_settings.llm_backend}",
            "", "NONE", "",
        ))
    except Exception as e:
        rows.append(DiagnosticRow("copilot", "ERROR", "", f"Config load failed: {e}", "LOW", "Check backend/copilot/config.py."))

    # ── AI ML filter layer ───────────────────────────────────────────
    try:
        from backend.ai.config import load_ai_settings
        ai_settings = load_ai_settings()
        rows.append(DiagnosticRow(
            "ai_ml_filter_layer", "OK" if not ai_settings.enabled else "DEGRADED",
            f"enabled={ai_settings.enabled}, mode={ai_settings.mode}",
            "" if not ai_settings.enabled else "AI ML filter is enabled — see docs/AI_LAYER.md for the walk-forward "
                                                "results before trusting its filtering in paper/live mode.",
            "NONE" if not ai_settings.enabled else "LOW",
            "" if not ai_settings.enabled else "Review docs/AI_LAYER.md; walk-forward validation did not show "
                                                "the AI filter adding value as of the last training run.",
        ))
    except Exception as e:
        rows.append(DiagnosticRow("ai_ml_filter_layer", "ERROR", "", f"Config load failed: {e}", "LOW", "Check backend/ai/config.py."))

    # ── Background jobs (scan loop / reconciliation) ────────────────
    scanner_hook_wired = bool(tools.scanner is not None and getattr(tools.scanner, "copilot_hook", None) is not None)
    rows.append(DiagnosticRow(
        "background_jobs", "OK" if scanner_hook_wired else "DEGRADED",
        f"scanner_copilot_hook_wired={scanner_hook_wired}",
        "" if scanner_hook_wired else "Copilot scan loop is not wired into the attached scanner's copilot_hook, "
                                       "and shadow-log reconciliation (reconcile_forever) is a callable, not "
                                       "confirmed running as a scheduled task in this process.",
        "NONE" if scanner_hook_wired else "LOW",
        "" if scanner_hook_wired else "Pass copilot_hook=live_scanner_copilot_hook(tools) when constructing "
                                       "LiveScanner, and start reconcile_forever() as a background task.",
    ))

    # ── Recent errors ──────────────────────────────────────────────
    errors = tools.get_recent_errors(limit=5)
    if errors.get("available"):
        n = len(errors.get("recent_error_lines", []))
        rows.append(DiagnosticRow("recent_errors", "DEGRADED" if n else "OK",
                                   f"{n} recent error log lines found." if n else "No recent errors.",
                                   f"{n} recent errors — see logs/errors.log." if n else "", "MEDIUM" if n else "NONE",
                                   "Review logs/errors.log." if n else ""))
    else:
        rows.append(DiagnosticRow("recent_errors", "UNKNOWN", "", errors.get("reason", ""), "LOW",
                                   "No error log found — this may be expected if none have occurred yet."))

    overall = "OK"
    if any(r.status == "ERROR" for r in rows):
        overall = "ERROR"
    elif any(r.status in ("DEGRADED", "UNKNOWN") for r in rows):
        overall = "DEGRADED"

    return {
        "available": True,
        "overall_status": overall,
        "checked_components": len(rows),
        "rows": [r.to_dict() for r in rows],
    }
