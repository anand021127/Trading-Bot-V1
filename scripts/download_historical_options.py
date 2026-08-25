#!/usr/bin/env python3
"""Historical Options Data Acquisition Pipeline.

Authoritative downloader and validator for Upstox Expired Options Historical Data.
Populates real_data/options_cache/ with verified option contract OHLCV candles
matching the exact schema expected by HistoricalOptionsDataLoader.

Key features:
1. Signal-based and grid-based contract requirement discovery (dry-run mode).
2. Authoritative resolution via official Upstox Expired Instruments API.
3. Strict multi-tier validation (OHLC validity, spot-vs-premium checks, timestamp ordering).
4. Atomic file persistence to prevent partial/corrupt files.
5. Resumable execution (skips already-cached and validated contracts).
6. Graceful token expiration / auth error handling with actionable remediation steps.

Usage:
  # Dry-run / Discovery of required contracts for 2024 backtests
  python3 scripts/download_historical_options.py --dry-run

  # Download missing contracts for specific symbols
  python3 scripts/download_historical_options.py --symbols NIFTY50,BANKNIFTY

  # Force re-download and re-validate all contracts
  python3 scripts/download_historical_options.py --force
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
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.broker.upstox_expired_options import (
    UpstoxExpiredOptionsClient,
    OptionsDataCache,
    OptionsDataValidator,
    UpstoxExpiredAPIError,
    DEFAULT_CACHE_DIR,
    INDEX_INSTRUMENT_KEYS,
    INDEX_STRIKE_INTERVALS,
)
from backend.broker.token_resolver import resolve_upstox_token
from backend.backtest.options_data_layer import HistoricalOptionsDataLoader
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

        # Pre-fetch expiries once per symbol
        symbol_expiries = []
        if client:
            try:
                symbol_expiries = client.get_expiries(sym)
            except Exception:
                symbol_expiries = []

        logger.info("Scanning %s (%d spot candles) for signals...", sym, len(candles))
        
        # Scan window (60 bars is optimal for EMA50/RSI14/ATR14 indicators)
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
                    
                    # Determine expiry date from pre-fetched list
                    expiry_date_str = ""
                    if symbol_expiries:
                        future_exp = [e for e in symbol_expiries if e >= target_dt.isoformat()]
                        if future_exp:
                            expiry_date_str = future_exp[0]

                    if not expiry_date_str:
                        # Fallback calculation using standard expiry weekday
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
        rate_limit_delay: float = 0.25,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache = OptionsDataCache(cache_dir=cache_dir)
        self.validator = OptionsDataValidator()
        self.rate_limit_delay = rate_limit_delay
        self.client = UpstoxExpiredOptionsClient(
            access_token=access_token,
            cache_dir=cache_dir,
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
        
        summary = {
            "total_required": len(requirements),
            "already_cached": len(cached_reqs),
            "to_download": len(missing_reqs) if not force else len(requirements),
            "downloaded": 0,
            "failed": 0,
            "skipped": 0,
            "errors": [],
            "auth_error": None,
        }

        if dry_run:
            logger.info("=== DRY RUN SUMMARY ===")
            logger.info("Total contracts required: %d", summary["total_required"])
            logger.info("Already cached in %s: %d", self.cache_dir, summary["already_cached"])
            logger.info("Missing contracts needing download: %d", summary["to_download"])
            return summary

        # Check authentication first
        auth = self.test_auth()
        if not auth["accessible"]:
            auth_msg = (
                f"Upstox API Access Error ({auth.get('error_code')}): {auth.get('error_message')}\n"
                f"Action Required: {auth.get('required_permission')}\n"
                f"To refresh your token:\n"
                f"  1. Log into your Upstox Developer App console.\n"
                f"  2. Complete OAuth login to generate a fresh access token.\n"
                f"  3. Set UPSTOX_ACCESS_TOKEN=<your_token> in .env or pass via --token <token>."
            )
            logger.error(auth_msg)
            summary["auth_error"] = auth
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
                logger.info("  -> SUCCESS: %d candles validated & saved", candles_cnt)
            else:
                summary["failed"] += 1
                err_record = {
                    "contract": req.key,
                    "error": err,
                }
                summary["errors"].append(err_record)
                logger.warning("  -> FAILED: %s", err)

            time.sleep(self.rate_limit_delay)

        return summary


def main():
    parser = argparse.ArgumentParser(description="Authoritative Upstox Historical Options Ingestion Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Perform discovery and cache audit without downloading")
    parser.add_argument("--force", action="store_true", help="Force re-download and re-validate existing cached contracts")
    parser.add_argument("--symbols", type=str, default=None, help="Comma-separated list of symbols (e.g. NIFTY50,BANKNIFTY)")
    parser.add_argument("--data-dir", type=str, default="real_data", help="Directory containing underlying spot JSON files")
    parser.add_argument("--cache-dir", type=str, default=DEFAULT_CACHE_DIR, help="Directory to store option contract JSON cache")
    parser.add_argument("--token", type=str, default=None, help="Upstox Access Token override")
    parser.add_argument("--limit", type=int, default=None, help="Max number of contracts to process")
    parser.add_argument("--interval", type=str, default="5minute", help="Candle interval (e.g. 5minute, 1minute)")
    parser.add_argument("--strategies", type=str, default=None, help="Comma-separated strategy names (e.g. OPTION_PREMIUM,EMA_TREND)")

    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    strategies = [s.strip().upper() for s in args.strategies.split(",")] if args.strategies else None
    
    pipeline = HistoricalOptionsIngestionPipeline(
        access_token=args.token,
        cache_dir=args.cache_dir,
    )

    print("=" * 70)
    print(" UPSTOX HISTORICAL OPTIONS DATA ACQUISITION & VALIDATION")
    print("=" * 70)

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
        print(f"  2. Run: python3 scripts/download_historical_options.py")
        sys.exit(0)

    # 4. Ingestion
    print("\n[Phase 3] Executing Acquisition & Validation Pipeline...")
    res = pipeline.run_ingestion(
        requirements=reqs,
        force=args.force,
        dry_run=False,
        limit=args.limit,
    )

    print("\n" + "=" * 70)
    print(" INGESTION RUN SUMMARY")
    print("=" * 70)
    print(f"Total Required    : {res['total_required']}")
    print(f"Already In Cache  : {res['already_cached']}")
    print(f"Attempted Fetch   : {res['to_download']}")
    print(f"Successfully Saved: {res['downloaded']}")
    print(f"Failed / Missing  : {res['failed']}")

    if res.get("auth_error"):
        print("\n[!] AUTHENTICATION ERROR DETECTED:")
        print(f"  Code   : {res['auth_error'].get('error_code')}")
        print(f"  Message: {res['auth_error'].get('error_message')}")
        print(f"  Action : {res['auth_error'].get('required_permission')}")
        sys.exit(1)

    if res["failed"] > 0:
        print(f"\n[!] Encountered {res['failed']} contract errors. See log details above.")
        sys.exit(2)

    print("\n[✓] Ingestion completed successfully.")


if __name__ == "__main__":
    main()
