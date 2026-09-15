"""RSS 2.0 / Atom 1.0 수집기 (PRD 3.1).

feedparser 가 두 포맷을 같은 인터페이스로 흡수하므로 분기할 필요가 없다.
네트워크는 feedparser 에 맡기지 않고 직접 가져온다 — 타임아웃과 User-Agent 를 통제하기 위해서.
"""

from __future__ import annotations

import re

import feedparser

from ..models import Article
from ..utils.http import fetch
from ..utils.text import clean_text, parse_datetime
from .base import Collector

# Google News 는 제목 끝에 " - 매체명" 을 붙인다
_SOURCE_SUFFIX = re.compile(r"\s+[-–—]\s+[^-–—]{2,30}$")


class RSSCollector(Collector):
    source_type = "rss"

    def collect(self) -> list[Article]:
        response = fetch(self.url, timeout=self.timeout, user_agent=self.user_agent)
        feed = feedparser.parse(response.content)

        articles: list[Article] = []
        for entry in feed.entries[: self.max_items]:
            link = entry.get("link") or ""
            title = entry.get("title", "")

            # 애그리게이터(구글뉴스 등)는 원 매체 정보를 source 필드로 준다.
            # 이걸 살려야 4단계에서 도메인 기준 독립 출처 카운트가 의미를 갖는다.
            source_name, source_url = None, None
            entry_source = entry.get("source")
            if entry_source:
                source_name = clean_text(entry_source.get("title")) or None
                source_url = entry_source.get("href") or None
                if source_name:
                    title = _SOURCE_SUFFIX.sub("", title)

            published = parse_datetime(
                entry.get("published_parsed")
                or entry.get("updated_parsed")
                or entry.get("published")
                or entry.get("updated")
            )

            snippet = entry.get("summary") or entry.get("description") or ""
            if not snippet and entry.get("content"):
                snippet = entry["content"][0].get("value", "")

            article = self.make_article(
                title=title,
                url=link,
                snippet=snippet,
                published_at=published.isoformat() if published else None,
                source_name=f"{source_name}" if source_name else None,
                source_url=source_url,
                publisher=source_name,
            )
            if article:
                articles.append(article)
        return articles
