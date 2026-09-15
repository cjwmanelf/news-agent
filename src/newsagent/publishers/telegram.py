"""Telegram 봇 발행 (PRD R5-5).

봇 토큰과 채팅 ID 로 sendMessage 를 호출한다. 메시지당 4096자 제한이 있어
기사 단위로 잘라 이어 보낸다.
"""

from __future__ import annotations

import html
import logging
import time
from typing import Any

from ..models import VERDICT_EMOJI, VERDICT_LABEL, VerifiedBrief
from ..utils.text import truncate
from .base import Publisher, post_with_retry

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_CHARS = 3800  # 상한 4096. 여유를 둔다


def esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def render_item(item: VerifiedBrief) -> str:
    brief = item.brief
    article = brief.article
    emoji = VERDICT_EMOJI.get(item.verdict, "")
    label = VERDICT_LABEL.get(item.verdict, item.verdict)

    title = esc(brief.headline)
    heading = f'<b><a href="{esc(article.url)}">{title}</a></b>' if article and article.url else f"<b>{title}</b>"

    lines = [heading, f"{emoji} {label} · 신뢰도 {item.confidence:.2f}"]
    if item.corroborating_sources:
        lines.append(f"교차 출처 {len(item.corroborating_sources)}곳: {esc(', '.join(item.corroborating_sources))}")
    lines.append("")
    lines += [f"• {esc(b)}" for b in brief.summary]
    if brief.why_it_matters:
        lines.append(f"<i>왜 중요한가 — {esc(brief.why_it_matters)}</i>")
    if article:
        lines.append(f"<i>{esc(article.source)}</i>")
    return "\n".join(lines)


def render_header(items: list[VerifiedBrief], meta: dict[str, Any]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.verdict] = counts.get(item.verdict, 0) + 1
    badge = " · ".join(
        f"{VERDICT_EMOJI.get(v, '')} {VERDICT_LABEL.get(v, v)} {n}건" for v, n in counts.items()
    )
    stats = meta.get("stats", {})
    return (
        f"<b>📰 {esc(meta.get('title', '뉴스 브리핑'))} — {esc(meta.get('date', ''))}</b>\n"
        f"{len(items)}건 · {badge}\n"
        f"<i>수집 {stats.get('collect', {}).get('unique', 0)}건 → "
        f"선별 {stats.get('curate', {}).get('selected', 0)}건 → 발행 {len(items)}건</i>"
    )


class TelegramPublisher(Publisher):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, cfg: dict[str, Any]):
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다.")
        self.url = API.format(token=token)
        self.chat_id = chat_id
        self.interval = float(cfg.get("send_interval", 1.0))

    @staticmethod
    def _retry_after(response) -> float:
        try:
            return float(response.json().get("parameters", {}).get("retry_after", 2)) + 0.25
        except (ValueError, AttributeError):
            return 2.0

    def _post(self, text: str, *, label: str = "Telegram") -> None:
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        post_with_retry(self.url, payload, label=label, retry_after=self._retry_after)

    def publish(self, items: list[VerifiedBrief], meta: dict[str, Any]) -> dict[str, Any]:
        blocks = [render_header(items, meta)] + [render_item(i) for i in items]

        messages: list[str] = []
        current = ""
        for block in blocks:
            block = truncate(block, MAX_CHARS)
            if current and len(current) + len(block) + 2 > MAX_CHARS:
                messages.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            messages.append(current)

        for index, message in enumerate(messages):
            if index:
                time.sleep(self.interval)
            self._post(message, label=f"Telegram {index + 1}/{len(messages)}")
            log.info("Telegram 전송 %d/%d", index + 1, len(messages))

        return {"target": "telegram", "messages": len(messages), "sent": len(items), "dry_run": False}
