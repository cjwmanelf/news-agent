"""수집기 레지스트리."""

from __future__ import annotations

from typing import Any

from .api import APICollector
from .base import Collector
from .crawl import CrawlCollector, RobotsBlocked
from .rss import RSSCollector

REGISTRY: dict[str, type[Collector]] = {
    "rss": RSSCollector,
    "api": APICollector,
    "crawl": CrawlCollector,
}


def build_collector(spec: dict[str, Any], collect_cfg: dict[str, Any]) -> Collector:
    source_type = spec.get("type")
    if source_type not in REGISTRY:
        raise ValueError(f"알 수 없는 소스 타입: {source_type}")
    return REGISTRY[source_type](spec, collect_cfg)


__all__ = ["Collector", "RobotsBlocked", "REGISTRY", "build_collector"]
