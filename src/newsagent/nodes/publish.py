"""5단계 발행 노드 (PRD 3.5).

dry_run 이면 네트워크를 타지 않고 콘솔과 파일로만 낸다. 아카이브는 항상 남긴다 (R5-4).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import VERDICT_EMOJI, VERDICT_LABEL, VerifiedBrief
from ..publishers import CHANNEL_LABEL, build_publisher, configured_targets
from ..publishers.discord import build_header
from ..state import NewsletterState
from ..store import PublishedStore
from .verify import event_text

log = logging.getLogger(__name__)


def render_markdown(items: list[VerifiedBrief], meta: dict[str, Any]) -> str:
    lines = [f"# {meta.get('title', '뉴스 브리핑')} — {meta.get('date', '')}", ""]
    stats = meta.get("stats", {})
    lines.append(
        f"수집 {stats.get('collect', {}).get('unique', 0)}건 → "
        f"선별 {stats.get('curate', {}).get('selected', 0)}건 → 발행 {len(items)}건"
    )
    lines.append("")

    for index, item in enumerate(items, 1):
        brief = item.brief
        article = brief.article
        emoji = VERDICT_EMOJI.get(item.verdict, "")
        label = VERDICT_LABEL.get(item.verdict, item.verdict)

        lines.append(f"## {index}. {brief.headline}")
        lines.append("")
        lines.append(f"{emoji} **{label}** · 신뢰도 `{item.confidence:.2f}` · 교차 출처 {len(item.corroborating_sources)}곳")
        if item.corroborating_sources:
            lines.append(f"　근거 도메인: {', '.join(item.corroborating_sources)}")
        lines.append("")
        for bullet in brief.summary:
            lines.append(f"- {bullet}")
        if brief.why_it_matters:
            lines.append("")
            lines.append(f"> **왜 중요한가** {brief.why_it_matters}")
        if item.fact_checks:
            lines.append("")
            lines.append("<details><summary>사실 확인 내역</summary>")
            lines.append("")
            for check in item.fact_checks:
                mark = {"supported": "✅", "contradicted": "❌", "unverified": "❔"}.get(check.status, "❔")
                evidence = f" — {check.evidence}" if check.evidence else ""
                lines.append(f"- {mark} {check.fact}{evidence}")
            lines.append("")
            lines.append("</details>")
        if article:
            lines.append("")
            lines.append(f"원문: [{article.source}]({article.url})")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def _serialize_state(state: NewsletterState, items: list[VerifiedBrief]) -> dict[str, Any]:
    return {
        "run_id": state.get("run_id"),
        "started_at": state.get("started_at"),
        "stats": state.get("stats", {}),
        "errors": state.get("errors", []),
        "selected": [a.to_dict() for a in state.get("selected", [])],
        "verified": [v.to_dict() for v in items],
        "raw_item_count": len(state.get("raw_items", [])),
    }


def make_publish_node(cfg: dict[str, Any], store: PublishedStore | None = None):
    publish_cfg = cfg["publish"]
    output_cfg = cfg["output"]
    history_cfg = cfg.get("history", {})
    include = set(publish_cfg.get("include_verdicts", []))

    def record_history(items: list[VerifiedBrief], run_id: str, *, was_dry_run: bool) -> int:
        """발행 이력을 남긴다.

        dry_run 은 기본적으로 기록하지 않는다. 연습 실행이 이력을 남기면
        정작 진짜 발행할 때 전부 '이미 발행함'으로 걸러져 빈 뉴스레터가 나간다.
        """
        if store is None or not store.enabled or not history_cfg.get("enabled", True):
            return 0
        if was_dry_run and not history_cfg.get("record_on_dry_run", False):
            return 0
        rows = [
            {
                "article_id": item.brief.article_id,
                "url": item.brief.article.url if item.brief.article else "",
                "headline": item.brief.headline,
                "event_text": event_text(item.brief.article) if item.brief.article else item.brief.headline,
                "domain": item.brief.article.domain if item.brief.article else "",
                "verdict": item.verdict,
                "confidence": item.confidence,
            }
            for item in items
        ]
        recorded = store.record(rows, run_id)
        store.prune(int(history_cfg.get("keep_days", 90)))
        return recorded

    def publish_node(state: NewsletterState) -> dict[str, Any]:
        started = time.monotonic()
        verified: list[VerifiedBrief] = state.get("verified", [])
        items = [v for v in verified if not include or v.verdict in include]
        errors: list[str] = []

        meta = {
            "title": publish_cfg.get("title", "뉴스 브리핑"),
            "date": datetime.now().strftime("%Y-%m-%d"),
            "stats": state.get("stats", {}),
        }

        out_dir = Path(output_cfg["dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        run_id = state.get("run_id", datetime.now().strftime("%Y%m%d_%H%M%S"))
        written: list[str] = []

        markdown = render_markdown(items, meta)
        if output_cfg.get("save_markdown", True):
            md_path = out_dir / f"{run_id}.md"
            md_path.write_text(markdown, encoding="utf-8")
            written.append(str(md_path))
        if output_cfg.get("save_json", True):
            json_path = out_dir / f"{run_id}.json"
            json_path.write_text(
                json.dumps(_serialize_state(state, items), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written.append(str(json_path))

        if not items:
            log.warning("발행할 기사가 없습니다.")
            result = {"target": "none", "sent": 0, "dry_run": publish_cfg["dry_run"], "files": written}
            return {
                "publish_result": result,
                "stats": {"publish": {"published": 0, "files": written, "elapsed_sec": 0.0}},
            }

        if publish_cfg["dry_run"]:
            # R5-3: 네트워크 없이 결과만 보여준다
            print("\n" + "=" * 70)
            print("[DRY RUN] 디스코드로 전송하지 않았습니다. 아래가 전송될 내용입니다.")
            print("=" * 70)
            print(build_header(items, meta))
            print()
            for item in items:
                emoji = VERDICT_EMOJI.get(item.verdict, "")
                print(f"{emoji} [{VERDICT_LABEL.get(item.verdict, item.verdict)} {item.confidence:.2f}] {item.brief.headline}")
                for bullet in item.brief.summary[:3]:
                    print(f"    • {bullet}")
                if item.corroborating_sources:
                    print(f"    ↳ 교차 출처: {', '.join(item.corroborating_sources)}")
                if item.brief.article:
                    print(f"    ↳ {item.brief.article.url}")
                print()
            channels = ", ".join(CHANNEL_LABEL.get(t, t) for t in configured_targets(publish_cfg))
            print(f"전송 대상이었을 채널: {channels or '(없음)'}")
            print("=" * 70)
            result = {"target": "dry_run", "sent": len(items), "dry_run": True, "files": written}
            recorded = record_history(items, run_id, was_dry_run=True)
        else:
            # 채널 하나가 실패해도 나머지는 계속 보낸다
            targets = configured_targets(publish_cfg)
            channel_results, delivered = [], []
            for channel in targets:
                try:
                    outcome = build_publisher(channel, publish_cfg).publish(items, meta)
                    channel_results.append(outcome)
                    delivered.append(channel)
                except Exception as exc:
                    errors.append(f"[publish:{channel}] {type(exc).__name__}: {exc}")
                    log.error("%s 발행 실패: %s", CHANNEL_LABEL.get(channel, channel), exc)
                    channel_results.append({"target": channel, "sent": 0, "error": str(exc)})
            result = {
                "targets": channel_results,
                "delivered": delivered,
                "sent": len(items) if delivered else 0,
                "dry_run": False,
                "files": written,
            }
            # 한 곳이라도 실제로 나갔을 때만 이력을 남긴다
            recorded = record_history(items, run_id, was_dry_run=False) if delivered else 0

        elapsed = time.monotonic() - started
        if publish_cfg["dry_run"]:
            log.info("5단계 발행 완료: %d건 (연습 실행, %.1fs)", len(items), elapsed)
        elif result.get("delivered"):
            log.info(
                "5단계 발행 완료: %d건 → %s (%.1fs)",
                len(items),
                ", ".join(CHANNEL_LABEL.get(c, c) for c in result["delivered"]),
                elapsed,
            )
        else:
            # 전송이 전부 실패했는데 '완료' 로만 찍으면 성공한 줄 안다
            log.error("5단계 발행 실패: %d건을 어느 채널에도 보내지 못했습니다 (%.1fs)", len(items), elapsed)

        return {
            "publish_result": result,
            "errors": errors,
            "stats": {
                "publish": {
                    "published": len(items),
                    "filtered_out": len(verified) - len(items),
                    "history_recorded": recorded,
                    "targets": configured_targets(publish_cfg),
                    "delivered": result.get("delivered", []),
                    "dry_run": publish_cfg["dry_run"],
                    "files": written,
                    "elapsed_sec": round(elapsed, 2),
                }
            },
        }

    return publish_node
