"""HTTP 세션과 robots.txt 준수 (PRD NFR-6, R1-6)."""

from __future__ import annotations

import threading
import urllib.robotparser
from urllib.parse import urlsplit, urlunsplit

import requests

_local = threading.local()
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_robots_lock = threading.Lock()

DEFAULT_UA = "newsagent/1.0 (+personal newsletter bot)"


def get_session(user_agent: str = DEFAULT_UA) -> requests.Session:
    """스레드별 세션 — 커넥션 재사용을 살리면서 스레드 안전을 지킨다."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        _local.session = session
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        }
    )
    return session


def fetch(
    url: str,
    *,
    timeout: float = 15,
    user_agent: str = DEFAULT_UA,
    headers: dict[str, str] | None = None,
    params: dict | None = None,
) -> requests.Response:
    session = get_session(user_agent)
    response = session.get(url, timeout=timeout, headers=headers, params=params, allow_redirects=True)
    response.raise_for_status()
    return response


def robots_allows(url: str, user_agent: str = DEFAULT_UA, timeout: float = 8) -> bool:
    """robots.txt 를 확인한다. 가져오지 못하면 허용으로 본다(과잉 차단 방지)."""
    parts = urlsplit(url)
    if not parts.netloc:
        return False
    root = urlunsplit((parts.scheme or "https", parts.netloc, "/robots.txt", "", ""))

    with _robots_lock:
        cached = _robots_cache.get(root, "__miss__")
    if cached == "__miss__":
        parser: urllib.robotparser.RobotFileParser | None
        try:
            response = requests.get(root, timeout=timeout, headers={"User-Agent": user_agent})
            if response.status_code >= 400:
                parser = None
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.parse(response.text.splitlines())
        except requests.RequestException:
            parser = None
        with _robots_lock:
            _robots_cache[root] = parser
        cached = parser

    if cached is None:
        return True
    return cached.can_fetch(user_agent, url)
