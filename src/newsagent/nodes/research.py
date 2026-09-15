"""3단계 취재 노드 (PRD 3.3).

본문 확보 → 구조화 요약. 여기서 만든 key_facts 가 4단계 검수의 입력이 되므로,
요약 품질보다 '검증 가능한 문장' 형태를 지키는 게 더 중요하다.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..llm import BaseLLM
from ..models import Article, Brief
from ..state import NewsletterState
from ..utils.extract import fetch_article_text
from ..utils.text import clean_text, truncate

log = logging.getLogger(__name__)


def _fallback_brief(article: Article, reason: str) -> Brief:
    """본문 추출이나 LLM 이 실패해도 기사를 떨구지 않는다 (R3-4)."""
    snippet = clean_text(article.snippet) or article.title
    return Brief(
        article_id=article.id,
        headline=article.title,
        summary=[truncate(snippet, 300)],
        key_facts=[truncate(snippet, 300)] if snippet else [],
        entities={},
        category="기타",
        why_it_matters="",
        article=article,
        degraded=True,
    )


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [clean_text(str(v)) for v in value if clean_text(str(v))]
    text = clean_text(str(value or ""))
    return [text] if text else []


def make_research_node(cfg: dict[str, Any], llm: BaseLLM):
    research_cfg = cfg["research"]
    collect_cfg = cfg["collect"]
    max_chars = int(research_cfg["max_content_chars"])
    fetch_full = bool(research_cfg["fetch_full_text"])

    def research_one(article: Article) -> tuple[Brief, str | None]:
        content = ""
        if fetch_full:
            content = fetch_article_text(
                article.url,
                timeout=collect_cfg["timeout"],
                user_agent=collect_cfg["user_agent"],
                max_chars=max_chars,
            )
        if not content:
            content = clean_text(article.snippet)
        if not content:
            return _fallback_brief(article, "본문·스니펫 모두 비어 있음"), f"[research] 본문 없음: {article.title[:40]}"

        article.content = content
        try:
            payload = llm.summarize(
                source=article.source,
                title=article.title,
                published_at=article.published_at or "",
                content=content[:max_chars],
            )
        except Exception as exc:
            return _fallback_brief(article, str(exc)), f"[research] LLM 실패({type(exc).__name__}): {article.title[:40]}"

        entities = payload.get("entities")
        brief = Brief(
            article_id=article.id,
            headline=clean_text(payload.get("headline")) or article.title,
            summary=_coerce_list(payload.get("summary"))[:5],
            key_facts=_coerce_list(payload.get("key_facts"))[:6],
            entities=entities if isinstance(entities, dict) else {},
            category=clean_text(payload.get("category")) or "기타",
            why_it_matters=clean_text(payload.get("why_it_matters")),
            article=article,
            degraded=False,
        )
        return brief, None

    def research_node(state: NewsletterState) -> dict[str, Any]:
        started = time.monotonic()
        selected: list[Article] = state.get("selected", [])
        briefs: list[Brief] = []
        errors: list[str] = []

        if not selected:
            return {"briefs": [], "stats": {"research": {"input": 0, "briefs": 0}}}

        workers = max(1, min(int(research_cfg["max_workers"]), len(selected)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(research_one, a): a for a in selected}
            for future in as_completed(futures):
                article = futures[future]
                try:
                    brief, warning = future.result()
                except Exception as exc:  # R3-2: 개별 실패는 스킵
                    errors.append(f"[research] {article.title[:40]}: {type(exc).__name__}: {exc}")
                    continue
                if warning:
                    errors.append(warning)
                briefs.append(brief)

        # 선별 점수 순서를 유지해 발행 순서가 흔들리지 않게 한다
        order = {a.id: i for i, a in enumerate(selected)}
        briefs.sort(key=lambda b: order.get(b.article_id, 999))

        degraded = sum(1 for b in briefs if b.degraded)
        elapsed = time.monotonic() - started
        log.info("3단계 취재 완료: %d건 요약 (폴백 %d건, %.1fs)", len(briefs), degraded, elapsed)

        return {
            "briefs": briefs,
            "errors": errors,
            "stats": {
                "research": {
                    "input": len(selected),
                    "briefs": len(briefs),
                    "degraded": degraded,
                    "full_text_ok": sum(1 for b in briefs if b.article and len(b.article.content) > 500),
                    "llm": llm.name,
                    "elapsed_sec": round(elapsed, 2),
                }
            },
        }

    return research_node
