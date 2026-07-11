"""Upstox REST API v2 client — production grade with retry and error handling."""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"
V3_BASE_URL = "https://api.upstox.com/v3"

# Upstox v3 Historical Candle Data API interval mapping: (unit, interval_number).
# This is what actually fixes the old v2 bug where 5minute/15minute were
# silently rewritten to 30minute — v3 genuinely supports all of these.
V3_INTERVAL_MAP: Dict[str, Tuple[str, int]] = {
    "1minute": ("minutes", 1), "1min": ("minutes", 1),
    "3minute": ("minutes", 3), "3min": ("minutes", 3),
    "5minute": ("minutes", 5), "5min": ("minutes", 5),
    "15minute": ("minutes", 15), "15min": ("minutes", 15),
    "30minute": ("minutes", 30), "30min": ("minutes", 30),
    "60minute": ("minutes", 60), "hour": ("minutes", 60),
    "day": ("days", 1), "1day": ("days", 1),
    "week": ("weeks", 1),
    "month": ("months", 1),
}

# Instrument key map for NIFTY50 stocks (NSE_EQ|ISIN format)
SYMBOL_TO_KEY: Dict[str, str] = {
    "RELIANCE":   "NSE_EQ|INE002A01018",
    "TCS":        "NSE_EQ|INE467B01029",
    "HDFCBANK":   "NSE_EQ|INE040A01034",
    "INFY":       "NSE_EQ|INE009A01021",
    "ICICIBANK":  "NSE_EQ|INE090A01021",
    "HINDUNILVR": "NSE_EQ|INE030A01027",
    "KOTAKBANK":  "NSE_EQ|INE237A01028",
    "LT":         "NSE_EQ|INE018A01030",
    "SBIN":       "NSE_EQ|INE062A01020",
    "AXISBANK":   "NSE_EQ|INE238A01034",
    "BHARTIARTL": "NSE_EQ|INE397D01024",
    "ITC":        "NSE_EQ|INE154A01025",
    "ASIANPAINT": "NSE_EQ|INE021A01026",
    "MARUTI":     "NSE_EQ|INE585B01010",
    "HCLTECH":    "NSE_EQ|INE860A01027",
    "SUNPHARMA":  "NSE_EQ|INE044A01036",
    "WIPRO":      "NSE_EQ|INE075A01022",
    "TITAN":      "NSE_EQ|INE280A01028",
    "ULTRACEMCO": "NSE_EQ|INE481G01011",
    "BAJFINANCE": "NSE_EQ|INE296A01024",
    "NESTLEIND":  "NSE_EQ|INE239N01024",
    "TECHM":      "NSE_EQ|INE669C01036",
    "NTPC":       "NSE_EQ|INE733E01010",
    "POWERGRID":  "NSE_EQ|INE752E01010",
    "ONGC":       "NSE_EQ|INE213A01029",
    "JSWSTEEL":   "NSE_EQ|INE019A01038",
    "TATASTEEL":  "NSE_EQ|INE081A01020",
    "HINDALCO":   "NSE_EQ|INE038A01020",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
    "M&M":        "NSE_EQ|INE101A01026",
    "BAJAJFINSV": "NSE_EQ|INE918I01026",
    "DRREDDY":    "NSE_EQ|INE089A01031",
    "CIPLA":      "NSE_EQ|INE059A01026",
    "DIVISLAB":   "NSE_EQ|INE361B01024",
    "APOLLOHOSP": "NSE_EQ|INE437A01024",
    "ADANIENT":   "NSE_EQ|INE423A01024",
    "ADANIPORTS": "NSE_EQ|INE742F01042",
    "COALINDIA":  "NSE_EQ|INE522F01014",
    "BPCL":       "NSE_EQ|INE029A01011",
    "EICHERMOT":  "NSE_EQ|INE066A01021",
    "HEROMOTOCO": "NSE_EQ|INE158A01026",
    "INDUSINDBK": "NSE_EQ|INE095A01012",
    "SBILIFE":    "NSE_EQ|INE123W01016",
    "HDFCLIFE":   "NSE_EQ|INE795G01014",
    "GRASIM":     "NSE_EQ|INE047A01021",
    "TATACONSUM": "NSE_EQ|INE192A01025",
    "UPL":        "NSE_EQ|INE628A01036",
    "BRITANNIA":  "NSE_EQ|INE216A01030",
    "SHREECEM":   "NSE_EQ|INE070A01015",
    "BAJAJ-AUTO": "NSE_EQ|INE917I01010",
}


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3, read=3, connect=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


class UpstoxAPIError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Upstox API {status_code}: {message}")


