"""발행 채널 레지스트리.

새 채널을 붙이려면 Publisher 를 구현하고 여기에 생성 규칙만 추가하면 된다.
"""

from __future__ import annotations

import os
from typing import Any

from .base import Publisher
from .discord import DiscordPublisher
from .slack import SlackPublisher
from .telegram import TelegramPublisher

CHANNELS = ("discord", "slack", "telegram")

# 채널별로 필요한 비밀값 — 설정 검증과 GUI 안내에 함께 쓴다
REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "discord": ("DISCORD_WEBHOOK_URL",),
    "slack": ("SLACK_WEBHOOK_URL",),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
}

CHANNEL_LABEL = {"discord": "디스코드", "slack": "슬랙", "telegram": "텔레그램"}


def missing_env(channel: str) -> list[str]:
    return [name for name in REQUIRED_ENV.get(channel, ()) if not os.environ.get(name, "").strip()]


def build_publisher(channel: str, publish_cfg: dict[str, Any]) -> Publisher:
    if channel == "discord":
        return DiscordPublisher(os.environ.get("DISCORD_WEBHOOK_URL", ""), publish_cfg)
    if channel == "slack":
        return SlackPublisher(os.environ.get("SLACK_WEBHOOK_URL", ""), publish_cfg)
    if channel == "telegram":
        return TelegramPublisher(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
            publish_cfg,
        )
    raise ValueError(f"알 수 없는 발행 채널: {channel}")


def configured_targets(publish_cfg: dict[str, Any]) -> list[str]:
    """설정에서 발행 채널 목록을 읽는다. 구버전 `target` 단일 값도 받아준다."""
    targets = publish_cfg.get("targets")
    if not targets:
        single = publish_cfg.get("target")
        targets = [single] if single else []
    if isinstance(targets, str):
        targets = [targets]
    return [t for t in targets if t]


__all__ = [
    "Publisher", "DiscordPublisher", "SlackPublisher", "TelegramPublisher",
    "CHANNELS", "CHANNEL_LABEL", "REQUIRED_ENV",
    "build_publisher", "configured_targets", "missing_env",
]
