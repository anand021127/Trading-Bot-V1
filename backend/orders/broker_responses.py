"""Ambiguous broker response classification (PHASE 5).

THE critical live-trading hazard: the bot submits an order, the network dies,
the client sees a timeout — but the broker may have ACCEPTED the order.
Re-submitting creates a duplicate position. This module classifies every
broker-call outcome into exactly one of:

  * ACKED            — broker returned an order id (safe to track)
  * REJECTED_KNOWN   — broker definitively refused BEFORE acceptance
                       (order never existed) — safe to surface/retry-as-new
  * UNKNOWN          — outcome not observable (timeout, reset, 5xx, DNS,
                       malformed). The order MAY exist at the broker.
  * LOCAL_ERROR      — client-side bug/validation; nothing was sent

UNKNOWN is a hard gate: the pipeline must reconcile with the broker (query
order book / positions by tag) before ANY retry. Retrying blind is a
defect, not a strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ResponseKind(str, Enum):
    ACKED = "ACKED"
    REJECTED_KNOWN = "REJECTED_KNOWN"
    UNKNOWN = "UNKNOWN"
    LOCAL_ERROR = "LOCAL_ERROR"


class ResponseOutcome(str, Enum):
    """Detailed outcome — machine-readable for logs/observability."""
    ORDER_ACKED = "ORDER_ACKED"
    BROKER_REJECTED = "BROKER_REJECTED"          # explicit synchronous rejection
    TIMEOUT_UNKNOWN_STATE = "TIMEOUT_UNKNOWN_STATE"      # incl. broker-side 408
    CONNECTION_RESET_UNKNOWN_STATE = "CONNECTION_RESET_UNKNOWN_STATE"
    SERVER_ERROR_UNKNOWN_STATE = "SERVER_ERROR_UNKNOWN_STATE"  # 5xx
    NETWORK_UNREACHABLE_UNKNOWN_STATE = "NETWORK_UNREACHABLE_UNKNOWN_STATE"  # DNS etc.
    MALFORMED_RESPONSE_UNKNOWN_STATE = "MALFORMED_RESPONSE_UNKNOWN_STATE"
    LOCAL_VALIDATION_ERROR = "LOCAL_VALIDATION_ERROR"


# Status codes that mean "the broker MIGHT have processed the request".
_AMBIGUOUS_5XX = {500, 502, 503, 504}


@dataclass
class BrokerResponse:
    kind: ResponseKind
    outcome: ResponseOutcome
    order_id: Optional[str] = None
    detail: str = ""
    requires_reconciliation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "outcome": self.outcome.value,
            "order_id": self.order_id,
            "detail": self.detail,
            "requires_reconciliation": self.requires_reconciliation,
        }


def _status_code(exc: BaseException) -> Optional[int]:
    return getattr(exc, "status_code", None) or getattr(exc, "code", None) \
        if isinstance(getattr(exc, "code", None), int) else getattr(exc, "status_code", None)


def classify_broker_exception(exc: BaseException) -> BrokerResponse:
    """Classify an exception raised by a broker submit call.

    The rule: if we cannot PROVE the broker did not see the request, the
    outcome is UNKNOWN and requires reconciliation. Only a broker response
    that explicitly rejects the order (4xx from the order API before any
    order-id exists) proves non-acceptance.
    """
    name = type(exc).__name__.lower()
    msg = str(exc)
    status = getattr(exc, "status_code", None)

    # Timeout family: request may have reached the broker.
    if "timeout" in name or "timedout" in name or isinstance(exc, TimeoutError):
        return BrokerResponse(
            ResponseKind.UNKNOWN, ResponseOutcome.TIMEOUT_UNKNOWN_STATE,
            detail=msg, requires_reconciliation=True,
        )
    # Connection reset / broken pipe: request may have reached the broker.
    if any(k in name for k in ("reset", "brokenpipe", "connectionabort", "incomplete")):
        return BrokerResponse(
            ResponseKind.UNKNOWN, ResponseOutcome.CONNECTION_RESET_UNKNOWN_STATE,
            detail=msg, requires_reconciliation=True,
        )
    # HTTP status classification
    if isinstance(status, int):
        if status in _AMBIGUOUS_5XX or status == 408:
            return BrokerResponse(
                ResponseKind.UNKNOWN, ResponseOutcome.SERVER_ERROR_UNKNOWN_STATE,
                detail=msg, requires_reconciliation=True,
            )
        if 400 <= status < 500:
            # The broker parsed and REFUSED the request — no order exists.
            # 408 is the exception (handled above): a timeout, not a refusal.
            return BrokerResponse(
                ResponseKind.REJECTED_KNOWN, ResponseOutcome.BROKER_REJECTED,
                detail=msg,
            )
    # DNS / unreachable: request likely never sent, but proxies/gateways can
    # still have delivered it — treat as unknown, reconcile.
    if any(k in name for k in ("gaierror", "nameor service", "nameresolution", "connectionerror", "oserror")) \
            or "unreachable" in msg.lower() or "name or service" in msg.lower():
        return BrokerResponse(
            ResponseKind.UNKNOWN, ResponseOutcome.NETWORK_UNREACHABLE_UNKNOWN_STATE,
            detail=msg, requires_reconciliation=True,
        )
    # Malformed/unparseable broker payload: the broker MIGHT have acted.
    if any(k in name for k in ("json", "decode", "value", "key", "type")) and "malformed" in msg.lower() \
            or "malformed" in msg.lower() or "expecting value" in msg.lower():
        return BrokerResponse(
            ResponseKind.UNKNOWN, ResponseOutcome.MALFORMED_RESPONSE_UNKNOWN_STATE,
            detail=msg, requires_reconciliation=True,
        )
    # Unknown exception shape: assume the worst (unknown), reconcile.
    return BrokerResponse(
        ResponseKind.UNKNOWN, ResponseOutcome.MALFORMED_RESPONSE_UNKNOWN_STATE,
        detail=f"{type(exc).__name__}: {msg}", requires_reconciliation=True,
    )


def classify_place_response(raw: Any) -> BrokerResponse:
    """Classify a *successful* broker HTTP response body for an order submit."""
    if raw is None:
        return BrokerResponse(
            ResponseKind.UNKNOWN, ResponseOutcome.MALFORMED_RESPONSE_UNKNOWN_STATE,
            detail="empty response body", requires_reconciliation=True,
        )
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        order_id = data.get("order_id") or data.get("orderId") or raw.get("order_id")
        if order_id:
            return BrokerResponse(
                ResponseKind.ACKED, ResponseOutcome.ORDER_ACKED, order_id=str(order_id),
            )
        status_code = raw.get("status")
        if isinstance(status_code, int) and 400 <= status_code < 500:
            return BrokerResponse(
                ResponseKind.REJECTED_KNOWN, ResponseOutcome.BROKER_REJECTED,
                detail=str(raw.get("errors") or raw.get("description") or raw)[:500],
            )
        # 2xx without an order id — ambiguous: unknown whether the order exists.
        return BrokerResponse(
            ResponseKind.UNKNOWN, ResponseOutcome.MALFORMED_RESPONSE_UNKNOWN_STATE,
            detail=f"no order_id in response: {str(raw)[:200]}", requires_reconciliation=True,
        )
    return BrokerResponse(
        ResponseKind.UNKNOWN, ResponseOutcome.MALFORMED_RESPONSE_UNKNOWN_STATE,
        detail=f"unexpected response type {type(raw).__name__}", requires_reconciliation=True,
    )


__all__ = [
    "ResponseKind", "ResponseOutcome", "BrokerResponse",
    "classify_broker_exception", "classify_place_response",
]
