"""Explicit suite registration without discovery or import side effects."""

from evalbench.suites.base import Suite


SUITES: dict[str, Suite] = {}


def register_suite(suite: Suite) -> None:
    """Register one suite instance by its unique public name."""
    if suite.name in SUITES:
        raise ValueError(f"Suite already registered: {suite.name}")
    SUITES[suite.name] = suite


def get_suite(name: str) -> Suite:
    """Return a registered suite or identify the available choices."""
    try:
        return SUITES[name]
    except KeyError:
        choices = ", ".join(sorted(SUITES)) or "none"
        raise KeyError(
            f"Unknown suite {name!r}; registered suites: {choices}"
        ) from None


def list_suites() -> list[Suite]:
    """Return registered suite instances sorted by suite name."""
    return sorted(SUITES.values(), key=lambda suite: suite.name)


# Explicit suite imports and register_suite(...) calls begin in Phase 2.
