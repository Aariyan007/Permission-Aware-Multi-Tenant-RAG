from app.rag.hybrid import reciprocal_rank_fusion
from app.rag.reranker import NoopReranker


def test_rrf_rewards_items_both_retrievers_agree_on():
    vector = ["handbook", "salary", "runbook"]
    keyword = ["salary", "handbook"]
    fused = [item for item, _ in reciprocal_rank_fusion([vector, keyword])]
    assert set(fused[:2]) == {"salary", "handbook"}  # in both lists
    assert fused[2] == "runbook"                      # only in one list, so last


def test_rrf_scores_follow_the_formula():
    scores = dict(reciprocal_rank_fusion([["a", "b"], ["b"]], k=60))
    assert abs(scores["a"] - 1 / 61) < 1e-12
    assert abs(scores["b"] - (1 / 62 + 1 / 61)) < 1e-12


def test_rrf_handles_empty_lists_and_one_sided_hits():
    assert reciprocal_rank_fusion([[], []]) == []
    assert [i for i, _ in reciprocal_rank_fusion([["x"], []])] == ["x"]


def test_rrf_uses_positions_not_raw_scores():
    """A retriever's #1 is worth the same whatever its raw score was; that's the point."""
    a = dict(reciprocal_rank_fusion([["p", "q"], ["q", "p"]]))
    assert abs(a["p"] - a["q"]) < 1e-12


async def test_noop_reranker_keeps_order_and_trims():
    assert await NoopReranker().rerank("q", ["a", "b", "c"], top_k=2) == [0, 1]
    assert await NoopReranker().rerank("q", [], top_k=5) == []
