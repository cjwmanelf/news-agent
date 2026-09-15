"""1단계 수집 노드 (PRD 3.1)."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any

from ..collectors import RobotsBlocked, build_collector
from ..models import Article
from ..state import NewsletterState
from ..utils.text import parse_datetime, title_key

log = logging.getLogger(__name__)


def _within_window(article: Article, cutoff: datetime) -> bool:
    """lookback 창 안의 기사만 통과. 시각을 모르면 버리지 않고 유지한다 (R1-4)."""
    if not article.published_at:
        return True
    published = parse_datetime(article.published_at)
    if published is None:
        return True
    return published >= cutoff


def deduplicate(articles: list[Article]) -> tuple[list[Article], int]:
    """정규화 URL 해시 + 제목 키로 중복을 제거한다 (R1-5).

    제목 중복 판정은 **같은 도메인 안에서만** 한다.
    매체가 다른데 제목이 같은 기사는 중복이 아니라 같은 사건을 각자 보도한 것이고,
    그게 바로 4단계가 찾는 독립 출처다. 전역으로 지우면 증인을 먼저 버리게 된다.
    (실측: 400건 수집분에서 매체가 다른데 제목이 같은 그룹이 4개 있었다.)
    """
    seen_ids: set[str] = set()
    seen_titles: set[tuple[str, str]] = set()
    unique: list[Article] = []
    for article in articles:
        tkey = title_key(article.title)
        domain_title = (article.domain, tkey)
        if article.id in seen_ids or (tkey and domain_title in seen_titles):
            continue
        seen_ids.add(article.id)
        if tkey:
            seen_titles.add(domain_title)
        unique.append(article)
    return unique, len(articles) - len(unique)


def make_collect_node(cfg: dict[str, Any]):
    collect_cfg = cfg["collect"]
    specs = [s for s in cfg["sources"] if s.get("enabled", True)]

    def collect_node(state: NewsletterState) -> dict[str, Any]:
        started = time.monotonic()
        default_lookback = collect_cfg["lookback_hours"]
        default_cutoff = datetime.now(timezone.utc) - timedelta(hours=default_lookback)

        gathered: list[Article] = []
        errors: list[str] = []
        per_source: dict[str, int] = {}

        max_workers = max(1, min(int(collect_cfg["max_workers"]), len(specs) or 1))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for spec in specs:
                try:
                    collector = build_collector(spec, collect_cfg)
                except ValueError as exc:
                    errors.append(f"[collect] {spec.get('name')}: {exc}")
                    continue
                futures[pool.submit(collector.collect)] = spec

            for future in as_completed(futures):
                spec = futures[future]
                name = spec.get("name", spec.get("url"))
                try:
                    # R1-3: 한 소스의 실패가 나머지를 막지 않는다
                    items = future.result(timeout=collect_cfg["timeout"] * 2)
                except RobotsBlocked as exc:
                    errors.append(f"[collect] {name}: robots.txt 차단 — {exc}")
                    per_source[name] = 0
                    continue
                except Exception as exc:
                    errors.append(f"[collect] {name}: {type(exc).__name__}: {exc}")
                    per_source[name] = 0
                    continue

                source_lookback = spec.get("lookback_hours")
                if source_lookback is not None:
                    source_cutoff = datetime.now(timezone.utc) - timedelta(hours=int(source_lookback))
                else:
                    source_cutoff = default_cutoff

                fresh = [a for a in items if _within_window(a, source_cutoff)]
                per_source[name] = len(fresh)
                gathered.extend(fresh)
                log.info("수집 %s: %d건 (윈도우 필터 전 %d건)", name, len(fresh), len(items))

        unique, dropped = deduplicate(gathered)
        elapsed = time.monotonic() - started
        log.info("1단계 수집 완료: %d건 (중복 %d건 제거, %.1fs)", len(unique), dropped, elapsed)

        return {
            "raw_items": unique,
            "errors": errors,
            "stats": {
                "collect": {
                    "sources_enabled": len(specs),
                    "sources_failed": sum(1 for v in per_source.values() if v == 0),
                    "fetched": len(gathered),
                    "duplicates_removed": dropped,
                    "unique": len(unique),
                    "per_source": per_source,
                    "elapsed_sec": round(elapsed, 2),
                }
            },
        }

    return collect_node
