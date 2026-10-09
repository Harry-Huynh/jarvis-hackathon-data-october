"""Manual review of flagged transactions.

Flagged transactions were already APPROVED and applied (per spec). An analyst
works the queue and records one decision per transaction:

  CLEAR     legitimate - no change to balances
  REVERSE   confirmed fraud/error - undo its balance effect (optionally freeze)
  ESCALATE  needs compliance / a senior analyst (e.g. FINTRAC filing)

Every decision is written to review_queue.json (current state) AND appended to
review_audit.csv (append-only trail: who, what, when, why). Balances changed
by a reversal are written back to accounts_after.csv.
"""
from __future__ import annotations

import csv
import getpass
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

DECISIONS = {"c": "CLEARED", "r": "REVERSED", "e": "ESCALATED"}

# ------------------------------------------------------------------ colours
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


bold = lambda s: _c("1", s)          # noqa: E731
red = lambda s: _c("31", s)          # noqa: E731
green = lambda s: _c("32", s)        # noqa: E731
yellow = lambda s: _c("33", s)       # noqa: E731
dim = lambda s: _c("2", s)           # noqa: E731

STATUS_COLOR = {"PENDING": yellow, "CLEARED": green, "REVERSED": red, "ESCALATED": bold}


# -------------------------------------------------------------------- store
class ReviewStore:
    def __init__(self, out_dir: Path):
        self.out = out_dir
        self.queue_path = out_dir / "review_queue.json"
        self.accounts_path = out_dir / "accounts_after.csv"
        self.audit_path = out_dir / "review_audit.csv"
        if not self.queue_path.exists():
            raise SystemExit(f"No review queue at {self.queue_path}. Run `process` first.")
        self.queue: list[dict] = json.loads(self.queue_path.read_text())
        self.by_id = {q["transactionId"]: q for q in self.queue}

    def save(self) -> None:
        tmp = self.queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.queue, indent=2, ensure_ascii=False))
        tmp.replace(self.queue_path)  # atomic: never leaves a half-written file

    def pending(self) -> list[dict]:
        return [q for q in self.queue if q["review"]["status"] == "PENDING"]

    # -- accounts_after.csv
    def _read_accounts(self) -> tuple[list[str], list[dict]]:
        with self.accounts_path.open(newline="", encoding="utf-8") as f:
            r = csv.DictReader(f)
            return r.fieldnames, list(r)

    def _write_accounts(self, fields, rows) -> None:
        with self.accounts_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)

    def account(self, acct_id: str) -> dict | None:
        return next((a for a in self._read_accounts()[1] if a["accountId"] == acct_id), None)

    def decide(self, tx_id: str, decision: str, analyst: str, note: str,
               freeze: bool = False) -> list[str]:
        """Apply a decision. Returns human-readable messages describing effects."""
        item = self.by_id.get(tx_id)
        if item is None:
            raise KeyError(f"{tx_id} is not in the review queue")
        if item["review"]["status"] != "PENDING":
            raise ValueError(f"{tx_id} already {item['review']['status']} - decisions are final")
        if decision not in DECISIONS.values():
            raise ValueError(f"unknown decision {decision}")
        if decision in ("REVERSED", "ESCALATED") and not note.strip():
            raise ValueError("a note is required to reverse or escalate")

        msgs = []
        if decision == "REVERSED":
            msgs += self._reverse(item, freeze)

        item["review"] = {"status": decision, "analyst": analyst,
                          "decidedAt": datetime.now().isoformat(timespec="seconds"),
                          "note": note.strip() or None}
        self.save()
        new = not self.audit_path.exists()
        with self.audit_path.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["decidedAt", "analyst", "transactionId", "decision",
                            "flags", "amount", "note"])
            w.writerow([item["review"]["decidedAt"], analyst, tx_id, decision,
                        "|".join(item["flags"]), item["amount"], note.strip()])
        return msgs

    def _reverse(self, item: dict, freeze: bool) -> list[str]:
        fields, rows = self._read_accounts()
        idx = {r["accountId"]: r for r in rows}
        amt = Decimal(item["amount"])
        msgs = []
        if item["fromAccount"]:          # money went out -> give it back
            a = idx[item["fromAccount"]]
            a["balance"] = f"{Decimal(a['balance']) + amt:.2f}"
            msgs.append(f"{a['accountId']} credited {amt:,.2f} -> balance {a['balance']}")
        if item["toAccount"]:            # money came in -> take it back
            a = idx[item["toAccount"]]
            new_bal = Decimal(a["balance"]) - amt
            a["balance"] = f"{new_bal:.2f}"
            msgs.append(f"{a['accountId']} debited {amt:,.2f} -> balance {a['balance']}")
            if new_bal < 0:
                msgs.append(f"WARNING: {a['accountId']} is now overdrawn; funds were already "
                            f"moved on - recovery required")
        if freeze:
            acct = item["fromAccount"] or item["toAccount"]
            idx[acct]["status"] = "FROZEN"
            msgs.append(f"{acct} status set to FROZEN")
        self._write_accounts(fields, rows)
        return msgs


