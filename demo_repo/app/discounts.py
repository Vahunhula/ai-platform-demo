"""Order discount behavior for DEMO-2."""


def discount_rate(quantity: int) -> float:
    """Return the discount rate for an order quantity."""

    if quantity > 10:
        return 0.05
    return 0.0
