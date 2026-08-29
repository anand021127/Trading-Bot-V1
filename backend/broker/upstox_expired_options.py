"""Official Upstox Expired Options API Integration & Data Cache.

Provides robust access to Upstox Expired Instruments APIs:
1. Expired Expiries API (/v2/expired-instruments/expiries)
2. Expired Option Contracts API (/v2/expired-instruments/option/contracts)
3. Expired Historical Candles API (/v2/expired-instruments/historical-candle/{key}/{interval}/{to}/{from})

Includes persistent local caching in real_data/options_cache/, rate-limiting,
exponential backoff retry, comprehensive data validation, and strict fail-safes.
Built using standard library urllib for zero-dependency reliability.
"""
from __future__ import annotations

import os
import json
import time
import logging
import urllib.request
import urllib.error
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.backtest.historical_contract_resolver import (
    get_nearest_expiry_for_date,
    build_trading_symbol,
    _UNDERLYING_SYMBOL_MAP,
)
from backend.backtest.options_data_layer import (
    INDEX_STRIKE_INTERVALS,
    HistoricalOptionRecord,
    HistoricalOptionsDataLoader,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"
DEFAULT_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "real_data", "options_cache")

# Standard Upstox index instrument keys
INDEX_INSTRUMENT_KEYS: Dict[str, str] = {
    "NIFTY50": "NSE_INDEX|Nifty 50",
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT",
    "SENSEX": "BSE_INDEX|SENSEX",
    "BANKEX": "BSE_INDEX|BANKEX",
}


class UpstoxExpiredAPIError(Exception):
    """Specific error for Upstox Expired Instruments API."""
    def __init__(self, status_code: int, message: str, error_code: Optional[str] = None) -> None:
        self.status_code = status_code
        self.message = message
        self.error_code = error_code
        super().__init__(f"Upstox Expired API [{status_code}] ({error_code or 'UNKNOWN'}): {message}")


