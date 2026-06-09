from __future__ import annotations

MAX_STAKE_STARS = 1_000
MAX_DEPOSIT_STARS = 2_000
MAX_WITHDRAWAL_STARS = 2_000
MAX_MARKET_MIN_BET_STARS = 1_000
MAX_DAILY_WITHDRAWAL_STARS = 5_000


class ProductLimitError(ValueError):
    """Raised when a product-level money limit is exceeded."""


def require_stars_limit(amount: int, limit: int, label: str) -> int:
    if not isinstance(amount, int) or isinstance(amount, bool) or amount < 1:
        raise ProductLimitError(f"{label} must be a positive integer.")
    if amount > limit:
        raise ProductLimitError(f"{label} must be at most {limit} Stars.")
    return amount
