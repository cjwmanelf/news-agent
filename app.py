#!/usr/bin/env python
"""종우's 뉴스레터 에이전트 로컬 GUI 서버.

  python app.py            # http://127.0.0.1:8765 에서 실행

브라우저 UI 지만 로컬에서만 돈다. 파이프라인이 로컬 파이썬 코드를 실행하고
.env 의 웹후크로 발행하기 때문에 원격에 올릴 수 있는 종류의 앱이 아니다.
"""

from __future__ import annotations

import collections
import io
import json
import logging
import os
import queue
import sys
import threading
import webbrowser
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory
from ruamel.yaml import YAML

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / "src"))

from newsagent.config import ConfigError, load_config, validate_config  # noqa: E402
from newsagent.graph import (  # noqa: E402
    build_checkpointer,
    build_pipeline,
    close_checkpointer,
    initial_state,
)
from newsagent.models import VERDICT_DESC, VERDICT_LABEL  # noqa: E402
from newsagent.publishers import CHANNEL_LABEL, CHANNELS, configured_targets, missing_env  # noqa: E402
from newsagent.runner import STAGES, run_stages  # noqa: E402
from newsagent.scheduler import NewsScheduler  # noqa: E402
from newsagent.secrets import MANAGED, describe, update_env, validate_name  # noqa: E402

CONFIG_DIR = ROOT / "config"
OUTPUT_DIR = ROOT / "output"

yaml_rt = YAML()
yaml_rt.preserve_quotes = True
yaml_rt.width = 4096  # 긴 URL 이 줄바꿈되지 않게

app = Flask(__name__, static_folder=None)
log = logging.getLogger("newsagent.gui")


# ────────────────────────────────────────────────────────────── 로그 중계

