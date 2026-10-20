"""Small, deterministic summaries for bounded release-host measurements."""

import math


def nearest_rank(values: list[float], percentile: float) -> float | None:
    """Return the nearest-rank percentile; never extrapolate sparse samples."""
    if not values:
        return None
    if not 0 < percentile <= 100:
        raise ValueError("Percentile must be greater than 0 and at most 100")
    ordered = sorted(values)
    return round(ordered[math.ceil(percentile / 100 * len(ordered)) - 1], 2)
