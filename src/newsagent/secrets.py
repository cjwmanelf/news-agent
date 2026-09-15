"""`.env` 읽기·쓰기.

GUI 에서 API 키를 입력받아 저장하기 위한 것. 지켜야 할 규칙이 둘 있다.

  1. 값을 브라우저로 되돌려주지 않는다. 설정 여부와 끝 4자리만 보낸다.
  2. 기존 `.env` 의 주석과 우리가 모르는 항목은 건드리지 않는다.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")

# GUI 에서 다루는 항목. 여기 없는 키는 읽지도 쓰지도 않는다.
MANAGED: dict[str, str] = {
    "ANTHROPIC_API_KEY": "Claude (Anthropic)",
    "OPENAI_API_KEY": "OpenAI",
    "GOOGLE_API_KEY": "Gemini (Google)",
    "LOCAL_LLM_API_KEY": "로컬 LLM",
    "NAVER_API_KEY_ID": "네이버 검색 Key ID (신규)",
    "NAVER_API_KEY": "네이버 검색 Key (신규)",
    "NAVER_CLIENT_ID": "네이버 Client ID (구 방식)",
    "NAVER_CLIENT_SECRET": "네이버 Client Secret (구 방식)",
    "DISCORD_WEBHOOK_URL": "디스코드 웹후크",
    "SLACK_WEBHOOK_URL": "슬랙 웹후크",
    "TELEGRAM_BOT_TOKEN": "텔레그램 봇 토큰",
    "TELEGRAM_CHAT_ID": "텔레그램 채팅 ID",
}

# 비밀이 아닌 값은 전체를 보여줘도 된다 (오히려 확인이 쉬워진다)
PLAIN = {"TELEGRAM_CHAT_ID", "NAVER_CLIENT_ID", "NAVER_API_KEY_ID"}

# 사용자가 직접 추가하는 키 이름 규칙.
# 커스텀 소스(${FINNHUB_TOKEN} 같은)를 쓰려면 임의 이름이 필요하지만,
# 프로세스 동작에 관여하는 변수까지 덮어쓰게 두면 안 된다.
NAME_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
RESERVED = {
    "PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONEXECUTABLE",
    "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "WINDIR",
    "COMSPEC", "SHELL", "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_FILE",
}


def validate_name(name: str) -> str:
    """커스텀 키 이름을 검사한다. 문제가 있으면 ValueError."""
    name = (name or "").strip().upper()
    if not NAME_PATTERN.match(name):
        raise ValueError(
            f"'{name}' 은 쓸 수 없는 이름입니다. 대문자로 시작하고 대문자·숫자·밑줄만 "
            "쓸 수 있습니다 (예: FINNHUB_TOKEN)."
        )
    if name in RESERVED:
        raise ValueError(f"'{name}' 은 시스템이 쓰는 이름이라 바꿀 수 없습니다.")
    return name


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _needs_quotes(value: str) -> bool:
    return bool(value) and (value != value.strip() or any(c in value for c in " #\"'\n"))


def read_env(path: str | Path = ".env") -> dict[str, str]:
    """`.env` 를 읽어 키·값 매핑으로 돌려준다. 파일이 없으면 빈 매핑."""
    file = Path(path)
    if not file.exists():
        return {}
    values: dict[str, str] = {}
    for raw in file.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        match = LINE.match(raw)
        if match:
            values[match.group(1)] = _unquote(match.group(2))
    return values


def mask(name: str, value: str) -> str:
    """화면에 보여줄 형태. 실제 값은 절대 내보내지 않는다."""
    if not value:
        return ""
    if name in PLAIN:
        return value
    if len(value) <= 8:
        return "•" * len(value)
    return f"{'•' * 8}{value[-4:]}"


def describe(path: str | Path = ".env") -> dict[str, dict]:
    """GUI 표시용 상태. {키: {label, set, preview, custom}}

    MANAGED 목록에 더해, .env 에 사용자가 직접 넣은 키도 함께 보고한다.
    커스텀 소스에서 ${VAR} 로 참조하는 값들이 여기 들어온다.
    """
    values = read_env(path)
    out: dict[str, dict] = {}
    for name, label in MANAGED.items():
        # .env 에 없더라도 셸 환경변수로 들어와 있을 수 있다
        value = values.get(name) or os.environ.get(name, "")
        out[name] = {
            "label": label,
            "set": bool(value.strip()),
            "preview": mask(name, value.strip()),
            "from_env_file": name in values,
            "custom": False,
        }
    for name, value in values.items():
        if name in MANAGED:
            continue
        out[name] = {
            "label": name,
            "set": bool(value.strip()),
            "preview": mask(name, value.strip()),
            "from_env_file": True,
            "custom": True,
        }
    return out


def update_env(updates: dict[str, str | None], path: str | Path = ".env") -> list[str]:
    """`.env` 를 갱신한다.

    updates 의 값이 None 이면 해당 항목을 지운다.
    빈 문자열은 '바꾸지 않음' 이 아니라 '빈 값으로 저장' 이므로 호출부에서 걸러야 한다.
    반환값은 실제로 바뀐 키 목록.
    """
    file = Path(path)
    lines = file.read_text(encoding="utf-8").splitlines() if file.exists() else []

    changed: list[str] = []
    remaining = dict(updates)

    result: list[str] = []
    for raw in lines:
        match = LINE.match(raw)
        if not match or match.group(1) not in remaining:
            result.append(raw)
            continue
        name = match.group(1)
        value = remaining.pop(name)
        if value is None:
            result.append(f"{name}=")
        else:
            result.append(f"{name}={value}" if not _needs_quotes(value) else f'{name}="{value}"')
        changed.append(name)

    # 파일에 없던 항목은 끝에 덧붙인다
    appended = [(k, v) for k, v in remaining.items() if v]
    if appended:
        if result and result[-1].strip():
            result.append("")
        result.append("# GUI 에서 추가됨")
        for name, value in appended:
            result.append(f"{name}={value}" if not _needs_quotes(value) else f'{name}="{value}"')
            changed.append(name)

    file.write_text("\n".join(result).rstrip() + "\n", encoding="utf-8")

    # 현재 프로세스에도 즉시 반영한다. 재시작 없이 바로 실행할 수 있어야 한다.
    for name in changed:
        value = updates.get(name)
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    return changed
