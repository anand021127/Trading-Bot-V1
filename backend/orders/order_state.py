"""Authoritative order state machine (PHASE 5).

One module defines the order lifecycle states, the legal transition graph,
and the fill aggregator used to maintain partial-fill truth. Every execution
component (pipeline, OrderManager, reconciliation) must use these — no
component may invent its own status transitions.

Design decisions (documented, enforced by tests):
  * Broker "COMPLETE/FILLED" is authoritative for FILLED; "PENDING/OPEN" map
    onto SUBMITTED/OPEN; unknown broker strings map to UNKNOWN, never guessed.
  * A CANCELLED broker order that carries fills is a legal terminal state —
    the FILLS are real even though the order is cancelled. The fill
    aggregator keeps them; positions reflect only actual fills.
  * FILLED → CREATED, REJECTED → FILLED, CANCELLED → FILLED (same order id)
    are illegal; a genuinely new attempt must be a NEW order with a NEW
    signal id (idempotency layer guarantees that).
  * UNKNOWN is a resting state demanding reconciliation, never auto-promoted
    to FILLED/REJECTED without broker evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


class OrderState(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"


# ── Legal transition graph ────────────────────────────────────────────
# CREATED starts; terminal states: FILLED, REJECTED, CANCELLED, FAILED.
# UNKNOWN/RECONCILING cycle back into any broker-reported state after
# reconciliation resolves them (transition function validates each hop).
_LEGAL: Dict[OrderState, frozenset] = {
    OrderState.CREATED: frozenset({
        OrderState.VALIDATING,          # normal start
        OrderState.FAILED,              # rejected locally before validation
        OrderState.REJECTED,            # pre-trade validation failure
    }),
    OrderState.VALIDATING: frozenset({
        OrderState.VALIDATED,
        OrderState.REJECTED,            # validation failed
        OrderState.FAILED,              # internal error during validation
    }),
    OrderState.VALIDATED: frozenset({
        OrderState.SUBMITTING,
        OrderState.FAILED,              # submission infra error before send
    }),
    OrderState.SUBMITTING: frozenset({
        OrderState.SUBMITTED,           # broker acknowledged
        OrderState.UNKNOWN,             # response lost — MUST reconcile
        OrderState.FAILED,              # broker definitively refused (no ack)
        OrderState.REJECTED,            # synchronous broker rejection
    }),
    OrderState.SUBMITTED: frozenset({
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCEL_REQUESTED,
        OrderState.CANCELLED,           # broker cancelled before any fill
        OrderState.REJECTED,            # async broker rejection
        OrderState.UNKNOWN,             # status query failed
        OrderState.RECONCILING,
    }),
    OrderState.PARTIALLY_FILLED: frozenset({
        OrderState.PARTIALLY_FILLED,    # more partial fills (self-loop)
        OrderState.FILLED,
        OrderState.CANCEL_REQUESTED,
        OrderState.CANCELLED,           # cancelled with SOME fills — fills stay
        OrderState.UNKNOWN,
        OrderState.RECONCILING,
    }),
    OrderState.FILLED: frozenset(),     # terminal
    OrderState.REJECTED: frozenset(),   # terminal
    OrderState.CANCELLED: frozenset(),  # terminal (any fills already recorded)
    OrderState.FAILED: frozenset(),     # terminal
    OrderState.UNKNOWN: frozenset({
        # ONLY reconciliation may resolve UNKNOWN:
        OrderState.RECONCILING,
    }),
    OrderState.RECONCILING: frozenset({
        OrderState.SUBMITTED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.UNKNOWN,             # broker could not be reached again
        OrderState.FAILED,
    }),
    OrderState.CANCEL_REQUESTED: frozenset({
        OrderState.CANCELLED,
        OrderState.PARTIALLY_FILLED,    # more fills raced the cancel
        OrderState.FILLED,              # fully filled before cancel landed
        OrderState.UNKNOWN,
        OrderState.RECONCILING,
    }),
}

TERMINAL_STATES = frozenset({
    OrderState.FILLED, OrderState.REJECTED, OrderState.CANCELLED, OrderState.FAILED,
})


class IllegalTransitionError(ValueError):
    pass


def can_transition(current: "OrderState | str", target: "OrderState | str") -> bool:
    c = OrderState(current)
    t = OrderState(target)
    return t in _LEGAL.get(c, frozenset())


def transition(current: "OrderState | str", target: "OrderState | str") -> OrderState:
    """Validate and return the target state; raises on illegal transitions."""
    c = OrderState(current)
    t = OrderState(target)
    if t not in _LEGAL.get(c, frozenset()):
        raise IllegalTransitionError(
            f"Illegal order transition {c.value} -> {t.value}"
        )
    return t


# ── Broker status normalization (extends order_models mapping) ────────
_BROKER_MAP = {
    "COMPLETE": OrderState.FILLED,
    "COMPLETED": OrderState.FILLED,
    "FILLED": OrderState.FILLED,
    "PARTIAL": OrderState.PARTIALLY_FILLED,
    "PARTIALLY_FILLED": OrderState.PARTIALLY_FILLED,
    "OPEN": OrderState.SUBMITTED,
    "TRIGGER_PENDING": OrderState.SUBMITTED,
    "TRIGGER PENDING": OrderState.SUBMITTED,
    "PENDING": OrderState.SUBMITTED,
    "PUT ORDER REQ RECEIVED": OrderState.SUBMITTED,
    "VALIDATION PENDING": OrderState.SUBMITTED,
    "SUBMITTED": OrderState.SUBMITTED,
    "NEW": OrderState.SUBMITTED,
    "CANCELLED": OrderState.CANCELLED,
    "CANCELED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
    "REJECT": OrderState.REJECTED,
    "FAILED": OrderState.FAILED,
}


def broker_state(raw: Optional[str]) -> OrderState:
    """Map a raw broker status string onto the authoritative state.
    Unknown strings map to UNKNOWN — never guessed."""
    if not raw:
        return OrderState.UNKNOWN
    key = " ".join(str(raw).strip().upper().split())  # normalize whitespace too
    return _BROKER_MAP.get(key, OrderState.UNKNOWN)


# ── Partial-fill aggregation ──────────────────────────────────────────
@dataclass
class FillRecord:
    quantity: int
    price: float
    timestamp: str = ""
    fill_id: str = ""


@dataclass
class FillAggregator:
    """Maintains filled/remaining/average-price truth across fills.

    capital_used is ALWAYS actual average fill price × actual filled
    quantity (the trade-accounting contract). A cancelled order keeps
    whatever fills it already received.
    """
    requested_quantity: int
    fills: List[FillRecord] = field(default_factory=list)
    state: OrderState = OrderState.SUBMITTED

    @property
    def filled_quantity(self) -> int:
        return sum(f.quantity for f in self.fills)

    @property
    def remaining_quantity(self) -> int:
        return max(0, self.requested_quantity - self.filled_quantity)

    @property
    def average_fill_price(self) -> float:
        qty = self.filled_quantity
        if qty <= 0:
            return 0.0
        return sum(f.price * f.quantity for f in self.fills) / qty

    @property
    def capital_used(self) -> float:
        return self.average_fill_price * self.filled_quantity

    def add_fill(self, quantity: int, price: float, timestamp: str = "", fill_id: str = "") -> "FillAggregator":
        if int(quantity) <= 0:
            raise ValueError("fill quantity must be positive")
        if float(price) <= 0:
            raise ValueError("fill price must be positive")
        if self.filled_quantity + int(quantity) > self.requested_quantity:
            raise ValueError(
                f"fill {quantity} would exceed requested {self.requested_quantity} "
                f"(already filled {self.filled_quantity}) — duplicate/overfill rejected"
            )
        if self.state in TERMINAL_STATES and self.state is not OrderState.CANCELLED:
            raise IllegalTransitionError(f"cannot add fills in terminal state {self.state.value}")
        self.fills.append(FillRecord(int(quantity), float(price), timestamp, fill_id))
        self._refresh_state()
        return self

    def apply_broker_status(self, raw_status: Optional[str]) -> OrderState:
        """Fold a broker status report onto the fill truth.

        REJECTED/CANCELLED never erase already-recorded fills (broker truth:
        fills happened). FILLED requires the fills to actually sum to the
        requested quantity — a broker claiming COMPLETE without full fills
        leaves the state at PARTIALLY_FILLED for reconciliation.
        """
        target = broker_state(raw_status)
        if target is OrderState.UNKNOWN:
            # do not lose current state; flag for reconciliation
            return self.state
        if target in (OrderState.REJECTED, OrderState.FAILED):
            if self.filled_quantity > 0:
                # contradictory report: fills exist — reconcile, don't trust
                self.state = OrderState.RECONCILING
                return self.state
            self.state = target
            return self.state
        if target is OrderState.CANCELLED:
            self.state = OrderState.CANCELLED
            return self.state
        if target is OrderState.FILLED:
            if self.remaining_quantity == 0 and self.filled_quantity > 0:
                self.state = OrderState.FILLED
            elif self.filled_quantity > 0:
                self.state = OrderState.PARTIALLY_FILLED  # needs reconciliation
            else:
                self.state = OrderState.RECONCILING      # FILLED with zero fills?
            return self.state
        if target is OrderState.PARTIALLY_FILLED:
            self.state = OrderState.PARTIALLY_FILLED if self.filled_quantity > 0 else OrderState.RECONCILING
            return self.state
        if self.state in (OrderState.SUBMITTED, OrderState.CANCEL_REQUESTED):
            self.state = target
        return self.state

    def _refresh_state(self) -> None:
        if self.remaining_quantity == 0 and self.filled_quantity > 0:
            self.state = OrderState.FILLED
        elif self.filled_quantity > 0:
            self.state = OrderState.PARTIALLY_FILLED


__all__ = [
    "OrderState", "TERMINAL_STATES", "IllegalTransitionError",
    "can_transition", "transition", "broker_state",
    "FillRecord", "FillAggregator",
]
