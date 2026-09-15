"""텍스트 정규화·중복 판정·TF-IDF 유사도 (PRD R1-5, 3.2, 3.4).

한국어 형태소 분석기 없이 동작해야 하므로 벡터화는 문자 n-gram 을 쓴다.
조사/어미 변화("엔비디아가", "엔비디아는")를 부분 문자열 수준에서 흡수한다.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

# 링크에 붙는 추적 파라미터 — 같은 기사를 다른 URL 로 보이게 만드는 주범
TRACKING_PARAMS = {
    "fbclid", "gclid", "igshid", "mc_cid", "mc_eid", "ref", "ref_src",
    "spm", "yclid", "_ga", "cmpid", "CMP", "smid", "partner",
}

_WS = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^0-9a-z가-힣]+")
_HTML_TAG = re.compile(r"<[^>]+>")


def clean_text(value: str | None) -> str:
    """HTML 태그·엔티티를 걷어내고 공백을 정리한다.

    엔티티 해제는 태그 제거 뒤에 한다. 순서를 바꾸면 &lt;script&gt; 같은 이스케이프된
    마크업이 태그로 되살아난다. 피드 description 에 &nbsp; 가 흔해 해제는 필수다.
    """
    if not value:
        return ""
    text = _HTML_TAG.sub(" ", str(value))
    text = html.unescape(text)
    text = _HTML_TAG.sub(" ", text)  # 엔티티 해제로 드러난 태그 잔재 정리
    text = unicodedata.normalize("NFKC", text)
    return _WS.sub(" ", text).strip()


def normalize_url(url: str) -> str:
    """추적 파라미터·프래그먼트를 제거해 중복 판정용 정규 URL 을 만든다 (R1-5)."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if not k.lower().startswith("utm_") and k not in TRACKING_PARAMS
    ]
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower() or "https", netloc, path, urlencode(query), ""))


def domain_of(url: str) -> str:
    """독립 출처 카운트의 기준이 되는 도메인 (R4 2단계)."""
    netloc = urlsplit(url).netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc.split(":")[0]


def make_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def title_key(title: str) -> str:
    """제목 기반 중복 판정 키 — 공백·기호·대소문자 차이를 무시한다."""
    return _NON_WORD.sub("", clean_text(title).lower())


