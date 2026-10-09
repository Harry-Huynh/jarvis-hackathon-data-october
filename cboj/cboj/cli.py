"""Command-line entry point.

  python -m cboj process  --accounts data/accounts.csv --transactions data/transactions.csv
  python -m cboj review                 # interactive queue for flagged transactions
  python -m cboj review --list
  python -m cboj review --show TX00083
  python -m cboj review --decide TX00083 clear --note "Known vehicle purchase"
  python -m cboj summary                # summary + review progress
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from .config import DEFAULT
from .engine import Engine
from .loader import InputFileError, load_accounts, load_transactions
from .reports import write_outputs
from .review import (DECISIONS, ReviewStore, default_analyst, interactive,
                     render_detail, render_list)


def cmd_process(args) -> int:
    try:
        accounts, acct_issues = load_accounts(args.accounts)
        txs = load_transactions(args.transactions)
    except InputFileError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    results = Engine(accounts, DEFAULT).process_all(txs)
    paths = write_outputs(Path(args.out), results, accounts, acct_issues)

    if not args.quiet:
        print(paths["results"].read_text(), end="")
        print()
    print(paths["summary"].read_text(), end="")
    print(f"\nOutputs written to {args.out}/:")
    for p in paths.values():
        print(f"  {p.name}")
    flagged = sum(r.flagged for r in results)
    if flagged:
        print(f"\n{flagged} transaction(s) need review -> python -m cboj review --out {args.out}")
    return 0


def cmd_review(args) -> int:
    store = ReviewStore(Path(args.out))
    if args.list:
        print(render_list(store.queue))
        return 0
    if args.show:
        item = store.by_id.get(args.show.upper())
        if not item:
            print(f"{args.show} is not in the review queue", file=sys.stderr)
            return 1
        print(render_detail(item, store))
        return 0
    if args.decide:
        tx_id, choice = args.decide
        decision = DECISIONS.get(choice[0].lower()) if choice else None
        if not decision:
            print("decision must be clear | reverse | escalate", file=sys.stderr)
            return 1
        try:
            for m in store.decide(tx_id.upper(), decision, args.analyst, args.note or "",
                                  args.freeze):
                print(m)
        except (KeyError, ValueError) as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        print(f"{tx_id.upper()} {decision}")
        return 0
    interactive(store, args.analyst)
    return 0


def cmd_summary(args) -> int:
    out = Path(args.out)
    print((out / "summary.txt").read_text())
    store = ReviewStore(out)
    c = Counter(q["review"]["status"] for q in store.queue)
    print("REVIEW PROGRESS")
    print("===============")
    for s in ("PENDING", "CLEARED", "REVERSED", "ESCALATED"):
        print(f"  {s:<10} {c.get(s, 0)}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cboj", description="CBOJ transaction processing engine")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("process", help="process a batch of transactions")
    pr.add_argument("--accounts", default="data/accounts.csv")
    pr.add_argument("--transactions", default="data/transactions.csv")
    pr.add_argument("--out", default="out")
    pr.add_argument("-q", "--quiet", action="store_true", help="don't print per-transaction lines")
    pr.set_defaults(func=cmd_process)

    rv = sub.add_parser("review", help="review flagged transactions")
    rv.add_argument("--out", default="out")
    rv.add_argument("--analyst", default=default_analyst())
    rv.add_argument("--list", action="store_true")
    rv.add_argument("--show", metavar="TX_ID")
    rv.add_argument("--decide", nargs=2, metavar=("TX_ID", "clear|reverse|escalate"))
    rv.add_argument("--note")
    rv.add_argument("--freeze", action="store_true", help="with reverse: freeze the account")
    rv.set_defaults(func=cmd_review)

    sm = sub.add_parser("summary", help="show processing summary and review progress")
    sm.add_argument("--out", default="out")
    sm.set_defaults(func=cmd_summary)

    args = p.parse_args(argv)
    return args.func(args)
