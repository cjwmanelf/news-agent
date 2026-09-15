"""LangGraph 조립 (PRD 2.1).

collect → curate → [선별 0건이면 종료] → research → verify → publish

체크포인터를 붙이면 실행 상태가 디스크에 남아, 발행만 실패한 경우 수집·요약을
다시 돌리지 않고 이어서 재개할 수 있다. LLM 비용이 걸린 단계를 재실행하지 않는 게 핵심이다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from .llm import BaseLLM, get_llm
from .nodes import (
    make_collect_node,
    make_curate_node,
    make_publish_node,
    make_research_node,
    make_verify_node,
)
from .state import NewsletterState
from .store import PublishedStore

log = logging.getLogger(__name__)


@dataclass
class Pipeline:
    """조립된 그래프와 그 안에서 공유되는 자원."""

    graph: Any
    llm: BaseLLM
    store: PublishedStore | None
    checkpointer: Any = None
    interrupts_before_publish: bool = False

    def close(self) -> None:
        """이 파이프라인이 연 자원을 전부 닫는다. 여러 번 불러도 안전하다.

        GUI(app.py)는 한 프로세스가 계속 떠 있으면서 실행마다 파이프라인을 새로
        만든다. 여기서 빠뜨리면 SQLite 커넥션과 파일 핸들이 실행 횟수만큼 쌓인다.
        """
        if self.store is not None:
            self.store.close()
            self.store = None
        close_checkpointer(self.checkpointer)
        self.checkpointer = None


def has_selection(state: NewsletterState) -> str:
    """선별 결과가 없으면 LLM 을 부르기 전에 끊는다 (PRD 2.1 - 비용 방어)."""
    if state.get("selected"):
        return "research"
    log.warning("선별된 기사가 없어 파이프라인을 조기 종료합니다.")
    return "publish"


def close_checkpointer(checkpointer: Any) -> None:
    """체크포인터가 물고 있는 SQLite 커넥션을 닫는다.

    SqliteSaver 에는 close() 가 없어 감싸고 있는 커넥션을 직접 닫아야 한다.
    build_pipeline 이 도중에 실패해 Pipeline 객체가 만들어지지 않은 경우에도
    호출할 수 있도록 모듈 함수로 뒀다.
    """
    connection = getattr(checkpointer, "conn", None)
    if connection is None:
        return
    try:
        connection.close()
    except Exception:  # 이미 닫혔거나 다른 스레드가 쓰는 중
        log.debug("체크포인터 커넥션을 닫지 못했습니다", exc_info=True)


def build_checkpointer(path: str | Path):
    """SQLite 체크포인터. 프로세스가 죽어도 재개할 수 있도록 파일에 남긴다."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path), check_same_thread=False)
    saver = SqliteSaver(connection)
    saver.setup()
    return saver


def build_pipeline(
    cfg: dict[str, Any],
    *,
    checkpointer: Any = None,
    approve_before_publish: bool = False,
    on_stage: Any = None,
) -> Pipeline:
    llm = get_llm(cfg)

    history_cfg = cfg.get("history", {})
    store = PublishedStore(
        history_cfg.get("path", "state/published.db"),
        enabled=bool(history_cfg.get("enabled", True)),
    )

    graph = StateGraph(NewsletterState)

    def wrap_node(name: str, fn: Any) -> Any:
        if not on_stage:
            return fn

        def wrapped(state: NewsletterState) -> dict[str, Any]:
            try:
                on_stage(name, "start")
            except Exception:
                pass
            res = fn(state)
            try:
                on_stage(name, "done")
            except Exception:
                pass
            return res

        return wrapped

    # 재시도 정책은 collect 에만 건다.
    #   collect  — 네트워크 실패가 실제로 잦고, 다시 돌려도 HTTP GET 뿐이라 안전하다.
    #   curate   — 순수 계산이라 재시도할 이유가 없다.
    #   research / verify — 노드 안에서 기사별 실패를 이미 격리한다. 노드째 재시도하면
    #                      이미 성공한 기사까지 LLM 을 다시 호출해 비용만 두 배가 된다.
    #   publish  — 절대 재시도하지 않는다. 전송 도중 끊긴 경우 같은 글을 두 번 올릴 수 있다.
    #              레이트리밋 재시도는 DiscordPublisher 안에서 멱등하게 처리한다.
    graph.add_node(
        "collect",
        wrap_node("collect", make_collect_node(cfg)),
        retry_policy=RetryPolicy(max_attempts=2, initial_interval=2.0),
    )
    graph.add_node("curate", wrap_node("curate", make_curate_node(cfg, store)))
    graph.add_node("research", wrap_node("research", make_research_node(cfg, llm)))
    graph.add_node("verify", wrap_node("verify", make_verify_node(cfg, llm, store)))
    graph.add_node("publish", wrap_node("publish", make_publish_node(cfg, store)))

    graph.add_edge(START, "collect")
    graph.add_edge("collect", "curate")
    graph.add_conditional_edges("curate", has_selection, {"research": "research", "publish": "publish"})
    graph.add_edge("research", "verify")
    graph.add_edge("verify", "publish")
    graph.add_edge("publish", END)

    compile_kwargs: dict[str, Any] = {}
    if checkpointer is not None:
        compile_kwargs["checkpointer"] = checkpointer
    if approve_before_publish:
        # 발행 직전에 멈춘다. 사람이 내용을 확인한 뒤에만 재개된다.
        # 체크포인터가 있어야 멈춘 지점을 보관할 수 있다.
        compile_kwargs["interrupt_before"] = ["publish"]

    return Pipeline(
        graph=graph.compile(**compile_kwargs),
        llm=llm,
        store=store,
        checkpointer=checkpointer,
        interrupts_before_publish=approve_before_publish,
    )


def build_graph(cfg: dict[str, Any], *, on_stage: Any = None):
    """체크포인터 없이 그래프만 필요할 때 (테스트·임베드 용)."""
    return build_pipeline(cfg, on_stage=on_stage).graph


def initial_state() -> NewsletterState:
    now = datetime.now()
    return {
        "run_id": now.strftime("%Y%m%d_%H%M%S"),
        "started_at": now.isoformat(timespec="seconds"),
        "raw_items": [],
        "selected": [],
        "corpus": [],
        "briefs": [],
        "verified": [],
        "publish_result": {},
        "errors": [],
        "stats": {},
    }
