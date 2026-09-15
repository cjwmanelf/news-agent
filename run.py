#!/usr/bin/env python
"""뉴스레터 에이전트 엔트리포인트.

  python run.py                     # 설정대로 전 구간 실행
  python run.py --check             # 설정만 검증하고 종료
  python run.py --send              # dry_run 을 무시하고 실제 디스코드로 전송
  python run.py --approve --send    # 발행 직전에 멈춰 사람 확인을 받고 전송
  python run.py --resume 20260914_130226   # 중단된 실행을 이어서 재개
  python run.py --stage curate      # 해당 단계까지만 실행 (디버깅용)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from newsagent.config import ConfigError, load_config, validate_config  # noqa: E402
from newsagent.graph import build_checkpointer, build_pipeline, initial_state  # noqa: E402
from newsagent.models import VERDICT_EMOJI, VERDICT_LABEL  # noqa: E402
from newsagent.runner import STAGES, run_stages  # noqa: E402


def setup_logging(verbose: bool) -> None:
    # Windows 콘솔 기본 코드페이지에서 한글/이모지가 깨지지 않게 UTF-8 로 고정한다
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("urllib3", "httpx", "httpcore", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def print_summary(state: dict) -> None:
    stats = state.get("stats", {})
    print("\n" + "─" * 70)
    print(f"실행 요약  run_id={state.get('run_id')}")
    print("─" * 70)
    labels = {
        "collect": "1. 수집",
        "curate": "2. 선별",
        "research": "3. 취재",
        "verify": "4. 검수",
        "publish": "5. 발행",
    }
    for stage, label in labels.items():
        data = stats.get(stage)
        if not data:
            print(f"{label:<10} (실행 안 됨)")
            continue
        elapsed = data.get("elapsed_sec", 0)
        if stage == "collect":
            detail = (f"{data['unique']}건 (소스 {data['sources_enabled']}개, "
                      f"실패 {data['sources_failed']}개, 중복 {data['duplicates_removed']}건 제거)")
        elif stage == "curate":
            detail = f"{data['candidates']}건 → {data['selected']}건 (mode={data['mode']}, threshold={data['threshold']})"
            if data.get("skipped_already_published"):
                detail += f" · 기발행 {data['skipped_already_published']}건 제외"
        elif stage == "research":
            detail = f"{data['briefs']}건 요약 (폴백 {data.get('degraded', 0)}건, llm={data.get('llm')})"
        elif stage == "verify":
            detail = (f"{data['verified']}건 판정 {data.get('distribution', {})} "
                      f"평균신뢰도 {data.get('avg_confidence')} 평균출처 {data.get('avg_sources')}곳")
            extras = []
            if data.get("peer_bodies_fetched"):
                extras.append(f"증인본문 {data['peer_bodies_fetched']}건")
            if data.get("peers_collapsed_as_reprint"):
                extras.append(f"전재제외 {data['peers_collapsed_as_reprint']}건")
            if data.get("skipped_already_published"):
                extras.append(f"기발행사건 {data['skipped_already_published']}건 제외")
            if extras:
                detail += " · " + ", ".join(extras)
        else:
            detail = f"{data['published']}건 발행 (dry_run={data.get('dry_run')})"
            if data.get("history_recorded"):
                detail += f" · 이력 {data['history_recorded']}건 기록"
        print(f"{label:<10} {detail}  [{elapsed}s]")

    usage = stats.get("llm")
    if usage:
        tokens = usage["input_tokens"] + usage["output_tokens"]
        line = f"LLM        호출 {usage['calls']}회"
        if tokens:
            line += f" · 입력 {usage['input_tokens']:,} / 출력 {usage['output_tokens']:,} 토큰"
        if usage.get("failures"):
            line += f" · 실패 {usage['failures']}회"
        print(line)

    errors = state.get("errors", [])
    if errors:
        print(f"\n경고/오류 {len(errors)}건:")
        for message in errors[:15]:
            print(f"  - {message}")
        if len(errors) > 15:
            print(f"  ... 외 {len(errors) - 15}건")

    files = stats.get("publish", {}).get("files", [])
    if files:
        print("\n저장된 파일:")
        for path in files:
            print(f"  - {path}")
    print("─" * 70)


def preview_for_approval(state: dict) -> None:
    verified = state.get("verified", [])
    print("\n" + "=" * 70)
    print(f"발행 대기 — {len(verified)}건. 내용을 확인하세요.")
    print("=" * 70)
    for index, item in enumerate(verified, 1):
        emoji = VERDICT_EMOJI.get(item.verdict, "")
        label = VERDICT_LABEL.get(item.verdict, item.verdict)
        print(f"{index:>2}. {emoji} [{label} {item.confidence:.2f}] {item.brief.headline}")
        if item.corroborating_sources:
            print(f"     교차 출처 {len(item.corroborating_sources)}곳: {', '.join(item.corroborating_sources)}")
        if item.brief.article:
            print(f"     {item.brief.article.url}")
    print("=" * 70)


def run_until(cfg: dict, stage: str) -> dict:
    """그래프 대신 노드를 직접 순차 호출한다 (단계별 디버깅용)."""
    return run_stages(
        cfg, stage,
        on_stage=lambda name, phase: (
            print(f"선별된 기사가 없어 이후 단계를 건너뜁니다.") if phase == "skipped" and name == "research" else None
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="LangGraph 뉴스레터 에이전트")
    parser.add_argument("--config", default="config", help="설정 디렉토리 (기본: config)")
    parser.add_argument("--check", action="store_true", help="설정 검증만 하고 종료")
    parser.add_argument("--send", action="store_true", help="dry_run 을 끄고 실제 발행")
    parser.add_argument("--dry-run", action="store_true", help="강제로 dry_run 으로 실행")
    parser.add_argument("--approve", action="store_true", help="발행 직전에 멈춰 사람 확인을 받는다")
    parser.add_argument("--resume", metavar="RUN_ID", help="중단된 실행을 이어서 재개")
    parser.add_argument("--stage", choices=STAGES, help="이 단계까지만 실행")
    parser.add_argument("--provider", choices=["anthropic", "openai", "mock"], help="LLM 백엔드 임시 지정")
    parser.add_argument("--max-articles", type=int, help="선별 상한 임시 지정")
    parser.add_argument("--no-history", action="store_true", help="발행 이력 확인·기록을 끈다")
    parser.add_argument("--schedule", action="store_true", help="스케줄러 설정에 맞춰 주기적으로 자동 실행")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    try:
        cfg = load_config(args.config)
        if args.provider:
            cfg["llm"]["provider"] = args.provider
        if args.send:
            cfg["publish"]["dry_run"] = False
        if args.dry_run:
            cfg["publish"]["dry_run"] = True
        if args.max_articles:
            cfg["curate"]["max_articles"] = args.max_articles
        if args.no_history:
            cfg["history"]["enabled"] = False
        warnings = validate_config(cfg)
    except ConfigError as exc:
        print(f"[설정 오류] {exc}", file=sys.stderr)
        return 2

    enabled = [s for s in cfg["sources"] if s.get("enabled", True)]
    print(f"설정 OK — 소스 {len(enabled)}개 / 관심사 {len(cfg['curate']['interests'])}개 / "
          f"LLM {cfg['llm']['provider']} / dry_run {cfg['publish']['dry_run']}")
    for warning in warnings:
        print(f"  · {warning}")

    if args.check:
        print("\n활성 소스:")
        for src in enabled:
            role = src.get("role", "content")
            tag = "" if role == "content" else "  [증인 전용]"
            print(f"  [{src['type']:<5}] {src['name']}{tag}")
        print("\n관심사:")
        for interest in cfg["curate"]["interests"]:
            print(f"  - {interest['name']} (키워드 {len(interest.get('keywords', []))}개)")
        return 0

    if args.schedule:
        import time
        from datetime import datetime
        from newsagent.scheduler import NewsScheduler

        def run_scheduled():
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{now_str}] ⏰ 예약된 자동 실행 시작...")
            try:
                run_cfg = load_config(args.config)
                if args.send:
                    run_cfg["publish"]["dry_run"] = False
                pipeline = build_pipeline(run_cfg)
                try:
                    s_state = pipeline.graph.invoke(initial_state(), {})
                    s_state.setdefault("stats", {})["llm"] = pipeline.llm.usage
                    print_summary(s_state)
                finally:
                    pipeline.close()
            except Exception as exc:  # noqa: BLE001
                print(f"[스케줄 실행 실패] {exc}", file=sys.stderr)

        sch_cfg = dict(cfg.get("schedule", {}))
        sch_cfg["enabled"] = True
        scheduler = NewsScheduler(callback=run_scheduled)
        scheduler.update_config(sch_cfg)
        scheduler.start()

        st = scheduler.get_status()
        print(f"\n뉴스레터 스케줄러 실행 중 (모드: {st['mode']}, 다음 실행: {st['next_run']})")
        print("종료하려면 Ctrl+C 를 누르세요.\n")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            scheduler.stop()
            print("\n스케줄러를 종료했습니다.")
        return 0

    if args.stage:
        state = run_until(cfg, args.stage)
        if args.stage in ("collect", "curate") and args.verbose:
            key = "raw_items" if args.stage == "collect" else "selected"
            print(json.dumps([a.to_dict() for a in state.get(key, [])][:5], ensure_ascii=False, indent=2))
        print_summary(state)
        return 0

    # 체크포인터는 재개(--resume)와 발행 승인(--approve)의 전제 조건이다
    runtime_cfg = cfg.get("runtime", {})
    need_checkpoint = bool(runtime_cfg.get("checkpoint", True)) or args.approve or args.resume
    checkpointer = build_checkpointer(runtime_cfg.get("checkpoint_path", "state/checkpoints.db")) \
        if need_checkpoint else None
    if (args.approve or args.resume) and checkpointer is None:
        print("[오류] --approve / --resume 에는 체크포인터가 필요합니다 (runtime.checkpoint: true).",
              file=sys.stderr)
        return 2

    pipeline = build_pipeline(cfg, checkpointer=checkpointer, approve_before_publish=args.approve)
    try:
        start_state = initial_state()
        thread_id = args.resume or start_state["run_id"]
        config = {"configurable": {"thread_id": thread_id}} if checkpointer else {}

        if args.resume:
            print(f"run_id={thread_id} 실행을 재개합니다.")
            state = pipeline.graph.invoke(None, config)
        else:
            state = pipeline.graph.invoke(start_state, config)

        # --approve 는 publish 직전에 멈춘다. 아직 발행되지 않은 상태다.
        if pipeline.interrupts_before_publish and not state.get("publish_result"):
            preview_for_approval(state)
            answer = input("발행할까요? [y/N] ").strip().lower()
            if answer in ("y", "yes"):
                state = pipeline.graph.invoke(None, config)
            else:
                print(f"발행하지 않았습니다. 나중에 재개하려면: run.py --resume {thread_id}")
                state.setdefault("stats", {})["llm"] = pipeline.llm.usage
                print_summary(state)
                return 0

        state.setdefault("stats", {})["llm"] = pipeline.llm.usage
        print_summary(state)
    finally:
        pipeline.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
