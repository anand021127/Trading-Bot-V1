"""Real Historical Options Data Layer for Options Backtesting.

This module provides verified historical option OHLCV data ingestion,
contract resolution, and strict fail-safe validation for options backtesting.

Core Principles:
1. Every option trade MUST execute using verified historical option contract OHLCV candles.
2. If real option data for a contract/timestamp is not available, return DATA_UNAVAILABLE.
3. NEVER fabricate synthetic option prices, random numbers, or theoretical Black-Scholes estimates.
4. NEVER substitute index spot prices as option entry/exit prices.
"""
from __future__ import annotations

import os
import json
import glob
import bisect
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from backend.backtest.historical_contract_resolver import (
    get_nearest_expiry_for_date,
    build_trading_symbol,
    _EXPIRY_WEEKDAYS,
    _UNDERLYING_SYMBOL_MAP,
    _EXCHANGE_SEGMENT,
)
from backend.indicators.atr import calculate_atr

logger = logging.getLogger(__name__)

# Default strike intervals for major Indian indices
INDEX_STRIKE_INTERVALS: Dict[str, float] = {
    "NIFTY50": 50.0,
    "NIFTY": 50.0,
    "BANKNIFTY": 100.0,
    "FINNIFTY": 50.0,
    "MIDCPNIFTY": 25.0,
    "SENSEX": 100.0,
    "BANKEX": 100.0,
}

# Standard lot sizes for major Indian indices (NSE/BSE)
INDEX_LOT_SIZES: Dict[str, int] = {
    "NIFTY50": 25,     # Current NSE lot size
    "NIFTY": 25,
    "BANKNIFTY": 15,
    "FINNIFTY": 25,
    "MIDCPNIFTY": 50,
    "SENSEX": 10,
    "BANKEX": 15,
}


def normalize_underlying(symbol: str) -> str:
    """Normalize underlying index name to standard canonical key."""
    s = symbol.upper().replace(" ", "").replace("_", "").replace("-", "")
    if s in ("NIFTY", "NIFTY50", "NIFTY50INDEX", "NSEINDEXNIFTY50"):
        return "NIFTY50"
    if s in ("BANKNIFTY", "NIFTYBANK", "BANKNIFTYINDEX", "NSEINDEXNIFTYBANK"):
        return "BANKNIFTY"
    if s in ("FINNIFTY", "NIFTYFINSERVICE", "FINNIFTYINDEX", "NSEINDEXNIFTYFINSERVICE"):
        return "FINNIFTY"
    if s in ("MIDCPNIFTY", "NIFTYMIDSELECT", "MIDCPNIFTYINDEX", "NSEINDEXNIFTYMIDSELECT"):
        return "MIDCPNIFTY"
    if s in ("SENSEX", "BSEINDEXSENSEX", "BSESENSEX"):
        return "SENSEX"
    if s in ("BANKEX", "BSEINDEXBANKEX", "BSEBANKEX"):
        return "BANKEX"
    return symbol.upper()


@dataclass
class HistoricalOptionRecord:
    """A verified historical option candle record."""
    date: str
    timestamp: str
    underlying: str
    expiry: str
    strike: float
    option_type: str  # 'CE' or 'PE'
    instrument_key: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date,
            "timestamp": self.timestamp,
            "underlying": self.underlying,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "instrument_key": self.instrument_key,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "oi": self.oi,
        }


