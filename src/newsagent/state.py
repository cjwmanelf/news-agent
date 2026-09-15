"""LangGraph 상태 정의 (PRD 2.2).

노드는 부분 상태만 반환하고 LangGraph 가 머지한다.
errors / stats 는 덮어쓰기가 아니라 누적되어야 하므로 리듀서를 붙였다.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from .models import Article, Brief, VerifiedBrief


def merge_errors(left: list[str], right: list[str]) -> list[str]:
    """노드마다 발생한 경고/오류를 누적한다."""
    return (left or []) + (right or [])


def merge_stats(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """단계별 통계를 한 딕셔너리에 모은다."""
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class NewsletterState(TypedDict, total=False):
    run_id: str
    started_at: str

    raw_items: list[Article]  # 1. 수집
    selected: list[Article]  # 2. 선별 통과
    corpus: list[Article]  # 2. 검수용 대조군 (탈락분 포함 전체)
    briefs: list[Brief]  # 3. 취재
    verified: list[VerifiedBrief]  # 4. 검수
    publish_result: dict[str, Any]  # 5. 발행

    errors: Annotated[list[str], merge_errors]
    stats: Annotated[dict[str, Any], merge_stats]
