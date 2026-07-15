"""Small text helpers."""


def slugify(value: str) -> str:
    """Return a lowercase URL slug."""

    return value.lower().replace(" ", "-")
