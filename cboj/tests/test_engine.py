"""Run with:  python -m unittest discover -s tests -v"""
import shutil
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from cboj.engine import Engine
from cboj.loader import _clean_tx, load_accounts, load_transactions, parse_amount
from cboj.models import (Account, AccountStatus, FlagReason, RejectReason,
                         Status)
from cboj.reports import write_outputs
from cboj.review import ReviewStore

ROOT = Path(__file__).resolve().parent.parent


def acct(aid, bal="1000", limit="500", status=AccountStatus.ACTIVE):
    return Account(aid, "Test", "CHEQUING", status, Decimal(bal), Decimal(limit), "CAD", "2020-01-01")


def tx(tid, typ, frm="", to="", amount="10.00", ts="2026-10-15T10:00:00", line=2):
    return _clean_tx(line, {"transactionId": tid, "timestamp": ts, "type": typ,
                            "fromAccount": frm, "toAccount": to, "amount": amount,
                            "channel": "POS", "description": "x"})


class ParsingTests(unittest.TestCase):
    def test_amount_formats(self):
        self.assertEqual(parse_amount("12.50")[0], Decimal("12.50"))
        self.assertEqual(parse_amount("-60.00")[0], Decimal("-60.00"))
        self.assertIsNotNone(parse_amount("12.5O")[1])   # letter O
        self.assertIsNotNone(parse_amount("15.005")[1])  # fractional cent
        self.assertIsNotNone(parse_amount("")[1])

    def test_type_is_normalized(self):
        t = tx("T1", "purchase", frm="A")
        self.assertEqual(t.type.value, "PURCHASE")
        self.assertTrue(t.cleaning_notes)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.e = Engine({"A": acct("A"), "B": acct("B"),
                         "F": acct("F", status=AccountStatus.FROZEN)})

    def reason(self, t):
        r = self.e.process(t)
        return r.reject_reason if r.status is Status.REJECTED else None

    def test_unknown_account(self):
        self.assertEqual(self.reason(tx("T1", "PURCHASE", frm="ZZZ")), RejectReason.INVALID_ACCOUNT)

    def test_inactive_account(self):
        self.assertEqual(self.reason(tx("T1", "DEPOSIT", to="F")), RejectReason.ACCOUNT_NOT_ACTIVE)

    def test_non_positive_amount(self):
        self.assertEqual(self.reason(tx("T1", "PURCHASE", frm="A", amount="0")), RejectReason.INVALID_AMOUNT)
        self.assertEqual(self.reason(tx("T2", "DEPOSIT", to="A", amount="-5")), RejectReason.INVALID_AMOUNT)

    def test_duplicate_id_even_after_rejection(self):
        self.assertEqual(self.reason(tx("T1", "PURCHASE", frm="A", amount="5000")),
                         RejectReason.INSUFFICIENT_FUNDS)
        self.assertEqual(self.reason(tx("T1", "PURCHASE", frm="A", amount="5")),
                         RejectReason.DUPLICATE_ID)

    def test_insufficient_funds_leaves_balance_untouched(self):
        self.assertEqual(self.reason(tx("T1", "WITHDRAWAL", frm="A", amount="1000.01")),
                         RejectReason.INSUFFICIENT_FUNDS)
        self.assertEqual(self.e.state.accounts["A"].balance, Decimal("1000"))

    def test_exact_balance_to_zero_is_allowed(self):
        self.assertIsNone(self.reason(tx("T1", "WITHDRAWAL", frm="A", amount="1000.00")))
        self.assertEqual(self.e.state.accounts["A"].balance, Decimal("0"))

    def test_transfer_is_atomic(self):
        # destination frozen -> source must NOT be debited
        self.assertEqual(self.reason(tx("T1", "TRANSFER", frm="A", to="F")),
                         RejectReason.ACCOUNT_NOT_ACTIVE)
        self.assertEqual(self.e.state.accounts["A"].balance, Decimal("1000"))
        self.assertIsNone(self.reason(tx("T2", "TRANSFER", frm="A", to="B", amount="100")))
        self.assertEqual(self.e.state.accounts["A"].balance, Decimal("900"))
        self.assertEqual(self.e.state.accounts["B"].balance, Decimal("1100"))

    def test_unsupported_and_missing(self):
        self.assertEqual(self.reason(tx("T1", "REVERSAL", frm="A")), RejectReason.UNSUPPORTED_TYPE)
        self.assertEqual(self.reason(tx("T2", "WITHDRAWAL")), RejectReason.MISSING_ACCOUNT)


