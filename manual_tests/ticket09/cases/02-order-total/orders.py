"""Order price calculation."""

from discounts import discount_basis_points


def order_total(subtotal_cents: int, tier: str, loyalty_points: int) -> int:
    """Return the amount due in integer cents."""

    discount = discount_basis_points(tier, loyalty_points)
    return subtotal_cents - discount
