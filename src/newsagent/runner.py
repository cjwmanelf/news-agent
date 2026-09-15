"""단계 실행 헬퍼 — CLI(run.py)와 GUI(app.py)가 공유한다.

그래프를 쓰지 않고 노드를 직접 순차 호출한다. 특정 단계까지만 돌려보며
임계값을 조정할 때, 그리고 GUI 가 단계별 진행 상황을 중계할 때 쓴다.
"""

from __future__ import annotations

from typing import Any, Callable

from .llm import BaseLLM, get_llm
from .nodes import (
    make_collect_node,
    make_curate_node,
    make_publish_node,
    make_research_node,
    make_verify_node,
)
from .store import PublishedStore

STAGES = ["collect", "curate", "research", "verify", "publish"]


def merge_update(state: dict[str, Any], update: dict[str, Any]) -> None:
    """노드가 돌려준 부분 상태를 합친다. LangGraph 리듀서와 같은 규칙."""
    for key, value in update.items():
        if key == "errors":
            state["errors"] = state.get("errors", []) + value
        elif key == "stats":
            state.setdefault("stats", {}).update(value)
        else:
            state[key] = value


def build_nodes(cfg: dict[str, Any], llm: BaseLLM, store: PublishedStore | None) -> dict[str, Callable]:
    return {
        "collect": make_collect_node(cfg),
        "curate": make_curate_node(cfg, store),
        "research": make_research_node(cfg, llm),
        "verify": make_verify_node(cfg, llm, store),
        "publish": make_publish_node(cfg, store),
    }


def run_stages(
    cfg: dict[str, Any],
    upto: str,
    *,
    state: dict[str, Any] | None = None,
    llm: BaseLLM | None = None,
    store: PublishedStore | None = None,
    on_stage: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """`upto` 단계까지 순차 실행한다.

    on_stage(stage, phase) 로 진행 상황을 알린다 (phase: start | done | skipped).
    store 를 넘기지 않으면 이 함수가 만들고 끝날 때 닫는다.
    """
    from .graph import initial_state

    llm = llm or get_llm(cfg)
    owns_store = store is None
    if owns_store:
        history_cfg = cfg.get("history", {})
        store = PublishedStore(
            history_cfg.get("path"), enabled=bool(history_cfg.get("enabled", True))
        )

    nodes = build_nodes(cfg, llm, store)
    state = state if state is not None else dict(initial_state())

    try:
        for name in STAGES[: STAGES.index(upto) + 1]:
            if on_stage:
                on_stage(name, "start")
            merge_update(state, nodes[name](state))
            if on_stage:
                on_stage(name, "done")
            # 선별이 0건이면 LLM 을 부르기 전에 끊는다 (그래프의 조건부 엣지와 같은 규칙)
            if name == "curate" and not state.get("selected"):
                for skipped in STAGES[STAGES.index("curate") + 1 : STAGES.index(upto) + 1]:
                    if on_stage:
                        on_stage(skipped, "skipped")
                break
    finally:
        if owns_store:
            store.close()

    state.setdefault("stats", {})["llm"] = llm.usage
    return state
