"""설정 로딩·검증 (PRD 4).

config.yaml / sources.yaml / interests.yaml 을 읽고 기본값과 딥머지한 뒤,
문자열 안의 ${ENV_VAR} 를 환경변수로 치환한다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

PROVIDERS = ("anthropic", "openai", "gemini", "local", "mock")

# provider 별로 반드시 있어야 하는 환경변수.
# local 은 인증 없는 서버(Ollama 등)가 많아 키를 요구하지 않는다.
PROVIDER_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GOOGLE_API_KEY",
    "local": None,
    "mock": None,
}

DEFAULTS: dict[str, Any] = {
    "llm": {
        "provider": "mock",
        "model": {
            "anthropic": "claude-sonnet-5",
            "openai": "gpt-4.1-mini",
            "gemini": "gemini-2.5-flash",
            "local": "llama3.1",
        },
        "base_url": {"local": "http://localhost:11434/v1"},
        "temperature": 0.2,
        "max_tokens": 2000,
    },
    "collect": {
        "lookback_hours": 24,
        "max_workers": 8,
        "timeout": 15,
        "default_max_items": 50,
        "respect_robots": True,
        "user_agent": "newsagent/1.0 (+personal newsletter bot)",
    },
    "research": {
        "max_workers": 4,
        "max_content_chars": 12000,
        "fetch_full_text": True,
    },
    "verify": {
        "cluster_threshold": 0.35,
        "peer_max_age_hours": 96,
        "fetch_peer_content": True,
        "max_peer_fetch": 4,
        "peer_body_chars": 1500,
        "corroboration_search": {
            "enabled": True,
            "provider": "auto",
            "max_results": 20,
            "hl": "ko",
            "gl": "KR",
            "ceid": "KR:ko",
        },
        "max_cluster_peers": 6,
        "min_confidence_to_publish": 0.0,
        "duplicate_threshold": 0.9,
    },
    "history": {
        "enabled": True,
        "path": "state/published.db",
        "lookback_days": 7,
        "keep_days": 90,
        "record_on_dry_run": False,
    },
    "runtime": {
        "checkpoint": True,
        "checkpoint_path": "state/checkpoints.db",
    },
    "publish": {
        "target": "discord",
        "targets": ["discord"],
        "dry_run": True,
        "username": "뉴스레터 봇",
        "title": "오늘의 뉴스 브리핑",
        "max_embeds_per_message": 10,
        "send_interval": 1.0,
        "include_verdicts": ["VERIFIED", "LIKELY", "SINGLE_SOURCE", "DISPUTED"],
    },
    "output": {"dir": "output", "save_json": True, "save_markdown": True},
    "curate": {
        "mode": "hybrid",
        "threshold": 0.30,
        "max_articles": 8,
        "weights": {"keyword": 0.6, "vector": 0.4},
        "interests": [],
    },
    "schedule": {
        "enabled": False,
        "mode": "interval",
        "interval_hours": 6,
        "daily_time": "08:30",
    },
    "sources": [],
}


class ConfigError(Exception):
    """설정이 잘못됐을 때. 실행 초반에 명확히 죽이기 위한 예외."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _expand_env(node: Any) -> Any:
    """설정값 안의 ${VAR} 를 환경변수로 치환한다 (API 키를 yaml 에 적지 않게)."""
    if isinstance(node, str):
        return ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), node)
    if isinstance(node, dict):
        return {k: _expand_env(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand_env(v) for v in node]
    return node


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"설정 파일 최상위는 매핑이어야 합니다: {path}")
    return data


def load_config(config_dir: str | Path = "config") -> dict[str, Any]:
    """세 개의 yaml 을 하나의 설정 딕셔너리로 합쳐 돌려준다."""
    load_dotenv()

    base = Path(config_dir)
    main = _read_yaml(base / "config.yaml")
    sources = _read_yaml(base / "sources.yaml")
    interests = _read_yaml(base / "interests.yaml")

    cfg = _deep_merge(DEFAULTS, main)
    cfg["sources"] = sources.get("sources", [])
    cfg["curate"] = _deep_merge(cfg["curate"], interests)

    cfg = _expand_env(cfg)
    cfg["_config_dir"] = str(base.resolve())
    return cfg


