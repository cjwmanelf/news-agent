"""자동 실행 스케줄러 (interval & daily 모드).

파이썬 표준 라이브러리(threading, datetime)만으로 동작하는 경량 스케줄러입니다.
GUI(app.py) 및 CLI(run.py --schedule)에서 공유하여 백그라운드 정기 실행을 담당합니다.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

log = logging.getLogger(__name__)


def parse_daily_time(val: str) -> tuple[int, int]:
    """'08:30' 형태의 문자열을 (hour, minute) 튜플로 파싱."""
    parts = str(val).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"시간 형식이 올바르지 않습니다: {val} (예: 08:30)")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"시(0~23) 또는 분(0~59) 범위 초과: {val}")
    return h, m


class NewsScheduler:
    """주기 실행(interval) 또는 매일 특정 시각(daily) 실행을 관리하는 스케줄러."""

    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self.callback = callback
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self.enabled: bool = False
        self.mode: str = "interval"  # "interval" | "daily"
        self.interval_hours: int = 6
        self.daily_time: str = "08:30"

        self.last_run: datetime | None = None
        self.next_run: datetime | None = None

    def update_config(self, cfg: dict[str, Any]) -> None:
        """설정을 반영하고 다음 실행 시각을 즉시 재계산한다."""
        with self._lock:
            self.enabled = bool(cfg.get("enabled", False))
            self.mode = str(cfg.get("mode", "interval")).lower()
            if self.mode not in ("interval", "daily"):
                self.mode = "interval"
            self.interval_hours = max(1, int(cfg.get("interval_hours", 6)))
            self.daily_time = str(cfg.get("daily_time", "08:30")).strip()
            self._recompute_next_run_locked(datetime.now())

    def _recompute_next_run_locked(self, now: datetime) -> None:
        if not self.enabled:
            self.next_run = None
            return

        if self.mode == "interval":
            base = self.last_run if (self.last_run and self.last_run <= now) else now
            nxt = base + timedelta(hours=self.interval_hours)
            if nxt <= now:
                nxt = now + timedelta(hours=self.interval_hours)
            self.next_run = nxt
        elif self.mode == "daily":
            try:
                h, m = parse_daily_time(self.daily_time)
            except ValueError:
                h, m = 8, 30
            target = now.replace(hour=h, minute=m, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            self.next_run = target

    def compute_next_run(self, now: datetime | None = None) -> datetime | None:
        """외부에서 현재 설정 기준 다음 실행 시각을 계산해볼 때 사용."""
        with self._lock:
            self._recompute_next_run_locked(now or datetime.now())
            return self.next_run

    def get_status(self) -> dict[str, Any]:
        """UI 표시에 필요한 상태 정보를 반환한다."""
        with self._lock:
            now = datetime.now()
            remaining_seconds = 0
            if self.enabled and self.next_run:
                remaining_seconds = max(0, int((self.next_run - now).total_seconds()))

            return {
                "enabled": self.enabled,
                "mode": self.mode,
                "interval_hours": self.interval_hours,
                "daily_time": self.daily_time,
                "last_run": self.last_run.isoformat(timespec="seconds") if self.last_run else None,
                "next_run": self.next_run.isoformat(timespec="seconds") if self.next_run else None,
                "next_run_display": self.next_run.strftime("%H:%M") if self.next_run else "",
                "remaining_seconds": remaining_seconds,
            }

    def start(self) -> None:
        """백그라운드 스케줄러 루프 시작."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run_loop, name="NewsScheduler", daemon=True)
            self._thread.start()
            log.info("뉴스레터 스케줄러 데몬 시작됨.")

    def stop(self) -> None:
        """스케줄러 루프 중지."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        log.info("뉴스레터 스케줄러 데몬 중지됨.")

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(5)
            if self._stop_event.is_set():
                break

            now = datetime.now()
            should_fire = False
            with self._lock:
                if self.enabled and self.next_run and now >= self.next_run:
                    should_fire = True
                    self.last_run = now
                    self._recompute_next_run_locked(now)

            if should_fire and self.callback:
                try:
                    log.info("예약된 스케줄 시각 도달 — 자동 실행 시작")
                    self.callback()
                except Exception as exc:  # noqa: BLE001
                    log.exception("스케줄러 자동 실행 콜백 실패: %s", exc)