# ---------------------------------------------------------------- rendering
def render_list(items: list[dict]) -> str:
    out = [bold(f"{'#':>3}  {'TX ID':<9} {'TIME':<8} {'ACCOUNT':<8} {'TYPE':<10} "
                f"{'AMOUNT':>11}  {'STATUS':<10} FLAGS")]
    for i, q in enumerate(items, 1):
        st = q["review"]["status"]
        out.append(f"{i:>3}  {q['transactionId']:<9} {q['timestamp'][11:19]:<8} "
                   f"{(q['fromAccount'] or q['toAccount']):<8} {q['type']:<10} "
                   f"{Decimal(q['amount']):>11,.2f}  {STATUS_COLOR[st](f'{st:<10}')} "
                   + ", ".join(q["flags"]))
    return "\n".join(out)


def render_detail(q: dict, store: ReviewStore) -> str:
    acct_id = q["fromAccount"] or q["toAccount"]
    a = store.account(acct_id) or {}
    route = (f"{q['fromAccount']} -> {q['toAccount']}" if q["fromAccount"] and q["toAccount"]
             else acct_id)
    lines = [
        "",
        bold(f"=== {q['transactionId']}  {q['type']}  ${Decimal(q['amount']):,.2f}  ==="),
        f"  When:     {q['timestamp'].replace('T', ' ')}",
        f"  Route:    {route}   via {q['channel']}  ({q['description']})",
        f"  Customer: {a.get('customerName', '?')}  [{a.get('accountType', '?')}, "
        f"{a.get('status', '?')}]  balance now ${Decimal(a.get('balance', '0')):,.2f}  "
        f"daily limit ${Decimal(a.get('dailyLimit', '0')):,.2f}",
        f"  Review:   {STATUS_COLOR[q['review']['status']](q['review']['status'])}"
        + (f" by {q['review']['analyst']} - {q['review']['note'] or ''}"
           if q["review"]["analyst"] else ""),
        "",
        bold("  Why it was flagged:"),
    ]
    lines += [f"    {red('!')} {f}: {d}" for f, d in zip(q["flags"], q["flagDetails"])]
    lines += ["", bold(f"  {acct_id} activity today:")]
    for o in q["accountActivity"]:
        mark = bold(">>") if o["transactionId"] == q["transactionId"] else "  "
        status = green(o["status"]) if o["status"] == "APPROVED" else red(o["status"])
        lines.append(f"   {mark} {o['time']} {o['transactionId']:<8} {o['type']:<10} "
                     f"{o['amount']:>10}  {status:<8} {dim(o['description'])}"
                     + (f"  {yellow(o['note'])}" if o["note"] else ""))
    return "\n".join(lines)


# -------------------------------------------------------------- interactive
HELP = """Commands:
  <n> or <TX ID>   open a transaction          l   list queue (all)
  n                next pending                 p   list pending only
  q                quit (progress is saved after every decision)"""


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return "q"


def interactive(store: ReviewStore, analyst: str) -> None:
    print(bold(f"\nCBOJ Manual Review  -  analyst: {analyst}"))
    print(f"{len(store.pending())} of {len(store.queue)} flagged transactions pending.\n")
    print(render_list(store.queue))
    print("\n" + HELP)
    while True:
        cmd = _ask(f"\n[{len(store.pending())} pending] review> ")
        if cmd in ("q", "quit", "exit"):
            break
        if cmd in ("l", "list"):
            print(render_list(store.queue))
            continue
        if cmd == "p":
            print(render_list(store.pending()) if store.pending() else green("Queue is clear."))
            continue
        if cmd in ("h", "help", "?"):
            print(HELP)
            continue
        if cmd in ("n", "next", ""):
            pend = store.pending()
            if not pend:
                print(green("Queue is clear - nothing pending."))
                continue
            item = pend[0]
        elif cmd.isdigit() and 1 <= int(cmd) <= len(store.queue):
            item = store.queue[int(cmd) - 1]
        elif cmd.upper() in store.by_id:
            item = store.by_id[cmd.upper()]
        else:
            print(f"Unknown command {cmd!r}. Type h for help.")
            continue
        _review_one(store, item, analyst)
    s = store.pending()
    print(f"\nSaved. {len(s)} pending. Audit trail: {store.audit_path}")


def _review_one(store: ReviewStore, item: dict, analyst: str) -> None:
    print(render_detail(item, store))
    if item["review"]["status"] != "PENDING":
        print(dim("\n  Already decided - decisions are final (see audit trail)."))
        return
    choice = _ask("\n  [c]lear  [r]everse  [e]scalate  [s]kip > ").lower()
    if choice not in DECISIONS:
        print("  Skipped.")
        return
    decision = DECISIONS[choice]
    note = _ask(f"  Note{' (required)' if decision != 'CLEARED' else ' (optional)'}: ")
    freeze = False
    if decision == "REVERSED":
        acct = item["fromAccount"] or item["toAccount"]
        freeze = _ask(f"  Also freeze {acct}? [y/N] ").lower() == "y"
        if _ask(f"  Reverse ${Decimal(item['amount']):,.2f}? This changes balances. "
                f"Type YES to confirm: ") != "YES":
            print("  Cancelled.")
            return
    try:
        for m in store.decide(item["transactionId"], decision, analyst, note, freeze):
            print("  " + (red(m) if m.startswith("WARNING") else m))
        print(f"  {STATUS_COLOR[decision](decision)} {item['transactionId']}")
    except ValueError as e:
        print(red(f"  {e}"))


def default_analyst() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return "analyst"
