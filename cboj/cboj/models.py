"""Core data model for the CBOJ transaction processing engine.

Money is always `Decimal`, never float: 0.1 + 0.2 != 0.3 in binary floating
point, and a bank cannot be off by a cent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    CLOSED = "CLOSED"
    DORMANT = "DORMANT"


class TxType(str, Enum):
    PURCHASE = "PURCHASE"      # debit fromAccount
    WITHDRAWAL = "WITHDRAWAL"  # debit fromAccount
    DEPOSIT = "DEPOSIT"        # credit toAccount
    TRANSFER = "TRANSFER"      # debit fromAccount, credit toAccount

    @property
    def debits(self) -> bool:
        return self in (TxType.PURCHASE, TxType.WITHDRAWAL, TxType.TRANSFER)

    @property
    def credits(self) -> bool:
        return self in (TxType.DEPOSIT, TxType.TRANSFER)


class Status(str, Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RejectReason(str, Enum):
    MALFORMED_RECORD = "MALFORMED RECORD"
    DUPLICATE_ID = "DUPLICATE TRANSACTION ID"
    UNSUPPORTED_TYPE = "UNSUPPORTED TRANSACTION TYPE"
    MISSING_ACCOUNT = "MISSING ACCOUNT"
    INVALID_ACCOUNT = "INVALID ACCOUNT"
    ACCOUNT_NOT_ACTIVE = "ACCOUNT NOT ACTIVE"
    INVALID_AMOUNT = "INVALID AMOUNT"
    INSUFFICIENT_FUNDS = "INSUFFICIENT FUNDS"


class FlagReason(str, Enum):
    LARGE_AMOUNT = "LARGE AMOUNT"
    NEAR_THRESHOLD = "NEAR REPORTING THRESHOLD"
    DAILY_LIMIT = "DAILY LIMIT EXCEEDED"
    HIGH_VELOCITY = "HIGH VELOCITY"


@dataclass
class Account:
    account_id: str
    customer_name: str
    account_type: str
    status: AccountStatus
    balance: Decimal
    daily_limit: Decimal
    currency: str
    opened_date: str

    @property
    def is_active(self) -> bool:
        return self.status is AccountStatus.ACTIVE


@dataclass
class RawTransaction:
    """A transaction row after cleaning. Fields that could not be parsed are
    None and the problem is recorded in `parse_errors` - the row is never
    dropped, so every input line gets an APPROVED/REJECTED result."""
    line_no: int
    transaction_id: str
    timestamp: Optional[datetime]
    raw_type: str
    type: Optional[TxType]
    from_account: Optional[str]
    to_account: Optional[str]
    amount: Optional[Decimal]
    raw_amount: str
    channel: str
    description: str
    parse_errors: list[str] = field(default_factory=list)
    cleaning_notes: list[str] = field(default_factory=list)

    @property
    def primary_account(self) -> Optional[str]:
        """The account a person/analyst thinks of this transaction as 'on'."""
        return self.from_account or self.to_account


@dataclass
class Result:
    tx: RawTransaction
    status: Status
    reject_reason: Optional[RejectReason] = None
    reject_detail: str = ""
    flags: list[FlagReason] = field(default_factory=list)
    flag_details: list[str] = field(default_factory=list)
    # balances after this transaction was applied (approved only)
    balances_after: dict[str, Decimal] = field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    def line(self) -> str:
        tid = self.tx.transaction_id or f"<line {self.tx.line_no}>"
        if self.status is Status.APPROVED:
            s = f"{tid} APPROVED"
            if self.flagged:
                s += " [FLAGGED: " + ", ".join(f.value for f in self.flags) + "]"
            return s
        return f"{tid} REJECTED - {self.reject_reason.value}"