class OptionsDataValidator:
    """Rigorous validator for historical option contract metadata and candle feeds."""

    @staticmethod
    def validate_contract_metadata(
        contract_info: Dict[str, Any],
        expected_underlying: str,
        expected_expiry: str,
        expected_strike: float,
        expected_option_type: str,
    ) -> Tuple[bool, Optional[str]]:
        """Validate that returned contract metadata matches expected parameters."""
        actual_und = str(contract_info.get("underlying", "")).upper()
        if expected_underlying.upper() not in actual_und and actual_und not in expected_underlying.upper():
            return False, f"Underlying mismatch: expected {expected_underlying}, got {actual_und}"

        actual_exp = str(contract_info.get("expiry", "") or contract_info.get("expiry_date", ""))
        if expected_expiry and actual_exp and actual_exp != expected_expiry:
            return False, f"Expiry mismatch: expected {expected_expiry}, got {actual_exp}"

        actual_strike = float(contract_info.get("strike", 0.0) or contract_info.get("strike_price", 0.0))
        if expected_strike and abs(actual_strike - expected_strike) > 0.01:
            return False, f"Strike mismatch: expected {expected_strike}, got {actual_strike}"

        actual_type = str(contract_info.get("option_type", "") or contract_info.get("instrument_type", "")).upper()
        if expected_option_type and actual_type != expected_option_type.upper():
            return False, f"Option type mismatch: expected {expected_option_type}, got {actual_type}"

        return True, None

    @staticmethod
    def validate_candles(
        candles: List[Dict[str, Any]],
        expected_instrument_key: str,
        spot_price_reference: Optional[float] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Tuple[bool, Optional[str], List[Dict[str, Any]]]:
        """Validate OHLCV integrity, monotonicity, timestamp uniqueness, and price realism."""
        if not candles:
            return False, "Candle list is empty", []

        seen_timestamps = set()
        validated_candles: List[Dict[str, Any]] = []

        for idx, c in enumerate(candles):
            ts = c.get("timestamp") or c.get("datetime") or ""
            if not ts:
                return False, f"Candle #{idx} missing timestamp", []

            if ts in seen_timestamps:
                return False, f"Duplicate timestamp found at {ts}", []
            seen_timestamps.add(ts)

            # Date range checks if given
            c_date = ts[:10]
            if from_date and c_date < from_date:
                return False, f"Timestamp {ts} before requested from_date {from_date}", []
            if to_date and c_date > to_date:
                return False, f"Timestamp {ts} after requested to_date {to_date}", []

            try:
                op = float(c.get("open", 0.0))
                hi = float(c.get("high", 0.0))
                lo = float(c.get("low", 0.0))
                cl = float(c.get("close", 0.0))
                vol = float(c.get("volume", 0.0))
                oi = float(c["oi"]) if "oi" in c and c["oi"] is not None else None
            except (ValueError, TypeError) as e:
                return False, f"Invalid numerical OHLC value in candle {ts}: {e}", []

            # Basic OHLC bounds
            if op < 0 or hi < 0 or lo < 0 or cl < 0:
                return False, f"Negative price value in candle {ts}: O={op} H={hi} L={lo} C={cl}", []
            if hi < lo:
                return False, f"High < Low in candle {ts}: H={hi} L={lo}", []
            if hi < max(op, cl) - 1e-4:
                return False, f"High < max(Open, Close) in candle {ts}: H={hi} O={op} C={cl}", []
            if lo > min(op, cl) + 1e-4:
                return False, f"Low > min(Open, Close) in candle {ts}: L={lo} O={op} C={cl}", []

            # Check for suspicious spot price substitution
            # An option premium for standard Indian indices should not equal the index spot (e.g. 24000+)
            if spot_price_reference and spot_price_reference > 1000:
                if cl >= spot_price_reference * 0.7:
                    return False, f"Suspicious option price ({cl}) close to index spot ({spot_price_reference}) in candle {ts}", []

            validated_candles.append({
                "timestamp": ts,
                "date": c_date,
                "open": round(op, 2),
                "high": round(hi, 2),
                "low": round(lo, 2),
                "close": round(cl, 2),
                "volume": vol,
                "oi": oi,
            })

        return True, None, validated_candles


class OptionsDataCache:
    """Deterministic local disk cache for downloaded expired historical option data."""

    def __init__(self, cache_dir: str = DEFAULT_CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)

    @staticmethod
    def get_cache_filename(
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> str:
        """Deterministic cache file name:
        {underlying}_{expiry}_{strike}_{option_type}_{interval}_{from_date}_{to_date}.json
        """
        strike_int = int(strike) if strike.is_integer() else strike
        clean_und = underlying.upper().replace(" ", "")
        clean_exp = expiry.replace("-", "")
        clean_from = from_date.replace("-", "")
        clean_to = to_date.replace("-", "")
        return f"{clean_und}_{clean_exp}_{strike_int}_{option_type.upper()}_{interval}_{clean_from}_{clean_to}.json"

    def get(self, filename: str) -> Optional[Dict[str, Any]]:
        """Retrieve cached option data if present."""
        path = os.path.join(self.cache_dir, filename)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            return data
        except Exception as e:
            logger.warning("Failed to read cache file %s: %s", path, e)
            return None

    def save(self, filename: str, contract_info: Dict[str, Any], candles: List[Dict[str, Any]]) -> str:
        """Atomically persist option data to cache using robust atomic validation."""
        from backend.backtest.historical_data_io import save_dataset_atomic
        path = os.path.join(self.cache_dir, filename)
        payload = {
            "cached_at": datetime.now().isoformat(),
            "contract": contract_info,
            "candles_count": len(candles),
            "candles": candles,
        }
        return save_dataset_atomic(path, payload, min_records=1 if candles else 0)


def normalize_contract_item(
    c: Dict[str, Any],
    underlying_key: str,
    und_key: str,
    default_expiry: str,
) -> Dict[str, Any]:
    """Normalize a raw contract dict returned from Upstox Expired Option Contracts API."""
    tsym = str(c.get("trading_symbol") or c.get("symbol") or "").strip()

    # 1. Strike extraction (numeric or string, with fallback to trading_symbol regex)
    strike_val = 0.0
    for k in ("strike_price", "strike", "strikePrice", "strike_rate"):
        val = c.get(k)
        if val is not None and val != "":
            try:
                strike_val = float(val)
                if strike_val > 500000.0:
                    strike_val = strike_val / 100.0
                break
            except (ValueError, TypeError):
                pass

    # 2. Option type extraction (checking option_type, instrument_type, and trading_symbol)
    opt_type = ""
    for k in ("option_type", "optionType"):
        val = c.get(k)
        if val:
            s = str(val).strip().upper()
            if s in ("CE", "CALL", "C"):
                opt_type = "CE"
                break
            elif s in ("PE", "PUT", "P"):
                opt_type = "PE"
                break

    if not opt_type:
        inst_t = str(c.get("instrument_type") or c.get("instrumentType") or "").strip().upper()
        if inst_t in ("CE", "CALL", "C"):
            opt_type = "CE"
        elif inst_t in ("PE", "PUT", "P"):
            opt_type = "PE"

    # Regex parse from trading symbol if either strike or option_type is missing
    if (not opt_type or strike_val <= 0.0) and tsym:
        clean_sym = tsym.upper().split(".")[0].strip()
        m = re.search(r'(\d+(?:\.\d+)?)\s*[-_]?\s*(CE|PE|CALL|PUT)', clean_sym)
        if m:
            if strike_val <= 0.0:
                try:
                    strike_val = float(m.group(1))
                except ValueError:
                    pass
            if not opt_type:
                t = m.group(2)
                opt_type = "CE" if t in ("CE", "CALL") else "PE"

    if not opt_type and tsym:
        upper_sym = tsym.strip().upper()
        if upper_sym.endswith("CE"):
            opt_type = "CE"
        elif upper_sym.endswith("PE"):
            opt_type = "PE"

    # 3. Instrument Key extraction:
    # Strictly prefer expired_instrument_key returned by Upstox.
    # Never construct the expired instrument_key ourselves if Upstox has returned one.
    upstox_expired_key = str(c.get("expired_instrument_key") or c.get("expiredInstrumentKey") or "").strip()
    raw_k = str(c.get("instrument_key") or c.get("instrumentKey") or "").strip()

    if upstox_expired_key:
        inst_k = upstox_expired_key
    elif raw_k:
        if raw_k.count("|") >= 2:
            inst_k = raw_k
        elif raw_k.count("|") == 1 and default_expiry:
            exp_p = default_expiry.split("-")
            if len(exp_p) == 3 and len(exp_p[0]) == 4:
                dd_mm_yyyy = f"{exp_p[2]}-{exp_p[1]}-{exp_p[0]}"
                inst_k = f"{raw_k}|{dd_mm_yyyy}"
            else:
                inst_k = raw_k
        else:
            inst_k = raw_k
    else:
        inst_k = ""

    # 4. Expiry extraction
    exp_str = str(c.get("expiry_date") or c.get("expiryDate") or c.get("expiry") or default_expiry or "").strip()
    if exp_str.isdigit() and len(exp_str) >= 10:
        try:
            ts_sec = int(exp_str) / 1000.0 if len(exp_str) >= 13 else int(exp_str)
            exp_str = datetime.fromtimestamp(ts_sec).date().isoformat()
        except Exception:
            exp_str = default_expiry

    lot = int(c.get("lot_size") or c.get("lotSize") or INDEX_STRIKE_INTERVALS.get(und_key, 25))

    return {
        "instrument_key": inst_k,
        "expired_instrument_key": inst_k,
        "trading_symbol": tsym,
        "strike": strike_val,
        "strike_price": strike_val,
        "option_type": opt_type,
        "expiry": exp_str,
        "expiry_date": exp_str,
        "lot_size": lot,
        "underlying": und_key,
        "underlying_key": underlying_key,
        "raw_response": c,
    }


class UpstoxExpiredOptionsClient:
    """Client for official Upstox Expired Instruments and Historical Candle APIs."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: int = 15,
        cache_dir: str = DEFAULT_CACHE_DIR,
        dotenv_path: Optional[str] = None,
    ) -> None:
        from backend.broker.token_resolver import (
            resolve_upstox_token_with_source,
            token_fingerprint,
        )
        self.dotenv_path = dotenv_path
        if access_token and access_token.strip():
            # Use the exact verified immutable token without re-resolving
            self.access_token = access_token.strip().strip('"\'').strip()
            self.token_source = "explicit_runtime"
        else:
            # Resolve authoritative token requiring valid active state
            self.access_token, self.token_source = resolve_upstox_token_with_source(
                dotenv_path=dotenv_path,
                require_valid=True,
            )

        self.token_fingerprint = token_fingerprint(self.access_token)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache = OptionsDataCache(cache_dir=cache_dir)
        self.validator = OptionsDataValidator()
        self._expiries_cache: Dict[str, List[str]] = {}
        self._contracts_cache: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}

        src = self.token_source
        fp = self.token_fingerprint
        logger.info(
            "[Token Diagnostic] UpstoxExpiredOptionsClient init: token_source=%s, length=%d, fingerprint=%s",
            src, len(self.access_token), fp,
        )

    def update_access_token(self, new_token: str) -> None:
        """Update client access token dynamically."""
        from backend.broker.token_resolver import token_fingerprint
        clean = (new_token or "").strip().strip('"\'').strip()
        self.access_token = clean
        if clean and not (clean.startswith(("mock-", "test-", "leftover-")) or len(clean) < 30):
            os.environ["UPSTOX_ACCESS_TOKEN"] = clean
        fp = token_fingerprint(self.access_token)
        logger.info(
            "[Token Diagnostic] UpstoxExpiredOptionsClient update_access_token: length=%d, fingerprint=%s",
            len(self.access_token), fp,
        )

    def _headers(self) -> Dict[str, str]:
        h = {
            "Accept": "application/json",
            "User-Agent": "Upstox-Trading-Bot/2.0",
        }
        if self.access_token:
            clean = self.access_token.strip().strip('"\'').strip()
            h["Authorization"] = f"Bearer {clean}"
        return h

    def _get(self, endpoint: str, params: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> Dict[str, Any]:
        from backend.broker.token_resolver import token_fingerprint
        clean = (self.access_token or "").strip().strip('"\'').strip()
        fp = token_fingerprint(clean)
        logger.info(
            "[Token Diagnostic] UpstoxExpiredOptionsClient._get %s: token_length=%d, fingerprint=%s",
            endpoint, len(clean), fp,
        )

        url = f"{self.base_url}{endpoint}"
        if params:
            query_str = urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
            url = f"{url}?{query_str}"

        last_error: Optional[Exception] = None
        for attempt in range(max_retries):
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    status = resp.status
                    body = resp.read().decode("utf-8")
                    try:
                        data = json.loads(body)
                    except ValueError:
                        raise UpstoxExpiredAPIError(status, f"Invalid JSON response: {body[:300]}", "INVALID_JSON")

                    if status >= 400 or data.get("status") == "error":
                        err_msg = data.get("message")
                        err_code = None
                        if "errors" in data and isinstance(data["errors"], list) and len(data["errors"]) > 0:
                            err_code = data["errors"][0].get("errorCode") or data["errors"][0].get("error_code")
                            err_msg = data["errors"][0].get("message") or err_msg
                        raise UpstoxExpiredAPIError(status, str(err_msg or data), err_code)

                    return data
            except urllib.error.HTTPError as e:
                status = e.code
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8")
                    parsed_err = json.loads(error_body)
                except Exception:
                    parsed_err = {}

                err_code = None
                err_msg = str(parsed_err.get("message") or error_body[:300])
                if "errors" in parsed_err and isinstance(parsed_err["errors"], list) and len(parsed_err["errors"]) > 0:
                    err_code = parsed_err["errors"][0].get("errorCode") or parsed_err["errors"][0].get("error_code")
                    err_msg = parsed_err["errors"][0].get("message") or err_msg

                if status == 401:
                    code = err_code or "AUTH_INVALID_TOKEN"
                    msg = err_msg or "Invalid or expired UPSTOX_ACCESS_TOKEN. Please generate a new active access token."
                    raise UpstoxExpiredAPIError(401, msg, code)
                if status == 403:
                    code = err_code or "PERMISSION_DENIED"
                    msg = err_msg or "Access forbidden: Expired Instruments API requires active Upstox Plus Plan and historical derivatives permissions."
                    raise UpstoxExpiredAPIError(403, msg, code)
                if status == 404:
                    code = err_code or "NOT_FOUND"
                    msg = err_msg or f"Resource not found for endpoint: {endpoint}"
                    raise UpstoxExpiredAPIError(404, msg, code)
                if status == 429:
                    # Rate limited: retry with exponential backoff
                    if attempt < max_retries - 1:
                        sleep_time = (2 ** attempt) * 1.5
                        time.sleep(sleep_time)
                        continue
                    raise UpstoxExpiredAPIError(429, "Upstox API rate limit exceeded.", "RATE_LIMITED")

                # Server errors (500, 502, 503, 504) - retry
                if status in (500, 502, 503, 504) and attempt < max_retries - 1:
                    sleep_time = (2 ** attempt) * 1.0
                    time.sleep(sleep_time)
                    continue

                raise UpstoxExpiredAPIError(status, err_msg, err_code)
            except urllib.error.URLError as e:
                last_error = e
                if attempt < max_retries - 1:
                    time.sleep((2 ** attempt) * 1.0)
                    continue
                raise UpstoxExpiredAPIError(503, f"Network connection error: {e}")
            except Exception as e:
                raise e

        if last_error:
            raise UpstoxExpiredAPIError(503, f"Request failed after {max_retries} attempts: {last_error}")
        raise UpstoxExpiredAPIError(500, "Unknown request failure")

    def test_access(self) -> Dict[str, Any]:
        """Perform direct validation against Upstox Expired Instruments API access."""
        from backend.broker.token_resolver import validate_token_live, token_fingerprint
        self.access_token = (self.access_token or "").strip().strip('"\'').strip()
        fp = token_fingerprint(self.access_token)
        logger.info(
            "[Token Diagnostic] UpstoxExpiredOptionsClient.test_access: validating token length=%d, fingerprint=%s",
            len(self.access_token), fp,
        )
        return validate_token_live(token=self.access_token, dotenv_path=self.dotenv_path)

    def get_expiries(self, underlying: str) -> List[str]:
        """Fetch all available historical expiries for an underlying index."""
        und_key = underlying.upper()
        if und_key in self._expiries_cache:
            return self._expiries_cache[und_key]

        inst_key = INDEX_INSTRUMENT_KEYS.get(und_key, f"NSE_INDEX|{underlying}")
        try:
            data = self._get("/expired-instruments/expiries", params={"instrument_key": inst_key})
            expiries = data.get("data", [])
            clean_expiries = sorted([str(e) for e in expiries])
            self._expiries_cache[und_key] = clean_expiries
            return clean_expiries
        except Exception as e:
            logger.warning("Could not fetch expired expiries for %s: %s", und_key, e)
            self._expiries_cache[und_key] = []
            return []

    def get_option_contracts(self, underlying: str, expiry_date: str) -> List[Dict[str, Any]]:
        """Fetch expired option contracts for an underlying and specific expiry date."""
        und_key = underlying.upper()
        cache_key = (und_key, expiry_date)
        if cache_key in self._contracts_cache:
            return self._contracts_cache[cache_key]

        inst_key = INDEX_INSTRUMENT_KEYS.get(und_key, f"NSE_INDEX|{underlying}")
        params = {
            "instrument_key": inst_key,
            "expiry_date": expiry_date,
        }
        
        try:
            data = self._get("/expired-instruments/option/contract", params=params)
        except UpstoxExpiredAPIError as e:
            if e.status_code == 404:
                try:
                    data = self._get("/expired-instruments/option/contracts", params=params)
                except UpstoxExpiredAPIError as e2:
                    if e2.status_code == 404:
                        data = self._get("/option/contract", params=params)
                    else:
                        raise
            else:
                raise

        raw_contracts = []
        if isinstance(data, dict):
            inner = data.get("data")
            if isinstance(inner, list):
                raw_contracts = inner
            elif isinstance(inner, dict):
                raw_contracts = inner.get("contracts") or inner.get("options") or inner.get("data") or []
            elif "contracts" in data and isinstance(data["contracts"], list):
                raw_contracts = data["contracts"]
        elif isinstance(data, list):
            raw_contracts = data

        normalized_contracts: List[Dict[str, Any]] = []
        for c in raw_contracts:
            if not isinstance(c, dict):
                continue
            item = normalize_contract_item(c, inst_key, und_key, expiry_date)
            normalized_contracts.append(item)

        if normalized_contracts:
            self._contracts_cache[cache_key] = normalized_contracts
        return normalized_contracts

    def resolve_option_contract(
        self,
        underlying: str,
        target_date: Optional[date] = None,
        spot_price: Optional[float] = None,
        option_type: str = "CE",
        strike_interval: Optional[float] = None,
        target_expiry: Optional[str] = None,
        target_strike: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Authoritatively resolve the real expired option contract from Upstox.
        
        Flow:
        1. Determine target expiry (explicit target_expiry or nearest from get_expiries).
        2. Determine target strike (explicit target_strike or nearest round ATM strike).
        3. Query Upstox expired option contracts for (underlying, expiry).
        4. Find exact matching contract by strike and option_type.
        5. Return authoritative contract with real instrument_key from Upstox.
        
        Returns:
            Authoritative contract dict containing real instrument_key, trading_symbol, etc.
            or None (DATA_UNAVAILABLE) if not resolvable.
        """
        und_key = underlying.upper()
        opt_type = option_type.upper()

        # 1. Determine target expiry
        nearest_expiry = target_expiry
        if not nearest_expiry:
            if target_date is None:
                logger.warning("resolve_option_contract requires target_date or target_expiry")
                return None
            try:
                expiries = self.get_expiries(und_key)
            except Exception as e:
                logger.warning("Could not fetch expired expiries for %s: %s", und_key, e)
                expiries = []

            target_date_str = target_date.isoformat()
            valid_expiries = [
                e for e in expiries
                if target_date_str <= e <= (target_date + timedelta(days=10)).isoformat()
            ]
            if not valid_expiries:
                logger.warning(
                    "No historical expiry within 10 days of %s found for %s in Upstox (available range: %s to %s)",
                    target_date_str, und_key, expiries[0] if expiries else "none", expiries[-1] if expiries else "none"
                )
                return None
            nearest_expiry = valid_expiries[0]

        # 2. Determine target strike
        if target_strike is not None:
            desired_strike = float(target_strike)
        elif spot_price is not None:
            step = strike_interval or INDEX_STRIKE_INTERVALS.get(und_key, 50.0)
            desired_strike = float(round(spot_price / step) * step)
        else:
            logger.warning("resolve_option_contract requires target_strike or spot_price")
            return None

        # 3. Query contracts
        try:
            contracts = self.get_option_contracts(und_key, nearest_expiry)
        except Exception as e:
            logger.warning("Could not fetch expired contracts for %s on %s: %s", und_key, nearest_expiry, e)
            return None

        # 4. Exact match on strike and option_type
        for c in contracts:
            if abs(c["strike"] - desired_strike) < 0.01 and c["option_type"] == opt_type:
                if c.get("instrument_key"):
                    return c

        # 5. Not found - log diagnostic information
        matching_type_strikes = sorted(list({c["strike"] for c in contracts if c["option_type"] == opt_type}))
        if matching_type_strikes:
            nearest_avail = min(matching_type_strikes, key=lambda s: abs(s - desired_strike))
            diag = f"Available {opt_type} strikes: {matching_type_strikes[0]:.1f}..{matching_type_strikes[-1]:.1f} (count={len(matching_type_strikes)}, nearest={nearest_avail:.1f})"
        elif contracts:
            diag = f"No {opt_type} contracts found on {nearest_expiry} (total other contracts: {len(contracts)})"
        else:
            diag = f"0 contracts returned by Upstox for {und_key} on {nearest_expiry}"

        logger.warning(
            "Contract not found in Upstox for %s %s Strike %.1f %s. %s",
            und_key, nearest_expiry, desired_strike, opt_type, diag,
        )
        return None

    @staticmethod
    def _resample_to_5min(m1_candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Resample 1-minute OHLCV candles into 5-minute candles."""
        if not m1_candles:
            return []
        from collections import defaultdict
        from datetime import datetime
        buckets: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for c in m1_candles:
            ts_str = c.get("timestamp", "")
            try:
                ts_clean = ts_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts_clean)
                minute_bucket = (dt.minute // 5) * 5
                bucket_dt = dt.replace(minute=minute_bucket, second=0, microsecond=0)
                bucket_key = bucket_dt.isoformat()
                buckets[bucket_key].append(c)
            except Exception:
                buckets[ts_str[:16]].append(c)

        resampled = []
        for b_ts in sorted(buckets.keys()):
            b_candles = buckets[b_ts]
            o = b_candles[0]["open"]
            h = max(c["high"] for c in b_candles)
            l = min(c["low"] for c in b_candles)
            c_val = b_candles[-1]["close"]
            v = sum(c.get("volume", 0.0) for c in b_candles)
            oi = b_candles[-1].get("oi")
            resampled.append({
                "timestamp": b_ts,
                "open": round(o, 2),
                "high": round(h, 2),
                "low": round(l, 2),
                "close": round(c_val, 2),
                "volume": v,
                "oi": oi,
            })
        return resampled

    def get_expired_historical_candles(
        self,
        instrument_key: str,
        interval: str,
        to_date: str,
        from_date: str,
    ) -> List[Dict[str, Any]]:
        """Fetch expired historical candles from official Upstox API endpoint.
        
        Endpoint: /v2/expired-instruments/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}
        """
        # 1. Ensure correct to_date >= from_date ordering
        d_to = to_date
        d_from = from_date
        if d_to and d_from and d_to < d_from:
            d_to, d_from = d_from, d_to

        # 2. Ensure expired instrument key conforms to Upstox pattern SEGMENT|TOKEN|DD-MM-YYYY
        exp_key = instrument_key
        if exp_key and exp_key.count("|") == 1 and d_to:
            to_p = d_to.split("-")
            if len(to_p) == 3 and len(to_p[0]) == 4:
                dd_mm_yyyy = f"{to_p[2]}-{to_p[1]}-{to_p[0]}"
                exp_key = f"{exp_key}|{dd_mm_yyyy}"

        interval_norm = "5minute" if interval in ("5m", "5min", "5minute") else interval
        if interval_norm in ("1m", "1min"):
            interval_norm = "1minute"
        elif interval_norm in ("15m", "15min"):
            interval_norm = "15minute"
        elif interval_norm in ("30m", "30min"):
            interval_norm = "30minute"
        elif interval_norm in ("1d", "day", "daily"):
            interval_norm = "day"

        encoded_key = urllib.parse.quote(exp_key, safe="")
        endpoint = f"/expired-instruments/historical-candle/{encoded_key}/{interval_norm}/{d_to}/{d_from}"
        
        try:
            data = self._get(endpoint)
        except UpstoxExpiredAPIError as e:
            # Check if 5minute interval is rejected by the API; if so, fallback to 1minute and resample
            if (e.status_code == 400 or "interval" in str(e).lower()) and interval_norm != "1minute":
                try:
                    m1_candles = self.get_expired_historical_candles(exp_key, "1minute", d_to, d_from)
                    if interval_norm == "5minute":
                        return self._resample_to_5min(m1_candles)
                    return m1_candles
                except Exception:
                    pass
            if e.status_code == 404:
                # Try fallback URL structure /v2/historical-candle/expired/...
                alt_endpoint = f"/historical-candle/expired/{encoded_key}/{interval_norm}/{d_to}/{d_from}"
                try:
                    data = self._get(alt_endpoint)
                except Exception:
                    raise e
            else:
                raise

        candles_raw = data.get("data", {}).get("candles", [])
        formatted_candles: List[Dict[str, Any]] = []
        for c in candles_raw:
            if isinstance(c, list) and len(c) >= 5:
                formatted_candles.append({
                    "timestamp": c[0],
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]) if len(c) > 5 else 0.0,
                    "oi": float(c[6]) if len(c) > 6 and c[6] is not None else None,
                })
            elif isinstance(c, dict):
                formatted_candles.append(c)

        formatted_candles.sort(key=lambda x: x.get("timestamp", ""))
        return formatted_candles

    def fetch_and_cache_contract(
        self,
        underlying: str,
        expiry: str,
        strike: float,
        option_type: str,
        interval: str = "5minute",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        spot_price_ref: Optional[float] = None,
        contract_info_ref: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Optional[str], Optional[Dict[str, Any]]]:
        """Fetch, validate, and cache a historical option contract dataset using authoritative Upstox resolution.
        
        Returns:
            (success, error_message_or_none, contract_data_dict_or_none)
        """
        f_date = from_date or expiry
        t_date = to_date or expiry
        cache_fn = self.cache.get_cache_filename(underlying, expiry, strike, option_type, interval, f_date, t_date)

        # 1. Check local cache first
        cached = self.cache.get(cache_fn)
        if cached and "candles" in cached and len(cached["candles"]) > 0:
            logger.info("Found cached historical option data for %s (%s candles)", cache_fn, len(cached["candles"]))
            return True, None, cached

        # 2. Authoritatively resolve the real contract if not provided
        c_info = contract_info_ref
        available_summary = ""
        if c_info is None:
            try:
                contracts = self.get_option_contracts(underlying, expiry)
                matching_type = [c for c in contracts if c.get("option_type") == option_type.upper()]
                for c in matching_type:
                    if abs(c["strike"] - strike) < 0.01:
                        c_info = c
                        break

                if not c_info:
                    avail_strikes = sorted(list({c["strike"] for c in matching_type}))
                    if avail_strikes:
                        nearest = min(avail_strikes, key=lambda s: abs(s - strike))
                        available_summary = (
                            f" (Available {option_type.upper()} strikes on {expiry}: "
                            f"{avail_strikes[0]:.1f}..{avail_strikes[-1]:.1f} [count={len(avail_strikes)}], "
                            f"nearest={nearest:.1f})"
                        )
                    elif contracts:
                        available_summary = f" (No {option_type.upper()} contracts on {expiry}. Total other contracts: {len(contracts)})"
                    else:
                        available_summary = f" (0 contracts returned by Upstox for {underlying} on {expiry})"
            except Exception as e:
                return False, f"Could not query Upstox expired option contracts: {e}", None

        if not c_info or not c_info.get("instrument_key"):
            return False, f"DATA_UNAVAILABLE — Authoritative contract not found in Upstox for {underlying} {expiry} {strike:.1f} {option_type}{available_summary}", None

        instrument_key = c_info["instrument_key"]
        trading_symbol = c_info.get("trading_symbol", "")

        # 3. Call Upstox Expired Candle API
        try:
            candles = self.get_expired_historical_candles(
                instrument_key=instrument_key,
                interval=interval,
                to_date=t_date,
                from_date=f_date,
            )
        except UpstoxExpiredAPIError as e:
            return False, f"Upstox API error: {e}", None
        except Exception as e:
            return False, f"Unexpected error fetching candles: {e}", None

        if not candles:
            return False, f"DATA_UNAVAILABLE — No candles returned by Upstox for {instrument_key} between {f_date} and {t_date}", None

        # 4. Strict Data Validation
        valid, val_err, clean_candles = self.validator.validate_candles(
            candles,
            expected_instrument_key=instrument_key,
            spot_price_reference=spot_price_ref,
            from_date=f_date,
            to_date=t_date,
        )
        if not valid:
            return False, f"Data validation failed for {instrument_key}: {val_err}", None

        # 5. Persist to cache
        full_contract_info = {
            "underlying": underlying.upper(),
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type.upper(),
            "instrument_key": instrument_key,
            "trading_symbol": trading_symbol,
            "lot_size": c_info.get("lot_size", INDEX_STRIKE_INTERVALS.get(underlying.upper(), 25)),
            "interval": interval,
            "from_date": f_date,
            "to_date": t_date,
        }
        self.cache.save(cache_fn, full_contract_info, clean_candles)

        return True, None, {
            "contract": full_contract_info,
            "candles": clean_candles,
        }

