"""목록 페이지 크롤링 수집기 (PRD 3.1, R1-6).

RSS 도 API 도 없는 사이트용. CSS 셀렉터로 목록에서 링크와 제목을 뽑는다.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..models import Article
from ..utils.http import fetch, robots_allows
from ..utils.text import clean_text
from .base import Collector


class RobotsBlocked(Exception):
    """robots.txt 가 막은 경로. 소스를 조용히 건너뛰되 사유는 남긴다."""


class CrawlCollector(Collector):
    source_type = "crawl"

    def collect(self) -> list[Article]:
        if self.cfg.get("respect_robots", True) and not robots_allows(self.url, self.user_agent):
            raise RobotsBlocked(f"robots.txt 가 수집을 허용하지 않습니다: {self.url}")

        response = fetch(self.url, timeout=self.timeout, user_agent=self.user_agent)
        soup = BeautifulSoup(response.text, "lxml")

        list_selector = self.spec["list_selector"]
        title_selector = self.spec.get("title_selector")
        snippet_selector = self.spec.get("snippet_selector")
        link_attr = self.spec.get("link_attr", "href")
        base_url = self.spec.get("base_url") or self.url

        articles: list[Article] = []
        seen: set[str] = set()
        for node in soup.select(list_selector):
            # list_selector 가 <a> 자체일 수도, <a> 를 품은 컨테이너일 수도 있다
            anchor = node if node.name == "a" else node.select_one("a")
            if anchor is None:
                continue
            href = anchor.get(link_attr)
            if not href:
                continue
            url = urljoin(base_url, href)
            if url in seen:
                continue
            seen.add(url)

            if title_selector:
                title_node = node.select_one(title_selector)
                title = clean_text(title_node.get_text(" ", strip=True)) if title_node else ""
            else:
                title = clean_text(anchor.get_text(" ", strip=True))

            snippet = ""
            if snippet_selector:
                snippet_node = node.select_one(snippet_selector)
                snippet = clean_text(snippet_node.get_text(" ", strip=True)) if snippet_node else ""

            article = self.make_article(title=title, url=url, snippet=snippet)
            if article:
                articles.append(article)
            if len(articles) >= self.max_items:
                break
        return articles
