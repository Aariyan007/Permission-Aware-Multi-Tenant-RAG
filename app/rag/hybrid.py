"""Merging ranked lists from different retrievers."""
from collections.abc import Hashable, Sequence

RRF_K = 60  # standard constant from the RRF paper; dampens how much rank #1 dominates


def reciprocal_rank_fusion(rankings: Sequence[Sequence[Hashable]], k: int = RRF_K) -> list[tuple[Hashable, float]]:
    """Combine rankings by position, not by score.

    Vector distance and keyword rank are in different units, so their raw scores can't be added.
    Positions can: each item earns 1 / (k + position) from every list it appears in. Items that
    several retrievers agree on rise to the top. Returns (item, score) sorted best-first; ties keep
    first-seen order so results are deterministic."""
    scores: dict[Hashable, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