def parse_datetime(value) -> datetime | None:
    """피드/API 마다 제각각인 시간 표현을 UTC aware datetime 으로 맞춘다.

    파싱 실패 시 None 을 돌려주고, 호출부는 이를 '버리지 않고 유지'로 처리한다 (R1-4).
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, (tuple, list)) and len(value) >= 6:  # feedparser struct_time
        try:
            return datetime(*value[:6], tzinfo=timezone.utc)
        except ValueError:
            return None

    text = str(value).strip()
    if not text:
        return None

    # Unix 타임스탬프를 문자열로 주는 API 가 흔하다 (finnhub 의 datetime 등).
    # 초/밀리초 둘 다 받는다. 10자리면 초, 13자리면 밀리초.
    if text.lstrip("-").isdigit():
        try:
            number = int(text)
            if abs(number) > 10_000_000_000:  # 밀리초로 보인다
                number /= 1000
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        pass
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _vectorizer() -> TfidfVectorizer:
    # char_wb = 단어 경계를 존중하는 문자 n-gram. 한국어·영어 혼재 코퍼스에 무난하다.
    return TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 4),
        sublinear_tf=True,
        min_df=1,
        max_features=60000,
    )


def similarity_to_profiles(docs: list[str], profiles: list[str]) -> np.ndarray:
    """기사 × 관심사프로필 코사인 유사도 행렬 (2단계 벡터 점수).

    어휘를 공유해야 비교가 성립하므로 두 집합을 합쳐 한 번에 fit 한다.
    """
    if not docs or not profiles:
        return np.zeros((len(docs), len(profiles)))
    safe_docs = [d if d.strip() else " " for d in docs]
    safe_profiles = [p if p.strip() else " " for p in profiles]
    try:
        matrix = _vectorizer().fit_transform(safe_docs + safe_profiles)
    except ValueError:  # 코퍼스가 전부 공백인 극단적 경우
        return np.zeros((len(docs), len(profiles)))
    return cosine_similarity(matrix[: len(docs)], matrix[len(docs) :])


_WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")


def event_tokens(text: str) -> list[str]:
    """사건 식별용 토큰. 5자 이상 단어는 앞 4자만 남겨 조사·어미를 흡수한다.

    형태소 분석기 없이 '삼성전자가/삼성전자는' → '삼성전자' 를 같은 토큰으로 만든다.
    """
    return [w[:4] if len(w) > 4 else w for w in _WORD.findall(text.lower())]


def event_similarity_matrix(docs: list[str]) -> np.ndarray:
    """같은 사건을 다룬 기사끼리의 유사도 행렬 (4단계 클러스터링).

    2단계와 달리 여기서는 문자 n-gram 을 쓰면 안 된다. 실측 결과 char_wb(2,4) 는
    같은 사건 기사쌍(0.225)과 '같은 기업이 나오지만 다른 사건'인 기사쌍(0.227)을
    전혀 구분하지 못했다 — 변별폭 -0.001. 두 기사 모두 '삼성전자·SK하이닉스·반도체'라는
    문자열을 공유하기 때문이다.

    단어 단위 TF-IDF 는 같은 조건에서 0.415 대 0.129 로 갈랐다(변별폭 +0.286).
    IDF 가 '삼성전자·반도체' 처럼 어디에나 나오는 토큰의 무게를 깎고,
    '전기료·선납·한전' 처럼 그 사건에만 나오는 토큰을 살리기 때문이다.

    이 구분이 무너지면 무관한 기사가 서로의 '독립 출처'로 잡혀 허위 교차검증이 된다.
    검증하지 못하는 것보다 잘못 검증하는 쪽이 훨씬 나쁘다.
    """
    if len(docs) < 2:
        return np.eye(len(docs)) if docs else np.zeros((0, 0))
    safe = [d if d.strip() else " " for d in docs]
    try:
        matrix = TfidfVectorizer(
            analyzer=event_tokens, sublinear_tf=True, min_df=1
        ).fit_transform(safe)
    except ValueError:
        return np.eye(len(docs))
    return cosine_similarity(matrix)


# 통신사 전재 판별용 바이라인 패턴.
# 같은 통신사 기사를 여러 매체가 받아쓴 것을 '독립 출처 N곳'으로 세면 신뢰도가 부풀려진다.
# 도메인은 서로 다르지만 원 취재원은 하나이기 때문이다.
_WIRE_NAMES = r"연합뉴스|뉴시스|뉴스1|뉴스핌|Reuters|로이터|AP통신|AFP|Bloomberg|블룸버그"
_WIRE_BYLINE = re.compile(rf"[(\[]\s*[가-힣A-Za-z·\s]{{0,12}}=\s*({_WIRE_NAMES})\s*[)\]]")
_WIRE_CREDIT = re.compile(rf"(?:저작권자|제공|=)\s*(?:ⓒ|©)?\s*({_WIRE_NAMES})")
_WIRE_PAREN = re.compile(rf"[(\[]\s*({_WIRE_NAMES})\s*[)\]]")


def wire_origin(text: str | None) -> str | None:
    """기사 텍스트에서 통신사 바이라인을 찾는다. 없으면 None.

    '(서울=연합뉴스)', '[세종=뉴시스]', '저작권자 ⓒ 연합뉴스', '(Reuters)' 등을 잡는다.
    반환값은 4단계에서 도메인 대신 쓰는 '독립 출처 키'가 된다.
    """
    if not text:
        return None
    for pattern in (_WIRE_BYLINE, _WIRE_CREDIT, _WIRE_PAREN):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def event_similarity_to(target: str, docs: list[str]) -> np.ndarray:
    """target 하나와 여러 후보 사이의 사건 유사도 (행 하나만 필요할 때)."""
    if not docs:
        return np.zeros(0)
    safe = [d if d.strip() else " " for d in [target] + docs]
    try:
        matrix = TfidfVectorizer(
            analyzer=event_tokens, sublinear_tf=True, min_df=1
        ).fit_transform(safe)
    except ValueError:
        return np.zeros(len(docs))
    return cosine_similarity(matrix[0], matrix[1:])[0]


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix
