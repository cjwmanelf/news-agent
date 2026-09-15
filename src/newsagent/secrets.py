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


# 따옴표 안에서 해제되는 이스케이프 — python-dotenv 의 규칙을 그대로 따른다.
#   큰따옴표: \\ \" \n \r \t 를 해제한다
#   작은따옴표: 내용을 그대로 두되 \' 만 해제한다
_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "r": "\r", "t": "\t"}

# 따옴표로 감싸지 않으면 dotenv 가 값을 잘라먹거나 줄 자체가 깨지는 문자들.
# 역슬래시가 포함된 이유: 따옴표 안에서는 이스케이프 문자로 해석되므로
# 원문 그대로 남기려면 우리가 먼저 escape 해줘야 한다.
_UNSAFE_CHARS = " \t#\"'\n\r\\"


def _unquote(value: str) -> str:
    """`.env` 한 줄의 값 부분을 실제 값으로 되돌린다 (dotenv 와 같은 규칙)."""
    value = value.strip()
    if len(value) < 2 or value[0] != value[-1] or value[0] not in "\"'":
        return value

    quote, body = value[0], value[1:-1]
    if quote == "'":
        return body.replace("\\'", "'")

    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _needs_quotes(value: str) -> bool:
    return bool(value) and (value != value.strip() or any(c in value for c in _UNSAFE_CHARS))


def format_line(name: str, value: str) -> str:
    """`.env` 에 쓸 한 줄을 만든다.

    따옴표로 감쌀 때는 값 안의 역슬래시·큰따옴표·줄바꿈을 반드시 escape 한다.
    빠뜨리면 값에 " 가 하나만 있어도 `KEY="ab"cd"` 같은 깨진 줄이 나오고
    dotenv 는 그 줄을 통째로 버린다. 그러면 GUI 에는 '저장됨' 으로 보이는데
    정작 파이프라인은 키가 없는 상태로 도는, 가장 알아채기 어려운 실패가 된다.
    """
    if not _needs_quotes(value):
        return f"{name}={value}"
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
    return f'{name}="{escaped}"'


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
            result.append(format_line(name, value))
        changed.append(name)

    # 파일에 없던 항목은 끝에 덧붙인다
    appended = [(k, v) for k, v in remaining.items() if v]
    if appended:
        if result and result[-1].strip():
            result.append("")
        result.append("# GUI 에서 추가됨")
        for name, value in appended:
            result.append(format_line(name, value))
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
