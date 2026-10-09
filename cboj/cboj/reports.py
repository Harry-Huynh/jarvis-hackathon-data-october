"""Turns engine results into the required outputs plus a review queue file."""
from __future__ import annotations

import csv
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from .models import Account, Result, Status


def summary_text(results: list[Result]) -> str:
    approved = sum(r.status is Status.APPROVED for r in results)
    rejected = len(results) - approved
    flagged = sum(r.flagged for r in results)
    lines = [
        "PROCESSING SUMMARY",
        "==================",
        f"Transactions Processed: {len(results)}",
        f"Approved: {approved}",
        f"Rejected: {rejected}",
        f"Flagged For Review: {flagged}",
        "",
        "Rejections by reason:",
    ]
    for reason, n in Counter(r.reject_reason.value for r in results
                             if r.status is Status.REJECTED).most_common():
        lines.append(f"  {reason:<30} {n}")
    lines += ["", "Flags by reason (a transaction can have several):"]
    for reason, n in Counter(f.value for r in results for f in r.flags).most_common():
        lines.append(f"  {reason:<30} {n}")
    total_in = sum(r.tx.amount for r in results
                   if r.status is Status.APPROVED and r.tx.type.credits and not r.tx.type.debits)
    total_out = sum(r.tx.amount for r in results
                    if r.status is Status.APPROVED and r.tx.type.debits and not r.tx.type.credits)
    lines += ["", f"Approved deposits (money in):           ${total_in:,.2f}",
              f"Approved purchases/withdrawals (out):   ${total_out:,.2f}"]
    return "\n".join(lines) + "\n"


def flagged_report_text(results: list[Result], accounts: dict[str, Account]) -> str:
    flagged = [r for r in results if r.flagged]
    lines = ["FLAGGED TRANSACTIONS - MANUAL REVIEW REPORT",
             "===========================================",
             f"{len(flagged)} approved transaction(s) require review.", ""]
    by_acct = Counter(r.tx.primary_account for r in flagged)
    lines.append("Accounts with most flags:")
    for acct, n in by_acct.most_common(5):
        a = accounts.get(acct)
        lines.append(f"  {acct} {a.customer_name if a else '':<20} {n} flag(s)")
    lines.append("")
    hdr = f"{'TX ID':<9} {'TIME':<8} {'ACCOUNT':<8} {'TYPE':<10} {'AMOUNT':>11}  REASONS"
    lines += [hdr, "-" * len(hdr)]
    for r in flagged:
        t = r.tx
        lines.append(f"{t.transaction_id:<9} {t.timestamp:%H:%M:%S} {t.primary_account:<8} "
                     f"{t.type.value:<10} {t.amount:>11,.2f}  "
                     + ", ".join(f.value for f in r.flags))
        for d in r.flag_details:
            lines.append(f"{'':<9}   - {d}")
    return "\n".join(lines) + "\n"


def data_quality_text(results: list[Result], account_issues: list[str]) -> str:
    lines = ["DATA QUALITY REPORT", "===================", "", "Accounts file:"]
    lines += [f"  {i}" for i in account_issues] or ["  no issues"]
    lines += ["", "Transactions file (cleaned automatically):"]
    notes = [(r.tx, n) for r in results for n in r.tx.cleaning_notes]
    lines += [f"  line {t.line_no} {t.transaction_id}: {n}" for t, n in notes] or ["  none"]
    lines += ["", "Transactions file (unrepairable - rejected):"]
    errs = [(r.tx, e) for r in results for e in r.tx.parse_errors]
    lines += [f"  line {t.line_no} {t.transaction_id}: {e}" for t, e in errs] or ["  none"]
    return "\n".join(lines) + "\n"


def _money(d: Decimal | None) -> str:
    return "" if d is None else f"{d:.2f}"


def write_outputs(out: Path, results: list[Result], accounts: dict[str, Account],
                  account_issues: list[str]) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = {k: out / v for k, v in {
        "results": "results.txt", "results_csv": "results.csv",
        "summary": "summary.txt", "flagged": "flagged_report.txt",
        "quality": "data_quality.txt", "accounts": "accounts_after.csv",
        "queue": "review_queue.json"}.items()}

    paths["results"].write_text("\n".join(r.line() for r in results) + "\n")
    paths["summary"].write_text(summary_text(results))
    paths["flagged"].write_text(flagged_report_text(results, accounts))
    paths["quality"].write_text(data_quality_text(results, account_issues))

    with paths["results_csv"].open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["line", "transactionId", "timestamp", "type", "fromAccount", "toAccount",
                    "amount", "channel", "description", "status", "rejectReason",
                    "rejectDetail", "flags", "flagDetails"])
        for r in results:
            t = r.tx
            w.writerow([t.line_no, t.transaction_id, t.timestamp.isoformat() if t.timestamp else "",
                        t.type.value if t.type else t.raw_type, t.from_account or "",
                        t.to_account or "", t.raw_amount if t.amount is None else _money(t.amount),
                        t.channel, t.description, r.status.value,
                        r.reject_reason.value if r.reject_reason else "", r.reject_detail,
                        "|".join(f.value for f in r.flags), " | ".join(r.flag_details)])

    write_accounts(paths["accounts"], accounts)

    # Review queue: everything an analyst needs, so the review CLI does not
    # need to re-run the engine.
    queue = []
    for r in results:
        if not r.flagged:
            continue
        t = r.tx
        same_acct = [o for o in results if o.tx.primary_account == t.primary_account
                     or t.primary_account in (o.tx.from_account, o.tx.to_account)]
        queue.append({
            "transactionId": t.transaction_id,
            "timestamp": t.timestamp.isoformat(),
            "type": t.type.value,
            "fromAccount": t.from_account,
            "toAccount": t.to_account,
            "amount": _money(t.amount),
            "channel": t.channel,
            "description": t.description,
            "flags": [f.value for f in r.flags],
            "flagDetails": r.flag_details,
            "balancesAfter": {k: _money(v) for k, v in r.balances_after.items()},
            "accountActivity": [
                {"transactionId": o.tx.transaction_id,
                 "time": o.tx.timestamp.strftime("%H:%M:%S") if o.tx.timestamp else "",
                 "type": o.tx.type.value if o.tx.type else o.tx.raw_type,
                 "amount": _money(o.tx.amount) or o.tx.raw_amount,
                 "description": o.tx.description,
                 "status": o.status.value,
                 "note": (o.reject_reason.value if o.reject_reason else
                          ",".join(f.value for f in o.flags))}
                for o in same_acct],
            "review": {"status": "PENDING", "analyst": None, "decidedAt": None, "note": None},
        })
    paths["queue"].write_text(json.dumps(queue, indent=2, ensure_ascii=False))
    return paths


def write_accounts(path: Path, accounts: dict[str, Account]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["accountId", "customerName", "accountType", "status", "balance",
                    "dailyLimit", "currency", "openedDate"])
        for a in accounts.values():
            w.writerow([a.account_id, a.customer_name, a.account_type, a.status.value,
                        _money(a.balance), _money(a.daily_limit), a.currency, a.opened_date])
