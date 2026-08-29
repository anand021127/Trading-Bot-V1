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
        
        # 1. Load user specified data directory if provided
        if data_directory and os.path.exists(data_directory):
            self.load_from_directory(data_directory)

        # 2. Auto-load local persistent options cache
        if auto_load_cache:
            cache_dir = os.path.join(
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
            
        self._contracts_data[contract_key].append(record)
        self._timestamp_index[(contract_key, record.timestamp)] = record
        norm_ts = record.timestamp.replace(" ", "T")
        if norm_ts != record.timestamp:
            self._timestamp_index[(contract_key, norm_ts)] = record

    def load_contract_candles(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        instrument_key: str,
        candles: List[Dict[str, Any]],
    ) -> int:
        """Load candle list for a specific option contract."""
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
                        self.load_contract_candles(
                            underlying=und_key,
                            expiry=exp_str,
                            strike=resolved_strike,
                            option_type=opt_type,
                            instrument_key=inst_key,
                            candles=data["candles"],
                        )
                        return inst_key, exp_str, resolved_strike, opt_type
            except Exception as e:
                logger.warning("Could not authoritatively resolve option contract via Upstox: %s", e)

        # 2. Lookup in local pre-loaded contracts index
        # Search all loaded expiries >= target_date
        target_date_str = target_date.isoformat()
        matching_entries = [
            k for k in self._lookup_index.keys()
            if k[0] == und_key and (k[1] == target_expiry if target_expiry else k[1] >= target_date_str) and abs(k[2] - desired_strike) < 0.01 and k[3] == opt_type
        ]
        if matching_entries:
            matching_entries.sort(key=lambda x: x[1])  # Nearest expiry
            best_tuple = matching_entries[0]
            contract_key = self._lookup_index[best_tuple]
            return contract_key, best_tuple[1], desired_strike, opt_type

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
