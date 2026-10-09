"""Loading + cleaning of the input CSVs.

Cleaning philosophy (important for a bank):
  * NORMALIZE things whose meaning is unambiguous: surrounding whitespace,
    letter case of enums ("purchase" -> PURCHASE), blank -> None.
  * NEVER GUESS money. "12.5O" might be 12.50 or 12.5 + typo'd digit; "15.005"
    has fractional cents. Those rows are rejected with a reason, not repaired.
  * NEVER DROP a row. Every input line produces a result, so the totals in the
    processing summary always reconcile with the input file.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .models import Account, AccountStatus, RawTransaction, TxType

ACCOUNT_COLUMNS = ["accountId", "customerName", "accountType", "status",
                   "balance", "dailyLimit", "currency", "openedDate"]
TX_COLUMNS = ["transactionId", "timestamp", "type", "fromAccount",
              "toAccount", "amount", "channel", "description"]

# optional sign, digits, optional 1-2 decimal places. Nothing else.
AMOUNT_RE = re.compile(r"^-?\d+(\.\d{1,2})?$")


class InputFileError(Exception):
    """The file itself is unusable (missing, wrong header) - abort the batch."""


def _clean(v: str | None) -> str:
    return (v or "").strip()


def _check_header(path: Path, found: list[str] | None, expected: list[str]) -> None:
    found = [h.strip() for h in (found or [])]
    missing = [c for c in expected if c not in found]
    if missing:
        raise InputFileError(f"{path}: missing column(s) {missing}; found {found}")


def parse_amount(text: str) -> tuple[Decimal | None, str | None]:
    """Return (amount, error). Sign is kept so the engine can reject <= 0 with
    the business reason rather than a format error."""
    t = text.strip().replace(",", "") if text else ""
    if t.startswith("$"):
        t = t[1:]
    if not t:
        return None, "amount is blank"
    if not AMOUNT_RE.match(t):
        return None, f"amount {text!r} is not a valid currency value"
    try:
        return Decimal(t), None
    except InvalidOperation:  # pragma: no cover - regex already guards this
        return None, f"amount {text!r} is not a number"


def load_accounts(path: str | Path) -> tuple[dict[str, Account], list[str]]:
    """Returns ({accountId: Account}, data-quality issues).
    A dict gives O(1) lookup by ID for every transaction."""
    path = Path(path)
    if not path.exists():
        raise InputFileError(f"accounts file not found: {path}")
    accounts: dict[str, Account] = {}
    issues: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _check_header(path, reader.fieldnames, ACCOUNT_COLUMNS)
        for line_no, row in enumerate(reader, start=2):
            row = {k.strip(): _clean(v) for k, v in row.items() if k}
            aid = row["accountId"].upper()
            if not aid:
                issues.append(f"accounts line {line_no}: blank accountId - skipped")
                continue
            if aid in accounts:
                issues.append(f"accounts line {line_no}: duplicate accountId {aid} - kept first")
                continue
            try:
                status = AccountStatus(row["status"].upper())
            except ValueError:
                issues.append(f"accounts line {line_no}: unknown status {row['status']!r} "
                              f"for {aid} - treated as FROZEN (not active)")
                status = AccountStatus.FROZEN
            bal, err = parse_amount(row["balance"])
            if err:
                issues.append(f"accounts line {line_no}: {aid} balance {err} - treated as FROZEN")
                bal, status = Decimal("0"), AccountStatus.FROZEN
            limit, err = parse_amount(row["dailyLimit"])
            if err:
                issues.append(f"accounts line {line_no}: {aid} dailyLimit {err} - limit set to 0 "
                              f"(every debit will be flagged)")
                limit = Decimal("0")
            accounts[aid] = Account(
                account_id=aid,
                customer_name=row["customerName"],
                account_type=row["accountType"].upper(),
                status=status,
                balance=bal,
                daily_limit=limit,
                currency=row["currency"].upper() or "CAD",
                opened_date=row["openedDate"],
            )
    return accounts, issues


def load_transactions(path: str | Path) -> list[RawTransaction]:
    path = Path(path)
    if not path.exists():
        raise InputFileError(f"transactions file not found: {path}")
    txs: list[RawTransaction] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _check_header(path, reader.fieldnames, TX_COLUMNS)
        for line_no, row in enumerate(reader, start=2):
            row = {k.strip(): _clean(v) for k, v in row.items() if k}
            txs.append(_clean_tx(line_no, row))
    # Process in time order; Python's sort is stable so ties keep file order.
    # Rows with an unparseable timestamp go last (they will be rejected anyway).
    txs.sort(key=lambda t: (t.timestamp is None, t.timestamp or datetime.min))
    return txs


def _clean_tx(line_no: int, row: dict[str, str]) -> RawTransaction:
    errors: list[str] = []
    notes: list[str] = []

    tid = row.get("transactionId", "").upper()
    if not tid:
        errors.append("transactionId is blank")

    ts = None
    raw_ts = row.get("timestamp", "")
    try:
        ts = datetime.fromisoformat(raw_ts)
    except ValueError:
        errors.append(f"timestamp {raw_ts!r} is not ISO-8601")

    raw_type = row.get("type", "")
    tx_type = None
    try:
        tx_type = TxType(raw_type.upper())
        if raw_type != tx_type.value:
            notes.append(f"type {raw_type!r} normalized to {tx_type.value}")
    except ValueError:
        pass  # engine rejects as UNSUPPORTED TRANSACTION TYPE with the raw value

    amount, err = parse_amount(row.get("amount", ""))
    if err:
        errors.append(err)

    return RawTransaction(
        line_no=line_no,
        transaction_id=tid,
        timestamp=ts,
        raw_type=raw_type,
        type=tx_type,
        from_account=row.get("fromAccount", "").upper() or None,
        to_account=row.get("toAccount", "").upper() or None,
        amount=amount,
        raw_amount=row.get("amount", ""),
        channel=row.get("channel", "").upper(),
        description=row.get("description", ""),
        parse_errors=errors,
        cleaning_notes=notes,
    )
