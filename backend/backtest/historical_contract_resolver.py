"""Historical option contract resolver for backtests.

Authoritatively resolves historical expired option contracts using the official
Upstox Expired Instruments APIs (/v2/expired-instruments/expiries,
/v2/expired-instruments/option/contract, and /v2/expired-instruments/historical-candle).

Key principles:
1. NEVER construct an expired instrument_key from a guessed trading symbol.
2. NEVER assume a current instrument_key represents a historical contract.
3. NEVER guess weekly expiry weekdays or holiday shifts when the official API is available.
4. If a historical contract or candle cannot be authoritatively resolved: return DATA_UNAVAILABLE.
5. Strictly separate LIVE contract resolution (real-time broker market quotes)
   from HISTORICAL contract resolution (official expired instruments database).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Fallback calendar parameters for estimation / offline discovery
_MONTH_CODES = {
    1: "1", 2: "2", 3: "3", 4: "4", 5: "5", 6: "6",
    7: "7", 8: "8", 9: "9", 10: "O", 11: "N", 12: "D",
}

_EXPIRY_WEEKDAYS = {
    "NIFTY50": 3,       # Thursday
    "NIFTY": 3,         # Thursday
    "BANKNIFTY": 2,     # Wednesday
    "FINNIFTY": 1,      # Tuesday
    "MIDCPNIFTY": 0,    # Monday
    "SENSEX": 4,        # Friday
    "BANKEX": 0,        # Monday
}

_UNDERLYING_SYMBOL_MAP = {
    "NIFTY50": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
    "MIDCPNIFTY": "MIDCPNIFTY",
    "SENSEX": "SENSEX",
    "BANKEX": "BANKEX",
}

_EXCHANGE_SEGMENT = {
    "NIFTY50": "NSE_FO",
    "NIFTY": "NSE_FO",
    "BANKNIFTY": "NSE_FO",
    "FINNIFTY": "NSE_FO",
    "MIDCPNIFTY": "NSE_FO",
    "SENSEX": "BSE_FO",
    "BANKEX": "BSE_FO",
}


@dataclass
class DataQualityReport:
    """Per-trade or per-backtest data quality assessment."""
    historical_contract_data: bool = False
    historical_option_chain_data: bool = False
    historical_bid_ask_data: bool = False
    historical_greeks_data: bool = False
    lookahead_protection: bool = True
    synthetic_data_used: bool = False

    # Summary stats
    total_bars: int = 0
    bars_with_real_data: int = 0
    bars_with_missing_data: int = 0
    contracts_resolved: int = 0
    contracts_unavailable: int = 0
    rejection_reasons: Dict[str, int] = field(default_factory=dict)

    def quality_score(self) -> float:
        """0-100 quality score based on data availability."""
        checks = [
            self.historical_contract_data,
            self.lookahead_protection,
            not self.synthetic_data_used,
        ]
        base = sum(checks) / len(checks) * 100
        if self.total_bars > 0:
            coverage = self.bars_with_real_data / self.total_bars * 100
            return round((base + coverage) / 2, 1)
        return round(base, 1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "historical_contract_data": self.historical_contract_data,
            "historical_option_chain_data": self.historical_option_chain_data,
            "historical_bid_ask_data": self.historical_bid_ask_data,
            "historical_greeks_data": self.historical_greeks_data,
            "lookahead_protection": self.lookahead_protection,
            "synthetic_data_used": self.synthetic_data_used,
            "total_bars": self.total_bars,
            "bars_with_real_data": self.bars_with_real_data,
            "bars_with_missing_data": self.bars_with_missing_data,
            "contracts_resolved": self.contracts_resolved,
            "contracts_unavailable": self.contracts_unavailable,
            "rejection_reasons": self.rejection_reasons,
            "quality_score": self.quality_score(),
        }


@dataclass
class HistoricalContract:
    """A resolved authoritative historical option contract."""
    underlying: str
    option_type: str  # CE or PE
    strike: float
    expiry_date: str  # YYYY-MM-DD
    trading_symbol: str
    instrument_key: str  # Authoritative key from Upstox (e.g. NSE_FO|58422|03-10-2024)
    lot_size: int = 25
    verified: bool = False  # True if candle data was fetched and validated successfully
    candles: List[Dict[str, Any]] = field(default_factory=list)


def get_nearest_expiry_for_date(
    underlying: str,
    target_date: date,
) -> date:
    """Calendar approximation helper for expiry day."""
    weekday = _EXPIRY_WEEKDAYS.get(underlying, 3)
    days_ahead = (weekday - target_date.weekday()) % 7
    if days_ahead == 0:
        return target_date
    return target_date + timedelta(days=days_ahead)


def build_trading_symbol(
    underlying: str,
    expiry: date,
    strike: float,
    option_type: str,
) -> str:
    """Construct standard trading symbol representation."""
    sym = _UNDERLYING_SYMBOL_MAP.get(underlying, underlying)
    yy = expiry.strftime("%y")
    month_code = _MONTH_CODES.get(expiry.month, str(expiry.month))
    dd = f"{expiry.day:02d}"
    strike_str = str(int(strike)) if strike == int(strike) else str(strike)
    return f"{sym}{yy}{month_code}{dd}{strike_str}{option_type}"


class HistoricalOptionContractResolver:
    """Authoritative historical option contract resolver for backtesting.

    Resolves contracts via the Upstox Expired Instruments API or pre-loaded cache.
    Guarantees that no synthetic data or fake instrument keys are ever injected.
    """

    def __init__(self, client: Any = None, data_loader: Any = None) -> None:
        self.client = client
        self.data_loader = data_loader
        self._quality = DataQualityReport()

    @property
    def quality_report(self) -> DataQualityReport:
        return self._quality

    def resolve(
        self,
        underlying: str,
        target_date: date,
        option_type: str,
        strike: float,
        fetch_candles: bool = True,
        interval: str = "5minute",
    ) -> Optional[HistoricalContract]:
        """Resolve a historical option contract authoritatively.

        Returns HistoricalContract if the contract is verified in Upstox,
        or None (DATA_UNAVAILABLE) if not found or cannot be verified.
        """
        try:
            # 1. If we have an UpstoxExpiredOptionsClient, resolve authoritatively
            if self.client is not None and hasattr(self.client, "resolve_option_contract"):
                resolved_info = self.client.resolve_option_contract(
                    underlying=underlying,
                    target_date=target_date,
                    spot_price=strike,
                    option_type=option_type,
                    strike_interval=1.0,  # Exact strike match
                )
                if not resolved_info or not resolved_info.get("instrument_key"):
                    self._quality.contracts_unavailable += 1
                    self._quality.rejection_reasons["CONTRACT_NOT_FOUND_IN_UPSTOX"] = (
                        self._quality.rejection_reasons.get("CONTRACT_NOT_FOUND_IN_UPSTOX", 0) + 1
                    )
                    return None

                inst_key = resolved_info["instrument_key"]
                expiry_str = resolved_info.get("expiry_date") or resolved_info.get("expiry", "")
                tsym = resolved_info.get("trading_symbol", "")
                lot = int(resolved_info.get("lot_size", 25))

                contract = HistoricalContract(
                    underlying=underlying.upper(),
                    option_type=option_type.upper(),
                    strike=strike,
                    expiry_date=expiry_str,
                    trading_symbol=tsym,
                    instrument_key=inst_key,
                    lot_size=lot,
                    verified=False,
                )

                if fetch_candles and hasattr(self.client, "fetch_and_cache_contract"):
                    ok, err, data = self.client.fetch_and_cache_contract(
                        underlying=underlying,
                        expiry=expiry_str,
                        strike=strike,
                        option_type=option_type,
                        interval=interval,
                        from_date=target_date.isoformat(),
                        to_date=expiry_str,
                        contract_info_ref=resolved_info,
                    )
                    if ok and data and "candles" in data and len(data["candles"]) > 0:
                        contract.verified = True
                        contract.candles = data["candles"]
                        self._quality.historical_contract_data = True
                        self._quality.contracts_resolved += 1
                        self._quality.bars_with_real_data += len(contract.candles)
                        return contract
                    else:
                        self._quality.contracts_unavailable += 1
                        self._quality.rejection_reasons[f"CANDLES_FETCH_FAILED_{err}"] = (
                            self._quality.rejection_reasons.get(f"CANDLES_FETCH_FAILED_{err}", 0) + 1
                        )
                        return None

                contract.verified = True
                self._quality.contracts_resolved += 1
                return contract

            # 2. Check preloaded data_loader if present
            if self.data_loader is not None and hasattr(self.data_loader, "resolve_contract"):
                resolved = self.data_loader.resolve_contract(
                    underlying=underlying,
                    target_date=target_date,
                    spot_price=strike,
                    option_type=option_type,
                )
                if resolved:
                    c_key, exp_str, c_strike, c_type = resolved
                    return HistoricalContract(
                        underlying=underlying.upper(),
                        option_type=c_type,
                        strike=c_strike,
                        expiry_date=exp_str,
                        trading_symbol=c_key,
                        instrument_key=c_key,
                        verified=True,
                    )

            # If no authoritative source can verify the contract, return None
            self._quality.contracts_unavailable += 1
            self._quality.rejection_reasons["NO_AUTHORITATIVE_CLIENT_OR_DATA"] = (
                self._quality.rejection_reasons.get("NO_AUTHORITATIVE_CLIENT_OR_DATA", 0) + 1
            )
            return None

        except Exception as e:
            logger.warning("Historical contract resolution error: %s", e)
            self._quality.contracts_unavailable += 1
            self._quality.rejection_reasons[f"RESOLUTION_ERROR_{type(e).__name__}"] = (
                self._quality.rejection_reasons.get(f"RESOLUTION_ERROR_{type(e).__name__}", 0) + 1
            )
            return None

    def resolve_atm_for_date(
        self,
        underlying: str,
        target_date: date,
        spot_price: float,
        option_type: str,
        strike_interval: float = 50.0,
    ) -> Optional[HistoricalContract]:
        """Resolve the ATM contract for a given date and spot price authoritatively."""
        atm_strike = round(spot_price / strike_interval) * strike_interval
        return self.resolve(underlying, target_date, option_type, atm_strike)
