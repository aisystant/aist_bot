"""Discount arithmetic keeps a minimum card payment (no YooKassa payment of 0)."""

from decimal import Decimal

import pytest

from db.queries.redeem import compute_discount

D = Decimal


@pytest.mark.parametrize(
    "balance,rate,requested",
    [
        (D("1000000"), D("1"), D("5000")),      # balance far above the price
        (D("5000"), D("1"), D("5000")),         # balance exactly the price
        (D("999999.99"), D("3"), D("5000")),    # rate that does not divide the price
        (D("123456.78"), D("0.7"), D("4990")),  # fractional rate
        (D("9999"), D("7"), D("1990")),
    ],
)
def test_min_payable_keeps_at_least_one_rouble(balance, rate, requested):
    pts, discount, payable = compute_discount(balance, None, requested, rate, D("1"))
    assert payable >= D("1")
    assert int(payable) >= 1  # handlers pass int(payable_rub) to YooKassa
    assert discount == requested - payable
    assert pts * rate >= discount  # roubles of discount are backed by the points burned


def test_without_floor_a_large_balance_covers_the_whole_price():
    """The cashback model (subscription) keeps its old behaviour."""
    _, discount, payable = compute_discount(D("1000000"), None, D("5000"), D("1"))
    assert discount == D("5000")
    assert payable == D("0")


def test_floor_does_not_change_a_partial_discount():
    floor = compute_discount(D("1500"), None, D("5000"), D("1"), D("1"))
    plain = compute_discount(D("1500"), None, D("5000"), D("1"))
    assert floor == plain == (D("1500.00"), D("1500.00"), D("3500.00"))


def test_ceiling_still_applies_with_floor():
    pts, discount, payable = compute_discount(D("1000000"), D("200"), D("5000"), D("1"), D("1"))
    assert (pts, discount, payable) == (D("200.00"), D("200.00"), D("4800.00"))


def test_price_at_or_below_floor_gives_no_discount():
    pts, discount, payable = compute_discount(D("1000"), None, D("1"), D("1"), D("1"))
    assert (pts, discount, payable) == (D("0.00"), D("0.00"), D("1.00"))
