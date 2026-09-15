"""수집기 공통 인터페이스 (PRD 3.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Article
from ..utils.text import clean_text, domain_of, make_id, truncate


class Collector(ABC):
    """모든 수집기는 spec(소스 설정) 을 받아 Article 리스트를 돌려준다."""

    source_type = "base"

    def __init__(self, spec: dict[str, Any], collect_cfg: dict[str, Any]):
        self.spec = spec
        self.cfg = collect_cfg
        self.name = spec.get("name") or spec.get("url", "unknown")
        self.url = spec.get("url", "")
        self.max_items = int(spec.get("max_items", collect_cfg.get("default_max_items", 50)))
        self.role = spec.get("role", "content")
        self.timeout = float(collect_cfg.get("timeout", 15))
        self.user_agent = collect_cfg.get("user_agent", "newsagent/1.0")

    @abstractmethod
    def collect(self) -> list[Article]:
        ...

    def make_article(
        self,
        *,
        title: str,
        url: str,
        snippet: str = "",
        published_at: str | None = None,
        source_name: str | None = None,
        source_url: str | None = None,
        publisher: str | None = None,
    ) -> Article | None:
        """공통 정규화를 거쳐 Article 을 만든다. 제목이나 URL 이 없으면 None."""
        title = clean_text(title)
        url = (url or "").strip()
        if not title or not url.startswith("http"):
            return None
        return Article(
            id=make_id(url),
            title=truncate(title, 300),
            url=url,
            source=source_name or self.name,
            # 4단계 독립 출처 카운트의 기준. 애그리게이터는 원 매체 도메인을 넘겨준다.
            domain=domain_of(source_url or url),
            source_type=self.source_type,
            role=self.role,
            publisher=clean_text(publisher or ""),
            published_at=published_at,
            snippet=truncate(clean_text(snippet), 1000),
        )