class UpstoxClient:
    """Production Upstox REST API v2 client."""

    def __init__(
        self,
        access_token: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: int = 15,
    ) -> None:
        self.access_token = access_token or os.getenv("UPSTOX_ACCESS_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = _build_session()

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        return self._get_url(f"{self.base_url}{path}", params)

    def _get_url(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        try:
            r = self._session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        except requests.Timeout:
            raise UpstoxAPIError(408, f"Timeout for {url}")
        except requests.ConnectionError as e:
            raise UpstoxAPIError(503, f"Connection error: {e}")

        if r.status_code == 401:
            raise UpstoxAPIError(401, "Token invalid or expired — go to Settings and generate a new token.")
        if r.status_code == 410:
            raise UpstoxAPIError(410, "This API endpoint is deprecated. Update to Upstox API v2.")
        if r.status_code == 429:
            raise UpstoxAPIError(429, "Rate limit hit.")
        if r.status_code == 403:
            raise UpstoxAPIError(403, "Access forbidden — check API permissions in Upstox developer portal.")

        try:
            data = r.json()
        except ValueError:
            raise UpstoxAPIError(r.status_code, f"Invalid JSON: {r.text[:200]}")

        if r.status_code >= 400:
            # Extract Upstox error message
            err = data.get("message") or data.get("errors") or str(data)
            if isinstance(err, list):
                err = "; ".join(str(e) for e in err)
            raise UpstoxAPIError(r.status_code, str(err))

        return data

    # ─── Auth ─────────────────────────────────────────────────────────────────

    def is_token_valid(self) -> bool:
        if not self.access_token or len(self.access_token) < 20:
            return False
        try:
            data = self._get("/user/profile")
            return data.get("status") == "success" or "data" in data
        except UpstoxAPIError:
            return False

    def get_profile(self) -> Dict[str, Any]:
        return self._get("/user/profile")

    # ─── Market data ──────────────────────────────────────────────────────────

    def get_live_quote(self, symbol: str) -> Dict[str, Any]:
        """Get live market quote. Returns ltp=0 when market is closed (expected)."""
        instrument_key = SYMBOL_TO_KEY.get(symbol.upper(), f"NSE_EQ|{symbol}")
        try:
            data = self._get(
                "/market-quote/quotes",
                params={"instrument_key": instrument_key},
            )
            raw = data.get("data", {})
            # Upstox response key can be the instrument key or symbol
            quote = raw.get(instrument_key) or (list(raw.values())[0] if raw else {})
            if not quote:
                return self._empty_quote(symbol)

            ohlc  = quote.get("ohlc", {})
            ltp   = float(quote.get("last_price", 0) or 0)
            prev  = float(ohlc.get("close", ltp) or ltp)
            chg   = ltp - prev
            chg_p = (chg / prev * 100) if prev else 0.0

            return {
                "symbol": symbol,
                "ltp": ltp,
                "open":   float(ohlc.get("open",  0) or 0),
                "high":   float(ohlc.get("high",  0) or 0),
                "low":    float(ohlc.get("low",   0) or 0),
                "close":  float(ohlc.get("close", 0) or 0),
                "volume": int(quote.get("volume", 0) or 0),
                "change":     round(chg,   2),
                "change_pct": round(chg_p, 3),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except UpstoxAPIError:
            raise
        except Exception as e:
            logger.warning("get_live_quote %s: %s", symbol, e)
            return self._empty_quote(symbol)

    def get_multiple_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """Batch fetch quotes for multiple symbols (stocks or indices) in
        one API call."""
        if not symbols:
            return {}
        keys = ",".join(ALL_INSTRUMENTS.get(s.upper(), f"NSE_EQ|{s}") for s in symbols)
        try:
            data = self._get("/market-quote/quotes", params={"instrument_key": keys})
            raw = data.get("data", {})
            result: Dict[str, Any] = {}
            for sym in symbols:
                key = ALL_INSTRUMENTS.get(sym.upper(), f"NSE_EQ|{sym}")
                q = raw.get(key, {})
                if q:
                    ohlc = q.get("ohlc", {})
                    ltp  = float(q.get("last_price", 0) or 0)
                    prev = float(ohlc.get("close", ltp) or ltp)
                    chg  = ltp - prev
                    result[sym] = {
                        "symbol": sym, "ltp": ltp,
                        "open":   float(ohlc.get("open",  0) or 0),
                        "high":   float(ohlc.get("high",  0) or 0),
                        "low":    float(ohlc.get("low",   0) or 0),
                        "close":  float(ohlc.get("close", 0) or 0),
                        "volume": int(q.get("volume", 0) or 0),
                        "change":     round(chg, 2),
                        "change_pct": round((chg / prev * 100) if prev else 0, 3),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "has_data": True,
                    }
                else:
                    result[sym] = self._empty_quote(sym)
            return result
        except Exception as e:
            logger.warning("get_multiple_quotes error: %s", e)
            return {s: self._empty_quote(s) for s in symbols}

    def get_historical_candles(
        self,
        symbol: str,
        interval: str = "day",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch OHLCV candles from the Upstox v3 Historical Candle Data API.

        v3 fixed the v2 limitation that silently remapped 5minute/15minute
        requests to 30minute — it now genuinely supports 1/3/5/15/30-minute,
        daily, weekly, and monthly candles via:
            GET /v3/historical-candle/{instrument_key}/{unit}/{interval}/{to_date}/{from_date}
        where unit ∈ {minutes, days, weeks, months} and interval is an int.

        Minute-level data has a limited per-request window, so multi-month
        ranges are fetched in ~25-day chunks and concatenated — this is what
        makes a full year of 5-minute backtesting actually possible (the old
        client fetched one request's worth and quietly gave up).
        """
        if "|" in symbol:
            instrument_key = symbol
        else:
            instrument_key = ALL_INSTRUMENTS.get(symbol.upper(), f"NSE_EQ|{symbol}")

        unit, unit_interval = V3_INTERVAL_MAP.get(interval.lower(), ("days", 1))

        if not to_date:
            to_date = date.today().strftime("%Y-%m-%d")
        if not from_date:
            days_back = 30 if unit == "minutes" else 365
            from_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        to_dt = date.fromisoformat(to_date)
        from_dt = date.fromisoformat(from_date)

        # Chunk minute-granularity requests — Upstox v3 rejects overly wide
        # windows at fine granularity with an explicit error rather than
        # silently truncating, so we chunk proactively instead of guessing.
        chunk_days = 25 if unit == "minutes" else (180 if unit == "days" else 3650)

        all_rows: List[List[Any]] = []
        chunk_end = to_dt
        while chunk_end >= from_dt:
            chunk_start = max(from_dt, chunk_end - timedelta(days=chunk_days))
            path = (
                f"/historical-candle/{instrument_key}/{unit}/{unit_interval}"
                f"/{chunk_end.isoformat()}/{chunk_start.isoformat()}"
            )
            try:
                data = self._get_url(f"{V3_BASE_URL}{path}")
                rows = data.get("data", {}).get("candles", [])
                all_rows.extend(rows)
            except UpstoxAPIError as e:
                logger.warning(
                    "Historical candle chunk failed for %s %s/%s [%s..%s]: %s "
                    "— continuing with remaining chunks (gap, not fabricated data)",
                    symbol, unit, unit_interval, chunk_start, chunk_end, e,
                )
            chunk_end = chunk_start - timedelta(days=1)

        try:
            # Dedupe (chunks can overlap at boundaries) and sort oldest-first.
            seen: set = set()
            result: List[Dict[str, Any]] = []
            for c in all_rows:
                if len(c) < 6:
                    continue
                ts = str(c[0])
                if ts in seen:
                    continue
                seen.add(ts)
                result.append({
                    "timestamp": ts,
                    "open":   float(c[1]),
                    "high":   float(c[2]),
                    "low":    float(c[3]),
                    "close":  float(c[4]),
                    "volume": int(c[5]),
                })
            result.sort(key=lambda r: r["timestamp"])
            return result[-limit:] if limit else result
        except UpstoxAPIError as e:
            logger.error("Historical candles failed for %s (%s): %s", symbol, interval, e)
            raise
        except Exception as e:
            raise UpstoxAPIError(500, str(e))

    def get_historical_candles_full_range(
        self, symbol: str, interval: str, from_date: str, to_date: str,
    ) -> List[Dict[str, Any]]:
        """Same as get_historical_candles but returns every candle in the
        range with no `limit` truncation — used by the backtest engine,
        which needs the complete history, not just the most recent N bars."""
        return self.get_historical_candles(
            symbol, interval, from_date=from_date, to_date=to_date, limit=0,
        )

    # ─── Options ────────────────────────────────────────────────────────────

    def get_option_chain(self, underlying_symbol: str, expiry_date: str) -> List[Dict[str, Any]]:
        """
        Fetch the option chain for an underlying (index or stock) and expiry.

        Endpoint: GET /option/chain?instrument_key=...&expiry_date=YYYY-MM-DD

        Returns a flat list of contracts:
          [{"strike": 22000.0, "option_type": "CE", "instrument_key": "...",
            "ltp": 123.45, "close_price": 118.0, "volume": 5000, "oi": 12000}, ...]

        Never returns fabricated contracts — an API failure raises
        UpstoxAPIError, and the caller (OptionPremiumStrategy) treats an
        empty chain as "contract not resolved", not as a signal to trade.
        """
        instrument_key = ALL_INSTRUMENTS.get(underlying_symbol.upper(), f"NSE_EQ|{underlying_symbol}")
        try:
            data = self._get("/option/chain", params={
                "instrument_key": instrument_key,
                "expiry_date": expiry_date,
            })
            raw = data.get("data", [])
            contracts: List[Dict[str, Any]] = []
            for row in raw:
                strike = row.get("strike_price")
                for opt_type, key_field in (("CE", "call_options"), ("PE", "put_options")):
                    opt = row.get(key_field)
                    if not opt:
                        continue
                    market_data = opt.get("market_data", {}) or {}
                    contracts.append({
                        "strike": float(strike) if strike is not None else None,
                        "option_type": opt_type,
                        "instrument_key": opt.get("instrument_key"),
                        "ltp": market_data.get("ltp"),
                        "close_price": market_data.get("close_price"),
                        "volume": market_data.get("volume"),
                        "oi": market_data.get("oi"),
                    })
            return contracts
        except UpstoxAPIError as e:
            logger.error("Option chain fetch failed for %s (%s): %s", underlying_symbol, expiry_date, e)
            raise
        except Exception as e:
            raise UpstoxAPIError(500, str(e))

    # ─── Orders ───────────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "D",
    ) -> Dict[str, Any]:
        instrument_key = SYMBOL_TO_KEY.get(symbol.upper(), f"NSE_EQ|{symbol}")
        payload = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": price,
            "tag": "upstox-bot",
            "instrument_token": instrument_key,
            "order_type": order_type.upper(),
            "transaction_type": transaction_type.upper(),
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "is_amo": False,
        }
        url = f"{self.base_url}/order/place"
        r = self._session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
        data = r.json()
        if r.status_code == 200 and data.get("status") == "success":
            return {"success": True, "order_id": data.get("data", {}).get("order_id"), "raw": data}
        raise UpstoxAPIError(r.status_code, data.get("message", str(data)))

    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        data = self._get("/order/details", params={"order_id": order_id})
        return data.get("data", {})

    def cancel_order(self, order_id: str) -> bool:
        try:
            r = self._session.delete(
                f"{self.base_url}/order/cancel",
                params={"order_id": order_id},
                headers=self._headers(), timeout=self.timeout,
            )
            return r.json().get("status") == "success"
        except Exception:
            return False

    def get_positions(self) -> List[Dict[str, Any]]:
        try:
            return self._get("/portfolio/short-term-positions").get("data", [])
        except Exception:
            return []

    def get_funds(self) -> Dict[str, Any]:
        try:
            data = self._get("/user/get-funds-and-margin", params={"segment": "SEC"})
            eq = data.get("data", {}).get("equity", {})
            return {
                "available_margin": float(eq.get("available_margin", 0) or 0),
                "used_margin": float(eq.get("used_margin", 0) or 0),
                "total": float(eq.get("net", 0) or 0),
            }
        except Exception:
            return {"available_margin": 0.0, "used_margin": 0.0, "total": 0.0}

    # ─── Helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _empty_quote(symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol, "ltp": 0.0, "open": 0.0, "high": 0.0,
            "low": 0.0, "close": 0.0, "volume": 0, "change": 0.0, "change_pct": 0.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "has_data": False,  # explicit: placeholder, not a real ₹0.00 price
        }

    # Backward compat
    def get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        return self._get(path, params)

# ── Market Indices (NSE) ──────────────────────────────────────────────────────
INDEX_TO_KEY: Dict[str, str] = {
    "NIFTY50":   "NSE_INDEX|Nifty 50",
    "NIFTY 50":  "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "NIFTY BANK":"NSE_INDEX|Nifty Bank",
    "FINNIFTY":  "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY":"NSE_INDEX|NIFTY MID SELECT",
    "SENSEX":    "BSE_INDEX|SENSEX",
}

# Combined lookup: check indices first, then stocks
ALL_INSTRUMENTS: Dict[str, str] = {**INDEX_TO_KEY, **SYMBOL_TO_KEY}

# Instrument categories for UI
INSTRUMENT_CATEGORIES = {
    "indices": list(INDEX_TO_KEY.keys())[:5],
    "nifty50": list(SYMBOL_TO_KEY.keys()),
}

