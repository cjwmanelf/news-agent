"""기사 본문 추출 (PRD 3.3 - 1단계).

readability 같은 무거운 의존성 없이, 노이즈 제거 → 컨테이너 후보 선택 →
문단 텍스트 결합의 3단 휴리스틱으로 처리한다. 실패는 예외가 아니라 빈 문자열로 알린다.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from .http import fetch
from .text import clean_text

NOISE_TAGS = [
    "script", "style", "noscript", "iframe", "nav", "aside", "footer",
    "header", "form", "svg", "figure", "video",
]

# 광고·추천기사·댓글 블록에 흔히 붙는 클래스/아이디 조각
NOISE_HINTS = ("comment", "advert", "banner", "related", "recommend", "share", "newsletter", "promo")


def _score_container(node) -> float:
    """문단 텍스트가 실제로 많이 든 컨테이너에 높은 점수를 준다."""
    paragraphs = node.find_all("p")
    if not paragraphs:
        return 0.0
    text_len = sum(len(p.get_text(strip=True)) for p in paragraphs)
    link_len = sum(len(a.get_text(strip=True)) for a in node.find_all("a"))
    # 링크 비율이 높으면 목록/추천 블록일 가능성이 크다
    link_ratio = link_len / max(text_len, 1)
    return text_len * (1.0 - min(link_ratio, 0.9))


def extract_main_text(html: str, *, min_chars: int = 200) -> str:
    """HTML 에서 본문으로 보이는 텍스트를 뽑는다. 못 찾으면 빈 문자열."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")

    for tag in soup(NOISE_TAGS):
        tag.decompose()
    for node in soup.find_all(attrs={"class": True}):
        # find_all 결과에는 중첩 노드가 함께 들어온다. 부모를 decompose 하면
        # 뒤따라오는 자식 Tag 는 이미 해체된 상태이고, bs4 는 그 객체의 attrs 를
        # None 으로 만든다 → node.get() 이 AttributeError 로 터진다.
        if node.decomposed:
            continue
        classes = node.get("class") or []
        joined = " ".join(classes).lower()
        if any(hint in joined for hint in NOISE_HINTS):
            node.decompose()

    candidates = []
    candidates.extend(soup.find_all("article"))
    candidates.extend(soup.find_all("main"))
    candidates.extend(soup.find_all(attrs={"itemprop": "articleBody"}))
    candidates.extend(soup.find_all("div"))
    candidates.extend(soup.find_all("section"))

    best, best_score = None, 0.0
    for node in candidates:
        score = _score_container(node)
        if score > best_score:
            best, best_score = node, score

    if best is None:
        return ""

    paragraphs = [clean_text(p.get_text(" ", strip=True)) for p in best.find_all("p")]
    body = "\n".join(p for p in paragraphs if len(p) > 20)
    if len(body) < min_chars:
        # 문단 태그를 안 쓰는 사이트 대비 폴백
        body = clean_text(best.get_text(" ", strip=True))
    return body if len(body) >= min_chars else ""


def fetch_article_text(url: str, *, timeout: float = 15, user_agent: str, max_chars: int) -> str:
    """기사 URL → 본문 텍스트. 실패는 빈 문자열(호출부가 스니펫으로 폴백, R3-4)."""
    try:
        response = fetch(url, timeout=timeout, user_agent=user_agent)
    except Exception:
        return ""
    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        return ""
    return extract_main_text(response.text)[:max_chars]
