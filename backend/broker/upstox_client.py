"""Upstox REST API v2 client — production grade with retry and error handling."""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    requests = None
    HTTPAdapter = None
    Retry = None

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

def _build_session() -> Any:
    if requests is None:
        return None
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
        from backend.broker.token_resolver import resolve_upstox_token
        self.access_token = resolve_upstox_token(access_token)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = _build_session()

    def _headers(self) -> Dict[str, str]:
        h = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    def _resolve_key(self, symbol: str) -> str:
        """Resolve a trading symbol to its instrument_key, preferring the
        live daily-refreshed Upstox instrument master over the canonical
        index map. Raw index or option instrument keys pass through untouched."""
        if "|" in symbol:
            if not symbol.upper().startswith(("NSE_INDEX|", "BSE_INDEX|", "NSE_FO|", "BSE_FO|")):
                raise UpstoxAPIError(400, f"Unsupported non-options instrument key: {symbol}")
            return symbol
        from backend.config.universe_config import VALID_OPTION_INDICES
        if symbol.upper() not in VALID_OPTION_INDICES:
            raise UpstoxAPIError(400, f"Unsupported index option underlying: {symbol}")
        from backend.broker.instrument_master import resolve_instrument_key
        normalized = symbol.upper()
        static_fallback = ALL_INSTRUMENTS.get(normalized)
        resolved = resolve_instrument_key(symbol, static_fallback=static_fallback)
        if resolved:
            return resolved
        raise UpstoxAPIError(400, f"Unsupported index option underlying: {symbol}")

    def _get(self, path: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        return self._get_url(f"{self.base_url}{path}", params)

    def _get_url(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        if self._session is not None and requests is not None:
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

        # Fallback to standard library urllib for zero-dependency runtime
        full_url = url
        if params:
            query_str = urllib.parse.urlencode(params)
            full_url = f"{full_url}?{query_str}"

        req = urllib.request.Request(full_url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                try:
                    data = json.loads(body)
                except ValueError:
                    raise UpstoxAPIError(status, f"Invalid JSON: {body[:200]}")

                if status >= 400 or data.get("status") == "error":
                    err = data.get("message") or data.get("errors") or str(data)
                    if isinstance(err, list):
                        err = "; ".join(str(e) for e in err)
                    raise UpstoxAPIError(status, str(err))

                return data
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = e.read().decode("utf-8")
                parsed = json.loads(body)
                err = parsed.get("message") or parsed.get("errors") or body[:200]
                if isinstance(err, list):
                    err = "; ".join(str(item) for item in err)
            except Exception:
                err = str(e)

            if status == 401:
                raise UpstoxAPIError(401, "Token invalid or expired — go to Settings and generate a new token.")
            if status == 403:
                raise UpstoxAPIError(403, "Access forbidden — check API permissions in Upstox developer portal.")
            if status == 429:
                raise UpstoxAPIError(429, "Rate limit hit.")
            raise UpstoxAPIError(status, str(err))
        except urllib.error.URLError as e:
            raise UpstoxAPIError(503, f"Connection error: {e}")
        except Exception as e:
            if isinstance(e, UpstoxAPIError):
                raise
            raise UpstoxAPIError(500, str(e))

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
        instrument_key = self._resolve_key(symbol)
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
        """Batch fetch quotes for multiple supported index underlyings in
        one API call."""
        if not symbols:
            return {}
        keys = ",".join(self._resolve_key(s) for s in symbols)
        try:
            data = self._get("/market-quote/quotes", params={"instrument_key": keys})
            raw = data.get("data", {})
            result: Dict[str, Any] = {}
            for sym in symbols:
                key = self._resolve_key(sym)
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
        instrument_key = self._resolve_key(symbol)

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
        encoded_key = urllib.parse.quote(instrument_key, safe="")
        chunk_end = to_dt
        while chunk_end >= from_dt:
            chunk_start = max(from_dt, chunk_end - timedelta(days=chunk_days))
            path = (
                f"/historical-candle/{encoded_key}/{unit}/{unit_interval}"
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

    def get_option_expiries(self, underlying_symbol: str) -> List[str]:
        """
        All available expiry dates for a supported index option underlying.

        Endpoint: GET /option/contract?instrument_key=... (no expiry_date
        param returns contracts across every available expiry).

        Returns ISO date strings ("YYYY-MM-DD"), sorted ascending. Never
        fabricates a date — an API failure raises UpstoxAPIError, and an
        empty list means "Upstox genuinely has no contracts for this
        underlying right now," not "assume some date."
        """
        instrument_key = self._resolve_key(underlying_symbol)
        try:
            data = self._get("/option/contract", params={"instrument_key": instrument_key})
            raw = data.get("data", [])
            expiries = sorted({row["expiry"] for row in raw if row.get("expiry")})
            return expiries
        except UpstoxAPIError:
            raise
        except Exception as e:
            raise UpstoxAPIError(500, str(e))

    def get_nearest_expiry(self, underlying_symbol: str) -> Optional[str]:
        """The nearest expiry that hasn't already passed. Returns None
        (never a guessed date) if Upstox has no upcoming expiries or the
        call fails."""
        try:
            expiries = self.get_option_expiries(underlying_symbol)
        except UpstoxAPIError as e:
            logger.warning("Could not fetch expiries for %s: %s", underlying_symbol, e)
            return None
        today = date.today().isoformat()
        upcoming = [e for e in expiries if e >= today]
        return upcoming[0] if upcoming else None

    def get_option_chain(self, underlying_symbol: str, expiry_date: str) -> List[Dict[str, Any]]:
        """
        Fetch the option chain for a supported index underlying and expiry.

        Endpoint: GET /option/chain?instrument_key=...&expiry_date=YYYY-MM-DD

        Returns a flat list of contracts:
          [{"strike": 22000.0, "option_type": "CE", "instrument_key": "...",
            "ltp": 123.45, "close_price": 118.0, "volume": 5000, "oi": 12000,
            "bid_price": 122.0, "ask_price": 124.0,
            "iv": 14.2, "delta": 0.52, "theta": -8.3, "gamma": 0.004, "vega": 12.1}, ...]

        Greeks/IV come straight from Upstox's own `option_greeks` block — this
        client never computes or estimates them itself. Never returns
        fabricated contracts — an API failure raises UpstoxAPIError, and the
        caller (OptionPremiumStrategy) treats an empty chain as "contract not
        resolved," not as a signal to trade.
        """
        instrument_key = self._resolve_key(underlying_symbol)
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
                    greeks = opt.get("option_greeks", {}) or {}
                    contract = {
                        "strike": float(strike) if strike is not None else None,
                        "option_type": opt_type,
                        "instrument_key": opt.get("instrument_key"),
                        "ltp": market_data.get("ltp"),
                        "close_price": market_data.get("close_price"),
                        "volume": market_data.get("volume"),
                        "oi": market_data.get("oi"),
                        "oi_change": market_data.get("oi_change"),
                        "bid_price": market_data.get("bid_price"),
                        "ask_price": market_data.get("ask_price"),
                        "iv": greeks.get("iv"),
                        "delta": greeks.get("delta"),
                        "theta": greeks.get("theta"),
                        "gamma": greeks.get("gamma"),
                        "vega": greeks.get("vega"),
                        "lot_size": opt.get("lot_size") or row.get("lot_size"),
                        "freeze_quantity": opt.get("freeze_quantity") or row.get("freeze_quantity"),
                    }
                    if contract["instrument_key"]:
                        from backend.broker.instrument_master import get_instrument_metadata
                        metadata = get_instrument_metadata(contract["instrument_key"])
                        contract["lot_size"] = contract["lot_size"] or metadata.get("lot_size")
                        contract["freeze_quantity"] = contract["freeze_quantity"] or metadata.get("freeze_quantity")
                    contracts.append(contract)
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
        instrument_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Place an order. If `instrument_key` is provided (e.g. for option
        contracts), it is used DIRECTLY — `_resolve_key(symbol)` is only
        used as a fallback for non-option/underlying orders."""
        if instrument_key and "|" in instrument_key:
            resolved_key = instrument_key
        else:
            resolved_key = self._resolve_key(symbol)
        payload = {
            "quantity": quantity,
            "product": product,
            "validity": "DAY",
            "price": price,
            "tag": "upstox-bot",
            "instrument_token": resolved_key,
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

    def get_positions_with_details(self) -> List[Dict[str, Any]]:
        """Fetch positions with instrument keys for reconciliation."""
        try:
            data = self._get("/portfolio/short-term-positions")
            positions = data.get("data", [])
            return [
                {
                    "instrument_key": p.get("instrument_token") or p.get("instrument_key"),
                    "quantity": int(p.get("quantity", 0) or 0),
                    "average_price": float(p.get("average_price", 0) or 0),
                    "buy_quantity": int(p.get("buy_quantity", 0) or 0),
                    "sell_quantity": int(p.get("sell_quantity", 0) or 0),
                    "pnl": float(p.get("pnl", 0) or 0),
                    "product": p.get("product"),
                    "trading_symbol": p.get("trading_symbol"),
                }
                for p in positions
            ]
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

    # ─── Option-specific quotes ────────────────────────────────────────────────

    def get_quote_by_instrument_key(self, instrument_key: str) -> Dict[str, Any]:
        """Retrieve a quote by raw instrument_key (e.g. 'NSE_FO|123456').

        Unlike get_live_quote(), this does NOT resolve through _resolve_key —
        it sends the instrument key directly. Used for:
          - Paper execution price discovery
          - Emergency price refresh for open option positions
          - Position reconciliation
          - Diagnostics
        """
        try:
            data = self._get(
                "/market-quote/quotes",
                params={"instrument_key": instrument_key},
            )
            raw = data.get("data", {})
            quote = raw.get(instrument_key) or (list(raw.values())[0] if raw else {})
            if not quote:
                return self._empty_quote(instrument_key)

            ohlc = quote.get("ohlc", {})
            ltp = float(quote.get("last_price", 0) or 0)
            prev = float(ohlc.get("close", ltp) or ltp)
            chg = ltp - prev
            chg_p = (chg / prev * 100) if prev else 0.0

            depth = quote.get("depth", {})
            best_bid = None
            best_ask = None
            buy_entries = depth.get("buy", []) if depth else []
            sell_entries = depth.get("sell", []) if depth else []
            if buy_entries and buy_entries[0].get("price"):
                best_bid = float(buy_entries[0]["price"])
            if sell_entries and sell_entries[0].get("price"):
                best_ask = float(sell_entries[0]["price"])

            return {
                "symbol": instrument_key,
                "instrument_key": instrument_key,
                "ltp": ltp,
                "open": float(ohlc.get("open", 0) or 0),
                "high": float(ohlc.get("high", 0) or 0),
                "low": float(ohlc.get("low", 0) or 0),
                "close": float(ohlc.get("close", 0) or 0),
                "volume": int(quote.get("volume", 0) or 0),
                "change": round(chg, 2),
                "change_pct": round(chg_p, 3),
                "bid_price": best_bid,
                "ask_price": best_ask,
                "oi": quote.get("oi"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "has_data": True,
            }
        except UpstoxAPIError:
            raise
        except Exception as e:
            logger.warning("get_quote_by_instrument_key %s: %s", instrument_key, e)
            return self._empty_quote(instrument_key)

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
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY":  "NSE_INDEX|Nifty Fin Service",
    "MIDCPNIFTY":"NSE_INDEX|NIFTY MID SELECT",
    "SENSEX":    "BSE_INDEX|SENSEX",
    "BANKEX":    "BSE_INDEX|BANKEX",
}

# Runtime subscriptions are deliberately limited to index underlyings. Option
# contract keys are added dynamically only after a chain has selected them.
ALL_INSTRUMENTS: Dict[str, str] = dict(INDEX_TO_KEY)

# Instrument categories for UI
INSTRUMENT_CATEGORIES = {
    "indices": list(INDEX_TO_KEY.keys()),
    "option_underlyings": list(INDEX_TO_KEY.keys()),
}

