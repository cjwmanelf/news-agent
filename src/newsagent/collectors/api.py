"""범용 JSON API 수집기 (PRD 3.1).

특정 뉴스 API 에 종속되지 않도록, 응답 구조를 설정으로 기술하게 한다.
  items_path: 기사 배열의 위치 (점 표기, 예: "data.articles")
  field_map : Article 필드 ← 응답 필드 (점 표기 지원, 예: source.name)
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ..models import Article
from ..utils.http import fetch
from ..utils.text import parse_datetime
from .base import Collector

# 날짜 자리표시자. 기간을 요구하는 API(finnhub 의 from/to 등)를 매일 돌리려면
# 날짜를 고정으로 적어두면 안 된다. 하루만 지나도 낡은 구간을 계속 조회하게 된다.
_DATE_TOKEN = re.compile(r"\{(today|yesterday|days_ago:(\d+))\}")


def expand_dates(value: Any) -> Any:
    """문자열 안의 {today} / {yesterday} / {days_ago:N} 을 YYYY-MM-DD 로 바꾼다."""
    if not isinstance(value, str):
        return value

    def swap(match: re.Match) -> str:
        now = datetime.now(timezone.utc)
        if match.group(1) == "today":
            target = now
        elif match.group(1) == "yesterday":
            target = now - timedelta(days=1)
        else:
            target = now - timedelta(days=int(match.group(2)))
        return target.strftime("%Y-%m-%d")

    return _DATE_TOKEN.sub(swap, value)

DEFAULT_FIELD_MAP = {
    "title": "title",
    "url": "url",
    "published_at": "publishedAt",
    "snippet": "description",
}


def dig(node: Any, path: str) -> Any:
    """점 표기 경로로 중첩 구조를 따라간다. 없으면 None."""
    if not path:
        return None
    current = node
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
        if current is None:
            return None
    return current


class APICollector(Collector):
    source_type = "api"

    def collect(self) -> list[Article]:
        params = {k: expand_dates(v) for k, v in (self.spec.get("params") or {}).items()}
        response = fetch(
            expand_dates(self.url),
            timeout=self.timeout,
            user_agent=self.user_agent,
            headers=self.spec.get("headers") or None,
            params=params or None,
        )
        payload = response.json()

        items_path = self.spec.get("items_path") or ""
        items = dig(payload, items_path) if items_path else payload
        if isinstance(items, dict):  # 단건 응답 방어
            items = [items]
        if not isinstance(items, list):
            raise ValueError(f"items_path '{items_path}' 가 배열을 가리키지 않습니다")

        field_map = {**DEFAULT_FIELD_MAP, **(self.spec.get("field_map") or {})}
        articles: list[Article] = []
        for item in items[: self.max_items]:
            published = parse_datetime(dig(item, field_map.get("published_at", "")))
            source_name = dig(item, field_map["source"]) if "source" in field_map else None
            article = self.make_article(
                title=dig(item, field_map["title"]) or "",
                url=dig(item, field_map["url"]) or "",
                snippet=dig(item, field_map.get("snippet", "")) or "",
                published_at=published.isoformat() if published else None,
                source_name=source_name,
                # 매체명을 주는 API 라면 독립 출처 키로 쓴다.
                # finnhub 처럼 url 이 자사 중계 링크면 도메인만으로는 매체를 구분할 수 없다.
                publisher=source_name,
            )
            if article:
                articles.append(article)
        return articles