class LogHub:
    """파이프라인 로그를 브라우저로 실시간 중계한다 (SSE)."""

    def __init__(self, keep: int = 600):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.buffer: collections.deque = collections.deque(maxlen=keep)

    def publish(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.buffer.append(item)
            targets = list(self._subscribers)
        for sub in targets:
            sub.put(item)

    def subscribe(self) -> queue.Queue:
        sub: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def clear(self) -> None:
        with self._lock:
            self.buffer.clear()


hub = LogHub()


class HubHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            hub.publish(
                {
                    "kind": "log",
                    "level": record.levelname,
                    "logger": record.name.replace("newsagent.", ""),
                    "message": record.getMessage(),
                    "time": datetime.now().strftime("%H:%M:%S"),
                }
            )
        except Exception:
            pass


def setup_logging() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "httpx", "httpcore", "openai", "anthropic", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("newsagent").addHandler(HubHandler())


# ──────────────────────────────────────────────────────────── 실행 세션

class Session:
    """한 번에 하나의 실행만 허용하는 단순 상태 기계.

    idle → running → (awaiting_approval) → done / error
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "idle"
        self.stage = None
        self.run_id = None
        self.state: dict[str, Any] | None = None
        self.pipeline = None
        self.graph_config: dict[str, Any] | None = None
        self.error: str | None = None
        self.started_at: str | None = None
        self.mode: dict[str, Any] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "run_id": self.run_id,
            "error": self.error,
            "started_at": self.started_at,
            "mode": self.mode,
        }

    def reset(self) -> None:
        if self.pipeline is not None:
            try:
                self.pipeline.close()
            except Exception:
                pass
        self.pipeline = None
        self.graph_config = None


session = Session()


def announce(kind: str, **payload: Any) -> None:
    hub.publish({"kind": kind, "time": datetime.now().strftime("%H:%M:%S"), **payload})


# ─────────────────────────────────────────────────────────── 직렬화

def serialize_article(article) -> dict[str, Any] | None:
    if article is None:
        return None
    return {
        "title": article.title,
        "url": article.url,
        "source": article.source,
        "domain": article.domain,
        "published_at": article.published_at,
        "score": article.score,
        "keyword_score": article.keyword_score,
        "vector_score": article.vector_score,
        "matched_interest": article.matched_interest,
        "matched_keywords": article.matched_keywords,
    }


def serialize_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for verified in state.get("verified", []):
        brief = verified.brief
        items.append(
            {
                "headline": brief.headline,
                "verdict": verified.verdict,
                "verdict_label": VERDICT_LABEL.get(verified.verdict, verified.verdict),
                "verdict_desc": VERDICT_DESC.get(verified.verdict, ""),
                "confidence": verified.confidence,
                "sources": verified.corroborating_sources,
                "cluster_size": verified.cluster_size,
                "summary": brief.summary,
                "why_it_matters": brief.why_it_matters,
                "category": brief.category,
                "degraded": brief.degraded,
                "notes": verified.notes,
                "fact_checks": [asdict(f) for f in verified.fact_checks],
                "article": serialize_article(brief.article),
            }
        )
    return items


def serialize_state(state: dict[str, Any] | None) -> dict[str, Any]:
    if not state:
        return {"stats": {}, "errors": [], "results": [], "selected": []}
    return {
        "run_id": state.get("run_id"),
        "stats": state.get("stats", {}),
        "errors": state.get("errors", []),
        "results": serialize_results(state),
        "selected": [serialize_article(a) for a in state.get("selected", [])],
        "publish_result": state.get("publish_result", {}),
    }


# ─────────────────────────────────────────────────────────── 설정 입출력

def env_presence() -> dict[str, bool]:
    def has(name: str) -> bool:
        return bool(os.environ.get(name, "").strip())

    return {
        "anthropic": has("ANTHROPIC_API_KEY"),
        "openai": has("OPENAI_API_KEY"),
        "gemini": has("GOOGLE_API_KEY"),
        "local": True,  # 로컬 서버는 키가 없어도 동작한다
        "discord": has("DISCORD_WEBHOOK_URL"),
        "slack": has("SLACK_WEBHOOK_URL"),
        "telegram": has("TELEGRAM_BOT_TOKEN") and has("TELEGRAM_CHAT_ID"),
        "naver_hub": has("NAVER_API_KEY_ID") and has("NAVER_API_KEY"),
        "naver_legacy": has("NAVER_CLIENT_ID") and has("NAVER_CLIENT_SECRET"),
    }


def read_yaml(name: str):
    with (CONFIG_DIR / name).open(encoding="utf-8") as fh:
        return yaml_rt.load(fh)


def write_yaml(name: str, data) -> None:
    """주석을 보존한 채 저장한다 (ruamel 라운드트립)."""
    buffer = io.StringIO()
    yaml_rt.dump(data, buffer)
    (CONFIG_DIR / name).write_text(buffer.getvalue(), encoding="utf-8")


def current_config() -> dict[str, Any]:
    cfg = load_config(str(CONFIG_DIR))
    try:
        warnings = validate_config(cfg)
        error = None
    except ConfigError as exc:
        warnings, error = [], str(exc)

    return {
        "llm": {
            "provider": cfg["llm"]["provider"],
            "model": dict(cfg["llm"]["model"]),
            "base_url": dict(cfg["llm"].get("base_url") or {}),
        },
        "publish": {
            "dry_run": cfg["publish"]["dry_run"],
            "title": cfg["publish"]["title"],
            "username": cfg["publish"]["username"],
            "include_verdicts": list(cfg["publish"]["include_verdicts"]),
            "targets": configured_targets(cfg["publish"]),
        },
        "channels": [
            {
                "id": channel,
                "label": CHANNEL_LABEL.get(channel, channel),
                "ready": not missing_env(channel),
                "missing": missing_env(channel),
            }
            for channel in CHANNELS
        ],
        "curate": {
            "mode": cfg["curate"]["mode"],
            "threshold": cfg["curate"]["threshold"],
            "max_articles": cfg["curate"]["max_articles"],
            "weights": dict(cfg["curate"]["weights"]),
            "interests": [
                {
                    "name": i.get("name", ""),
                    "weight": i.get("weight", 1.0),
                    "keywords": list(i.get("keywords") or []),
                    "exclude": list(i.get("exclude") or []),
                }
                for i in cfg["curate"]["interests"]
            ],
        },
        "verify": {
            "cluster_threshold": cfg["verify"]["cluster_threshold"],
            "peer_max_age_hours": cfg["verify"]["peer_max_age_hours"],
            "fetch_peer_content": cfg["verify"]["fetch_peer_content"],
            "search_provider": cfg["verify"]["corroboration_search"].get("provider", "auto"),
            "search_enabled": cfg["verify"]["corroboration_search"].get("enabled", True),
            "min_confidence_to_publish": cfg["verify"]["min_confidence_to_publish"],
        },
        "collect": {"lookback_hours": cfg["collect"]["lookback_hours"]},
        "history": {
            "enabled": cfg["history"]["enabled"],
            "lookback_days": cfg["history"]["lookback_days"],
        },
        "schedule": {
            "enabled": cfg.get("schedule", {}).get("enabled", False),
            "mode": cfg.get("schedule", {}).get("mode", "interval"),
            "interval_hours": cfg.get("schedule", {}).get("interval_hours", 6),
            "daily_time": cfg.get("schedule", {}).get("daily_time", "08:30"),
        },
        # 소스는 load_config 가 아니라 원본 yaml 에서 읽는다.
        # load_config 는 ${NEWSAPI_KEY} 같은 자리표시자를 실제 값으로 치환하므로
        # 그대로 내보내면 헤더에 든 API 키가 브라우저로 새어나간다.
        "sources": [
            {
                "name": s.get("name", ""),
                "type": s.get("type", ""),
                "enabled": s.get("enabled", True),
                "role": s.get("role", "content"),
                "url": s.get("url", ""),
                "max_items": s.get("max_items"),
                "lookback_hours": s.get("lookback_hours"),
                "items_path": s.get("items_path", ""),
                "field_map": dict(s.get("field_map") or {}),
                "params": dict(s.get("params") or {}),
                "headers": dict(s.get("headers") or {}),
                "list_selector": s.get("list_selector", ""),
                "title_selector": s.get("title_selector", ""),
                "snippet_selector": s.get("snippet_selector", ""),
                "link_attr": s.get("link_attr", "href"),
                "base_url": s.get("base_url", ""),
            }
            for s in (read_yaml("sources.yaml").get("sources") or [])
        ],
        "env": env_presence(),
        "warnings": warnings,
        "error": error,
    }


class PatchError(Exception):
    """설정 저장을 거부해야 하는 경우. 사용자에게 그대로 보여준다."""


def build_interests(incoming: list[dict[str, Any]]):
    """GUI 가 보낸 관심사 목록을 yaml 에 넣을 형태로 만든다.

    목록 전체를 교체하므로 추가·삭제·이름 변경이 모두 가능하다.
    다만 빈 목록으로 덮어쓰는 것은 막는다 — 화면이 덜 그려진 상태에서 저장하면
    관심사가 통째로 날아가고, 그러면 파이프라인이 아예 돌지 않는다.
    """
    from ruamel.yaml.comments import CommentedMap

    built = []
    seen: set[str] = set()
    for item in incoming or []:
        name = str(item.get("name", "")).strip()
        if not name:
            continue  # 이름 없는 항목은 조용히 버린다
        if name in seen:
            raise PatchError(f"관심사 이름이 중복됐습니다: {name}")
        seen.add(name)

        entry = CommentedMap()
        entry["name"] = name
        entry["weight"] = float(item.get("weight", 1.0))
        entry["keywords"] = [str(k).strip() for k in (item.get("keywords") or []) if str(k).strip()]
        entry["exclude"] = [str(k).strip() for k in (item.get("exclude") or []) if str(k).strip()]
        built.append(entry)

    if not built:
        raise PatchError("관심사를 최소 하나는 남겨야 합니다.")
    return built


# 소스 타입별로 의미 있는 키. 타입을 바꾸면 남의 키는 지운다.
TYPE_KEYS = {
    "rss": (),
    "api": ("items_path", "field_map", "params", "headers"),
    "crawl": ("list_selector", "title_selector", "snippet_selector", "link_attr", "base_url"),
}
ALL_TYPE_KEYS = {k for keys in TYPE_KEYS.values() for k in keys}


def build_sources(incoming: list[dict[str, Any]], existing: list):
    """GUI 가 보낸 소스 목록을 yaml 에 넣을 형태로 만든다.

    이름이 같은 기존 소스는 원본 매핑을 재사용한다. GUI 가 보내지 않는 키
    (주석·세부 옵션)를 보존하기 위해서다.
    """
    from ruamel.yaml.comments import CommentedMap

    by_name = {s.get("name"): s for s in (existing or [])}
    built = []
    seen: set[str] = set()

    for item in incoming or []:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        if name in seen:
            raise PatchError(f"소스 이름이 중복됐습니다: {name}")
        seen.add(name)

        source_type = str(item.get("type", "rss")).strip()
        if source_type not in TYPE_KEYS:
            raise PatchError(f"'{name}' 의 타입이 잘못됐습니다: {source_type}")
        url = str(item.get("url", "")).strip()
        if not url.startswith("http"):
            raise PatchError(f"'{name}' 의 주소가 비었거나 http 로 시작하지 않습니다.")
        if source_type == "crawl" and not str(item.get("list_selector", "")).strip():
            raise PatchError(f"크롤링 소스 '{name}' 에는 목록 셀렉터가 필요합니다.")

        entry = by_name.get(name)
        if entry is None:
            entry = CommentedMap()
        entry["name"] = name
        entry["type"] = source_type
        entry["enabled"] = bool(item.get("enabled", True))
        entry["url"] = url
        role = item.get("role", "content")
        if role not in ("content", "corroboration"):
            raise PatchError(f"'{name}' 의 역할 값이 잘못됐습니다: {role}")
        entry["role"] = role
        try:
            entry["max_items"] = int(item.get("max_items") or 30)
        except (TypeError, ValueError):
            raise PatchError(f"'{name}' 의 최대 건수가 숫자가 아닙니다.") from None

        lookback = item.get("lookback_hours")
        if lookback is not None and str(lookback).strip():
            try:
                lh = int(lookback)
                if lh <= 0:
                    raise ValueError
                entry["lookback_hours"] = lh
            except (TypeError, ValueError):
                raise PatchError(f"'{name}' 의 수집 기간(lookback_hours)은 1 이상의 정수여야 합니다.") from None
        else:
            entry.pop("lookback_hours", None)

        # 타입이 바뀌었을 수 있으니 다른 타입 전용 키는 제거한다
        for key in ALL_TYPE_KEYS - set(TYPE_KEYS[source_type]):
            entry.pop(key, None)

        if source_type == "api":
            entry["items_path"] = str(item.get("items_path", "")).strip()
            for key in ("field_map", "params", "headers"):
                value = {
                    str(k).strip(): v
                    for k, v in (item.get(key) or {}).items()
                    if str(k).strip()
                }
                if value:
                    entry[key] = value
                else:
                    entry.pop(key, None)
        elif source_type == "crawl":
            entry["list_selector"] = str(item.get("list_selector", "")).strip()
            entry["link_attr"] = str(item.get("link_attr") or "href").strip()
            for key in ("title_selector", "snippet_selector", "base_url"):
                value = str(item.get(key, "")).strip()
                if value:
                    entry[key] = value
                else:
                    entry.pop(key, None)

        built.append(entry)

    if not built:
        raise PatchError("수집 소스를 최소 하나는 남겨야 합니다.")
    if not any(e["enabled"] and e["role"] == "content" for e in built):
        raise PatchError(
            "본문 소스(역할이 '본문')가 최소 하나는 켜져 있어야 합니다. "
            "증인 전용 소스만으로는 뉴스레터를 만들 수 없습니다."
        )
    return built


def apply_patch(patch: dict[str, Any]) -> list[str]:
    """GUI 가 보낸 변경분을 해당 yaml 파일에 반영한다. 반환: 바뀐 파일 목록."""
    touched: list[str] = []

    curate = patch.get("curate")
    if curate:
        data = read_yaml("interests.yaml")
        for key in ("mode", "threshold", "max_articles"):
            if key in curate:
                data[key] = curate[key]
        if "weights" in curate:
            for key, value in curate["weights"].items():
                data["weights"][key] = value
        if "interests" in curate:
            data["interests"] = build_interests(curate["interests"])
        write_yaml("interests.yaml", data)
        touched.append("interests.yaml")

    sources = patch.get("sources")
    if sources is not None:
        data = read_yaml("sources.yaml")
        data["sources"] = build_sources(sources, data.get("sources") or [])
        write_yaml("sources.yaml", data)
        touched.append("sources.yaml")

    main_keys = {"llm", "publish", "verify", "collect", "history", "schedule"}
    if main_keys & patch.keys():
        data = read_yaml("config.yaml")
        if "llm" in patch:
            if "provider" in patch["llm"]:
                data["llm"]["provider"] = patch["llm"]["provider"]
            for key, value in (patch["llm"].get("model") or {}).items():
                data["llm"]["model"][key] = value
            for key, value in (patch["llm"].get("base_url") or {}).items():
                data["llm"].setdefault("base_url", {})[key] = value
        if "publish" in patch:
            for key in ("dry_run", "title", "username"):
                if key in patch["publish"]:
                    data["publish"][key] = patch["publish"][key]
            if "targets" in patch["publish"]:
                from ruamel.yaml.comments import CommentedSeq

                seq = CommentedSeq(patch["publish"]["targets"])
                seq.fa.set_flow_style()
                data["publish"]["targets"] = seq
        if "collect" in patch and "lookback_hours" in patch["collect"]:
            data["collect"]["lookback_hours"] = patch["collect"]["lookback_hours"]
        if "verify" in patch:
            verify = patch["verify"]
            for key in ("cluster_threshold", "peer_max_age_hours", "fetch_peer_content",
                        "min_confidence_to_publish"):
                if key in verify:
                    data["verify"][key] = verify[key]
            if "search_provider" in verify:
                data["verify"]["corroboration_search"]["provider"] = verify["search_provider"]
            if "search_enabled" in verify:
                data["verify"]["corroboration_search"]["enabled"] = verify["search_enabled"]
        if "history" in patch:
            for key in ("enabled", "lookback_days"):
                if key in patch["history"]:
                    data["history"][key] = patch["history"][key]
        if "schedule" in patch:
            sch = patch["schedule"]
            if "schedule" not in data or not isinstance(data["schedule"], dict):
                data["schedule"] = {}
            for key in ("enabled", "mode", "interval_hours", "daily_time"):
                if key in sch:
                    data["schedule"][key] = sch[key]
            scheduler.update_config(dict(data["schedule"]))
        write_yaml("config.yaml", data)
        touched.append("config.yaml")

    return touched


# ─────────────────────────────────────────────────────────── 파이프라인 실행

def build_run_config(options: dict[str, Any]) -> dict[str, Any]:
    cfg = load_config(str(CONFIG_DIR))
    if options.get("provider"):
        cfg["llm"]["provider"] = options["provider"]
    if "dry_run" in options:
        cfg["publish"]["dry_run"] = bool(options["dry_run"])
    if options.get("max_articles"):
        cfg["curate"]["max_articles"] = int(options["max_articles"])
    if options.get("no_history"):
        cfg["history"]["enabled"] = False
    validate_config(cfg)
    return cfg


def _worker(options: dict[str, Any]) -> None:
    stage = options.get("stage") or "publish"
    approve = bool(options.get("approve")) and stage == "publish"

    try:
        cfg = build_run_config(options)
    except ConfigError as exc:
        with session.lock:
            session.status = "error"
            session.error = str(exc)
        announce("error", message=str(exc))
        announce("finished")
        return

    start = initial_state()
    with session.lock:
        session.run_id = start["run_id"]
        session.started_at = start["started_at"]

    announce("started", run_id=start["run_id"], stage=stage, approve=approve,
             dry_run=cfg["publish"]["dry_run"], provider=cfg["llm"]["provider"])

    try:
        if stage != "publish":
            # 단계별 실행: 노드를 직접 순차 호출한다
            state = run_stages(
                cfg, stage,
                state=dict(start),
                on_stage=lambda name, phase: announce("stage", stage=name, phase=phase),
            )
            with session.lock:
                session.state = state
                session.status = "done"
                session.stage = stage
        else:
            checkpointer = build_checkpointer(
                cfg.get("runtime", {}).get("checkpoint_path", "state/checkpoints.db")
            ) if approve else None
            pipeline = None
            handed_off = False  # 승인 대기로 세션에 넘겼는가
            try:
                pipeline = build_pipeline(
                    cfg,
                    checkpointer=checkpointer,
                    approve_before_publish=approve,
                    on_stage=lambda name, phase: announce("stage", stage=name, phase=phase),
                )
                graph_config = {"configurable": {"thread_id": start["run_id"]}} if checkpointer else {}
                state = pipeline.graph.invoke(dict(start), graph_config)
                state.setdefault("stats", {})["llm"] = pipeline.llm.usage

                if approve and not state.get("publish_result"):
                    # publish 직전에서 멈췄다. 사용자가 확인 버튼을 누를 때까지 대기.
                    # 파이프라인 소유권이 세션으로 넘어가므로 여기서 닫으면 안 된다
                    # (api_approve / api_discard 가 닫는다).
                    with session.lock:
                        session.state = state
                        session.pipeline = pipeline
                        session.graph_config = graph_config
                        session.status = "awaiting_approval"
                    handed_off = True
                    announce("awaiting_approval", count=len(state.get("verified", [])))
                    announce("finished")
                    return

                with session.lock:
                    session.state = state
                    session.status = "done"
                    session.stage = "publish"
            finally:
                # 실행이 예외로 끝나도 반드시 닫는다. GUI 는 프로세스가 계속 떠
                # 있어서, 여기서 놓치면 실패할 때마다 SQLite 커넥션이 쌓인다.
                if not handed_off:
                    if pipeline is not None:
                        pipeline.close()
                    else:
                        # build_pipeline 이 실패해 Pipeline 이 안 만들어진 경우
                        close_checkpointer(checkpointer)
    except Exception as exc:  # noqa: BLE001
        log.exception("실행 실패")
        with session.lock:
            session.status = "error"
            session.error = f"{type(exc).__name__}: {exc}"
        announce("error", message=f"{type(exc).__name__}: {exc}")

    announce("finished")


def trigger_scheduled_run() -> None:
    """스케줄러가 예정 시각에 도달했을 때 백그라운드 파이프라인을 구동한다."""
    with session.lock:
        if session.status in ("running", "awaiting_approval"):
            log.warning("[스케줄러] 이미 파이프라인이 실행 중이어서 이번 자동 실행을 건너뜁니다.")
            announce("log", level="WARNING", message="⏰ [스케줄러] 이미 다른 작업이 실행 중이어서 이번 자동 실행을 건너뜁니다.")
            return
        session.reset()
        session.status = "running"
        session.stage = "publish"
        session.state = None
        session.error = None
        session.mode = {"stage": "publish", "approve": False, "dry_run": False, "scheduled": True}

    hub.clear()
    announce("log", level="INFO", message="⏰ [스케줄러] 예약된 자동 뉴스레터 발행을 시작합니다.")
    threading.Thread(target=_worker, args=({"stage": "publish", "approve": False},), daemon=True).start()


scheduler = NewsScheduler(callback=trigger_scheduled_run)


@app.post("/api/run")
def api_run():
    options = request.get_json(silent=True) or {}
    stage = options.get("stage") or "publish"
    if stage not in STAGES:
        return jsonify({"error": f"알 수 없는 단계: {stage}"}), 400

    with session.lock:
        if session.status in ("running", "awaiting_approval"):
            return jsonify({"error": "이미 실행 중입니다."}), 409
        session.reset()
        session.status = "running"
        session.stage = stage
        session.state = None
        session.error = None
        session.mode = {
            "stage": stage,
            "approve": bool(options.get("approve")),
            "dry_run": options.get("dry_run"),
        }

    hub.clear()
    threading.Thread(target=_worker, args=(options,), daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/approve")
def api_approve():
    with session.lock:
        if session.status != "awaiting_approval" or session.pipeline is None:
            return jsonify({"error": "승인 대기 중인 실행이 없습니다."}), 409
        pipeline = session.pipeline
        graph_config = session.graph_config
        session.status = "running"

    def finish():
        try:
            announce("stage", stage="publish", phase="start")
            state = pipeline.graph.invoke(None, graph_config)
            state.setdefault("stats", {})["llm"] = pipeline.llm.usage
            with session.lock:
                session.state = state
                session.status = "done"
            announce("stage", stage="publish", phase="done")
        except Exception as exc:  # noqa: BLE001
            log.exception("발행 실패")
            with session.lock:
                session.status = "error"
                session.error = f"{type(exc).__name__}: {exc}"
            announce("error", message=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                pipeline.close()
            except Exception:
                pass
            with session.lock:
                session.pipeline = None
                session.graph_config = None
            announce("finished")

    threading.Thread(target=finish, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/discard")
def api_discard():
    with session.lock:
        if session.status != "awaiting_approval":
            return jsonify({"error": "승인 대기 중인 실행이 없습니다."}), 409
        session.reset()
        session.status = "done"
    announce("discarded")
    return jsonify({"ok": True})


# ─────────────────────────────────────────────────────────── 조회 API

@app.get("/api/status")
def api_status():
    with session.lock:
        snapshot = session.snapshot()
        state = serialize_state(session.state)
    return jsonify({"session": snapshot, "result": state, "schedule": scheduler.get_status()})


@app.get("/api/config")
def api_config_get():
    try:
        return jsonify(current_config())
    except ConfigError as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/config")
def api_config_post():
    patch = request.get_json(silent=True) or {}
    try:
        touched = apply_patch(patch)
    except PatchError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, "files": touched, "config": current_config()})


@app.get("/api/secrets")
def api_secrets_get():
    """설정 여부와 마스킹된 미리보기만 돌려준다. 실제 값은 절대 내보내지 않는다."""
    return jsonify({"secrets": describe(ROOT / ".env"), "order": list(MANAGED)})


@app.post("/api/secrets")
def api_secrets_post():
    """빈 문자열은 '변경 없음' 으로 무시하고, clear 목록에 담긴 키만 지운다."""
    body = request.get_json(silent=True) or {}
    incoming = body.get("secrets") or {}
    clear = body.get("clear") or []

    existing = describe(ROOT / ".env")
    updates: dict[str, str | None] = {}
    try:
        for name, value in incoming.items():
            # 정해진 목록이 아니면 커스텀 키로 보고 이름을 검사한다
            key = name if name in MANAGED else validate_name(name)
            value = (value or "").strip()
            if value:
                updates[key] = value
        for name in clear:
            key = name if name in MANAGED else validate_name(name)
            if key in MANAGED or key in existing:
                updates[key] = None
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not updates:
        return jsonify({"ok": True, "changed": [], "secrets": describe(ROOT / ".env")})

    try:
        changed = update_env(updates, ROOT / ".env")
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 400

    # 값은 로그에 남기지 않는다 — 키 이름만 남긴다
    log.info("비밀값 갱신: %s", ", ".join(changed))
    return jsonify({"ok": True, "changed": changed, "secrets": describe(ROOT / ".env")})


@app.get("/api/history")
def api_history():
    runs = []
    for path in sorted(OUTPUT_DIR.glob("*.json"), reverse=True)[:50]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stats = data.get("stats", {})
        verified = data.get("verified", [])
        distribution: dict[str, int] = {}
        for item in verified:
            verdict = item.get("verdict", "")
            distribution[verdict] = distribution.get(verdict, 0) + 1
        runs.append(
            {
                "run_id": data.get("run_id") or path.stem,
                "started_at": data.get("started_at"),
                "collected": stats.get("collect", {}).get("unique", 0),
                "selected": stats.get("curate", {}).get("selected", 0),
                "published": len(verified),
                "distribution": distribution,
                "dry_run": stats.get("publish", {}).get("dry_run"),
            }
        )
    return jsonify({"runs": runs})


@app.get("/api/history/<run_id>")
def api_history_detail(run_id: str):
    # 경로 조작 방지 — run_id 는 파일명 형태만 허용한다
    if not run_id.replace("_", "").isalnum():
        return jsonify({"error": "잘못된 run_id"}), 400
    path = OUTPUT_DIR / f"{run_id}.json"
    if not path.exists():
        return jsonify({"error": "없는 실행입니다."}), 404
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


@app.get("/api/stream")
def api_stream():
    def generate():
        sub = hub.subscribe()
        try:
            for item in list(hub.buffer):
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            while True:
                try:
                    item = sub.get(timeout=20)
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            hub.unsubscribe(sub)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────── 정적 파일

@app.get("/")
def index():
    return send_from_directory(ROOT / "web", "index.html")


@app.get("/<path:filename>")
def static_files(filename: str):
    return send_from_directory(ROOT / "web", filename)


def main() -> None:
    setup_logging()
    try:
        cfg = load_config(str(CONFIG_DIR))
        scheduler.update_config(cfg.get("schedule", {}))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "설정을 읽지 못해 스케줄러를 비활성 상태로 시작합니다 "
            "(설정 화면에서 고쳐 저장하면 그때 반영됩니다): %s", exc
        )

    # 루프는 설정 로딩 성공 여부와 무관하게 항상 띄운다. 여기서 같이 죽이면
    # 나중에 설정 화면에서 스케줄을 켜도 (apply_patch 는 update_config 만 부르므로)
    # 영원히 발동하지 않는다. 루프는 매 틱 enabled 를 확인하므로 꺼진 채 도는
    # 비용은 5초마다 플래그 하나 보는 게 전부다.
    scheduler.start()

    port = int(os.environ.get("NEWSAGENT_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    print(f"\n  종우's 뉴스레터 에이전트 GUI — {url}\n  종료하려면 Ctrl+C\n")
    if os.environ.get("NEWSAGENT_NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # 127.0.0.1 에만 바인딩한다. 이 앱은 로컬 파일과 .env 비밀값을 다룬다.
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
