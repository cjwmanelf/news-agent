"""4단계 검수 노드 (PRD 3.4) — 이 에이전트의 핵심.

원칙: "서로 독립적인 여러 출처가 같은 말을 하면 사실로 본다."

한 기사만 보고 사실 여부를 물으면 LLM 은 자기 사전지식으로 답하게 되고,
그건 검증이 아니라 추측이다. 그래서 같은 사건을 다룬 다른 기사를 먼저 확보하고(증인 수집),
그 기사들을 근거로 주장별 판정을 받는다.

증인 확보는 4단으로 진행한다.
  1) 후보 수집   — corpus(선별 탈락분 포함) + 사건별 뉴스 검색
  2) 순위·시효   — 사건 유사도로 거르고, 대상 기사와 시간대가 맞는 것만 남긴다
  3) 본문 확보   — 상위 후보의 원문을 실제로 읽어온다. 제목만으로는 수치를 검증할 수 없다
  4) 독립성 판정 — 도메인·통신사 전재·근접중복을 걸러 '진짜 독립 출처'만 센다
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from ..llm import BaseLLM
from ..models import (
    DISPUTED,
    LIKELY,
    SINGLE_SOURCE,
    VERIFIED,
    Article,
    Brief,
    FactCheck,
    VerifiedBrief,
)
from ..search import search_related
from ..state import NewsletterState
from ..store import PublishedStore
from ..utils.extract import fetch_article_text
from ..utils.text import (
    domain_of,
    event_similarity_matrix,
    event_similarity_to,
    parse_datetime,
    truncate,
    wire_origin,
)

log = logging.getLogger(__name__)

# 본문을 가져올 수 없는 호스트. 애그리게이터 링크는 암호화 리다이렉트라
# 받아봐야 수백 KB 짜리 자바스크립트 셸이 돌아온다.
UNFETCHABLE_HOSTS = {"news.google.com", "news.yahoo.com", "flipboard.com"}


def event_text(article: Article) -> str:
    """사건 식별용 텍스트. 스니펫을 통째로 넣으면 제목 토큰이 묽어져 앞부분만 쓴다."""
    return f"{article.title} {article.snippet[:300]}"


def is_fetchable(url: str) -> bool:
    return bool(url) and domain_of(url) not in UNFETCHABLE_HOSTS


# 링크가 자사 중계 URL 이라 도메인으로 매체를 구분할 수 없는 호스트.
# 이런 소스의 기사는 도메인이 전부 같아져서, 그대로 세면 여러 매체가 한 곳으로 뭉개진다.
RELAY_HOSTS = {"finnhub.io", "news.google.com", "news.yahoo.com", "flipboard.com"}


def source_key(article: Article) -> str:
    """독립 출처를 세는 단위.

    우선순위는 통신사 > 매체명 > 도메인이다.
      - 통신사: 연합뉴스 기사를 세 매체가 받아썼으면 도메인이 셋이어도 취재원은 하나다.
      - 매체명: 링크가 중계 URL 이면 도메인이 전부 같아진다. 이때는 API 가 준 매체명을 쓴다.
      - 도메인: 그 외 일반적인 경우.
    """
    wire = wire_origin(article.content or article.snippet)
    if wire:
        return wire
    if article.publisher and (article.domain in RELAY_HOSTS or not article.domain):
        return article.publisher.lower()
    return article.domain


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_confidence(source_count: int, supported: int, contradicted: int, total_facts: int) -> float:
    """PRD 3.4 의 신뢰도 공식.

    독립 출처 수(55%)와 주장 지지율(45%)을 섞고, 상충이 하나라도 있으면 크게 깎는다.
    """
    independence = min(source_count / 3.0, 1.0)
    agreement = supported / max(total_facts, 1)
    penalty = 0.4 if contradicted > 0 else 0.0
    return round(_clamp(0.55 * independence + 0.45 * agreement - penalty), 3)


def decide_verdict(source_count: int, confidence: float, contradicted: int) -> str:
    if contradicted > 0:
        return DISPUTED
    if source_count >= 3 and confidence >= 0.7:
        return VERIFIED
    if source_count >= 2 and confidence >= 0.5:
        return LIKELY
    return SINGLE_SOURCE


def _within_age(candidate: Article, reference: datetime | None, max_age_hours: float) -> bool:
    """증인이 대상 기사와 같은 시간대의 보도인지 본다.

    보도가 적은 사건일수록 검색이 오래된 유사 기사를 끌어오고, 그게 '독립 출처'로
    잡히면 교차검증이 조용히 오염된다. 발행시각을 모르는 기사는 수집 단계와 같은
    원칙으로 유지한다 — 시각을 모른다는 이유로 증인을 버리지는 않는다.
    """
    if max_age_hours <= 0:
        return True
    published = parse_datetime(candidate.published_at)
    if published is None:
        return True
    anchor = reference or datetime.now(timezone.utc)
    return abs((published - anchor).total_seconds()) <= max_age_hours * 3600


def rank_candidates(
    target: Article,
    candidates: list[Article],
    *,
    cluster_threshold: float,
    max_age_hours: float,
    pool_size: int,
) -> list[tuple[Article, float]]:
    """사건 유사도와 시효로 증인 후보를 좁힌다 (2단계).

    뒤의 독립성 판정에서 상당수가 탈락하므로 최종 필요 수보다 넉넉히 남긴다.
    """
    if not candidates:
        return []
    sims = event_similarity_to(event_text(target), [event_text(c) for c in candidates])
    reference = parse_datetime(target.published_at)

    ranked: list[tuple[Article, float]] = []
    for index in np.argsort(-sims):
        score = float(sims[index])
        if score < cluster_threshold:
            break  # 내림차순이므로 여기서 끝
        candidate = candidates[int(index)]
        if candidate.domain == target.domain:
            continue
        if not _within_age(candidate, reference, max_age_hours):
            continue
        ranked.append((candidate, score))
        if len(ranked) >= pool_size:
            break
    return ranked


def dedupe_sources(
    target: Article,
    ranked: list[tuple[Article, float]],
    *,
    duplicate_threshold: float,
    max_peers: int,
) -> tuple[list[tuple[Article, float]], int]:
    """진짜 독립 출처만 남긴다 (4단계). 반환: (증인, 전재로 접힌 수)."""
    if not ranked:
        return [], 0

    peer_sim = event_similarity_matrix([event_text(a) for a, _ in ranked])
    seen_keys = {source_key(target)}

    accepted: list[tuple[Article, float]] = []
    accepted_rows: list[int] = []
    collapsed = 0
    for row, (article, score) in enumerate(ranked):
        key = source_key(article)
        if key in seen_keys:
            collapsed += 1
            continue
        if any(float(peer_sim[row][prev]) >= duplicate_threshold for prev in accepted_rows):
            collapsed += 1
            continue
        accepted.append((article, score))
        accepted_rows.append(row)
        seen_keys.add(key)
        if len(accepted) >= max_peers:
            break
    return accepted, collapsed


def make_verify_node(
    cfg: dict[str, Any],
    llm: BaseLLM,
    store: PublishedStore | None = None,
):
    verify_cfg = cfg["verify"]
    search_cfg = verify_cfg.get("corroboration_search", {})
    history_cfg = cfg.get("history", {})
    collect_cfg = cfg["collect"]

    cluster_threshold = float(verify_cfg["cluster_threshold"])
    duplicate_threshold = float(verify_cfg["duplicate_threshold"])
    max_peers = int(verify_cfg["max_cluster_peers"])
    max_age_hours = float(verify_cfg.get("peer_max_age_hours", 96))
    fetch_peer_content = bool(verify_cfg.get("fetch_peer_content", True))
    max_peer_fetch = int(verify_cfg.get("max_peer_fetch", 4))
    peer_body_chars = int(verify_cfg.get("peer_body_chars", 1500))
    min_confidence = float(verify_cfg["min_confidence_to_publish"])
    workers = max(1, int(cfg["research"]["max_workers"]))

    def gather_candidates(
        target: Article,
        corpus: list[Article],
        *,
        headline: str | None = None,
        entities: dict | None = None,
        keywords: list[str] | None = None,
    ) -> tuple[list[Article], int]:
        """증인 후보 풀 = corpus + 사건별 검색 결과 (1단계)."""
        pool: dict[str, Article] = {a.id: a for a in corpus if a.id != target.id}
        found = 0
        if search_cfg.get("enabled", True):
            for article in search_related(
                target.title,
                collect_cfg=collect_cfg,
                search_cfg=search_cfg,
                max_items=int(search_cfg.get("max_results", 20)),
                headline=headline,
                entities=entities,
                keywords=keywords,
            ):
                if article.id == target.id or article.id in pool:
                    continue
                pool[article.id] = article
                found += 1
        return list(pool.values()), found

    def load_peer_bodies(ranked: list[tuple[Article, float]]) -> int:
        """상위 증인의 원문을 실제로 읽어온다 (3단계).

        이게 없으면 LLM 이 제목만 보고 팩트체크한다. 실측상 검색 결과의 스니펫은
        '제목 + 매체명' 41자가 전부라, key_facts 의 수치는 영원히 unverified 가 된다.
        """
        targets = [
            article for article, _ in ranked[:max_peer_fetch]
            if not article.content and is_fetchable(article.url)
        ]
        if not targets:
            return 0

        loaded = 0
        with ThreadPoolExecutor(max_workers=min(3, len(targets))) as pool:
            futures = {
                pool.submit(
                    fetch_article_text,
                    article.url,
                    timeout=collect_cfg["timeout"],
                    user_agent=collect_cfg["user_agent"],
                    max_chars=peer_body_chars,
                ): article
                for article in targets
            }
            for future in as_completed(futures):
                article = futures[future]
                try:
                    body = future.result()
                except Exception:
                    continue
                if body:
                    article.content = body
                    loaded += 1
        return loaded

    def verify_one(brief: Brief, corpus: list[Article]) -> tuple[VerifiedBrief, str | None, dict]:
        article = brief.article
        metrics = {"searched": 0, "bodies": 0, "collapsed": 0}
        peers: list[tuple[Article, float]] = []

        if article is not None:
            candidates, metrics["searched"] = gather_candidates(
                article,
                corpus,
                headline=brief.headline,
                entities=brief.entities,
                keywords=getattr(article, "matched_keywords", []),
            )
            ranked = rank_candidates(
                article, candidates,
                cluster_threshold=cluster_threshold,
                max_age_hours=max_age_hours,
                pool_size=max_peers * 3,
            )
            if fetch_peer_content:
                metrics["bodies"] = load_peer_bodies(ranked)
            peers, metrics["collapsed"] = dedupe_sources(
                article, ranked,
                duplicate_threshold=duplicate_threshold,
                max_peers=max_peers,
            )

        source_count = 1 + len(peers)
        sources = [source_key(p) for p, _ in peers]
        log.info(
            "증인 '%s': 검색 %d → 독립 출처 %d곳 %s (본문확보 %d, 전재제외 %d)",
            brief.headline[:28], metrics["searched"], len(peers), sources,
            metrics["bodies"], metrics["collapsed"],
        )

        # R4-1: 증인이 없으면 LLM 을 부르지 않는다. 물어볼 근거 자체가 없다.
        if not peers:
            return (
                VerifiedBrief(
                    brief=brief,
                    verdict=SINGLE_SOURCE,
                    confidence=score_confidence(1, 0, 0, len(brief.key_facts)),
                    corroborating_sources=[],
                    cluster_size=1,
                    fact_checks=[
                        FactCheck(fact=f, status="unverified", note="대조할 다른 출처를 찾지 못함")
                        for f in brief.key_facts
                    ],
                    notes="같은 사건을 다룬 다른 매체 기사를 찾지 못했습니다. "
                          "단독 보도이거나 아직 확산되지 않은 소식일 수 있습니다.",
                ),
                None,
                metrics,
            )

        peer_payload = [
            {
                "source": f"{p.source} ({source_key(p)})",
                "title": p.title,
                "text": truncate(p.content or p.snippet, peer_body_chars),
            }
            for p, _ in peers
        ]

        warning = None
        try:
            result = llm.fact_check(
                source=article.source if article else "",
                headline=brief.headline,
                facts=brief.key_facts,
                peers=peer_payload,
            )
            raw_checks = result.get("fact_checks") or []
            overall = str(result.get("overall", ""))
        except Exception as exc:
            raw_checks, overall = [], ""
            warning = f"[verify] LLM 실패({type(exc).__name__}): {brief.headline[:40]}"

        checks: list[FactCheck] = []
        for item in raw_checks:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "unverified")).lower()
            if status not in ("supported", "contradicted", "unverified"):
                status = "unverified"
            checks.append(
                FactCheck(
                    fact=str(item.get("fact", "")),
                    status=status,
                    evidence=str(item.get("evidence", "")),
                    note=str(item.get("note", "")),
                )
            )
        if not checks:  # LLM 실패 시에도 출처 수만으로는 평가한다
            checks = [FactCheck(fact=f, status="unverified", note="판정 실패") for f in brief.key_facts]

        supported = sum(1 for c in checks if c.status == "supported")
        contradicted = sum(1 for c in checks if c.status == "contradicted")
        confidence = score_confidence(source_count, supported, contradicted, len(checks))

        return (
            VerifiedBrief(
                brief=brief,
                verdict=decide_verdict(source_count, confidence, contradicted),
                confidence=confidence,
                corroborating_sources=sources,
                cluster_size=source_count,
                fact_checks=checks,
                notes=overall,
            ),
            warning,
            metrics,
        )

    def dedupe_same_event(
        briefs: list[Brief], corpus: list[Article]
    ) -> tuple[list[Brief], int, int]:
        """같은 사건 중복을 두 방향으로 정리한다.

        1) 이번 실행 안에서 — 선별된 기사끼리 같은 사건이면 점수가 높은 쪽만 남긴다.
           밀려난 기사는 버려지지 않고 corpus 에 남아 대표 기사의 증인으로 다시 잡힌다.
        2) 지난 발행분과 대조 — 어제 나간 사건의 후속 기사는 ID 가 달라 그냥 두면 통과한다.
        """
        index_of = {a.id: i for i, a in enumerate(corpus)}
        sim = event_similarity_matrix([event_text(a) for a in corpus])

        past_events: list[str] = []
        if store is not None and store.enabled and history_cfg.get("enabled", True):
            past_events = [
                text for text, _ in store.recent_events(int(history_cfg.get("lookback_days", 7)))
                if text
            ]

        representatives: list[Brief] = []
        merged = 0
        already_published = 0
        for brief in briefs:  # briefs 는 선별 점수 순서
            idx = index_of.get(brief.article_id)

            if past_events and brief.article is not None:
                sims = event_similarity_to(event_text(brief.article), past_events)
                if sims.size and float(sims.max()) >= cluster_threshold:
                    already_published += 1
                    log.info("지난 발행분과 같은 사건이라 제외: '%s'", brief.headline[:36])
                    continue

            duplicate_of = None
            if idx is not None:
                for rep in representatives:
                    rep_idx = index_of.get(rep.article_id)
                    if rep_idx is not None and float(sim[idx][rep_idx]) >= cluster_threshold:
                        duplicate_of = rep
                        break
            if duplicate_of is not None:
                merged += 1
                log.info("같은 사건으로 병합: '%s' → '%s'", brief.headline[:32], duplicate_of.headline[:32])
                continue
            representatives.append(brief)
        return representatives, merged, already_published

    def verify_node(state: NewsletterState) -> dict[str, Any]:
        started = time.monotonic()
        briefs: list[Brief] = state.get("briefs", [])
        corpus: list[Article] = state.get("corpus", []) or state.get("raw_items", [])
        errors: list[str] = []

        if not briefs:
            return {"verified": [], "stats": {"verify": {"input": 0, "verified": 0}}}

        representatives, merged, already_published = dedupe_same_event(briefs, corpus)

        results: list[VerifiedBrief] = []
        totals = {"searched": 0, "bodies": 0, "collapsed": 0}
        if representatives:
            with ThreadPoolExecutor(max_workers=min(workers, len(representatives))) as pool:
                futures = {pool.submit(verify_one, b, corpus): b for b in representatives}
                for future in as_completed(futures):
                    brief = futures[future]
                    try:
                        verified, warning, metrics = future.result()
                    except Exception as exc:
                        errors.append(f"[verify] {brief.headline[:40]}: {type(exc).__name__}: {exc}")
                        continue
                    if warning:
                        errors.append(warning)
                    results.append(verified)
                    for key in totals:
                        totals[key] += metrics[key]

        order = {b.article_id: i for i, b in enumerate(briefs)}
        results.sort(key=lambda v: order.get(v.brief.article_id, 999))

        publishable = [v for v in results if v.confidence >= min_confidence]  # R4-3
        dropped = len(results) - len(publishable)

        distribution: dict[str, int] = {}
        for verified in publishable:
            distribution[verified.verdict] = distribution.get(verified.verdict, 0) + 1

        elapsed = time.monotonic() - started
        log.info("4단계 검수 완료: %d건 판정 %s (%.1fs)", len(publishable), distribution, elapsed)

        return {
            "verified": publishable,
            "errors": errors,
            "stats": {
                "verify": {
                    "input": len(briefs),
                    "merged_same_event": merged,
                    "skipped_already_published": already_published,
                    "verified": len(publishable),
                    "dropped_low_confidence": dropped,
                    "distribution": distribution,
                    "avg_confidence": round(
                        sum(v.confidence for v in publishable) / max(len(publishable), 1), 3
                    ),
                    "avg_sources": round(
                        sum(v.cluster_size for v in publishable) / max(len(publishable), 1), 2
                    ),
                    "peer_bodies_fetched": totals["bodies"],
                    "peers_collapsed_as_reprint": totals["collapsed"],
                    "search_provider": search_cfg.get("provider", "auto"),
                    "cluster_threshold": cluster_threshold,
                    "elapsed_sec": round(elapsed, 2),
                }
            },
        }

    return verify_node
