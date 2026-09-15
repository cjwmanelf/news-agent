"""발행 이력 저장소 — 같은 소식이 며칠 연속 나가는 것을 막는다.

기사 ID 만으로는 부족하다. 같은 사건을 다룬 '다른 기사'가 다음 날 올라오면
ID 가 달라 그대로 통과하기 때문이다. 그래서 사건 텍스트도 함께 보관해
4단계에서 사건 유사도로 비교한다.

기록 시점은 '실제로 발행에 성공했을 때'뿐이다. dry_run 실행이 이력을 남기면
정작 진짜 발행할 때 전부 '이미 발행함'으로 걸러져 빈 뉴스레터가 나간다.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS published (
    article_id   TEXT PRIMARY KEY,
    url          TEXT,
    headline     TEXT,
    event_text   TEXT,
    domain       TEXT,
    verdict      TEXT,
    confidence   REAL,
    run_id       TEXT,
    published_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_published_at ON published(published_at);
"""


class PublishedStore:
    """SQLite 기반 발행 이력. 비활성화되면 모든 메서드가 무해한 기본값을 돌려준다."""

    def __init__(self, path: str | Path | None, *, enabled: bool = True):
        self.enabled = bool(enabled and path)
        self._conn: sqlite3.Connection | None = None
        if not self.enabled:
            return
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # 노드가 스레드 풀에서 돌 수 있어 same-thread 검사를 끈다.
        # 쓰기는 발행 노드 한 곳에서만 일어나므로 경합은 없다.
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def published_ids(self, lookback_days: int) -> set[str]:
        """최근 N일 안에 발행한 기사 ID 집합 (2단계에서 즉시 제외용)."""
        if self._conn is None:
            return set()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = self._conn.execute(
            "SELECT article_id FROM published WHERE published_at >= ?", (cutoff,)
        ).fetchall()
        return {row[0] for row in rows}

    def recent_events(self, lookback_days: int) -> list[tuple[str, str]]:
        """최근 N일 발행분의 (사건 텍스트, 헤드라인) — 4단계 사건 중복 비교용."""
        if self._conn is None:
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = self._conn.execute(
            "SELECT event_text, headline FROM published "
            "WHERE published_at >= ? ORDER BY published_at DESC",
            (cutoff,),
        ).fetchall()
        return [(row[0] or "", row[1] or "") for row in rows]

    def record(self, items: list[dict[str, Any]], run_id: str) -> int:
        """발행 성공분을 기록한다. 이미 있는 기사는 건너뛴다."""
        if self._conn is None or not items:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            (
                item["article_id"], item.get("url", ""), item.get("headline", ""),
                item.get("event_text", ""), item.get("domain", ""),
                item.get("verdict", ""), float(item.get("confidence", 0.0)), run_id, now,
            )
            for item in items
        ]
        cursor = self._conn.executemany(
            "INSERT OR IGNORE INTO published "
            "(article_id, url, headline, event_text, domain, verdict, confidence, run_id, published_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()
        return cursor.rowcount

    def prune(self, keep_days: int = 90) -> int:
        """오래된 이력을 지운다. 저장소가 무한정 커지는 것을 막는다."""
        if self._conn is None:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
        cursor = self._conn.execute("DELETE FROM published WHERE published_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def __enter__(self) -> PublishedStore:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
