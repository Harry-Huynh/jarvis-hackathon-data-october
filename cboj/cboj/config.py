"""Tunable thresholds. Kept in one place so the business (not the code) owns them."""
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal


@dataclass(frozen=True)
class Config:
    # FINTRAC requires reporting of cash/wire transactions of CAD $10,000+,
    # so that is the natural "reasonable threshold" for a Canadian bank.
    large_amount_threshold: Decimal = Decimal("10000.00")
    # Amounts just under the reporting threshold are a classic structuring
    # pattern (e.g. a $9,999.99 transfer). Flag anything >= 90% of it.
    near_threshold_ratio: Decimal = Decimal("0.90")
    # Velocity: this many transaction *attempts* on one account inside the
    # window is unreasonable. Attempts include rejected ones - a burst of
    # declined card swipes is itself a fraud signal.
    velocity_count: int = 5
    velocity_window: timedelta = timedelta(minutes=10)


DEFAULT = Config()