class ReviewRuleTests(unittest.TestCase):
    def test_flagged_but_still_approved(self):
        e = Engine({"A": acct("A", bal="50000", limit="100000"), "B": acct("B")})
        r = e.process(tx("T1", "TRANSFER", frm="A", to="B", amount="10000"))
        self.assertIs(r.status, Status.APPROVED)
        self.assertIn(FlagReason.LARGE_AMOUNT, r.flags)
        self.assertEqual(e.state.accounts["B"].balance, Decimal("11000"))

    def test_near_threshold(self):
        e = Engine({"A": acct("A", bal="50000", limit="100000"), "B": acct("B")})
        r = e.process(tx("T1", "TRANSFER", frm="A", to="B", amount="9999.99"))
        self.assertEqual(r.flags, [FlagReason.NEAR_THRESHOLD])

    def test_daily_limit_counts_outgoing_only(self):
        e = Engine({"A": acct("A", bal="5000", limit="100")})
        self.assertFalse(e.process(tx("T0", "DEPOSIT", to="A", amount="900")).flagged)
        self.assertFalse(e.process(tx("T1", "PURCHASE", frm="A", amount="60",
                                      ts="2026-10-15T09:00:00")).flagged)
        r = e.process(tx("T2", "PURCHASE", frm="A", amount="60", ts="2026-10-15T11:00:00"))
        self.assertEqual(r.flags, [FlagReason.DAILY_LIMIT])

    def test_velocity_window(self):
        e = Engine({"A": acct("A", limit="1000")})
        flags = [e.process(tx(f"T{i}", "PURCHASE", frm="A", amount="1",
                              ts=f"2026-10-15T10:0{i}:00")).flagged for i in range(6)]
        self.assertEqual(flags, [False, False, False, False, True, True])
        # 20 minutes later the window has emptied
        self.assertFalse(e.process(tx("T9", "PURCHASE", frm="A", amount="1",
                                      ts="2026-10-15T10:25:00")).flagged)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        accounts, issues = load_accounts(ROOT / "data/accounts.csv")
        self.results = Engine(accounts).process_all(load_transactions(ROOT / "data/transactions.csv"))
        write_outputs(self.tmp, self.results, accounts, issues)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_every_row_has_a_result(self):
        self.assertEqual(len(self.results), 240)
        self.assertTrue(all(r.status in (Status.APPROVED, Status.REJECTED) for r in self.results))
        self.assertTrue(all(r.reject_reason for r in self.results if r.status is Status.REJECTED))

    def test_no_negative_balances(self):
        for r in self.results:
            for bal in r.balances_after.values():
                self.assertGreaterEqual(bal, 0)

    def test_review_reverse_restores_balance(self):
        store = ReviewStore(self.tmp)
        before = Decimal(store.account("ACC1013")["balance"])
        store.decide("TX00083", "REVERSED", "tester", "confirmed fraud", freeze=True)
        a = ReviewStore(self.tmp).account("ACC1013")
        self.assertEqual(Decimal(a["balance"]), before + Decimal("11450.00"))
        self.assertEqual(a["status"], "FROZEN")
        with self.assertRaises(ValueError):  # decisions are final
            store.decide("TX00083", "CLEARED", "tester", "")
        self.assertTrue((self.tmp / "review_audit.csv").exists())

    def test_reverse_requires_note(self):
        with self.assertRaises(ValueError):
            ReviewStore(self.tmp).decide("TX00069", "REVERSED", "tester", "  ")


if __name__ == "__main__":
    unittest.main()
