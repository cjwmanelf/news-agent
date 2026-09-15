"""교차검증용 기사 검색 (PRD 3.4).

고정된 피드 풀만 뒤져서는 교차검증이 거의 항상 실패한다.
피드는 '오늘 뜨는 기사'를 주지, '내가 검증하려는 그 사건을 다룬 기사'를 주지 않기 때문이다.
실측에서도 선별 8건 전부 증인 0건이 나왔다. 그래서 사건별로 직접 찾는다.

백엔드 세 가지:

  google_news  — 키가 필요 없다. 다만 링크가 암호화 리다이렉트라 원문 본문을 못 가져오고,
                 스니펫도 '제목 + 매체명'이 전부다(실측: content 0자, snippet 41자).
                 즉 LLM 이 제목만 보고 팩트체크하게 된다.

  naver_hub    — 네이버 검색 API. originallink(원문 URL)와 실제 description 을 주므로
                 증인 본문을 읽고 대조할 수 있어 검증 품질이 크게 달라진다.
                 2026-07-31 부로 신규 발급이 개발자센터에서 NAVER API HUB
                 (네이버클라우드 플랫폼)로 이관됐다. 지금 새로 신청하면 이쪽이다.

  naver_legacy — 개발자센터에서 예전에 받은 Client ID/Secret 을 쓰는 구 방식.
                 2027-06-30 까지만 지원한다고 공지돼 있다. 기존 키가 있을 때만 쓴다.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import quote_plus

from .collectors.api import APICollector
from .collectors.rss import RSSCollector
from .models import Article

log = logging.getLogger(__name__)

GOOGLE_NEWS_ENDPOINT = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"

# 신규 발급 경로 (네이버클라우드 플랫폼 > NAVER API HUB)
NAVER_HUB_ENDPOINT = "https://naverapihub.apigw.ntruss.com/search/v1/news"
# 개발자센터에서 받은 구 키용. 2027-06-30 까지만 동작한다고 공지돼 있다.
NAVER_LEGACY_ENDPOINT = "https://openapi.naver.com/v1/search/news.json"

_PUNCT = re.compile(r"[^0-9A-Za-z가-힣\s]+")
_BRACKET = re.compile(r"[\[(【〔][^\])】〕]*[\])】〕]")

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def build_query(title: str, *, max_words: int = 12) -> str:
    """제목을 검색 질의로 다듬는다.

    말머리([단독], [AI & LAW])와 문장부호는 검색을 좁히기만 하고 도움이 되지 않는다.
    """
    text = _BRACKET.sub(" ", title or "")
    text = _PUNCT.sub(" ", text)
    words = [w for w in text.split() if len(w) > 1]
    return " ".join(words[:max_words]).strip()


def naver_hub_credentials() -> tuple[str, str] | None:
    key_id = os.environ.get("NAVER_API_KEY_ID", "").strip()
    key = os.environ.get("NAVER_API_KEY", "").strip()
    return (key_id, key) if key_id and key else None


def naver_legacy_credentials() -> tuple[str, str] | None:
    client_id = os.environ.get("NAVER_CLIENT_ID", "").strip()
    client_secret = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    return (client_id, client_secret) if client_id and client_secret else None


def _search_google_news(query: str, collect_cfg: dict, max_items: int, search_cfg: dict) -> list[Article]:
    url = GOOGLE_NEWS_ENDPOINT.format(
        query=quote_plus(query),
        hl=search_cfg.get("hl", "ko"),
        gl=search_cfg.get("gl", "KR"),
        ceid=search_cfg.get("ceid", "KR:ko"),
    )
    spec = {
        "name": f"검색: {query[:24]}",
        "type": "rss",
        "url": url,
        "max_items": max_items,
        "role": "corroboration",
    }
    # 파싱·정규화·원매체 도메인 추출을 RSS 수집기와 똑같이 태운다
    return RSSCollector(spec, collect_cfg).collect()


# 두 경로 모두 같은 검색 API 라 응답 모양이 같다 (items[] / originallink / description / pubDate).
NAVER_FIELD_MAP = {
    "title": "title",
    # link(n.news.naver.com) 이 아니라 originallink 를 쓴다. 네이버 도메인으로
    # 통일돼 버리면 '독립 출처' 카운트가 전부 1곳으로 뭉개진다.
    "url": "originallink",
    "snippet": "description",
    "published_at": "pubDate",
}


def _search_naver(
    query: str, collect_cfg: dict, max_items: int,
    credentials: tuple[str, str], *, hub: bool,
) -> list[Article]:
    left, right = credentials
    headers = (
        {"X-NCP-APIGW-API-KEY-ID": left, "X-NCP-APIGW-API-KEY": right}
        if hub
        else {"X-Naver-Client-Id": left, "X-Naver-Client-Secret": right}
    )
    spec = {
        "name": f"네이버검색: {query[:20]}",
        "type": "api",
        "url": NAVER_HUB_ENDPOINT if hub else NAVER_LEGACY_ENDPOINT,
        "params": {"query": query, "display": min(max_items, 100), "sort": "sim"},
        "headers": headers,
        "items_path": "items",
        "field_map": NAVER_FIELD_MAP,
        "max_items": max_items,
        "role": "corroboration",
    }
    return APICollector(spec, collect_cfg).collect()


def search_related(
    title: str,
    *,
    collect_cfg: dict,
    search_cfg: dict | None = None,
    max_items: int = 20,
) -> list[Article]:
    """제목으로 관련 기사를 검색한다. 실패는 빈 리스트(검증을 포기할 뿐 파이프라인은 계속)."""
    search_cfg = search_cfg or {}
    query = build_query(title)
    if len(query) < 4:
        return []

    provider = search_cfg.get("provider", "auto")
    hub = naver_hub_credentials()
    legacy = naver_legacy_credentials()

    # 'naver' 는 구버전 설정 호환. 키가 있는 쪽으로 알아서 보낸다.
    if provider in ("auto", "naver"):
        provider = "naver_hub" if hub else ("naver_legacy" if legacy else "google_news")
    elif provider == "naver_hub" and not hub:
        _warn_once(
            "hub-missing",
            "search.provider=naver_hub 인데 NAVER_API_KEY_ID/NAVER_API_KEY 가 없어 "
            f"{'naver_legacy' if legacy else 'google_news'} 로 폴백합니다.",
        )
        provider = "naver_legacy" if legacy else "google_news"
    elif provider == "naver_legacy" and not legacy:
        _warn_once(
            "legacy-missing",
            "search.provider=naver_legacy 인데 NAVER_CLIENT_ID/SECRET 이 없어 "
            f"{'naver_hub' if hub else 'google_news'} 로 폴백합니다.",
        )
        provider = "naver_hub" if hub else "google_news"

    try:
        if provider == "naver_hub":
            return _search_naver(query, collect_cfg, max_items, hub, hub=True)
        if provider == "naver_legacy":
            return _search_naver(query, collect_cfg, max_items, legacy, hub=False)
        return _search_google_news(query, collect_cfg, max_items, search_cfg)
    except Exception as exc:
        log.warning("교차검증 검색 실패 [%s] (%s): %s", provider, query[:30], exc)
        return []
