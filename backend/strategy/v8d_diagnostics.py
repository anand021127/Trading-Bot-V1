"""Read-only explanation of a V8-D underlying evaluation.

Purpose: when V8-D says NO_SIGNAL the operator must be able to see EXACTLY which
condition failed, with the real numbers (EMA20, EMA50, separation, RSI, ATR,
price, pullback band, reversal test) for BOTH the CE and the PE side.

Guarantees
----------
* NOT a second strategy. It uses the very same indicator functions the strategy
  uses (looked up on the strategy MODULE, so a patched/changed indicator affects
  both identically) and the strategy instance's own parameters
  (ema_fast / ema_slow / rsi_period / min_candles).
* The boolean expressions mirror ``V8DStrategy.detect_pullback_signal`` one for
  one. ``backend/tests/test_v8d_diagnostics.py`` asserts, over every window of a
  year of REAL 5-minute candles for all six symbols, that this module's decision
  and every indicator value equal the strategy's own output — any future drift
  fails that test.
* It never places orders, never mutates the strategy, never invents data. If a
  value cannot be computed it is ``None`` and the reason is stated.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.strategy.strategies import v8d_strategy as _v8d

# Mirrors of the literals inside V8DStrategy.detect_pullback_signal. Kept in ONE
# place so the UI can show the thresholds; test_v8d_diagnostics.py proves they
# still match the strategy's decisions.
SEPARATION_MIN = 0.0015        # |EMA20-EMA50| / EMA50 must exceed 0.15 %
CE_BAND = (0.985, 1.003)       # low must sit within [EMA20*0.985, EMA20*1.003]
PE_BAND = (0.997, 1.015)       # high must sit within [EMA20*0.997, EMA20*1.015]
CE_RSI = (45.0, 62.0)
PE_RSI = (38.0, 55.0)
BODY_STRENGTH = 0.55           # body / range must exceed 55 % for the EMA20-close reversal path


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _cond(ok: bool, detail: str, **vals: Any) -> Dict[str, Any]:
    return {"pass": bool(ok), "detail": detail, **vals}


def explain_pullback(candles: List[Dict[str, Any]], strategy: Any = None) -> Dict[str, Any]:
    """Explain one V8-D underlying evaluation on ``candles`` (oldest → newest;
    the LAST candle is the one being evaluated)."""
    strat = strategy if strategy is not None else _v8d.V8DStrategy()
    min_candles = int(getattr(strat, "min_candles", 50))
    n = len(candles or [])
    out: Dict[str, Any] = {
        "evaluated": False, "candle_count": n, "min_candles": min_candles,
        "decision": None, "decision_label": "NO_SIGNAL",
        "thresholds": {
            "ema_separation_min_pct": SEPARATION_MIN * 100,
            "ce_pullback_band": list(CE_BAND), "pe_pullback_band": list(PE_BAND),
            "ce_rsi_zone": list(CE_RSI), "pe_rsi_zone": list(PE_RSI),
            "reversal_body_strength": BODY_STRENGTH,
        },
    }
    if n < min_candles:
        out["reason"] = f"insufficient_candles:{n}<{min_candles}"
        out["failed"] = ["sufficient_candles"]
        return out

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    opens = [float(c["open"]) for c in candles]

    ema20_s = _v8d.calculate_ema(closes, strat.ema_fast)
    ema50_s = _v8d.calculate_ema(closes, strat.ema_slow)
    rsi_s = _v8d.calculate_rsi(closes, strat.rsi_period)
    c_, o_, h_, l_ = closes[-1], opens[-1], highs[-1], lows[-1]
    e20, e50, rsi = ema20_s[-1], ema50_s[-1], rsi_s[-1]
    ph, pl = highs[-2], lows[-2]
    sep_pct = (e20 - e50) / e50 * 100 if e50 else None
    rng = max(h_ - l_, 1e-6)

    atr_v: Optional[float] = None
    try:  # informational ONLY — V8-D entry does not use the underlying ATR (stops use option ATR)
        atr_s = _v8d.calculate_atr(highs, lows, closes, 14)
        atr_v = float(atr_s[-1]) if atr_s else None
    except Exception:
        atr_v = None

    out.update({
        "evaluated": True,
        "price": {"close": c_, "open": o_, "high": h_, "low": l_, "prev_high": ph, "prev_low": pl},
        "ema20": e20, "ema50": e50, "ema_separation_pct": sep_pct,
        "rsi": rsi, "atr14_underlying": atr_v,
        "atr_note": "informational: not an entry condition (V8-D stop sizing uses the option's ATR)",
        "pullback_band": {
            "ce": [e20 * CE_BAND[0], e20 * CE_BAND[1]],
            "pe": [e20 * PE_BAND[0], e20 * PE_BAND[1]],
        },
    })

    # ── CE (bullish) — expressions mirror detect_pullback_signal exactly ────
    ce_t1 = e20 > e50
    ce_t2 = c_ > e50
    ce_t3 = ((e20 - e50) / e50 > SEPARATION_MIN)
    ce_trend = ce_t1 and ce_t2 and ce_t3
    ce_pull = (l_ <= e20 * CE_BAND[1]) and (l_ >= e20 * CE_BAND[0])
    ce_rsi = CE_RSI[0] <= rsi <= CE_RSI[1]
    ce_green = c_ > o_
    ce_break = c_ > ph
    ce_body = (c_ >= e20) and ((c_ - o_) / rng > BODY_STRENGTH)
    ce_rev = ce_green and (ce_break or ce_body)
    # ── PE (bearish) ─────────────────────────────────────────────────────────
    pe_t1 = e20 < e50
    pe_t2 = c_ < e50
    pe_t3 = ((e50 - e20) / e50 > SEPARATION_MIN)
    pe_trend = pe_t1 and pe_t2 and pe_t3
    pe_pull = (h_ >= e20 * PE_BAND[0]) and (h_ <= e20 * PE_BAND[1])
    pe_rsi = PE_RSI[0] <= rsi <= PE_RSI[1]
    pe_red = c_ < o_
    pe_break = c_ < pl
    pe_body = (c_ <= e20) and ((o_ - c_) / rng > BODY_STRENGTH)
    pe_rev = pe_red and (pe_break or pe_body)

    out["ce"] = {
        "trend": _cond(ce_trend,
                       f"EMA20 {e20:.2f} {'>' if ce_t1 else '<='} EMA50 {e50:.2f}; close {c_:.2f} {'>' if ce_t2 else '<='} EMA50; "
                       f"separation {sep_pct:+.3f}% (needs > +{SEPARATION_MIN*100:.2f}%)",
                       ema20_above_ema50=ce_t1, close_above_ema50=ce_t2, separation_ok=ce_t3),
        "pullback": _cond(ce_pull,
                          f"low {l_:.2f} vs band [{e20*CE_BAND[0]:.2f}, {e20*CE_BAND[1]:.2f}] around EMA20"),
        "rsi": _cond(ce_rsi, f"RSI {rsi:.2f} (CE zone {CE_RSI[0]:.0f}–{CE_RSI[1]:.0f})"),
        "reversal": _cond(ce_rev,
                          f"green candle: {'yes' if ce_green else 'no'}; close>prev high ({ph:.2f}): {'yes' if ce_break else 'no'}; "
                          f"or close>=EMA20 with body>{BODY_STRENGTH*100:.0f}% of range: {'yes' if ce_body else 'no'}",
                          green=ce_green, close_above_prev_high=ce_break, strong_body_above_ema20=ce_body),
    }
    out["pe"] = {
        "trend": _cond(pe_trend,
                       f"EMA20 {e20:.2f} {'<' if pe_t1 else '>='} EMA50 {e50:.2f}; close {c_:.2f} {'<' if pe_t2 else '>='} EMA50; "
                       f"separation {sep_pct:+.3f}% (needs < -{SEPARATION_MIN*100:.2f}%)",
                       ema20_below_ema50=pe_t1, close_below_ema50=pe_t2, separation_ok=pe_t3),
        "pullback": _cond(pe_pull,
                          f"high {h_:.2f} vs band [{e20*PE_BAND[0]:.2f}, {e20*PE_BAND[1]:.2f}] around EMA20"),
        "rsi": _cond(pe_rsi, f"RSI {rsi:.2f} (PE zone {PE_RSI[0]:.0f}–{PE_RSI[1]:.0f})"),
        "reversal": _cond(pe_rev,
                          f"red candle: {'yes' if pe_red else 'no'}; close<prev low ({pl:.2f}): {'yes' if pe_break else 'no'}; "
                          f"or close<=EMA20 with body>{BODY_STRENGTH*100:.0f}% of range: {'yes' if pe_body else 'no'}",
                          red=pe_red, close_below_prev_low=pe_break, strong_body_below_ema20=pe_body),
    }
    for side in ("ce", "pe"):
        s = out[side]
        s["all_pass"] = all(s[k]["pass"] for k in ("trend", "pullback", "rsi", "reversal"))
        s["failed"] = [k for k in ("trend", "pullback", "rsi", "reversal") if not s[k]["pass"]]

    decision: Optional[str] = "CE" if out["ce"]["all_pass"] else ("PE" if out["pe"]["all_pass"] else None)
    out["decision"] = decision
    out["decision_label"] = f"BUY_{decision}" if decision else "NO_SIGNAL"
    if decision:
        out["failed"] = []
    else:
        # The side that is CLOSER to a setup (fewer failed conditions); ties → the side the trend favours.
        ce_f, pe_f = out["ce"]["failed"], out["pe"]["failed"]
        side = "ce" if (len(ce_f), 0 if e20 >= e50 else 1) <= (len(pe_f), 0 if e20 < e50 else 1) else "pe"
        out["closest_side"] = side.upper()
        out["failed"] = list(out[side]["failed"])
        out["binding_condition"] = out["failed"][0]
        out["reason"] = (f"{side.upper()} side failed: " +
                         "; ".join(f"{k}: {out[side][k]['detail']}" for k in out["failed"]))
    return out


def explain_chain(option_chain: Optional[List[Dict[str, Any]]], spot: float, underlying: str,
                  strategy: Any = None) -> Dict[str, Any]:
    """Option-chain / ATM-contract availability for BOTH sides (informational).
    Uses the strategy's own ``get_atm_strike``; exact-ATM lookup only (the
    strategy additionally accepts the nearest same-side strike within one step)."""
    strat = strategy if strategy is not None else _v8d.V8DStrategy()
    chain = option_chain or []
    out: Dict[str, Any] = {"option_chain_count": len(chain), "spot": spot, "atm_strike": None,
                           "ce": None, "pe": None}
    if not chain or not spot:
        out["reason"] = "empty_option_chain" if not chain else "no_spot"
        return out
    atm = strat.get_atm_strike(spot, underlying)
    out["atm_strike"] = atm
    for side in ("CE", "PE"):
        hit = None
        for c in chain:
            ot = str(c.get("option_type") or c.get("instrument_type") or "").upper()
            ot = "CE" if "CALL" in ot else "PE" if "PUT" in ot else ot
            if ot != side:
                continue
            sk = _f(c.get("strike"))
            if sk is not None and abs(sk - float(atm)) < 0.01 and c.get("instrument_key"):
                hit = c
                break
        out[side.lower()] = None if hit is None else {
            "instrument_key": hit.get("instrument_key"), "strike": _f(hit.get("strike")),
            "premium_ltp": _f(hit.get("ltp")), "lot_size": hit.get("lot_size"),
            "option_atr": _f(hit.get("option_atr") or hit.get("atr")),
        }
    return out
