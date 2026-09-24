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


def calculate_combined_genz_score(
    gen1_rank: int | str | None,
    gen2_rank: int | str | None,
    gen1_weight: float = 0.60,
    gen2_weight: float = 0.40,
) -> float:
    """Return the combined Gen-Z score by weighting gen1 and gen2 rank scores."""
    gen1_score = calculate_rank_score(gen1_rank)
    gen2_score = calculate_rank_score(gen2_rank)
    return gen1_score * gen1_weight + gen2_score * gen2_weight


