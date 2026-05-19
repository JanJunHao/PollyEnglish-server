"""sourcing agent 主编排器 —— 阶段 2（Q3）。

一轮 `run` 做的事：
  1. 从 backlog 选优先级最高的待办源（pick_next）。
  2. 调对应连接器（subprocess），抓回若干候选条目。
  3. 对每条候选跑 LLM 合规判定：
        deny      → 丢弃
        uncertain → 进人工复核队列，不入库
        allow     → 进入质量评审
  4. 对 allow 的候选跑 LLM 质量评审：
        reject → 丢弃
        review → 进人工复核队列，不入库
        pass   → 收集起来准备入库
  5. 把 pass 的候选写成一个 fetch JSON，subprocess 调 ingest.py --from-fetch 入库；
     入库后 ingest 内部的 Q1 打分器会自动打分。
  6. 更新 backlog 状态、写 run_log。

编排 = agent「动脑」决定调哪个连接器、传什么参数；连接器与 ingest 一律当黑盒 CLI。

定时调度：本模块提供 `run_once()` 纯函数 + `main()` CLI。接 cron / launchd 时
只需周期性执行 `python -m scripts.sourcing_agent.agent run` 即可，无需改代码。
详见同目录 README.md。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from scripts.sourcing_agent import connectors, judge, state

# 本轮临时文件落到 state/tmp/ 下
_TMP_DIR = state.STATE_DIR / "tmp"


def _ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _ingest(fetch_path: Path) -> tuple[bool, str]:
    """subprocess 调 ingest.py --from-fetch 入库。返回 (成功?, 日志尾部)。"""
    cmd = [
        sys.executable, "-m", "scripts.ingest",
        "--from-fetch", str(fetch_path),
    ]
    print(f"  [ingest] {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(
        cmd,
        cwd=str(connectors.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800.0,  # 入库含字幕生成 / Q1 打分，给足时间
    )
    log_tail = (result.stdout + result.stderr).strip()
    for ln in log_tail.splitlines()[-20:]:
        print(f"    {ln}", file=sys.stderr)
    return result.returncode == 0, log_tail[-2000:]


def run_once(*, dry_run: bool = False) -> dict:
    """跑一轮发现 + 采集。返回本轮结果摘要 dict（也写进 run_log）。

    参数 dry_run=True 时只跑发现 + LLM 判断，不真正调 ingest 入库（调试用）。
    """
    print("=" * 60, file=sys.stderr)
    print(f"sourcing agent run @ {_ts_slug()}  dry_run={dry_run}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    backlog = state.load_backlog()
    item = state.pick_next(backlog)
    if item is None:
        print("backlog 无 pending 项 —— 无事可做。", file=sys.stderr)
        summary = {"status": "idle", "reason": "backlog 无 pending 项"}
        state.append_run_log(summary)
        return summary

    conn_key = item["connector"]
    print(f"选中 backlog 项：{item['id']}（连接器={conn_key}，优先级={item.get('priority')}）",
          file=sys.stderr)

    try:
        conn = connectors.get(conn_key)
    except KeyError as exc:
        state.mark_failed(backlog, item["id"], str(exc))
        summary = {"status": "failed", "item": item["id"], "error": str(exc)}
        state.append_run_log(summary)
        return summary

    # ── 步骤 2：调连接器抓候选 ──────────────────────────────
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    raw_out = _TMP_DIR / f"{item['id']}-{_ts_slug()}-raw.json"
    try:
        candidates = connectors.run_connector(conn, item.get("args", {}), raw_out)
    except Exception as exc:  # noqa: BLE001
        print(f"连接器执行失败：{exc}", file=sys.stderr)
        state.mark_failed(backlog, item["id"], f"连接器失败：{exc}")
        summary = {"status": "failed", "item": item["id"], "error": str(exc)}
        state.append_run_log(summary)
        return summary

    print(f"连接器产出 {len(candidates)} 条候选，开始逐条 LLM 判断…", file=sys.stderr)

    # ── 步骤 3+4：逐条做 LLM 合规 + 质量判断 ─────────────────
    approved: list[dict] = []      # 通过双重判断、准备入库
    review_count = 0               # 进人工复核队列的数量
    denied_count = 0               # 被丢弃的数量
    decisions: list[dict] = []     # 每条候选的判定明细，写进 run_log

    for cand in candidates:
        title = str(cand.get("title") or "(untitled)")

        # 3) 合规判定
        comp = judge.judge_compliance(
            connector_key=conn_key,
            license_note=conn.license_note,
            title=title,
            source=str(cand.get("source") or conn.display_name),
            description=str(cand.get("description") or ""),
            attribution=str(cand.get("attribution") or ""),
        )
        rec = {"title": title, "compliance": comp.decision, "compliance_reason": comp.reason}

        if comp.decision == "deny":
            denied_count += 1
            rec["outcome"] = "discarded"
            decisions.append(rec)
            print(f"  ✗ deny  «{title[:50]}» — {comp.reason}", file=sys.stderr)
            continue

        if comp.decision == "uncertain":
            state.add_to_review_queue({
                "candidate": cand,
                "connector": conn_key,
                "stage": "compliance",
                "reasons": [comp.reason],
            })
            review_count += 1
            rec["outcome"] = "review_queue"
            decisions.append(rec)
            print(f"  ? uncertain «{title[:50]}» — 合规存疑，转人工：{comp.reason}",
                  file=sys.stderr)
            continue

        # 4) 质量评审（仅 allow 的候选进这步）
        qual = judge.judge_quality(cand)
        rec["quality"] = qual.verdict
        rec["quality_score"] = qual.score
        rec["quality_reason"] = qual.reason

        if qual.verdict == "reject":
            denied_count += 1
            rec["outcome"] = "discarded"
            decisions.append(rec)
            print(f"  ✗ reject «{title[:50]}» — 质量不达标：{qual.reason}", file=sys.stderr)
            continue

        if qual.verdict == "review":
            state.add_to_review_queue({
                "candidate": cand,
                "connector": conn_key,
                "stage": "quality",
                "reasons": [qual.reason],
            })
            review_count += 1
            rec["outcome"] = "review_queue"
            decisions.append(rec)
            print(f"  ? review «{title[:50]}» — 质量存疑，转人工：{qual.reason}",
                  file=sys.stderr)
            continue

        # pass：通过合规 + 质量双重判断
        approved.append(cand)
        rec["outcome"] = "approved"
        decisions.append(rec)
        print(f"  ✓ pass  «{title[:50]}» — 准备入库（质量分 {qual.score:.2f}）",
              file=sys.stderr)

    # ── 步骤 5：把 approved 候选入库 ─────────────────────────
    ingested = 0
    ingest_ok = True
    if approved and not dry_run:
        fetch_path = _TMP_DIR / f"{item['id']}-{_ts_slug()}-approved.json"
        fetch_path.write_text(
            json.dumps(approved, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ingest_ok, _ = _ingest(fetch_path)
        ingested = len(approved) if ingest_ok else 0
        if not ingest_ok:
            print("  [ingest] 入库失败 —— 详见上方日志", file=sys.stderr)
    elif approved and dry_run:
        print(f"  [dry-run] 跳过入库，{len(approved)} 条 approved 未真正写库",
              file=sys.stderr)

    # ── 步骤 6：更新 backlog + 写 run_log ───────────────────
    result_summary = {
        "candidates": len(candidates),
        "approved": len(approved),
        "ingested": ingested,
        "review_queue": review_count,
        "discarded": denied_count,
        "ingest_ok": ingest_ok,
        "dry_run": dry_run,
    }
    if dry_run:
        # dry-run 不消耗 backlog，下次 run 仍能选到它
        print("  [dry-run] backlog 项保持 pending（未标记 done）", file=sys.stderr)
    elif ingest_ok:
        state.mark_done(backlog, item["id"], result_summary)
    else:
        state.mark_failed(backlog, item["id"], "ingest 入库失败")

    summary = {
        "status": "ok",
        "item": item["id"],
        "connector": conn_key,
        **result_summary,
        "decisions": decisions,
    }
    state.append_run_log(summary)

    print("-" * 60, file=sys.stderr)
    print(f"本轮完成：候选 {len(candidates)} → 入库 {ingested} / "
          f"复核队列 {review_count} / 丢弃 {denied_count}", file=sys.stderr)
    print("-" * 60, file=sys.stderr)
    return summary


def _cmd_run(args: argparse.Namespace) -> None:
    summary = run_once(dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def _cmd_backlog(_: argparse.Namespace) -> None:
    """打印当前 backlog 状态。"""
    for item in state.load_backlog():
        print(f"  [{item.get('status'):8}] {item['id']:24} "
              f"connector={item['connector']:18} priority={item.get('priority')}")


def _cmd_review_queue(_: argparse.Namespace) -> None:
    """打印人工复核队列。"""
    queue = state.load_review_queue()
    if not queue:
        print("人工复核队列为空。")
        return
    for i, entry in enumerate(queue, 1):
        cand = entry.get("candidate", {})
        print(f"  {i}. [{entry.get('review_status')}] «{cand.get('title', '?')[:50]}»")
        print(f"     连接器={entry.get('connector')}  阶段={entry.get('stage')}")
        print(f"     理由：{'; '.join(entry.get('reasons', []))}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.sourcing_agent.agent",
        description="Polly 内容采集 sourcing agent（阶段 2 — Q3）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="跑一轮发现 + 采集（定时调度的入口）")
    p_run.add_argument("--dry-run", action="store_true",
                       help="只跑发现 + LLM 判断，不真正入库")
    p_run.set_defaults(func=_cmd_run)

    sub.add_parser("backlog", help="查看采集 backlog 状态").set_defaults(func=_cmd_backlog)
    sub.add_parser("review-queue", help="查看人工复核队列").set_defaults(func=_cmd_review_queue)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
