from __future__ import annotations

import math


def calculate_rank_score(rank_value: int | str | None) -> float:
    """Return the shared Hype Wave rank score for a 1-100 chart rank."""
    try:
        if rank_value is None or rank_value == "":
            return 0.0
        rank = int(rank_value)
        if rank < 1 or rank > 100:
            return 0.0
    except (TypeError, ValueError):
        return 0.0

    power_score = 1.1 * (100.0 / math.sqrt(rank) - 10.0) + 1.0
    c1 = 99.0 / (1.0 - math.exp(-1.0))
    c2 = math.exp(-1.0)
    exp_score = c1 * (math.exp(-(rank - 1.0) / 99.0) - c2) + 1.0
    return (power_score + exp_score) / 2.0

