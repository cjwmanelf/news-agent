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


_NOISE_WORDS = {
    "단독", "속보", "종합", "포토", "현장", "영상", "특징주", "출격", "뜬다",
    "깜짝", "일축", "눈길", "화제", "대담", "이모저모", "맞손", "사기", "사기극",
    "상보", "직격", "비상", "주목", "발표",
}


def build_query(title: str, *, max_words: int = 6) -> str:
    """제목을 검색 질의로 다듬는다.

    말머리([단독], [AI & LAW]), 문장부호, 검색을 과도하게 좁히는 자극적 어휘를 정리하고
    핵심 키워드 4~6개를 추출한다.
    한 글자 한글 고유명사(예: 젠슨 황의 '황', '美', '韓')를 보존한다.
    """
    text = _BRACKET.sub(" ", title or "")
    text = _PUNCT.sub(" ", text)
    words = []
    for w in text.split():
        if len(w) == 1 and w.isascii():
            continue
        if w in _NOISE_WORDS:
            continue
        words.append(w)
    return " ".join(words[:max_words]).strip()


VIP_PERSONS = {
    "젠슨 황", "젠슨황", "트럼프", "도널드 트럼프", "머스크", "일론 머스크",
    "알트먼", "샘 알트먼", "샘 올트먼", "최태원", "이재용", "나델라", "사티아 나델라",
    "저커버그", "마크 저커버그", "팀 쿡", "손정의",
}

_MODEL_PATTERN = re.compile(
    r"\b([A-Za-z]+[-_]?[0-9]+[A-Za-z]*|[0-9]+[A-Za-z]+|RTX\s*(?:Pro\s*)?[0-9]+|HBM[0-9]*[A-Za-z]*|CXL|PIM|HBF|NVLink)\b",
    re.IGNORECASE,
)


def build_entity_query(
    entities: dict | None = None,
    keywords: list[str] | None = None,
    *,
    max_terms: int = 4,
    headline: str | None = None,
    title: str | None = None,
) -> str:
    """엔티티(제품/하드웨어, 주요 기관, 주요 인물) 및 키워드 기반 고정밀 검색 질의를 생성한다."""
    entities = entities or {}
    keywords = keywords or []
    terms: list[str] = []

    full_text = f"{headline or ''} {title or ''}"
    models = _MODEL_PATTERN.findall(full_text)
    products = entities.get("product", [])
    orgs = entities.get("org", [])
    persons = entities.get("person", [])

    # 1. 제품 / 하드웨어 식별자 최우선 (테크 기사에서 가장 변별력 높음)
    for prod in products + models:
        clean_prod = prod.strip()
        if clean_prod and clean_prod not in terms:
            terms.append(clean_prod)

    # 2. 주요 인물 중 대중적으로 널리 알려진 VIP 인물 우선
    # entities 에 person 이 없더라도 headline/title 텍스트에서 직접 탐색
    for vip in VIP_PERSONS:
        if vip in full_text and vip not in terms:
            terms.append(vip)

    for p in persons:
        if any(vip in p for vip in VIP_PERSONS):
            if p not in terms:
                terms.append(p)

    # 3. 핵심 기업/조직 (언론사/매체명 노이즈 제외)
    for o in orgs:
        if any(bad in o for bad in ("Hardware", "News", "뉴스", "닷컴", "신문", "일보", "방송", "Press", "미디어")):
            continue
        if o and o not in terms:
            terms.append(o)

    # 4. 매칭된 핵심 키워드
    for k in keywords:
        if k and k not in terms:
            terms.append(k)

    # 5. 지나치게 포괄적인 키워드만 모여 일반 주가/동향 뉴스로 흐르는 것 방지
    # (제목에서 핵심 사건/이벤트 단어 보강)
    if len(terms) < max_terms:
        event_words = [w for w in _PUNCT.sub(" ", full_text).split() if len(w) >= 2 and w not in _NOISE_WORDS and w not in terms]
        for ew in event_words:
            if any(marker in ew for marker in ("서밋", "인프라", "공개", "출시", "개발", "체결", "합의", "실적", "협력", "포럼")):
                terms.append(ew)
                if len(terms) >= max_terms:
                    break

    # 6. 기타 인물 (일반 인물 중 4자 이하)
    if len(terms) < max_terms:
        for p in persons:
            if len(p) <= 4 and p not in terms:
                terms.append(p)

    return " ".join(terms[:max_terms]).strip()


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
    headline: str | None = None,
    entities: dict | None = None,
    keywords: list[str] | None = None,
) -> list[Article]:
    """제목 및 엔티티/키워드를 종합해 관련 기사를 다각도로 검색한다.
    
    실패는 빈 리스트(검증을 포기할 뿐 파이프라인은 계속).
    """
    search_cfg = search_cfg or {}
    queries: list[str] = []

    # 1) 정제된 헤드라인 질의 (LLM이 정제한 가장 명확한 사건 요약문 최우선)
    if headline:
        q_head = build_query(headline, max_words=6)
        if len(q_head) >= 3 and q_head not in queries:
            queries.append(q_head)

    # 2) 엔티티/제품/VIP 인물 고정밀 질의
    q_entity = build_entity_query(entities, keywords, max_terms=4, headline=headline, title=title)
    if len(q_entity) >= 3 and q_entity not in queries:
        queries.append(q_entity)

    # 3) 정제된 원제목 질의
    q_title = build_query(title, max_words=6)
    if len(q_title) >= 3 and q_title not in queries:
        queries.append(q_title)

    # 4) 부제/절 분리 질의 (예: "— RTX Pro 5500 delivers...")
    for text in (headline, title):
        if text and any(sep in text for sep in ("—", " - ", ": ", " | ")):
            clauses = re.split(r"—|\s-\s|:\s|\s\|\s", text)
            for clause in clauses:
                q_clause = build_query(clause.strip(), max_words=5)
                if len(q_clause) >= 4 and q_clause not in queries:
                    queries.append(q_clause)
                    break

    if not queries:
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

    results_pool: dict[str, Article] = {}
    per_query_items = max(6, min(10, max_items // max(len(queries), 1) + 3))

    for idx, q in enumerate(queries):
        try:
            if provider == "naver_hub":
                batch = _search_naver(q, collect_cfg, per_query_items, hub, hub=True)
            elif provider == "naver_legacy":
                batch = _search_naver(q, collect_cfg, per_query_items, legacy, hub=False)
            else:
                batch = _search_google_news(q, collect_cfg, per_query_items, search_cfg)

            for art in batch:
                key = art.url or art.id
                if key not in results_pool:
                    results_pool[key] = art
                if len(results_pool) >= max_items:
                    break
        except Exception as exc:
            log.warning("교차검증 검색 실패 [%s] (%s): %s", provider, q[:30], exc)

        # 최소 2개 이상의 쿼리를 시도한 후 max_items 달성 시 종료 (단일 쿼리 독점 방지)
        if len(results_pool) >= max_items and idx >= 1:
            break

    return list(results_pool.values())[:max_items]
