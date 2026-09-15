"""Discord 웹후크 발행 (PRD 3.5).

웹후크를 쓰면 봇 상주 프로세스 없이도 지정한 이름/아바타로 채널에 글을 올릴 수 있다.
Discord 의 embed 제약(개수·길이)을 넘기면 요청 전체가 400 으로 떨어지므로
전송 전에 잘라내고 나눠 보낸다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..models import VERDICT_COLOR, VERDICT_EMOJI, VERDICT_LABEL, VerifiedBrief
from ..utils.text import truncate
from .base import Publisher, post_with_retry

log = logging.getLogger(__name__)

# Discord 제약 (R5-1)
MAX_EMBEDS = 10
MAX_TOTAL_CHARS = 6000
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_FOOTER = 2048


def _embed_size(embed: dict[str, Any]) -> int:
    size = len(embed.get("title", "")) + len(embed.get("description", ""))
    size += len(embed.get("footer", {}).get("text", ""))
    for field in embed.get("fields", []):
        size += len(field.get("name", "")) + len(field.get("value", ""))
    return size


def build_embed(item: VerifiedBrief) -> dict[str, Any]:
    brief = item.brief
    article = brief.article

    bullets = "\n".join(f"• {line}" for line in brief.summary) or "(요약 없음)"
    if brief.why_it_matters:
        bullets += f"\n\n**왜 중요한가** — {brief.why_it_matters}"

    fields = [
        {
            "name": "신뢰도",
            "value": f"{VERDICT_EMOJI.get(item.verdict, '')} {VERDICT_LABEL.get(item.verdict, item.verdict)} "
                     f"({item.confidence:.2f})",
            "inline": True,
        },
        {
            "name": "교차 출처",
            "value": truncate(", ".join(item.corroborating_sources) or "없음", MAX_FIELD_VALUE),
            "inline": True,
        },
    ]

    supported = sum(1 for c in item.fact_checks if c.status == "supported")
    contradicted = sum(1 for c in item.fact_checks if c.status == "contradicted")
    if item.fact_checks:
        fields.append(
            {
                "name": "사실 확인",
                "value": f"검증 대상 {len(item.fact_checks)}건 · 확인 {supported}건"
                         + (f" · ⚠️ 상충 {contradicted}건" if contradicted else ""),
                "inline": False,
            }
        )
    if contradicted and item.notes:
        fields.append({"name": "상충 내용", "value": truncate(item.notes, MAX_FIELD_VALUE), "inline": False})

    footer_parts = [article.source if article else "", brief.category]
    if article and article.published_at:
        footer_parts.append(article.published_at[:16].replace("T", " "))
    if brief.degraded:
        footer_parts.append("요약 폴백")

    return {
        "title": truncate(brief.headline, MAX_TITLE),
        "url": article.url if article else None,
        "description": truncate(bullets, MAX_DESCRIPTION),
        "color": VERDICT_COLOR.get(item.verdict, 0x95A5A6),
        "fields": fields,
        "footer": {"text": truncate(" · ".join(p for p in footer_parts if p), MAX_FOOTER)},
    }


def build_header(items: list[VerifiedBrief], meta: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    badge = " · ".join(
        f"{VERDICT_EMOJI.get(v, '')} {VERDICT_LABEL.get(v, v)} {n}건" for v, n in counts.items()
    )
    stats = meta.get("stats", {})
    collect = stats.get("collect", {})
    curate = stats.get("curate", {})
    return (
        f"## 📰 {meta.get('title', '뉴스 브리핑')} — {meta.get('date', '')}\n"
        f"**{len(items)}건** · {badge}\n"
        f"-# 수집 {collect.get('unique', 0)}건 → 선별 {curate.get('selected', 0)}건 → 발행 {len(items)}건"
    )


def chunk_embeds(embeds: list[dict[str, Any]], max_per_message: int) -> list[list[dict[str, Any]]]:
    """개수 상한과 총 문자수 상한을 동시에 만족하도록 나눈다 (R5-1)."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for embed in embeds:
        size = _embed_size(embed)
        if current and (len(current) >= max_per_message or current_size + size > MAX_TOTAL_CHARS):
            chunks.append(current)
            current, current_size = [], 0
        current.append(embed)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


class DiscordPublisher(Publisher):
    name = "discord"

    def __init__(self, webhook_url: str, cfg: dict[str, Any]):
        self.webhook_url = webhook_url
        self.username = cfg.get("username", "뉴스레터 봇")
        self.max_per_message = int(cfg.get("max_embeds_per_message", MAX_EMBEDS))
        self.interval = float(cfg.get("send_interval", 1.0))

    @staticmethod
    def _retry_after(response) -> float:
        """429 면 Discord 가 알려준 대기 시간을, 5xx 면 기본 대기를 쓴다 (R5-2)."""
        try:
            return float(response.json().get("retry_after", 1.0)) + 0.25
        except (ValueError, AttributeError):
            return 2.0

    def _post(self, payload: dict[str, Any], *, label: str = "Discord") -> None:
        post_with_retry(self.webhook_url, payload, label=label, retry_after=self._retry_after)

    def publish(self, items: list[VerifiedBrief], meta: dict[str, Any]) -> dict[str, Any]:
        embeds = [build_embed(item) for item in items]
        chunks = chunk_embeds(embeds, min(self.max_per_message, MAX_EMBEDS))

        self._post({"username": self.username, "content": build_header(items, meta)},
                   label="Discord 헤더")
        sent = 0
        for index, chunk in enumerate(chunks):
            time.sleep(self.interval)
            self._post({"username": self.username, "embeds": chunk},
                       label=f"Discord 묶음 {index + 1}/{len(chunks)}")
            sent += len(chunk)
            log.info("Discord 전송 %d/%d 묶음 (%d건)", index + 1, len(chunks), len(chunk))

        return {"target": "discord", "messages": len(chunks) + 1, "embeds_sent": sent, "dry_run": False}
