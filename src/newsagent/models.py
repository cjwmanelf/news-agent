"""파이프라인을 흐르는 데이터 모델 (PRD 2.3)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Article:
    """1단계 수집 산출물. 2단계에서 점수 필드가, 3단계에서 content 가 채워진다."""

    id: str
    title: str
    url: str
    source: str
    domain: str
    source_type: str  # rss | api | crawl
    # content       = 선별·요약 대상이자 교차검증 증인
    # corroboration = 교차검증 증인으로만 사용 (뉴스 애그리게이터처럼 원문 본문을
    #                 가져올 수 없는 소스. 자세한 이유는 config/sources.yaml 주석 참고)
    role: str = "content"
    # 매체명. 링크가 중계 URL 이라 도메인으로 매체를 알 수 없을 때 쓴다.
    # (finnhub 처럼 url 이 자사 리다이렉트인 API, 애그리게이터 피드 등)
    # 4단계 독립 출처 카운트가 이 값을 도메인보다 먼저 본다.
    publisher: str = ""
    published_at: str | None = None
    snippet: str = ""
    content: str = ""

    # 2단계 선별에서 채워지는 필드 (R2-4)
    score: float = 0.0
    keyword_score: float = 0.0
    vector_score: float = 0.0
    matched_interest: str | None = None
    matched_keywords: list[str] = field(default_factory=list)

    @property
    def match_text(self) -> str:
        """선별·클러스터링에 쓰는 대표 텍스트."""
        return f"{self.title} {self.snippet}".strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Brief:
    """3단계 취재 산출물."""

    article_id: str
    headline: str
    summary: list[str] = field(default_factory=list)
    key_facts: list[str] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    category: str = "기타"
    why_it_matters: str = ""
    article: Article | None = None
    degraded: bool = False  # 본문 추출/LLM 실패로 폴백 경로를 탔는지 (R3-4)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FactCheck:
    """key_fact 하나에 대한 교차검증 판정."""

    fact: str
    status: str  # supported | contradicted | unverified
    evidence: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# verdict 상수
VERIFIED = "VERIFIED"
LIKELY = "LIKELY"
SINGLE_SOURCE = "SINGLE_SOURCE"
DISPUTED = "DISPUTED"

VERDICT_LABEL = {
    VERIFIED: "교차검증됨",
    LIKELY: "사실로 추정",
    SINGLE_SOURCE: "단일 출처",
    DISPUTED: "내용 상충",
}

VERDICT_COLOR = {
    VERIFIED: 0x2ECC71,  # 초록
    LIKELY: 0x3498DB,  # 파랑
    SINGLE_SOURCE: 0x95A5A6,  # 회색
    DISPUTED: 0xE74C3C,  # 빨강
}

VERDICT_EMOJI = {
    VERIFIED: "✅",
    LIKELY: "🔵",
    SINGLE_SOURCE: "⚪",
    DISPUTED: "⚠️",
}


@dataclass
class VerifiedBrief:
    """4단계 검수 산출물."""

    brief: Brief
    verdict: str
    confidence: float
    corroborating_sources: list[str] = field(default_factory=list)
    cluster_size: int = 1
    fact_checks: list[FactCheck] = field(default_factory=list)
    notes: str = ""

    @property
    def article(self) -> Article | None:
        return self.brief.article

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
