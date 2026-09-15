"""2단계 선별 노드 (PRD 3.2).

의도적으로 LLM 을 쓰지 않는다. 수십~수백 건의 후보를 LLM 에 넣으면
비용과 지연이 선형으로 늘고, 관심사 매칭은 그만한 판단력이 필요하지 않다.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

import numpy as np

from ..models import Article
from ..state import NewsletterState
from ..store import PublishedStore
from ..utils.text import similarity_to_profiles

log = logging.getLogger(__name__)


def _interest_profile(interest: dict[str, Any]) -> str:
    """관심사를 하나의 프로필 문서로 펼친다 (벡터 점수의 비교 대상)."""
    keywords = " ".join(str(k) for k in interest.get("keywords", []))
    return f"{interest.get('name', '')} {keywords}".strip()


def keyword_idf(articles: list[Article], interests: list[dict[str, Any]]) -> dict[str, float]:
    """키워드별 변별력(IDF)을 그날의 코퍼스에서 직접 계산한다.

    'AI', '인공지능' 처럼 후보 절반에 등장하는 단어는 관심사 일치의 근거가 되지 못한다.
    반대로 'HBM', 'TSMC' 는 등장 자체가 강한 신호다. 같은 1점으로 세면
    범용어 두 개가 박힌 보도자료가 특종을 이긴다 — 실제로 그런 결과가 나왔다.
    """
    total = len(articles)
    if total == 0:
        return {}
    haystacks = [f"{a.title} {a.snippet}".lower() for a in articles]

    idf: dict[str, float] = {}
    scale = math.log(total + 1)
    for interest in interests:
        for keyword in interest.get("keywords", []):
            needle = str(keyword).lower().strip()
            if not needle or needle in idf:
                continue
            df = sum(1 for text in haystacks if needle in text)
            idf[needle] = min(math.log((total + 1) / (df + 1)) / scale, 1.0)
    return idf


def _keyword_score(
    article: Article, interest: dict[str, Any], idf: dict[str, float]
) -> tuple[float, list[str]]:
    """제목 매칭에 2배 가중, 키워드별 IDF 로 변별력을 반영해 0~1 로 정규화."""
    keywords = [str(k) for k in interest.get("keywords", []) if str(k).strip()]
    if not keywords:
        return 0.0, []
    title = article.title.lower()
    snippet = article.snippet.lower()

    matched: list[str] = []
    points = 0.0
    for keyword in keywords:
        needle = keyword.lower()
        weight = idf.get(needle, 1.0)
        if needle in title:
            points += 2.0 * weight
            matched.append(keyword)
        elif needle in snippet:
            points += 1.0 * weight
            matched.append(keyword)
    if not matched:
        return 0.0, []
    # 변별력 높은 키워드를 제목에서 1~2개 맞히면 만점에 근접하도록 스케일링
    return min(points / 3.0, 1.0), matched


def _normalize_vector_scores(scores: np.ndarray) -> np.ndarray:
    """코사인 원값을 그날 코퍼스 기준 상대 점수로 편다.

    char n-gram 코사인은 짧은 제목 대 키워드 뭉치에서 0.01~0.11 에 몰린다.
    원값에 가중치 0.4 를 곱하면 기여도가 0.04 라 하이브리드가 사실상 키워드 전용이 된다.

    정규화는 반드시 관심사별(열 단위)로 한다. 관심사마다 프로필 길이가 달라
    코사인 척도 자체가 다르기 때문이다(실측: AI 열 최대 0.108 vs 개발자도구 열 최대 0.061).
    전체를 한 기준으로 나누면 프로필이 긴 관심사가 구조적으로 유리해진다.
    나눗셈 기준은 퍼센타일이 아니라 열 최대값을 쓴다. p98 로 잘랐더니 상위 2% 가
    전부 1.0 으로 붙어, 정작 순위를 가려야 할 최상단에서 벡터 점수가 무력해졌다.
    """
    if scores.size == 0:
        return scores
    ceilings = scores.max(axis=0)
    ceilings = np.where(ceilings <= 1e-9, 1.0, ceilings)
    return np.clip(scores / ceilings, 0.0, 1.0)


def _excluded(article: Article, interest: dict[str, Any]) -> bool:
    """R2-1: 제외 키워드는 점수와 무관한 하드 필터."""
    text = f"{article.title} {article.snippet}".lower()
    return any(str(x).lower() in text for x in interest.get("exclude", []) if str(x).strip())


def make_curate_node(cfg: dict[str, Any], store: PublishedStore | None = None):
    curate_cfg = cfg["curate"]
    history_cfg = cfg.get("history", {})
    interests: list[dict[str, Any]] = curate_cfg["interests"]
    mode = curate_cfg["mode"]
    weights = curate_cfg["weights"]
    threshold = float(curate_cfg["threshold"])
    max_articles = int(curate_cfg["max_articles"])

    def curate_node(state: NewsletterState) -> dict[str, Any]:
        started = time.monotonic()
        articles: list[Article] = state.get("raw_items", [])
        if not articles:
            return {
                "selected": [],
                "corpus": [],
                "stats": {"curate": {"candidates": 0, "selected": 0, "elapsed_sec": 0.0}},
            }

        profiles = [_interest_profile(i) for i in interests]
        if mode in ("vector", "hybrid"):
            vector_scores = _normalize_vector_scores(
                similarity_to_profiles([a.match_text for a in articles], profiles)
            )
        else:
            vector_scores = None
        idf = keyword_idf(articles, interests) if mode in ("keyword", "hybrid") else {}

        scored: list[Article] = []
        for row, article in enumerate(articles):
            best_total = 0.0
            best: dict[str, Any] | None = None

            for col, interest in enumerate(interests):
                if _excluded(article, interest):
                    continue
                weight = float(interest.get("weight", 1.0))
                kw_score, matched = _keyword_score(article, interest, idf)
                vec_score = float(vector_scores[row][col]) if vector_scores is not None else 0.0

                if mode == "keyword":
                    combined = kw_score
                elif mode == "vector":
                    combined = vec_score
                else:
                    combined = weights["keyword"] * kw_score + weights["vector"] * vec_score
                total = combined * weight

                if total > best_total:
                    best_total = total
                    best = {
                        "interest": interest.get("name"),
                        "keywords": matched,
                        "kw": kw_score,
                        "vec": vec_score,
                    }

            article.score = round(best_total, 4)
            if best:
                article.matched_interest = best["interest"]
                article.matched_keywords = best["keywords"]
                article.keyword_score = round(best["kw"], 4)
                article.vector_score = round(best["vec"], 4)
            scored.append(article)

        # 이미 발행한 기사는 후보에서 뺀다. 사건 단위 중복은 4단계에서 한 번 더 거른다.
        published_ids: set[str] = set()
        if store is not None and store.enabled and history_cfg.get("enabled", True):
            published_ids = store.published_ids(int(history_cfg.get("lookback_days", 7)))

        # role=corroboration 소스는 점수를 매기되 선별 대상에서는 뺀다.
        # 애그리게이터 링크는 본문을 가져올 수 없어 3단계 요약이 제목 재탕이 되지만,
        # 같은 사건을 다룬 매체를 넓게 잡아주므로 4단계 증인으로는 가장 값지다.
        passed = sorted(
            (
                a for a in scored
                if a.score >= threshold and a.role == "content" and a.id not in published_ids
            ),
            key=lambda a: a.score,
            reverse=True,
        )
        selected = passed[:max_articles]
        corroboration_only = sum(1 for a in scored if a.role != "content")
        skipped_published = sum(
            1 for a in scored
            if a.score >= threshold and a.role == "content" and a.id in published_ids
        )

        elapsed = time.monotonic() - started
        log.info(
            "2단계 선별 완료: 후보 %d건(증인 전용 %d건) → 임계 통과 %d건"
            "(기발행 %d건 제외) → 상위 %d건 (%.2fs)",
            len(scored), corroboration_only, len(passed), skipped_published, len(selected), elapsed,
        )

        return {
            "selected": selected,
            # R2-3: 탈락분도 4단계 교차검증 대조군으로 남긴다
            "corpus": scored,
            "stats": {
                "curate": {
                    "mode": mode,
                    "threshold": threshold,
                    "candidates": len(scored),
                    "corroboration_only": corroboration_only,
                    "skipped_already_published": skipped_published,
                    "passed_threshold": len(passed),
                    "selected": len(selected),
                    "top_scores": [
                        {"title": a.title[:60], "score": a.score, "interest": a.matched_interest}
                        for a in selected
                    ],
                    "elapsed_sec": round(elapsed, 2),
                }
            },
        }

    return curate_node