class HistoricalOptionsDataLoader:
    """Historical option data store and contract resolver.
    
    Provides strict contract lookup and candle retrieval for historical option backtesting.
    Supports auto-loading from local directory/cache and dynamic on-demand retrieval via UpstoxExpiredOptionsClient.
    """

    def __init__(
        self,
        data_directory: Optional[str] = None,
        upstox_client: Optional[Any] = None,
        auto_load_cache: bool = True,
    ) -> None:
        self.data_directory = data_directory
        self.upstox_client = upstox_client
        # contract_key -> List[HistoricalOptionRecord]
        self._contracts_data: Dict[str, List[HistoricalOptionRecord]] = {}
        # (contract_key, timestamp) -> HistoricalOptionRecord (for O(1) lookup)
        self._timestamp_index: Dict[Tuple[str, str], HistoricalOptionRecord] = {}
        # (underlying, expiry, strike, option_type) -> contract_key
        self._lookup_index: Dict[Tuple[str, str, float, str], str] = {}
        # contract_key -> contract metadata (lot_size, underlying, etc.)
        self._contracts_metadata: Dict[str, Dict[str, Any]] = {}

        # PHASE 5.2 §17 per-contract acceleration structures (pure speed;
        # contents of every returned snapshot are unchanged):
        #   _contract_ts_sorted: contract_key -> [(timestamp, record), ...] sorted
        #   _contract_days: contract_key -> {date_str: [records sorted by ts]}
        #   _contract_atr: contract_key -> {timestamp: ATR(14) at/before ts}
        #     (Wilder recursion carried forward incrementally per contract)
        self._contract_ts_sorted: Dict[str, list] = {}
        self._contract_days: Dict[str, Dict[str, list]] = {}
        self._contract_atr: Dict[str, Dict[str, float]] = {}
        self._atr_period = 14
        # (underlying, option_type) -> [(expiry, strike, lookup_key), ...] sorted
        self._chain_lookup_cache: Dict[tuple, list] = {}
        # (contract_key, date_str) -> [timestamps] for bisect day lookups
        self._day_ts_index: Dict[tuple, list] = {}
        
        # 1. Load user specified data directory if provided
        if data_directory and os.path.exists(data_directory):
            self.load_from_directory(data_directory)

        # 2. Auto-load local persistent options cache
        if auto_load_cache:
            cache_dir = os.environ.get("HISTORICAL_OPTIONS_CACHE_DIR") or os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                "real_data",
                "options_cache",
            )
            if os.path.exists(cache_dir):
                self.load_from_directory(cache_dir)

    def is_data_available(self) -> bool:
        """Returns True if verified historical option data is loaded."""
        return len(self._contracts_data) > 0 and len(self._timestamp_index) > 0

    def available_contracts_count(self) -> int:
        """Returns total unique historical option contracts loaded."""
        return len(self._contracts_data)

    def available_candles_count(self) -> int:
        """Returns total historical option candles loaded across all contracts."""
        return len(self._timestamp_index)

    def list_contracts(self) -> List[str]:
        """Returns list of loaded contract keys / symbols."""
        return sorted(list(self._contracts_data.keys()))

    def register_option_candle(self, record: HistoricalOptionRecord) -> None:
        """Register a single verified historical option candle."""
        norm_und = normalize_underlying(record.underlying)
        contract_key = record.instrument_key or build_trading_symbol(
            norm_und,
            datetime.fromisoformat(record.expiry).date() if isinstance(record.expiry, str) and "-" in record.expiry else date.today(),
            record.strike,
            record.option_type,
        )
        
        if contract_key not in self._contracts_data:
            self._contracts_data[contract_key] = []
            lookup_tuple = (
                norm_und,
                record.expiry,
                float(record.strike),
                record.option_type.upper(),
            )
            self._lookup_index[lookup_tuple] = contract_key
            # invalidate acceleration structures for this contract
            self._contract_ts_sorted.pop(contract_key, None)
            self._contract_days.pop(contract_key, None)
            self._contract_atr.pop(contract_key, None)
            
        self._contracts_data[contract_key].append(record)
        self._timestamp_index[(contract_key, record.timestamp)] = record
        norm_ts = record.timestamp.replace(" ", "T")
        if norm_ts != record.timestamp:
            self._timestamp_index[(contract_key, norm_ts)] = record

    def _ensure_contract_accelerators(self, contract_key: str) -> None:
        """Build (once) the sorted-timestamp list, per-day buckets and the
        incremental Wilder-ATR table for a contract. Pure derived state —
        rebuilt from the same records, so outputs are identical (§17)."""
        if contract_key in self._contract_ts_sorted:
            return
        records = self._contracts_data.get(contract_key, [])
        ordered = sorted(records, key=lambda r: r.timestamp or "")
        self._contract_ts_sorted[contract_key] = ordered
        days: Dict[str, list] = {}
        for r in ordered:
            days.setdefault((r.timestamp or "")[:10], []).append(r)
        self._contract_days[contract_key] = days
        # Wilder ATR carried forward once over the ordered history — the
        # same arithmetic the previous per-bar recompute performed, cached.
        atr_table: Dict[str, float] = {}
        highs = [float(r.high) for r in ordered]
        lows = [float(r.low) for r in ordered]
        closes = [float(r.close) for r in ordered]
        n = len(closes)
        if n >= 2 and len(highs) == n and len(lows) == n:
            period = self._atr_period
            trs = []
            for i in range(1, n):
                trs.append(max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                ))
            if len(trs) >= period:
                atr = sum(trs[:period]) / period
                # match calculate_atr rounding (6dp) for identical values
                atr_table[ordered[period].timestamp or ""] = round(atr, 6)
                for i in range(period, len(trs)):
                    atr = ((period - 1) * atr + trs[i]) / period
                    ts_i = ordered[i + 1].timestamp or ""
                    atr_table[ts_i] = round(atr, 6)
        self._contract_atr[contract_key] = atr_table

    def _atr_at_or_before(self, contract_key: str, timestamp: str) -> float:
        """Cached ATR(14) at or before `timestamp` (never future bars)."""
        self._ensure_contract_accelerators(contract_key)
        table = self._contract_atr.get(contract_key) or {}
        if timestamp in table:
            return table[timestamp]
        best = 0.0
        best_ts = ""
        ordered = self._contract_ts_sorted.get(contract_key) or []
        # walk the table keys via the ordered list for a bisectable lookup
        atr_keys = sorted(table.keys())
        if atr_keys:
            idx = bisect.bisect_right(atr_keys, timestamp) - 1
            if idx >= 0:
                return table[atr_keys[idx]]
        return best

    def load_contract_candles(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        instrument_key: str,
        candles: List[Dict[str, Any]],
        lot_size: Optional[int] = None,
    ) -> int:
        """Load candle list for a specific option contract."""
        norm_und = normalize_underlying(underlying)
        # Contract metadata only — never invent exchange lot sizes.
        if lot_size is None or int(lot_size) <= 1:
            lot_size = 0

        self._contracts_metadata[instrument_key] = {
            "underlying": norm_und,
            "expiry": expiry,
            "strike": float(strike),
            "option_type": option_type.upper(),
            "instrument_key": instrument_key,
            "lot_size": int(lot_size),
        }

        count = 0
        for c in candles:
            ts = c.get("timestamp", "")
            d = ts[:10] if ts else c.get("date", "")
            record = HistoricalOptionRecord(
                date=d,
                timestamp=ts,
                underlying=underlying.upper(),
                expiry=expiry,
                strike=float(strike),
                option_type=option_type.upper(),
                instrument_key=instrument_key,
                open=float(c.get("open", 0.0)),
                high=float(c.get("high", 0.0)),
                low=float(c.get("low", 0.0)),
                close=float(c.get("close", 0.0)),
                volume=float(c.get("volume", 0.0)),
                oi=float(c["oi"]) if "oi" in c and c["oi"] is not None else None,
            )
            self.register_option_candle(record)
            count += 1
        return count

    def get_contract_lot_size(
        self,
        contract_key: str,
        underlying: str = "",
        target_date: Optional[date] = None,
    ) -> Optional[int]:
        """Resolve actual exchange lot size for a contract.
        
        Returns None if lot size cannot be resolved or is invalid (<= 1).
        """
        if contract_key in self._contracts_metadata:
            ls = self._contracts_metadata[contract_key].get("lot_size")
            if isinstance(ls, int) and ls > 1:
                return ls
        # No INDEX_LOT_SIZES / hardcoded fallback — missing metadata => unresolved
        return None

    def load_from_directory(self, dir_path: str) -> int:
        """Scan directory for historical option JSON/CSV files.
        
        Files must contain option contract data conforming to the schema.
        Note: Files named like '{UNDERLYING}_2024_5min.json' that only contain spot candles
        are spot index feeds, NOT option contract feeds.
        """
        if not os.path.exists(dir_path):
            return 0

        loaded_count = 0
        json_files = glob.glob(os.path.join(dir_path, "**/*.json"), recursive=True)
        
        from backend.backtest.historical_data_io import load_dataset_safe, salvage_truncated_json

        for file_path in json_files:
            filename = os.path.basename(file_path)
            # Spot files in real_data/ only contain index spot candles
            if any(filename.startswith(f"{idx}_2024_5min.json") for idx in INDEX_STRIKE_INTERVALS):
                continue
            
            try:
                data = None
                try:
                    with open(file_path, "r", encoding="utf-8") as fp:
                        data = json.load(fp)
                except json.JSONDecodeError:
                    with open(file_path, "r", encoding="utf-8") as fp:
                        raw_text = fp.read()
                    data = salvage_truncated_json(raw_text)
                    if not data:
                        raise

                if isinstance(data, dict) and "contract" in data and "candles" in data:
                    c_info = data["contract"]
                    candles = data["candles"]
                    self.load_contract_candles(
                        underlying=c_info.get("underlying", ""),
                        expiry=c_info.get("expiry", ""),
                        strike=float(c_info.get("strike", 0.0)),
                        option_type=c_info.get("option_type", "CE"),
                        instrument_key=c_info.get("instrument_key", filename.replace(".json", "")),
                        candles=candles,
                        lot_size=c_info.get("lot_size"),
                    )
                    loaded_count += len(candles)
                elif isinstance(data, list) and len(data) > 0 and ("strike" in data[0] or "option_type" in data[0]):
                    for c in data:
                        rec = HistoricalOptionRecord(
                            date=c.get("date", c.get("timestamp", "")[:10]),
                            timestamp=c.get("timestamp", ""),
                            underlying=c.get("underlying", ""),
                            expiry=c.get("expiry", ""),
                            strike=float(c.get("strike", 0)),
                            option_type=c.get("option_type", "CE"),
                            instrument_key=c.get("instrument_key", ""),
                            open=float(c.get("open", 0)),
                            high=float(c.get("high", 0)),
                            low=float(c.get("low", 0)),
                            close=float(c.get("close", 0)),
                            volume=float(c.get("volume", 0)),
                            oi=float(c["oi"]) if "oi" in c and c["oi"] is not None else None,
                        )
                        self.register_option_candle(rec)
                        loaded_count += 1
            except Exception as e:
                logger.warning("Could not parse potential option data file %s: %s", file_path, e)

        return loaded_count

    def resolve_contract(
        self,
        underlying: str,
        target_date: date,
        spot_price: float,
        option_type: str,
        strike_interval: Optional[float] = None,
        target_expiry: Optional[str] = None,
        target_strike: Optional[float] = None,
    ) -> Optional[Tuple[str, str, float, str]]:
        """Resolve historical expiry, strike, and contract key for a given spot and date.
        
        Returns:
            (contract_key, expiry_str, strike, option_type) if resolvable, else None (DATA_UNAVAILABLE).
        """
        und_key = normalize_underlying(underlying)
        opt_type = option_type.upper()
        step = strike_interval or INDEX_STRIKE_INTERVALS.get(und_key, 50.0)
        desired_strike = target_strike if target_strike is not None else float(round(spot_price / step) * step)

        # 1. Authoritative resolution via Upstox API client if available
        if self.upstox_client:
            try:
                resolved_info = self.upstox_client.resolve_option_contract(
                    underlying=und_key,
                    target_date=target_date,
                    spot_price=spot_price,
                    option_type=opt_type,
                    strike_interval=strike_interval,
                    target_expiry=target_expiry,
                    target_strike=desired_strike,
                )
                if resolved_info and resolved_info.get("instrument_key"):
                    inst_key = resolved_info["instrument_key"]
                    exp_str = resolved_info.get("expiry_date") or resolved_info.get("expiry", "")
                    resolved_strike = float(resolved_info.get("strike", desired_strike))
                    
                    # If already in memory:
                    if inst_key in self._contracts_data and len(self._contracts_data[inst_key]) > 0:
                        return inst_key, exp_str, resolved_strike, opt_type

                    # Otherwise fetch from Upstox and load into memory:
                    ok, err, data = self.upstox_client.fetch_and_cache_contract(
                        underlying=und_key,
                        expiry=exp_str,
                        strike=resolved_strike,
                        option_type=opt_type,
                        from_date=target_date.isoformat(),
                        to_date=exp_str,
                        spot_price_ref=spot_price,
                        contract_info_ref=resolved_info,
                    )
                    if ok and data and "candles" in data:
                        meta_lot = 0
                        try:
                            meta_lot = int(
                                (data.get("contract") or {}).get("lot_size")
                                or resolved_info.get("lot_size")
                                or 0
                            )
                        except (TypeError, ValueError):
                            meta_lot = 0
                        self.load_contract_candles(
                            underlying=und_key,
                            expiry=exp_str,
                            strike=resolved_strike,
                            option_type=opt_type,
                            instrument_key=inst_key,
                            candles=data["candles"],
                            lot_size=meta_lot if meta_lot > 1 else 0,
                        )
                        return inst_key, exp_str, resolved_strike, opt_type
            except Exception as e:
                logger.warning("Could not authoritatively resolve option contract via Upstox: %s", e)

        # 2. Lookup in local pre-loaded contracts index
        # Search all loaded expiries >= target_date
        target_date_str = target_date.isoformat()
        matching_entries = [
            k for k in self._lookup_index.keys()
            if k[0] == und_key and (k[1] == target_expiry if target_expiry else k[1] >= target_date_str) and k[3] == opt_type
        ]
        if matching_entries:
            # 2a. First try exact strike match
            exact_matches = [k for k in matching_entries if abs(k[2] - desired_strike) < 0.01]
            if exact_matches:
                exact_matches.sort(key=lambda x: x[1])  # Nearest expiry
                best_tuple = exact_matches[0]
                contract_key = self._lookup_index[best_tuple]
                return contract_key, best_tuple[1], desired_strike, opt_type

            # 2b. If exact strike is not preloaded, check nearest available strike within search radius (e.g. ±3 strikes)
            max_strike_diff = step * 3.5
            nearby_matches = [k for k in matching_entries if abs(k[2] - desired_strike) <= max_strike_diff]
            if nearby_matches:
                # Sort by nearest expiry first, then by closest strike distance to spot
                nearby_matches.sort(key=lambda x: (x[1], abs(x[2] - desired_strike)))
                best_tuple = nearby_matches[0]
                contract_key = self._lookup_index[best_tuple]
                return contract_key, best_tuple[1], best_tuple[2], opt_type

        return None

    def get_candle_at(
        self,
        contract_key: str,
        timestamp: str,
    ) -> Optional[HistoricalOptionRecord]:
        """Fetch exact historical option candle for a contract at a specific timestamp.
        
        Returns None (DATA_UNAVAILABLE) if candle is not present.
        """
        rec = self._timestamp_index.get((contract_key, timestamp))
        if rec:
            return rec
        norm_ts = timestamp.replace(" ", "T")
        return self._timestamp_index.get((contract_key, norm_ts))

    def build_option_chain_snapshot(
        self,
        underlying: str,
        target_date: date,
        spot_price: float,
        timestamp: str,
        exact_atm_only: bool = True,
    ) -> List[Dict[str, Any]]:
        """Build a real option_chain list from cached historical OHLCV only.

        Used so strategies that expect context['option_chain'] (e.g. V8_D) receive
        the same real-data contracts that resolve_contract / get_candle_at use.

        Rules:
        - Only contracts already loaded in the cache.
        - Premium = historical candle close at the given timestamp (or same calendar
          date if exact timestamp missing — still real data, not synthetic).
        - If exact_atm_only: only the ATM CE and PE for the nearest loaded expiry
          >= target_date with an exact strike match. No neighbouring-strike fallback.
        - Never fabricates prices, Black-Scholes, or live quotes.

        Returns a list of dicts with keys: strike, option_type, instrument_key,
        expiry, ltp, close_price, open, high, low, volume, lot_size (when known).
        Empty list if nothing can be resolved from real cache.
        """
        und_key = normalize_underlying(underlying)
        step = INDEX_STRIKE_INTERVALS.get(und_key, 50.0)
        atm_strike = float(round(spot_price / step) * step)
        target_date_str = target_date.isoformat() if isinstance(target_date, date) else str(target_date)[:10]
        chain: List[Dict[str, Any]] = []

        for opt_type in ("CE", "PE"):
            # PHASE 5.2 §17: per-(underlying, type) expiry-sorted key cache
            # replaces the full index rescan on every bar.
            cache_key = (und_key, opt_type)
            candidates = self._chain_lookup_cache.get(cache_key)
            if candidates is None:
                candidates = sorted(
                    (k for k in self._lookup_index.keys()
                     if k[0] == und_key and k[3] == opt_type),
                    key=lambda x: (x[1], x[2]),
                )
                self._chain_lookup_cache[cache_key] = candidates
            matching = [k for k in candidates
                        if k[1] >= target_date_str and abs(k[2] - atm_strike) < 0.01] \
                if exact_atm_only else \
                [k for k in candidates if k[1] >= target_date_str]
            if not matching:
                continue
            matching.sort(key=lambda x: (x[1], abs(x[2] - atm_strike)))
            best = matching[0]
            contract_key = self._lookup_index[best]
            expiry_str, strike_val = best[1], best[2]

            self._ensure_contract_accelerators(contract_key)
            rec = self.get_candle_at(contract_key, timestamp)
            if rec is None:
                # Same calendar day only — still real OHLCV, not interpolated
                # (bisect over the contract's per-day bucket, no full rescan).
                day_candidates = (self._contract_days.get(contract_key) or {}).get(target_date_str)
                if not day_candidates:
                    continue
                ts_list = self._day_ts_index.get((contract_key, target_date_str))
                if ts_list is None:
                    ts_list = [r.timestamp for r in day_candidates]
                    self._day_ts_index[(contract_key, target_date_str)] = ts_list
                idx = bisect.bisect_right(ts_list, timestamp) - 1
                rec = day_candidates[idx] if idx >= 0 else None

            if rec is None or float(rec.close) <= 0:
                continue

            meta = self._contracts_metadata.get(contract_key, {})

            # Historical option ATR at/before the current timestamp only —
            # served from the contract's incremental Wilder-ATR cache
            # (identical values to the previous full recompute; no lookahead).
            option_atr = self._atr_at_or_before(contract_key, timestamp)

            chain.append({
                "strike": float(strike_val),
                "option_type": opt_type,
                "instrument_key": contract_key,
                "expiry": expiry_str,
                "ltp": float(rec.close),
                "close_price": float(rec.close),
                "open": float(rec.open),
                "high": float(rec.high),
                "low": float(rec.low),
                "volume": float(rec.volume or 0),
                "lot_size": int(meta.get("lot_size") or 0),
                "timestamp": rec.timestamp,
                "option_atr": option_atr,
                "atr": option_atr,
            })

        return chain
