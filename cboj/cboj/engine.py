"""Transaction processing engine.

Pipeline per transaction (input sorted by timestamp):

  1. validation rules, in order - first failure REJECTS (balances untouched)
  2. apply to balances atomically (transfer = debit + credit, both or neither)
  3. review rules - add flags but never block (spec: "valid transactions
     should still be processed even if they require review")

Data structures:
  accounts      dict[id, Account]                 O(1) lookup / balance update
  seen_ids      set[str]                          O(1) duplicate detection
  daily_debits  dict[(acct, date), Decimal]       running outgoing total per day
  recent        dict[acct, deque[datetime]]       sliding window for velocity
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Optional

from .config import DEFAULT, Config
from .models import (Account, FlagReason, RawTransaction, RejectReason, Result,
                     Status)

Rejection = Optional[tuple[RejectReason, str]]


@dataclass
class EngineState:
    accounts: dict[str, Account]
    seen_ids: set[str] = field(default_factory=set)
    daily_debits: dict[tuple[str, date], Decimal] = field(
        default_factory=lambda: defaultdict(lambda: Decimal("0")))
    recent: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))


# ---------------------------------------------------------------- validation
# Each rule: (tx, state) -> None if OK, else (reason, human detail).

def rule_well_formed(tx: RawTransaction, st: EngineState) -> Rejection:
    other = [e for e in tx.parse_errors if not e.startswith("amount")]
    if other:
        return RejectReason.MALFORMED_RECORD, "; ".join(other)
    return None


def rule_not_duplicate(tx: RawTransaction, st: EngineState) -> Rejection:
    # Assumption: an ID is "used" the first time we see it, even if that first
    # attempt was rejected. A resend must carry a new ID (idempotency key).
    if tx.transaction_id in st.seen_ids:
        return RejectReason.DUPLICATE_ID, f"{tx.transaction_id} already processed"
    return None


def rule_supported_type(tx: RawTransaction, st: EngineState) -> Rejection:
    if tx.type is None:
        return (RejectReason.UNSUPPORTED_TYPE,
                f"type {tx.raw_type!r} is not one of PURCHASE/WITHDRAWAL/DEPOSIT/TRANSFER")
    return None


def _check_account(acct_id: Optional[str], role: str, st: EngineState) -> Rejection:
    if not acct_id:
        return RejectReason.MISSING_ACCOUNT, f"{role} account is required"
    acct = st.accounts.get(acct_id)
    if acct is None:
        return RejectReason.INVALID_ACCOUNT, f"{role} account {acct_id} does not exist"
    if not acct.is_active:
        return (RejectReason.ACCOUNT_NOT_ACTIVE,
                f"{role} account {acct_id} is {acct.status.value}")
    return None


def rule_accounts_valid(tx: RawTransaction, st: EngineState) -> Rejection:
    if tx.type.debits:
        if r := _check_account(tx.from_account, "source", st):
            return r
    if tx.type.credits:
        if r := _check_account(tx.to_account, "destination", st):
            return r
    if tx.from_account and tx.from_account == tx.to_account:
        return RejectReason.INVALID_ACCOUNT, "source and destination are the same account"
    return None


def rule_amount_positive(tx: RawTransaction, st: EngineState) -> Rejection:
    amt_errors = [e for e in tx.parse_errors if e.startswith("amount")]
    if amt_errors:
        return RejectReason.INVALID_AMOUNT, amt_errors[0]
    if tx.amount <= 0:
        return RejectReason.INVALID_AMOUNT, f"amount {tx.amount} must be > 0"
    return None


def rule_sufficient_funds(tx: RawTransaction, st: EngineState) -> Rejection:
    if tx.type.debits:
        acct = st.accounts[tx.from_account]
        if acct.balance - tx.amount < 0:
            return (RejectReason.INSUFFICIENT_FUNDS,
                    f"balance {acct.balance} < amount {tx.amount}")
    return None


VALIDATION_RULES: list[Callable[[RawTransaction, EngineState], Rejection]] = [
    rule_well_formed,
    rule_not_duplicate,
    rule_supported_type,
    rule_accounts_valid,
    rule_amount_positive,
    rule_sufficient_funds,
]


# ------------------------------------------------------------------- engine

class Engine:
    def __init__(self, accounts: dict[str, Account], config: Config = DEFAULT):
        self.state = EngineState(accounts=accounts)
        self.config = config

    def process_all(self, txs: list[RawTransaction]) -> list[Result]:
        return [self.process(tx) for tx in txs]

    def process(self, tx: RawTransaction) -> Result:
        st = self.state
        self._track_velocity(tx)

        for rule in VALIDATION_RULES:
            rejection = rule(tx, st)
            if rejection:
                if tx.transaction_id:
                    st.seen_ids.add(tx.transaction_id)
                return Result(tx, Status.REJECTED, rejection[0], rejection[1])

        st.seen_ids.add(tx.transaction_id)
        balances = self._apply(tx)
        result = Result(tx, Status.APPROVED, balances_after=balances)
        self._review(tx, result)
        return result

    # -- balance update: all checks passed above, so both legs always succeed
    def _apply(self, tx: RawTransaction) -> dict[str, Decimal]:
        st = self.state
        out = {}
        if tx.type.debits:
            a = st.accounts[tx.from_account]
            a.balance -= tx.amount
            st.daily_debits[(a.account_id, tx.timestamp.date())] += tx.amount
            out[a.account_id] = a.balance
        if tx.type.credits:
            a = st.accounts[tx.to_account]
            a.balance += tx.amount
            out[a.account_id] = a.balance
        return out

    def _track_velocity(self, tx: RawTransaction) -> None:
        acct = tx.primary_account
        if not acct or tx.timestamp is None:
            return
        window = self.state.recent[acct]
        window.append(tx.timestamp)
        while window and tx.timestamp - window[0] > self.config.velocity_window:
            window.popleft()

    # -- review rules: only flag, never block
    def _review(self, tx: RawTransaction, r: Result) -> None:
        c, st = self.config, self.state

        if tx.amount >= c.large_amount_threshold:
            r.flags.append(FlagReason.LARGE_AMOUNT)
            r.flag_details.append(f"{tx.amount} >= {c.large_amount_threshold} threshold")
        elif tx.amount >= c.large_amount_threshold * c.near_threshold_ratio:
            r.flags.append(FlagReason.NEAR_THRESHOLD)
            r.flag_details.append(
                f"{tx.amount} is just under the {c.large_amount_threshold} reporting "
                f"threshold (possible structuring)")

        if tx.type.debits:
            acct = st.accounts[tx.from_account]
            spent = st.daily_debits[(acct.account_id, tx.timestamp.date())]
            if spent > acct.daily_limit:
                r.flags.append(FlagReason.DAILY_LIMIT)
                r.flag_details.append(
                    f"{acct.account_id} outgoing today {spent} > daily limit {acct.daily_limit}")

        acct_id = tx.primary_account
        n = len(st.recent[acct_id])
        if n >= c.velocity_count:
            mins = int(c.velocity_window.total_seconds() // 60)
            r.flags.append(FlagReason.HIGH_VELOCITY)
            r.flag_details.append(f"{n} transactions on {acct_id} within {mins} min")
