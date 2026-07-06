"""Upstox REST API client — production-grade with retry, rate-limit, and timeout handling."""
from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"

# Upstox instrument key format: NSE_EQ|{ISIN} or NSE_EQ|{symbol}
# For market quotes the API uses instrument_key
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
    "NTPC":       "NSE_EQ|INE733E01010",
    "POWERGRID":  "NSE_EQ|INE752E01010",
    "ONGC":       "NSE_EQ|INE213A01029",
    "TATASTEEL":  "NSE_EQ|INE081A01020",
    "TATAMOTORS": "NSE_EQ|INE155A01022",
}


def _build_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """Create a requests Session with retry and backoff."""
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["GET", "POST", "PUT", "DELETE"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


class UpstoxAPIError(Exception):
    """Raised when the Upstox API returns an error response."""
    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Upstox API error {status_code}: {message}")


class UpstoxClient:
    """Production-grade Upstox REST client with retry, rate-limit, and timeout handling."""

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
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise UpstoxAPIError(408, f"Request timed out for {path}")
        except requests.exceptions.ConnectionError as e:
            raise UpstoxAPIError(503, f"Connection error: {e}")

        if resp.status_code == 401:
            raise UpstoxAPIError(401, "Access token invalid or expired. Regenerate token.")
        if resp.status_code == 429:
            raise UpstoxAPIError(429, "Rate limit hit. Slow down requests.")
        if resp.status_code == 403:
            raise UpstoxAPIError(403, "Access forbidden. Check API permissions.")

        try:
            data = resp.json()
        except ValueError:
            raise UpstoxAPIError(resp.status_code, f"Invalid JSON response: {resp.text[:200]}")

        if resp.status_code >= 400:
            msg = data.get("message") or data.get("errors") or str(data)
            raise UpstoxAPIError(resp.status_code, str(msg))

        return data

    # ─── Token validation ─────────────────────────────────────────────────────

    def is_token_valid(self) -> bool:
        """Check if the access token is set and valid by calling /user/profile."""
        if not self.access_token or len(self.access_token) < 20:
            return False
        try:
            data = self._get("/user/profile")
            return data.get("status") == "success" or "data" in data
        except UpstoxAPIError:
            return False

    def get_profile(self) -> Dict[str, Any]:
        """Get authenticated user profile."""
        return self._get("/user/profile")

    # ─── Market data ──────────────────────────────────────────────────────────

    def get_live_quote(self, symbol: str) -> Dict[str, Any]:
        """Get live market quote (LTP, OHLC, volume) for a symbol."""
        instrument_key = SYMBOL_TO_KEY.get(symbol, f"NSE_EQ|{symbol}")
        try:
            data = self._get(
                "/market-quote/quotes",
                params={"instrument_key": instrument_key},
            )
            # Upstox v2 response: data.data[instrument_key]
            raw = data.get("data", {})
            quote_data = raw.get(instrument_key, raw)
            if not quote_data:
                return self._empty_quote(symbol)

            ohlc = quote_data.get("ohlc", {})
            ltp = quote_data.get("last_price", 0.0)
            prev_close = ohlc.get("close", ltp) or ltp
            change = ltp - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0.0

            return {
                "symbol": symbol,
                "ltp": float(ltp),
                "open": float(ohlc.get("open", 0)),
                "high": float(ohlc.get("high", 0)),
                "low": float(ohlc.get("low", 0)),
                "close": float(ohlc.get("close", 0)),
                "volume": int(quote_data.get("volume", 0)),
                "change": round(change, 2),
                "change_pct": round(change_pct, 3),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except UpstoxAPIError:
            raise
        except Exception as e:
            logger.warning("get_live_quote %s error: %s", symbol, e)
            return self._empty_quote(symbol)

    def get_multiple_quotes(self, symbols: List[str]) -> Dict[str, Any]:
        """Batch fetch live quotes for multiple symbols in a single API call."""
        if not symbols:
            return {}
        keys = ",".join(SYMBOL_TO_KEY.get(s, f"NSE_EQ|{s}") for s in symbols)
        try:
            data = self._get("/market-quote/quotes", params={"instrument_key": keys})
            raw = data.get("data", {})
            result = {}
            for sym in symbols:
                key = SYMBOL_TO_KEY.get(sym, f"NSE_EQ|{sym}")
                q = raw.get(key, {})
                if q:
                    ohlc = q.get("ohlc", {})
                    ltp = float(q.get("last_price", 0))
                    prev = float(ohlc.get("close", ltp) or ltp)
                    change = ltp - prev
                    result[sym] = {
                        "symbol": sym,
                        "ltp": ltp,
                        "open": float(ohlc.get("open", 0)),
                        "high": float(ohlc.get("high", 0)),
                        "low": float(ohlc.get("low", 0)),
                        "close": float(ohlc.get("close", 0)),
                        "volume": int(q.get("volume", 0)),
                        "change": round(change, 2),
                        "change_pct": round((change / prev * 100) if prev else 0, 3),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    result[sym] = self._empty_quote(sym)
            return result
        except Exception as e:
            logger.warning("get_multiple_quotes error: %s", e)
            return {sym: self._empty_quote(sym) for sym in symbols}

    def get_historical_candles(
        self,
        symbol: str,
        interval: str = "15minute",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical OHLCV candles from Upstox.

        interval: 1minute | 3minute | 5minute | 10minute | 15minute | 30minute |
                  60minute | day | week | month
        """
        instrument_key = SYMBOL_TO_KEY.get(symbol, f"NSE_EQ|{symbol}")

        if not to_date:
            to_date = date.today().strftime("%Y-%m-%d")
        if not from_date:
            days_back = max(limit // 60, 5)
            from_date = (date.today() - timedelta(days=days_back)).strftime("%Y-%m-%d")

        # Upstox v2 historical candles endpoint
        path = f"/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}"
        try:
            data = self._get(path)
            candles_raw = data.get("data", {}).get("candles", [])
            result = []
            for c in candles_raw:
                # Format: [timestamp, open, high, low, close, volume, oi]
                if len(c) < 6:
                    continue
                result.append({
                    "timestamp": c[0],
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": int(c[5]),
                })
            # Most recent last
            result.reverse()
            return result[-limit:]
        except UpstoxAPIError as e:
            logger.error("Historical candles failed for %s: %s", symbol, e)
            raise
        except Exception as e:
            logger.error("Historical candles error for %s: %s", symbol, e)
            raise UpstoxAPIError(500, str(e))

    # ─── Order management ─────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        transaction_type: str,  # BUY | SELL
        quantity: int,
        order_type: str = "MARKET",  # MARKET | LIMIT | SL | SL-M
        price: float = 0.0,
        trigger_price: float = 0.0,
        product: str = "D",  # D=delivery, I=intraday
    ) -> Dict[str, Any]:
        """Place an order on Upstox."""
        instrument_key = SYMBOL_TO_KEY.get(symbol, f"NSE_EQ|{symbol}")
        payload = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": price,
            "tag": "upstox-bot",
            "instrument_token": instrument_key,
            "order_type": order_type,
            "transaction_type": transaction_type.upper(),
            "disclosed_quantity": 0,
            "trigger_price": trigger_price,
            "is_amo": False,
        }
        url = f"{self.base_url}/order/place"
        try:
            resp = self._session.post(url, json=payload, headers=self._headers(), timeout=self.timeout)
            data = resp.json()
            if resp.status_code == 200 and data.get("status") == "success":
                return {"success": True, "order_id": data.get("data", {}).get("order_id"), "raw": data}
            raise UpstoxAPIError(resp.status_code, data.get("message", str(data)))
        except UpstoxAPIError:
            raise
        except Exception as e:
            raise UpstoxAPIError(500, f"Order placement error: {e}")

    def get_order_details(self, order_id: str) -> Dict[str, Any]:
        """Get order status from Upstox."""
        data = self._get(f"/order/details", params={"order_id": order_id})
        return data.get("data", {})

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            url = f"{self.base_url}/order/cancel"
            resp = self._session.delete(
                url,
                params={"order_id": order_id},
                headers=self._headers(),
                timeout=self.timeout,
            )
            data = resp.json()
            return data.get("status") == "success"
        except Exception:
            return False

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all open positions."""
        try:
            data = self._get("/portfolio/short-term-positions")
            return data.get("data", [])
        except Exception:
            return []

    def get_funds(self) -> Dict[str, Any]:
        """Get available funds/margin."""
        try:
            data = self._get("/user/get-funds-and-margin", params={"segment": "SEC"})
            raw = data.get("data", {})
            equity = raw.get("equity", raw)
            return {
                "available_margin": float(equity.get("available_margin", 0)),
                "used_margin": float(equity.get("used_margin", 0)),
                "total": float(equity.get("net", 0)),
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
        }

    # ─── Backwards-compat (used by old code) ─────────────────────────────────

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Legacy GET method — preserved for backward compatibility."""
        return self._get(path, params)