def validate_config(cfg: dict[str, Any]) -> list[str]:
    """치명적이지 않은 문제는 경고 문자열로 돌려주고, 실행 불가 조건만 예외로 올린다."""
    warnings: list[str] = []

    provider = cfg["llm"]["provider"]
    if provider not in PROVIDERS:
        raise ConfigError(
            f"llm.provider 는 {' / '.join(PROVIDERS)} 중 하나여야 합니다 (현재: {provider})"
        )
    required_key = PROVIDER_ENV.get(provider)
    if required_key and not os.environ.get(required_key, "").strip():
        raise ConfigError(
            f"llm.provider={provider} 인데 {required_key} 가 없습니다. "
            "설정 화면의 'API 키' 칸에 입력하거나 provider 를 mock 으로 바꾸세요."
        )
    if provider == "local" and not (cfg["llm"].get("base_url") or {}).get("local"):
        raise ConfigError("llm.provider=local 인데 llm.base_url.local 이 비어 있습니다.")
    if provider == "mock":
        warnings.append("llm.provider=mock — 요약/검수가 실제 LLM 없이 규칙 기반으로 동작합니다.")

    enabled = [s for s in cfg["sources"] if s.get("enabled", True)]
    if not enabled:
        raise ConfigError("활성화된 수집 소스가 하나도 없습니다 (config/sources.yaml).")
    for src in enabled:
        if src.get("type") not in ("rss", "api", "crawl"):
            raise ConfigError(f"소스 '{src.get('name')}' 의 type 이 잘못됐습니다: {src.get('type')}")
        if not src.get("url"):
            raise ConfigError(f"소스 '{src.get('name')}' 에 url 이 없습니다.")
        if src["type"] == "crawl" and not src.get("list_selector"):
            raise ConfigError(f"crawl 소스 '{src.get('name')}' 에 list_selector 가 필요합니다.")
        if src.get("role", "content") not in ("content", "corroboration"):
            raise ConfigError(
                f"소스 '{src.get('name')}' 의 role 은 content / corroboration 중 하나여야 합니다."
            )
        if "lookback_hours" in src and src["lookback_hours"] is not None:
            val = src["lookback_hours"]
            if not isinstance(val, int) or val <= 0:
                raise ConfigError(
                    f"소스 '{src.get('name')}' 의 lookback_hours 는 1 이상의 정수여야 합니다 (현재: {val})"
                )

    if not [s for s in enabled if s.get("role", "content") == "content"]:
        raise ConfigError(
            "role=content 인 소스가 없습니다. corroboration 소스만으로는 뉴스레터를 만들 수 없습니다."
        )

    if not cfg["curate"]["interests"]:
        raise ConfigError("관심사가 비어 있습니다 (config/interests.yaml).")
    if cfg["curate"]["mode"] not in ("keyword", "vector", "hybrid"):
        raise ConfigError(f"curate.mode 값이 잘못됐습니다: {cfg['curate']['mode']}")

    search = cfg["verify"].get("corroboration_search", {})
    valid_search = ("auto", "naver", "naver_hub", "naver_legacy", "google_news")
    if search.get("provider") not in valid_search:
        raise ConfigError(
            f"verify.corroboration_search.provider 는 {' / '.join(valid_search)} 중 하나여야 합니다."
        )
    if search.get("enabled", True):
        has_hub = bool(
            os.environ.get("NAVER_API_KEY_ID", "").strip()
            and os.environ.get("NAVER_API_KEY", "").strip()
        )
        has_legacy = bool(
            os.environ.get("NAVER_CLIENT_ID", "").strip()
            and os.environ.get("NAVER_CLIENT_SECRET", "").strip()
        )
        if not (has_hub or has_legacy):
            warnings.append(
                "교차검증 검색이 google_news 로 동작합니다 — 증인 본문을 가져올 수 없어 "
                "팩트체크가 제목 대조 수준에 머뭅니다. 네이버 검색 키를 넣으면 개선됩니다 "
                "(네이버클라우드 플랫폼 > NAVER API HUB)."
            )
        elif has_legacy and not has_hub:
            warnings.append(
                "네이버 검색이 구 방식(개발자센터 Client ID) 으로 동작합니다. "
                "2027-06-30 까지만 지원되니 NAVER API HUB 키로 옮기는 것을 권합니다."
            )

    from .publishers import CHANNEL_LABEL, CHANNELS, configured_targets, missing_env

    targets = configured_targets(cfg["publish"])
    unknown = [t for t in targets if t not in CHANNELS]
    if unknown:
        raise ConfigError(
            f"알 수 없는 발행 채널: {', '.join(unknown)} (사용 가능: {', '.join(CHANNELS)})"
        )
    if not cfg["publish"]["dry_run"]:
        if not targets:
            raise ConfigError("발행 채널이 하나도 선택되지 않았습니다.")
        for channel in targets:
            gaps = missing_env(channel)
            if gaps:
                raise ConfigError(
                    f"{CHANNEL_LABEL.get(channel, channel)} 발행에 필요한 값이 없습니다: "
                    f"{', '.join(gaps)} — 설정 화면의 'API 키·웹후크' 에서 입력하세요."
                )
    else:
        label = ", ".join(CHANNEL_LABEL.get(t, t) for t in targets) or "없음"
        warnings.append(f"publish.dry_run=true — 실제 전송하지 않습니다 (대상: {label}).")

    schedule = cfg.get("schedule", {})
    if schedule.get("enabled"):
        mode = schedule.get("mode", "interval")
        if mode not in ("interval", "daily"):
            raise ConfigError(f"schedule.mode 는 interval 또는 daily 여야 합니다 (현재: {mode})")
        if mode == "interval":
            interval = schedule.get("interval_hours")
            if not isinstance(interval, int) or interval <= 0:
                raise ConfigError(f"schedule.interval_hours 는 1 이상의 정수여야 합니다 (현재: {interval})")
        elif mode == "daily":
            from .scheduler import parse_daily_time

            daily_time = schedule.get("daily_time", "")
            try:
                parse_daily_time(daily_time)
            except ValueError as exc:
                raise ConfigError(f"schedule.daily_time 이 올바르지 않습니다: {exc}") from None

    return warnings
