"""발행 인터페이스 (PRD R5-5).

Discord 외의 채널(슬랙·이메일)을 붙일 때 이 인터페이스만 구현하면 된다.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

import requests

from ..models import VerifiedBrief

log = logging.getLogger(__name__)

# 다시 시도해볼 만한 응답.
# 429 는 레이트리밋이고, 5xx 는 상대 서버가 잠깐 맛이 간 것이다.
# 실측: 규격에 맞는 페이로드인데도 Discord 가 500 을 돌려준 적이 있다.
# 이런 걸 한 번에 포기하면 그날 뉴스레터가 통째로 날아간다.
RETRYABLE = {429, 500, 502, 503, 504}


def post_with_retry(
    url: str,
    payload: dict[str, Any],
    *,
    label: str,
    retry_after: Callable[[requests.Response], float] | None = None,
    max_attempts: int = 4,
    timeout: float = 20,
) -> requests.Response:
    """POST 하고 일시적 실패는 되도록 넘긴다.

    label 은 오류 메시지에 들어간다 — 어느 메시지가 실패했는지 알 수 있어야
    묶음 전체를 다시 보내야 하는지 판단할 수 있다.
    """
    backoff = 1.0
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            log.warning("%s 전송 중 통신 오류 (%d/%d): %s", label, attempt, max_attempts, last_error)
        else:
            if response.status_code < 400:
                return response
            if response.status_code not in RETRYABLE:
                # 4xx 는 우리 잘못이다. 다시 보내도 같은 결과라 즉시 멈춘다.
                raise RuntimeError(
                    f"{label} 전송 실패 {response.status_code}: {response.text[:300]}"
                )
            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            wait = retry_after(response) if retry_after else backoff
            log.warning(
                "%s 전송 실패 %s — %.1fs 뒤 재시도 (%d/%d)",
                label, response.status_code, wait, attempt, max_attempts,
            )
            if attempt < max_attempts:
                time.sleep(wait)
                backoff = min(backoff * 2, 15)
                continue

        if attempt < max_attempts:
            time.sleep(backoff)
            backoff = min(backoff * 2, 15)

    raise RuntimeError(f"{label} 전송 실패 — {max_attempts}회 시도 후 포기 ({last_error})")


class Publisher(ABC):
    name = "base"

    @abstractmethod
    def publish(self, items: list[VerifiedBrief], meta: dict[str, Any]) -> dict[str, Any]:
        """발행 결과 요약 딕셔너리를 돌려준다."""
