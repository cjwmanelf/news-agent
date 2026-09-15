"""LLM 어댑터 (PRD 4 - 프로바이더 전환).

provider: anthropic / openai / gemini / local / mock

  local 은 OpenAI 호환 엔드포인트를 부른다. Ollama·LM Studio·vLLM·llama.cpp 가
  모두 이 규격을 내주므로 백엔드 하나로 전부 커버된다. base_url 만 맞춰주면 된다.
  mock 은 API 키 없이 전 구간을 돌려보기 위한 규칙 기반 백엔드로,
  3단계·4단계의 입출력 계약을 그대로 지킨다.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

from . import prompts
from .utils.text import clean_text, title_key

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def extract_json(text: str) -> dict[str, Any]:
    """모델 출력에서 JSON 객체를 건져낸다. 실패하면 ValueError."""
    if not text:
        raise ValueError("빈 응답")
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 앞뒤에 설명이 붙은 경우: 균형 잡힌 첫 객체를 스캔한다
    start = cleaned.find("{")
    while start != -1:
        depth, in_str, escape = 0, False, False
        for idx in range(start, len(cleaned)):
            ch = cleaned[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : idx + 1])
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    raise ValueError("JSON 을 찾지 못했습니다")


class BaseLLM:
    """모든 백엔드는 호출 횟수와 토큰을 집계한다 (PRD NFR-2 비용 관측)."""

    name = "base"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "failures": 0}

    def _track(self, *, input_tokens: int = 0, output_tokens: int = 0, failed: bool = False) -> None:
        # 3·4단계가 스레드 풀에서 병렬 호출하므로 집계에 락이 필요하다
        with self._lock:
            self.usage["calls"] += 1
            self.usage["input_tokens"] += input_tokens
            self.usage["output_tokens"] += output_tokens
            if failed:
                self.usage["failures"] += 1

    def summarize(self, *, source: str, title: str, published_at: str, content: str) -> dict[str, Any]:
        raise NotImplementedError

    def fact_check(self, *, source: str, headline: str, facts: list[str], peers: list[dict]) -> dict[str, Any]:
        raise NotImplementedError


class ChatLLM(BaseLLM):
    """langchain 챗 모델을 감싸 JSON 응답을 강제한다."""

    def __init__(
        self, provider: str, model: str, temperature: float, max_tokens: int,
        base_url: str | None = None,
    ):
        super().__init__()
        self.name = f"{provider}:{model}"
        if provider == "anthropic":
            from langchain_anthropic import ChatAnthropic

            self.client = ChatAnthropic(
                model=model, temperature=temperature, max_tokens=max_tokens,
                api_key=os.environ["ANTHROPIC_API_KEY"],
            )
        elif provider == "openai":
            from langchain_openai import ChatOpenAI

            self.client = ChatOpenAI(
                model=model, temperature=temperature, max_tokens=max_tokens,
                api_key=os.environ["OPENAI_API_KEY"],
            )
        elif provider == "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI

            self.client = ChatGoogleGenerativeAI(
                model=model, temperature=temperature, max_output_tokens=max_tokens,
                google_api_key=os.environ["GOOGLE_API_KEY"],
            )
        elif provider == "local":
            from langchain_openai import ChatOpenAI

            # 로컬 서버는 대개 인증을 요구하지 않지만 OpenAI 클라이언트가 빈 키를
            # 거부하므로 자리표시자를 넣는다.
            self.client = ChatOpenAI(
                model=model, temperature=temperature, max_tokens=max_tokens,
                openai_api_key=os.environ.get("LOCAL_LLM_API_KEY") or "not-needed",
                openai_api_base=base_url,
            )
        else:
            raise ValueError(f"지원하지 않는 provider: {provider}")

    def _invoke_json(self, system: str, user: str, *, retries: int = 1) -> dict[str, Any]:
        from langchain_core.messages import HumanMessage, SystemMessage

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            messages = [SystemMessage(content=system), HumanMessage(content=user)]
            if attempt:  # R3-4: 파싱 실패 시 1회 재시도할 때 형식을 다시 못박는다
                messages.append(
                    HumanMessage(content="직전 응답이 JSON 파싱에 실패했습니다. JSON 객체만 다시 출력하세요.")
                )
            response = self.client.invoke(messages)
            meta = getattr(response, "usage_metadata", None) or {}
            self._track(
                input_tokens=int(meta.get("input_tokens", 0) or 0),
                output_tokens=int(meta.get("output_tokens", 0) or 0),
            )
            text = response.content
            if isinstance(text, list):  # 블록 형태 응답 대응
                text = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block) for block in text
                )
            try:
                return extract_json(text)
            except ValueError as exc:
                last_error = exc
        self._track(failed=True)
        raise ValueError(f"LLM JSON 파싱 실패: {last_error}")

    def summarize(self, *, source: str, title: str, published_at: str, content: str) -> dict[str, Any]:
        return self._invoke_json(
            prompts.SUMMARIZE_SYSTEM,
            prompts.SUMMARIZE_USER.format(
                source=source, title=title, published_at=published_at or "미상", content=content
            ),
        )

    def fact_check(self, *, source: str, headline: str, facts: list[str], peers: list[dict]) -> dict[str, Any]:
        facts_block = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(facts)) or "(없음)"
        peers_block = (
            "\n\n".join(
                f"- 매체: {p['source']}\n  제목: {p['title']}\n  내용: {p['text']}" for p in peers
            )
            or "(대조할 기사 없음)"
        )
        return self._invoke_json(
            prompts.FACTCHECK_SYSTEM,
            prompts.FACTCHECK_USER.format(
                source=source, headline=headline, facts=facts_block, peers=peers_block
            ),
        )


class MockLLM(BaseLLM):
    """키 없이 돌리는 규칙 기반 백엔드.

    - summarize: 본문 앞부분 문장을 잘라 요약/사실 문장으로 쓴다.
    - fact_check: 사실 문장의 특징 토큰이 대조 기사에 등장하면 supported 로 본다.
      LLM 의 의미 판단은 못 하지만 4단계 점수 로직을 그대로 굴릴 수 있다.
    """

    name = "mock"
    _SENT = re.compile(r"(?<=[.!?。])\s+|\n+")

    def _sentences(self, text: str, limit: int) -> list[str]:
        parts = [clean_text(s) for s in self._SENT.split(text or "")]
        return [p for p in parts if len(p) > 15][:limit]

    def summarize(self, *, source: str, title: str, published_at: str, content: str) -> dict[str, Any]:
        self._track()
        sentences = self._sentences(content, 10)
        return {
            "headline": clean_text(title)[:120],
            "summary": sentences[:3] or [clean_text(title)],
            "key_facts": sentences[:8] or [clean_text(title)],
            "entities": {"org": [], "person": [], "product": []},
            "category": "기타",
            "why_it_matters": "(mock 백엔드 — 실제 LLM 으로 전환하면 채워집니다)",
            "_mock": True,
        }

    def fact_check(self, *, source: str, headline: str, facts: list[str], peers: list[dict]) -> dict[str, Any]:
        self._track()
        peer_text = title_key(" ".join(f"{p['title']} {p['text']}" for p in peers))
        checks = []
        for fact in facts:
            tokens = [title_key(t) for t in re.split(r"\s+", fact) if len(t) >= 2]
            tokens = [t for t in tokens if len(t) >= 2]
            hits = sum(1 for t in tokens if t and t in peer_text)
            ratio = hits / max(len(tokens), 1)
            status = "supported" if ratio >= 0.25 else "unverified"
            checks.append(
                {
                    "fact": fact,
                    "status": status,
                    "evidence": peers[0]["source"] if peers and status == "supported" else "",
                    "note": f"토큰 일치율 {ratio:.0%} (mock)",
                }
            )
        return {"fact_checks": checks, "overall": "mock 백엔드 토큰 대조 결과", "_mock": True}


def get_llm(cfg: dict[str, Any]) -> BaseLLM:
    llm_cfg = cfg["llm"]
    provider = llm_cfg["provider"]
    if provider == "mock":
        return MockLLM()
    model = llm_cfg["model"][provider]
    base_url = (llm_cfg.get("base_url") or {}).get(provider)
    return ChatLLM(provider, model, llm_cfg["temperature"], llm_cfg["max_tokens"], base_url)
