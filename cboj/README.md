# CBOJ Transaction Processing Engine (MVP)

Batch processor for the Canadian Bank of Jarvis: cleans and validates the day's
transaction file, applies the business rules, updates balances, flags suspicious
activity, and gives analysts a **review CLI** to work the flagged queue.

Pure Python 3.10+ standard library, so there's nothing to install and it runs on any judge's laptop.

## Run it

```bash
python3 -m cboj process --accounts data/accounts.csv --transactions data/transactions.csv
python3 -m cboj review                    # interactive review of flagged transactions
python3 -m cboj summary                   # processing summary + review progress
python3 -m unittest discover -s tests -v  # 18 tests
```

Non-interactive review (scriptable / for demos):
```bash
python3 -m cboj review --list
python3 -m cboj review --show TX00083
python3 -m cboj review --decide TX00221 reverse --note "Card reported stolen" --freeze
```

## Outputs (`out/`)

| File | What |
|---|---|
| `results.txt` | `TX00001 APPROVED` / `TX00051 REJECTED - INVALID ACCOUNT`, one line per input row |
| `results.csv` | same, with full detail (reject detail, flag detail) |
| `summary.txt` | Processed / Approved / Rejected / Flagged + breakdown by reason |
| `flagged_report.txt` | flagged-transaction summary report |
| `data_quality.txt` | what the cleaner normalized, and what it refused to repair |
| `accounts_after.csv` | end-of-day balances (updated by reviewer reversals) |
| `review_queue.json` | flagged items with account context, plus the review decision |
| `review_audit.csv` | append-only log: who decided what, when, and why |

Sample run: **240 processed, 215 approved, 25 rejected, 13 flagged.**

## Design

```
CSV ──> loader (clean/parse) ──> engine ──> reports ──> out/
                                   │                      │
                     validation rules (reject)      review CLI (clear / reverse / escalate)
                     apply balances (atomic)
                     review rules (flag only)
```

* `models.py`: `Account`, `RawTransaction`, `Result`, plus enums for types, statuses and reasons.
* `loader.py`: cleaning. It normalizes the unambiguous problems and records the rest.
* `engine.py`: the rules are a list of small functions, so adding a rule means adding one function.
* `config.py`: all thresholds in one place.
* `review.py`/`cli.py`: the analyst workflow.

**Data structures**
* `dict` accounts by ID: O(1) lookup and update.
* `set` of seen transaction IDs: O(1) duplicate detection.
* `dict[(account, date)] → Decimal`: running outgoing total for the daily-limit check.
* `deque` of timestamps per account: sliding window for the velocity check.

**Money is `Decimal`, never `float`.**

## Business rules

Rejected, checked in this order (the first failure wins and balances are not touched):
1. `MALFORMED RECORD`: blank ID or unparseable timestamp
2. `DUPLICATE TRANSACTION ID`
3. `UNSUPPORTED TRANSACTION TYPE`: anything other than PURCHASE/WITHDRAWAL/DEPOSIT/TRANSFER
4. `MISSING ACCOUNT` / `INVALID ACCOUNT` / `ACCOUNT NOT ACTIVE`: checks both legs of a transfer
5. `INVALID AMOUNT`: <= 0, or not a valid currency value
6. `INSUFFICIENT FUNDS`: the balance would go negative

Flagged (the transaction is still approved and applied):
* `LARGE AMOUNT`: >= $10,000, the FINTRAC large-transaction reporting threshold
* `NEAR REPORTING THRESHOLD`: $9,000–$9,999.99, a possible structuring pattern
* `DAILY LIMIT EXCEEDED`: the account's outgoing total today is over its `dailyLimit`
* `HIGH VELOCITY`: 5 or more attempts on one account within 10 minutes

## Assumptions (and questions we'd ask the bank)

1. **Order:** transactions are processed in timestamp order, with ties kept in file order.
2. **Duplicates:** an ID is used up the first time it is seen, *even if that attempt was rejected*. A resend needs a new ID. (TX00071 is rejected for insufficient funds, then resubmitted at a lower amount with the same ID, so it is rejected as a duplicate.) *Q: Should a retry of a rejected transaction be allowed to reuse its ID?*
3. **Cleaning:** we normalize case and whitespace (`purchase` → `PURCHASE`) but never guess money. `12.5O` and `15.005` are rejected, not repaired.
4. **Active:** only `ACTIVE` accounts can transact. FROZEN, CLOSED and DORMANT are all rejected, on either side of a transfer.
5. **Daily limit** applies to *outgoing* money only (purchases, withdrawals, transfers out). Exceeding it flags the transaction rather than rejecting it, because the spec lists it under review. *Q: Is the daily limit really a soft limit?*
6. **Velocity** counts attempts, including rejected ones, because a burst of declines is itself a signal.
7. **REVERSAL** rows are rejected as unsupported: there is no reference to the original transaction, so we can't know what to reverse. *Q: What is the reversal file format?*
8. **Transfer to the same account** is rejected as invalid.
9. **Currency:** everything is CAD. FX is out of scope.
10. **Only approved transactions are flagged.** Rejected ones are already stopped.

## Review workflow

A flagged transaction has already been applied. The analyst chooses:
* **Clear**: legitimate, nothing changes.
* **Reverse**: undo its balance effect, optionally freeze the account. Requires a note and typed confirmation.
* **Escalate**: send to compliance, e.g. for a FINTRAC report. Requires a note.

Decisions are final and logged to `review_audit.csv`. The queue file is written atomically after every decision, so quitting mid-review loses nothing.

## Future improvements

* Group a burst into one review case. Right now the gift-card burst on ACC1017 has to be reviewed one row at a time, and the first 4 rows of it are not flagged.
* Store state in a database (SQLite/Postgres) instead of files, for concurrent analysts and multi-day processing.
* Account for later activity during reversals: undoing a credit after the funds have already been spent can overdraw the account. We warn about this today.
* Make the thresholds per account type or customer risk profile, and move them to a config file.
* Stream large files instead of loading them into memory.
