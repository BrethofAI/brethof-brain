"""THE VECTOR ARM'S CONFIDENCE IS RELATIVE TO THE QUERY'S BEST HIT
(2026-09-12). Measured on 56 fresh questions: the right answer's distance
(median 0.51) overlaps the best wrong answer's (0.55), so the old absolute
scale (strong 0.40, noise past 0.62) scored a rank-one hit at 0.58 as 9 of
100 and lost eleven answers to playbooks at 36. Pure predicate, pinned."""
from core import recall_pg as R


def test_the_best_hit_is_worth_one_and_a_hit_far_behind_nothing():
    assert R._vec_prox(0.58, 0.58) == 1.0                      # the old scale gave 0.18
    assert R._vec_prox(0.40, 0.40) == 1.0
    assert 0.4 < R._vec_prox(0.58 + 0.08, 0.58) < 0.6          # the median runner-up gap
    assert R._vec_prox(0.58 + R._VEC_SPREAD, 0.58) == 0.0
    assert R._vec_prox(0.80, 0.58) == 0.0                      # past the cap: noise


def test_a_query_whose_nearest_record_is_far_has_no_strong_answer():
    assert R._vec_prox(R._VEC_SOFT, R._VEC_SOFT) == 1.0
    mid = (R._VEC_SOFT + R._VEC_DIST_CAP) / 2
    assert 0.4 < R._vec_prox(mid, mid) < 0.6
    assert R._vec_prox(R._VEC_DIST_CAP, R._VEC_DIST_CAP) == 0.0


def test_search_memory_unpacks_the_idf_pair():
    src = open("core/tools.py", encoding="utf-8").read()
    body = src.split("def search_memory(", 1)[1].split("\ndef ", 1)[0]
    assert "rows, _ = recall_pg._idf_fts(" in body
