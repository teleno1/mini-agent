"""Customer discount rules expressed in basis points."""

TIER_DISCOUNTS = {
    "standard": 0,
    "silver": 500,
    "gold": 1_500,
}


def discount_basis_points(tier: str, loyalty_points: int) -> int:
    """Return the earned discount in basis points."""

    if loyalty_points < 100:
        return 0
    return TIER_DISCOUNTS.get(tier, 0)
