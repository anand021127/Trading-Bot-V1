#!/usr/bin/env python3
"""Historical Options Data Acquisition & Validation Pipeline.

Authoritative downloader and validator for Upstox Expired Options Historical Data.
Populates real_data/options_cache/ with verified option contract OHLCV candles
matching the exact schema expected by HistoricalOptionsDataLoader.

Key features:
1. Signal-based contract requirement discovery across all 6 major index underlyings.
2. Authoritative resolution via official Upstox Expired Instruments API.
3. Strict token priority (--token -> os.environ -> repository .env -> SQLite DB).
4. Safe token masking (never exposes secrets in console, logs, or error traces).
5. Strict multi-tier validation (OHLC bounds, spot-vs-premium integrity, timestamp ordering).
6. Atomic file persistence to prevent partial/corrupt files.
7. Resumable execution (skips already-cached and validated contracts unless --force is given).
8. Comprehensive post-run audit & diagnostic reporting.

Usage:
  # Download missing contracts for all 6 indices
  python3 scripts/download_historical_options.py --symbols NIFTY50,BANKNIFTY,FINNIFTY,MIDCPNIFTY,SENSEX,BANKEX

  # Explicit token override
  python3 scripts/download_historical_options.py --token <UPSTOX_TOKEN>

  # Dry-run / Discovery mode
  python3 scripts/download_historical_options.py --dry-run

  # Audit cache only
  python3 scripts/download_historical_options.py --audit-only
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.broker.token_resolver import (
    resolve_upstox_token,
    resolve_upstox_token_with_source,
    get_token_metadata,
    find_repo_dotenv_path,
    load_repo_dotenv,
    validate_token_live,
    decode_jwt_safe,
    token_fingerprint,
    persist_upstox_token,
    get_token_diagnostic_candidates,
)
from backend.broker.upstox_expired_options import (
    UpstoxExpiredOptionsClient,
    OptionsDataCache,
    OptionsDataValidator,
    UpstoxExpiredAPIError,
    DEFAULT_CACHE_DIR,
    INDEX_INSTRUMENT_KEYS,
    INDEX_STRIKE_INTERVALS,
)
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader, normalize_underlying
from backend.backtest.historical_data_io import load_dataset_safe
from backend.strategy.strategy_engine import MultiStrategyEngine
from backend.strategy.strategies.ema_trend import EMATrendStrategy
from backend.strategy.strategies.option_premium import OptionPremiumStrategy
from backend.indicators.ema import calculate_ema
from backend.indicators.choppiness import choppiness_index

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("download_historical_options")


@dataclass
class ContractRequirement:
    """Specification of an option contract required for backtesting."""
    underlying: str
    expiry: str
    strike: float
    option_type: str  # CE or PE
    from_date: str
    to_date: str
    interval: str = "5minute"
    signal_timestamps: List[str] = field(default_factory=list)
    spot_prices: List[float] = field(default_factory=list)

    @property
    def key(self) -> str:
        stk = int(self.strike) if self.strike.is_integer() else self.strike
        return f"{self.underlying.upper()}_{self.expiry}_{stk}_{self.option_type.upper()}_{self.interval}_{self.from_date}_{self.to_date}"

    @property
    def cache_filename(self) -> str:
        return OptionsDataCache.get_cache_filename(
            self.underlying,
            self.expiry,
            self.strike,
            self.option_type,
            self.interval,
            self.from_date,
            self.to_date,
        )


def build_trend_series(candles: List[Dict[str, Any]], ema_fast: int = 20, ema_slow: int = 50, ci_period: int = 14) -> Dict[str, str]:
    """Calculate underlying EMA trend series for strategy context."""
    if len(candles) < ema_slow:
        return {}
    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    ema_fast_vals = calculate_ema(closes, ema_fast)
    ema_slow_vals = calculate_ema(closes, ema_slow)
    ci_vals = choppiness_index(highs, lows, closes, ci_period)
    ci_offset = len(closes) - len(ci_vals)
    series: Dict[str, str] = {}
    for i in range(ema_slow - 1, len(closes)):
        ts = candles[i].get("timestamp")
        if not ts:
            continue
        ci_idx = i - ci_offset
        if 0 <= ci_idx < len(ci_vals) and ci_vals[ci_idx] > 61.8:
            series[ts] = "NEUTRAL"
        elif ema_fast_vals[i] > ema_slow_vals[i] and closes[i] > ema_fast_vals[i]:
            series[ts] = "BULLISH"
        elif ema_fast_vals[i] < ema_slow_vals[i]:
            series[ts] = "BEARISH"
        else:
            series[ts] = "NEUTRAL"
    return series


def discover_required_contracts_from_signals(
    symbols: Optional[List[str]] = None,
    data_dir: str = "real_data",
    interval: str = "5minute",
    client: Optional[UpstoxExpiredOptionsClient] = None,
    strategy_names: Optional[List[str]] = None,
) -> List[ContractRequirement]:
    """Scan spot datasets, evaluate strategy signal generator, and extract required option contracts."""
    data_files = sorted(glob.glob(os.path.join(data_dir, "*_2024_5min.json")))
    if not data_files:
        logger.warning("No spot candle datasets found in %s", data_dir)
        return []

    target_symbols = [s.upper() for s in symbols] if symbols else list(INDEX_STRIKE_INTERVALS.keys())
    
    available_strategies = [OptionPremiumStrategy(), EMATrendStrategy()]
    if strategy_names:
        selected_strategies = [s for s in available_strategies if s.name in [n.upper() for n in strategy_names]]
    else:
        selected_strategies = [OptionPremiumStrategy()]
    strategy_engine = MultiStrategyEngine(selected_strategies)
    
    requirements_map: Dict[str, ContractRequirement] = {}

    for file_path in data_files:
        sym = os.path.basename(file_path).replace("_2024_5min.json", "").upper()
        if sym not in target_symbols:
            continue

        try:
            candles = load_dataset_safe(file_path, auto_repair=True)
        except Exception as e:
            logger.warning("Could not load spot dataset %s: %s", file_path, e)
            continue

        if not candles:
            continue

        step = INDEX_STRIKE_INTERVALS.get(sym, 50.0)
        trend_series = build_trend_series(candles)

        # Pre-fetch expiries once per symbol if client is active
        symbol_expiries = []
        if client:
            try:
                symbol_expiries = client.get_expiries(sym)
            except Exception:
                symbol_expiries = []

        logger.info("Scanning %s (%d spot candles) for strategy signals...", sym, len(candles))
        
        # Scan window (60 bars warmup for EMA50/RSI14/ATR14 indicators)
        for i in range(55, len(candles)):
            window = candles[max(0, i + 1 - 60): i + 1]
            curr_bar = window[-1]
            ts = curr_bar.get("timestamp", "")
            spot_price = float(curr_bar.get("close", 0.0))
            if not ts or spot_price <= 0:
                continue

            bar_date = ts[:10]
            trend = trend_series.get(ts, "NEUTRAL")
            bar_context = {
                "symbol": sym,
                "spot_price": spot_price,
                "current_bar_date": bar_date,
                "evaluation_date": bar_date,
                "underlying_trend": trend,
            }

            signals = strategy_engine.evaluate(sym, window, context=bar_context)
            for sig in signals:
                sig_val = getattr(sig, "signal", None)
                if isinstance(sig_val, str) and sig_val in ("BUY", "SELL"):
                    opt_type = sig.indicators.get("option_type") or sig.indicators.get("directional_intent") or ("CE" if sig_val == "BUY" else "PE")
                    atm_strike = float(round(spot_price / step) * step)
                    target_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                    
                    # Determine expiry date from pre-fetched list (enforce <= 10 day proximity)
                    expiry_date_str = ""
                    if symbol_expiries:
                        max_expiry_date = (target_dt + timedelta(days=10)).isoformat()
                        future_exp = [
                            e for e in symbol_expiries
                            if target_dt.isoformat() <= e <= max_expiry_date
                        ]
                        if future_exp:
                            expiry_date_str = future_exp[0]

                    if not expiry_date_str:
                        # Fallback calculation using calendar weekly expiry date
                        from backend.backtest.historical_contract_resolver import get_nearest_expiry_for_date
                        expiry_dt = get_nearest_expiry_for_date(sym, target_dt)
                        expiry_date_str = expiry_dt.isoformat()

                    from_date = target_dt.isoformat()
                    to_date = expiry_date_str

                    req_key = f"{sym}_{expiry_date_str}_{int(atm_strike)}_{opt_type}_{interval}_{from_date}_{to_date}"
                    if req_key not in requirements_map:
                        requirements_map[req_key] = ContractRequirement(
                            underlying=sym,
                            expiry=expiry_date_str,
                            strike=atm_strike,
                            option_type=opt_type,
                            from_date=from_date,
                            to_date=to_date,
                            interval=interval,
                            signal_timestamps=[ts],
                            spot_prices=[spot_price],
                        )
                    else:
                        requirements_map[req_key].signal_timestamps.append(ts)
                        requirements_map[req_key].spot_prices.append(spot_price)

    return sorted(
        requirements_map.values(),
        key=lambda r: (r.underlying, r.expiry, r.strike, r.option_type),
    )


class HistoricalOptionsIngestionPipeline:
    """Robust acquisition and validation pipeline for historical expired option datasets."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        cache_dir: str = DEFAULT_CACHE_DIR,
        dotenv_path: Optional[str] = None,
        rate_limit_delay: float = 0.25,
    ) -> None:
        from backend.broker.token_resolver import token_fingerprint, get_token_source
        self.cache_dir = cache_dir
        self.cache = OptionsDataCache(cache_dir=cache_dir)
        self.validator = OptionsDataValidator()
        self.rate_limit_delay = rate_limit_delay
        self.dotenv_path = dotenv_path
        self.client = UpstoxExpiredOptionsClient(
            access_token=access_token,
            cache_dir=cache_dir,
            dotenv_path=dotenv_path,
        )
        src = get_token_source(access_token, dotenv_path=dotenv_path)
        fp = token_fingerprint(self.client.access_token)
        logger.info(
            "[Token Diagnostic] Pipeline init: client_token_source=%s, length=%d, fingerprint=%s",
            src, len(self.client.access_token), fp,
        )

    def test_auth(self) -> Dict[str, Any]:
        """Check if Upstox token is valid and expired instruments API is accessible."""
        return self.client.test_access()

    def check_cache_status(self, requirements: List[ContractRequirement]) -> Tuple[List[ContractRequirement], List[ContractRequirement]]:
        """Separate requirements into already cached and missing."""
        cached_reqs: List[ContractRequirement] = []
        missing_reqs: List[ContractRequirement] = []

        for req in requirements:
            filename = req.cache_filename
            cached_data = self.cache.get(filename)
            if cached_data and "candles" in cached_data and len(cached_data["candles"]) > 0:
                cached_reqs.append(req)
            else:
                missing_reqs.append(req)

        return cached_reqs, missing_reqs

    def download_requirement(
        self,
        req: ContractRequirement,
        force: bool = False,
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Fetch, validate, and atomically save a single contract dataset."""
        filename = req.cache_filename

        if not force:
            cached_data = self.cache.get(filename)
            if cached_data and "candles" in cached_data and len(cached_data["candles"]) > 0:
                return True, None, cached_data

        ref_spot = req.spot_prices[0] if req.spot_prices else None
        
        # Call client fetch and cache
        success, err, data = self.client.fetch_and_cache_contract(
            underlying=req.underlying,
            expiry=req.expiry,
            strike=req.strike,
            option_type=req.option_type,
            interval=req.interval,
            from_date=req.from_date,
            to_date=req.to_date,
            spot_price_ref=ref_spot,
        )

        return success, err, data

    def run_ingestion(
        self,
        requirements: List[ContractRequirement],
        force: bool = False,
        dry_run: bool = False,
        limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Execute full ingestion pipeline with progress reporting and error handling."""
        cached_reqs, missing_reqs = self.check_cache_status(requirements)
        
        summary: Dict[str, Any] = {
            "total_required": len(requirements),
            "already_cached": len(cached_reqs),
            "to_download": len(missing_reqs) if not force else len(requirements),
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
            "auth_error": None,
            "error_categories": {
                "AUTH_INVALID_TOKEN": 0,
                "PERMISSION_DENIED": 0,
                "DATA_UNAVAILABLE": 0,
                "RATE_LIMITED": 0,
                "VALIDATION_FAILED": 0,
                "OTHER": 0,
            },
        }

        if dry_run:
            logger.info("=== DRY RUN SUMMARY ===")
            logger.info("Total contracts required: %d", summary["total_required"])
            logger.info("Already cached in %s: %d", self.cache_dir, summary["already_cached"])
            logger.info("Missing contracts needing download: %d", summary["to_download"])
            return summary

        # Check authentication first
        auth = self.test_auth()
        logger.info(
            "[Token Diagnostic] Pipeline test_auth: accessible=%s, valid=%s, profile_status=%s, expired_status=%s, error_code=%s, fingerprint=%s",
            auth.get("accessible"),
            auth.get("valid"),
            auth.get("profile_status"),
            auth.get("expired_instruments_status"),
            auth.get("error_code"),
            auth.get("token_fingerprint"),
        )
        is_accessible = bool(
            auth.get("accessible")
            or (
                auth.get("valid") is True
                and auth.get("profile_verified") is True
                and auth.get("expired_instruments_entitled") is True
            )
        )
        if not is_accessible:
            err_code = auth.get("error_code") or "AUTH_INVALID_TOKEN"
            err_msg = auth.get("error_message") or auth.get("message") or "Authentication or Expired Derivatives entitlement check failed."
            req_perm = auth.get("required_permission") or "Active Upstox Access Token with Plus Plan entitlement"
            auth_msg = (
                f"Upstox API Access Error ({err_code}): {err_msg}\n"
                f"Action Required: {req_perm}\n"
                f"To refresh your token:\n"
                f"  1. Log into your Upstox Developer App console.\n"
                f"  2. Complete OAuth login to generate a fresh access token.\n"
                f"  3. Set UPSTOX_ACCESS_TOKEN=<your_token> in .env or pass via --token <token>."
            )
            logger.error(auth_msg)
            summary["auth_error"] = auth
            err_code_str = str(err_code)
            if err_code_str in summary["error_categories"]:
                summary["error_categories"][err_code_str] += len(missing_reqs)
            else:
                summary["error_categories"]["OTHER"] += len(missing_reqs)
            return summary

        targets = requirements if force else missing_reqs
        if limit:
            targets = targets[:limit]

        logger.info("Starting ingestion of %d contracts...", len(targets))
        
        for idx, req in enumerate(targets, 1):
            logger.info(
                "[%d/%d] Ingesting %s %s Strike %.1f %s (%s to %s)...",
                idx, len(targets), req.underlying, req.expiry, req.strike, req.option_type, req.from_date, req.to_date,
            )

            success, err, data = self.download_requirement(req, force=force)
            if success:
                summary["downloaded"] += 1
                candles_cnt = len(data.get("candles", [])) if data else 0
                logger.info("  -> SUCCESS: %d candles validated & saved to cache", candles_cnt)
            else:
                summary["failed"] += 1
                err_str = str(err)
                err_record = {
                    "contract": req.key,
                    "underlying": req.underlying,
                    "expiry": req.expiry,
                    "strike": req.strike,
                    "option_type": req.option_type,
                    "from_date": req.from_date,
                    "to_date": req.to_date,
                    "error": err_str,
                }
                summary["errors"].append(err_record)
                
                # Categorize error
                if "401" in err_str or "AUTH" in err_str:
                    summary["error_categories"]["AUTH_INVALID_TOKEN"] += 1
                elif "403" in err_str or "PERMISSION" in err_str:
                    summary["error_categories"]["PERMISSION_DENIED"] += 1
                elif "DATA_UNAVAILABLE" in err_str or "404" in err_str or "not found" in err_str.lower():
                    summary["error_categories"]["DATA_UNAVAILABLE"] += 1
                elif "429" in err_str or "rate limit" in err_str.lower():
                    summary["error_categories"]["RATE_LIMITED"] += 1
                elif "validation" in err_str.lower():
                    summary["error_categories"]["VALIDATION_FAILED"] += 1
                else:
                    summary["error_categories"]["OTHER"] += 1

                logger.warning("  -> FAILED: %s", err_str)

            time.sleep(self.rate_limit_delay)

        return summary


def run_cache_audit(
    cache_dir: str = DEFAULT_CACHE_DIR,
    discovered_reqs: Optional[List[ContractRequirement]] = None,
) -> Dict[str, Any]:
    """Audit all option cache files on disk, validate integrity, and compile statistical summary."""
    validator = OptionsDataValidator()
    cache_files = sorted(glob.glob(os.path.join(cache_dir, "*.json")))
    
    total_files = len(cache_files)
    valid_files = 0
    invalid_files = 0
    invalid_details = []
    
    underlying_counts: Dict[str, int] = {}
    ce_pe_counts: Dict[str, int] = {"CE": 0, "PE": 0}
    total_candles = 0
    
    earliest_candle: Optional[str] = None
    latest_candle: Optional[str] = None
    
    for f in cache_files:
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            
            if not isinstance(data, dict) or "contract" not in data or "candles" not in data:
                invalid_files += 1
                invalid_details.append({"file": os.path.basename(f), "error": "Missing contract/candles schema"})
                continue

            c_info = data["contract"]
            candles = data["candles"]
            
            if not candles:
                invalid_files += 1
                invalid_details.append({"file": os.path.basename(f), "error": "Candles array is empty"})
                continue

            # Validate candles
            is_valid, err, _ = validator.validate_candles(candles, expected_instrument_key=c_info.get("instrument_key", ""))
            if not is_valid:
                invalid_files += 1
                invalid_details.append({"file": os.path.basename(f), "error": err})
                continue

            valid_files += 1
            und = normalize_underlying(c_info.get("underlying", "UNKNOWN"))
            underlying_counts[und] = underlying_counts.get(und, 0) + 1
            
            opt_type = c_info.get("option_type", "CE").upper()
            ce_pe_counts[opt_type] = ce_pe_counts.get(opt_type, 0) + 1
            
            total_candles += len(candles)
            
            for c in candles:
                ts = c.get("timestamp")
                if ts:
                    if earliest_candle is None or ts < earliest_candle:
                        earliest_candle = ts
                    if latest_candle is None or ts > latest_candle:
                        latest_candle = ts
        except Exception as e:
            invalid_files += 1
            invalid_details.append({"file": os.path.basename(f), "error": str(e)})

    # Check against discovered requirements if provided
    missing_count = 0
    if discovered_reqs:
        for r in discovered_reqs:
            cf = os.path.join(cache_dir, r.cache_filename)
            if not os.path.exists(cf):
                missing_count += 1

    return {
        "total_cache_files": total_files,
        "valid_cache_files": valid_files,
        "invalid_cache_files": invalid_files,
        "invalid_details": invalid_details,
        "missing_contracts": missing_count,
        "contracts_by_underlying": underlying_counts,
        "contracts_by_type": ce_pe_counts,
        "earliest_option_candle": earliest_candle or "None",
        "latest_option_candle": latest_candle or "None",
        "total_option_candles": total_candles,
    }


def main():
    parser = argparse.ArgumentParser(description="Authoritative Upstox Historical Options Ingestion & Validation Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Perform discovery and cache audit without calling download APIs")
    parser.add_argument("--force", action="store_true", help="Force re-download and re-validate existing cached contracts")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated list of symbols (e.g. NIFTY50,BANKNIFTY,FINNIFTY,MIDCPNIFTY,SENSEX,BANKEX)")
    parser.add_argument("--data-dir", type=str, default="real_data", help="Directory containing underlying spot JSON files")
    parser.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE_DIR, help="Directory to store option contract JSON cache")
    parser.add_argument("--token", type=str, default=None, help="Upstox Access Token explicit override (--token)")
    parser.add_argument("--env-file", type=str, default=None, help="Explicit path to .env file")
    parser.add_argument("--limit", type=int, default=None, help="Max number of contracts to process")
    parser.add_argument("--interval", type=str, default="5minute", help="Candle interval (e.g. 5minute, 1minute)")
    parser.add_argument("--strategies", type=str, default=None, help="Comma-separated strategy names (e.g. OPTION_PREMIUM,EMA_TREND)")
    parser.add_argument("--audit-only", action="store_true", help="Only run cache audit and exit")

    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    strategies = [s.strip().upper() for s in args.strategies.split(",")] if args.strategies else None
    
    # 0. Load dotenv & authoritative token resolution
    load_repo_dotenv(args.env_file)
    candidate_token, token_src = resolve_upstox_token_with_source(
        explicit_token=args.token,
        dotenv_path=args.env_file,
        require_valid=True,
    )

    # Perform ONE live auth check if candidate token is found
    val_result: Dict[str, Any] = {}
    if candidate_token:
        val_result = validate_token_live(token=candidate_token, dotenv_path=args.env_file)
        jwt_meta = decode_jwt_safe(candidate_token)
        fp = token_fingerprint(candidate_token)
        tok_len = len(candidate_token)
        iat_iso = jwt_meta.get("issued_at_iso") or "N/A"
        exp_iso = jwt_meta.get("expires_at_iso") or "N/A"
        is_exp = "yes" if jwt_meta.get("is_expired") else "no"
        is_plus = "true" if (val_result.get("expired_instruments_entitled") or jwt_meta.get("isPlusPlan")) else "false"
        prof_stat = val_result.get("profile_status")
        prof_ok = val_result.get("profile_verified", False)
        prof_str = f"verified ({prof_stat})" if prof_ok else f"failed ({prof_stat})"
        exp_stat = val_result.get("expired_instruments_status")
        exp_ok = val_result.get("expired_instruments_entitled", False)
        exp_str = f"entitled ({exp_stat})" if exp_ok else f"not entitled ({exp_stat})"
    else:
        diag_candidates = get_token_diagnostic_candidates(explicit_token=args.token, dotenv_path=args.env_file)
        best_diag = diag_candidates[0] if diag_candidates else {}
        token_src = best_diag.get("source", "none")
        fp = best_diag.get("fingerprint", "NONE")
        tok_len = best_diag.get("length", 0)
        iat_iso = best_diag.get("issued_at_iso") or "N/A"
        exp_iso = best_diag.get("expires_at_iso") or "N/A"
        is_exp = "yes" if best_diag.get("is_expired") else "no"
        is_plus = "true" if best_diag.get("isPlusPlan") else "false"
        prof_str = "failed (no active token)"
        exp_str = "not entitled (no active token)"
        prof_ok = False
        exp_ok = False

    # Requirement D: Print ONLY the 9 specified lines at downloader startup
    print(f"Token source                    : {token_src}")
    print(f"Token fingerprint               : {fp}")
    print(f"Token length                    : {tok_len}")
    print(f"JWT issued-at                   : {iat_iso}")
    print(f"JWT expiration                  : {exp_iso}")
    print(f"JWT expired yes/no              : {is_exp}")
    print(f"isPlusPlan                      : {is_plus}")
    print(f"Live profile verification       : {prof_str}")
    print(f"Expired-instruments entitlement : {exp_str}")

    # Requirement F: If no verified token exists or auth/entitlement failed, abort
    if not (candidate_token and prof_ok and exp_ok):
        print("\nNO VERIFIED ACTIVE UPSTOX TOKEN AVAILABLE")
        print("Please complete OAuth from the application Settings to generate and verify an active Upstox access token.")
        print("Do NOT fall back to stale SQLite/.env/JSON tokens.")
        sys.exit(1)

    # Immutable verified token for the entire ingestion run
    immutable_token = candidate_token
    persist_upstox_token(
        immutable_token,
        {"user_name": val_result.get("user_name", ""), "user_id": val_result.get("user_id", "")},
        verification_info={"verified": True, "source": token_src, "is_plus_plan": True},
    )

    if args.audit_only:
        print("\n[Audit Mode] Running audit on cache directory...")
        audit = run_cache_audit(cache_dir=args.cache_dir)
        print(json.dumps(audit, indent=2))
        return

    pipeline = HistoricalOptionsIngestionPipeline(
        access_token=immutable_token,
        cache_dir=args.cache_dir,
        dotenv_path=args.env_file,
    )

    # 1. Discover requirements
    print("\n[Phase 1] Discovering required option contracts from spot signals...")
    reqs = discover_required_contracts_from_signals(
        symbols=symbols,
        data_dir=args.data_dir,
        interval=args.interval,
        client=pipeline.client,
        strategy_names=strategies,
    )
    print(f"Discovered {len(reqs)} unique historical option contracts required for backtests.")

    # 2. Group by underlying and display breakdown
    underlying_counts: Dict[str, int] = {}
    for r in reqs:
        underlying_counts[r.underlying] = underlying_counts.get(r.underlying, 0) + 1
    for u, count in sorted(underlying_counts.items()):
        print(f"  - {u:12s}: {count:3d} contracts")

    # 3. Check Cache
    cached, missing = pipeline.check_cache_status(reqs)
    print(f"\n[Phase 2] Local Cache Audit ({args.cache_dir}):")
    print(f"  - Already Cached & Validated: {len(cached)}")
    print(f"  - Missing / Needing Fetch   : {len(missing)}")

    if args.dry_run:
        print("\n[Phase 3] Dry-Run Mode Active: Skipping API queries.")
        print(f"To acquire the missing {len(missing)} contracts:")
        print(f"  1. Ensure your UPSTOX_ACCESS_TOKEN is active and valid.")
        print(f"  2. Run: python3 scripts/download_historical_options.py --symbols NIFTY50,BANKNIFTY,FINNIFTY,MIDCPNIFTY,SENSEX,BANKEX")
        sys.exit(0)

    # 4. Ingestion
    print("\n[Phase 3] Executing Acquisition & Validation Pipeline...")
    res = pipeline.run_ingestion(
        requirements=reqs,
        force=args.force,
        dry_run=False,
        limit=args.limit,
    )

    print("\n" + "=" * 75)
    print(" INGESTION RUN SUMMARY")
    print("=" * 75)
    print(f"Total Required    : {res['total_required']}")
    print(f"Already In Cache  : {res['already_cached']}")
    print(f"Attempted Fetch   : {res['to_download']}")
    print(f"Successfully Saved: {res['downloaded']}")
    print(f"Failed / Missing  : {res['failed']}")
    if res.get("error_categories"):
        print("\nFailure Categorization:")
        for cat, cnt in res["error_categories"].items():
            if cnt > 0:
                print(f"  - {cat:20s}: {cnt:3d}")

    # 5. Run Post-Download Audit
    print("\n" + "=" * 75)
    print(" POST-INGESTION CACHE VALIDATION & AUDIT")
    print("=" * 75)
    audit = run_cache_audit(cache_dir=args.cache_dir, discovered_reqs=reqs)
    print(f"Total Cache Files      : {audit['total_cache_files']}")
    print(f"Valid Cache Files      : {audit['valid_cache_files']}")
    print(f"Invalid Cache Files    : {audit['invalid_cache_files']}")
    print(f"Missing Contracts      : {audit['missing_contracts']}")
    print(f"Earliest Option Candle : {audit['earliest_option_candle']}")
    print(f"Latest Option Candle   : {audit['latest_option_candle']}")
    print(f"Total Option Candles   : {audit['total_option_candles']}")
    print("\nContracts by Underlying:")
    for u, count in sorted(audit["contracts_by_underlying"].items()):
        print(f"  - {u:12s}: {count:3d} contracts")
    print("\nContracts by Option Type:")
    for t, count in sorted(audit["contracts_by_type"].items()):
        print(f"  - {t:6s}: {count:3d} contracts")

    if res.get("auth_error"):
        print("\n[!] AUTHENTICATION ERROR DETECTED:")
        print(f"  Code   : {res['auth_error'].get('error_code')}")
        print(f"  Message: {res['auth_error'].get('error_message')}")
        print(f"  Action : {res['auth_error'].get('required_permission')}")
        sys.exit(1)

    if res["failed"] > 0:
        print(f"\n[!] Completed with {res['failed']} unavailable/failed contracts.")
        sys.exit(2)

    print("\n[✓] Ingestion & validation completed successfully.")


if __name__ == "__main__":
    main()
