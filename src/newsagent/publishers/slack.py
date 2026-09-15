"""Slack 웹후크 발행 (PRD R5-5).

Discord 와 같은 구조지만 포맷이 다르다. Slack 은 embed 대신 Block Kit 을 쓰고,
링크 문법이 `<url|텍스트>` 이며 블록 수·길이 제한도 다르다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..models import VERDICT_EMOJI, VERDICT_LABEL, VerifiedBrief
from ..utils.text import truncate
from .base import Publisher, post_with_retry

log = logging.getLogger(__name__)

MAX_BLOCKS = 45  # Slack 상한은 50. 헤더·구분선 몫을 남겨둔다
MAX_SECTION_CHARS = 2900  # section 텍스트 상한 3000


def mrkdwn_escape(text: str) -> str:
    """Slack mrkdwn 특수문자 이스케이프. 순서를 지켜야 &amp; 가 이중 변환되지 않는다."""
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_blocks(item: VerifiedBrief) -> list[dict[str, Any]]:
    brief = item.brief
    article = brief.article
    emoji = VERDICT_EMOJI.get(item.verdict, "")
    label = VERDICT_LABEL.get(item.verdict, item.verdict)

    title = mrkdwn_escape(brief.headline)
    heading = f"*<{article.url}|{title}>*" if article and article.url else f"*{title}*"

    lines = [heading]
    lines += [f"• {mrkdwn_escape(b)}" for b in brief.summary]
    if brief.why_it_matters:
        lines.append(f"> *왜 중요한가* {mrkdwn_escape(brief.why_it_matters)}")

    context_bits = [f"{emoji} *{label}* `{item.confidence:.2f}`"]
    if item.corroborating_sources:
        context_bits.append(f"교차 출처 {len(item.corroborating_sources)}곳: "
                            f"{mrkdwn_escape(', '.join(item.corroborating_sources))}")
    else:
        context_bits.append("교차 출처 없음")
    if article:
        context_bits.append(mrkdwn_escape(article.source))

    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": truncate("\n".join(lines), MAX_SECTION_CHARS)}},
        {"type": "context", "elements": [{"type": "mrkdwn",
                                          "text": truncate(" · ".join(context_bits), MAX_SECTION_CHARS)}]},
        {"type": "divider"},
    ]


def build_header_blocks(items: list[VerifiedBrief], meta: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    badge = " · ".join(
        f"{VERDICT_EMOJI.get(v, '')} {VERDICT_LABEL.get(v, v)} {n}건" for v, n in counts.items()
    )
    stats = meta.get("stats", {})
    return [
        {"type": "header", "text": {"type": "plain_text",
                                    "text": truncate(f"📰 {meta.get('title', '뉴스 브리핑')} — {meta.get('date', '')}", 150),
                                    "emoji": True}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text":
            f"*{len(items)}건* · {badge}\n수집 {stats.get('collect', {}).get('unique', 0)}건 → "
            f"선별 {stats.get('curate', {}).get('selected', 0)}건 → 발행 {len(items)}건"}]},
        {"type": "divider"},
    ]


class SlackPublisher(Publisher):
    name = "slack"

    def __init__(self, webhook_url: str, cfg: dict[str, Any]):
        if not webhook_url:
            raise ValueError("SLACK_WEBHOOK_URL 이 없습니다.")
        self.webhook_url = webhook_url
        self.interval = float(cfg.get("send_interval", 1.0))

    @staticmethod
    def _retry_after(response) -> float:
        return float(response.headers.get("Retry-After", 2)) + 0.25

    def _post(self, payload: dict[str, Any], *, label: str = "Slack") -> None:
        post_with_retry(self.webhook_url, payload, label=label, retry_after=self._retry_after)

    def publish(self, items: list[VerifiedBrief], meta: dict[str, Any]) -> dict[str, Any]:
        blocks = build_header_blocks(items, meta)
        chunks: list[list[dict[str, Any]]] = []
        current = blocks
        for item in items:
            item_blocks = build_blocks(item)
            if len(current) + len(item_blocks) > MAX_BLOCKS:
                chunks.append(current)
                current = []
            current.extend(item_blocks)
        if current:
            chunks.append(current)

        for index, chunk in enumerate(chunks):
            if index:
                time.sleep(self.interval)
            self._post({"blocks": chunk}, label=f"Slack 묶음 {index + 1}/{len(chunks)}")
            log.info("Slack 전송 %d/%d 묶음", index + 1, len(chunks))

        return {"target": "slack", "messages": len(chunks), "sent": len(items), "dry_run": False}
