"""agent 的持久化状态：采集 backlog + 人工复核队列。

按任务要求用 JSON 文件而非新建数据库表 —— 简单、可读、可手改、易接定时调度。
三个文件都放在本目录的 state/ 子目录下：

  state/backlog.json       采集待办清单（源 backlog）
  state/review_queue.json  人工复核队列（LLM 判定为 uncertain 的候选）
  state/run_log.json       每轮 run 的执行记录（审计 / 调试用）
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path(__file__).resolve().parent / "state"
BACKLOG_PATH = STATE_DIR / "backlog.json"
REVIEW_QUEUE_PATH = STATE_DIR / "review_queue.json"
RUN_LOG_PATH = STATE_DIR / "run_log.json"


def _now() -> str:
    """UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 采集 backlog ────────────────────────────────────────────────
# backlog 每项结构：
#   {
#     "id": "voa-daily",
#     "connector": "voa",            # connectors.REGISTRY 的 key
#     "args": {"--limit": "3"},      # 透传给连接器的 CLI 参数
#     "priority": 10,                # 数字越大越先采
#     "status": "pending",           # pending | done | failed
#     "note": "VOA 每日文章",
#     "last_run": null,              # 上次采集时间
#     "last_result": null            # 上次结果摘要
#   }

# 首批默认 backlog：覆盖几个安全的公有领域源。文件不存在时用它初始化。
_DEFAULT_BACKLOG: list[dict] = [
    {
        "id": "nasa-science",
        "connector": "nasa",
        "args": {"--query": "ScienceCasts,explained", "--limit": "3"},
        "priority": 20,
        "status": "pending",
        "note": "NASA 科普短片（公有领域，最安全的起步源）",
        "last_run": None,
        "last_result": None,
    },
    {
        "id": "voa-daily",
        "connector": "voa",
        "args": {"--limit": "3"},
        "priority": 18,
        "status": "pending",
        "note": "VOA Learning English 文章（公有领域，自带分级）",
        "last_run": None,
        "last_result": None,
    },
    {
        "id": "mit-ocw-lectures",
        "connector": "mit_ocw",
        "args": {"--limit": "3"},
        "priority": 12,
        "status": "pending",
        "note": "MIT OCW 讲座（CC BY-NC-SA）",
        "last_run": None,
        "last_result": None,
    },
    {
        "id": "ia-academic-films",
        "connector": "internet_archive",
        "args": {"--collection": "academic_films", "--limit": "3"},
        "priority": 6,
        "status": "pending",
        "note": "Internet Archive 学术影片（版权混杂，需 LLM 合规复核）",
        "last_run": None,
        "last_result": None,
    },
]


def load_backlog() -> list[dict]:
    """读 backlog；文件不存在则用默认 backlog 初始化并落盘。"""
    if not BACKLOG_PATH.exists():
        _save(BACKLOG_PATH, _DEFAULT_BACKLOG)
        return list(_DEFAULT_BACKLOG)
    return _load(BACKLOG_PATH, [])


def save_backlog(backlog: list[dict]) -> None:
    _save(BACKLOG_PATH, backlog)


def pick_next(backlog: list[dict]) -> dict | None:
    """从 backlog 选下一个要采的源：status=pending 中 priority 最高的。"""
    pending = [item for item in backlog if item.get("status") == "pending"]
    if not pending:
        return None
    return max(pending, key=lambda x: x.get("priority", 0))


def mark_done(backlog: list[dict], item_id: str, result_summary: dict) -> None:
    """把 backlog 某项标记为已采集，并回写本轮结果摘要。"""
    for item in backlog:
        if item.get("id") == item_id:
            item["status"] = "done"
            item["last_run"] = _now()
            item["last_result"] = result_summary
            break
    save_backlog(backlog)


def mark_failed(backlog: list[dict], item_id: str, error: str) -> None:
    for item in backlog:
        if item.get("id") == item_id:
            item["status"] = "failed"
            item["last_run"] = _now()
            item["last_result"] = {"error": error}
            break
    save_backlog(backlog)


# ── 人工复核队列 ────────────────────────────────────────────────
def load_review_queue() -> list[dict]:
    return _load(REVIEW_QUEUE_PATH, [])


def add_to_review_queue(entry: dict) -> None:
    """把一个 uncertain 候选追加进人工复核队列。

    entry 至少含：candidate（候选原始数据）、connector、reasons（LLM 判词）。
    """
    queue = load_review_queue()
    entry = {**entry, "queued_at": _now(), "review_status": "pending"}
    queue.append(entry)
    _save(REVIEW_QUEUE_PATH, queue)


# ── run 执行日志 ────────────────────────────────────────────────
def append_run_log(record: dict) -> None:
    """追加一条 run 记录（保留最近 50 条，防文件无限膨胀）。"""
    log = _load(RUN_LOG_PATH, [])
    log.append({**record, "logged_at": _now()})
    _save(RUN_LOG_PATH, log[-50:])
