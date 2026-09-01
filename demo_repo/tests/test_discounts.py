"""Regression tests that expose DEMO-2 while preserving the boundary behavior."""

from app.discounts import discount_rate


def test_more_than_ten_items_receive_ten_percent_discount() -> None:
    assert discount_rate(11) == 0.10


def test_ten_or_fewer_items_keep_existing_behavior() -> None:
    assert discount_rate(10) == 0.0
    assert discount_rate(1) == 0.0
