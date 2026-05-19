"""连接器登记表（connector registry）。

把现有的确定性平台连接器（scripts/fetch_*.py）登记成 agent 可调用的「工具」。
每个连接器是一段黑盒 CLI：agent 决定调哪个、传什么参数，本模块只负责
「以 subprocess 方式把它跑起来 → 读回它产的 fetch JSON」。

⚠️ 本目录的代码绝不修改连接器本身。连接器一律当黑盒 CLI 调用。

连接器统一约定（阶段 0/1 已成形）：
  - 都是 `python -m scripts.fetch_xxx ...` 形式的模块入口；
  - 都接受 `--out <path>` 指定 fetch JSON 输出文件；
  - 产出的 JSON 是「候选条目数组」，能直接喂给 `ingest.py --from-fetch`。
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

# polly-server 工程根目录（scripts/sourcing_agent/connectors.py → 上溯两级）
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Connector:
    """一个平台连接器的元数据 + 调用方式。"""

    key: str               # 唯一标识，backlog 里用它指定源
    module: str            # `python -m <module>` 的模块路径
    display_name: str      # 人类可读名
    license_note: str      # 版权性质，给 LLM 合规判定当输入
    safe: bool             # True = 公有领域 / 明确 CC，可直接采；False = 需 LLM 合规复核
    # 该连接器接受的、可由 backlog 透传的命令行参数白名单
    allowed_args: list[str] = field(default_factory=list)


# ── 连接器登记表 ────────────────────────────────────────────────
# 只登记阶段 0/1 已落地、且不在合规禁区内的连接器。
# 禁区（BBC / TED 主频道 / CNN / Netflix）的连接器不在此登记 —— fetch_ted_channel.py
# 虽存在，但 TED 主频道属禁区，故 *不* 登记为 agent 工具。
REGISTRY: dict[str, Connector] = {
    "nasa": Connector(
        key="nasa",
        module="scripts.fetch_nasa",
        display_name="NASA Images and Video Library",
        license_note="美国政府作品，公有领域（17 U.S.C. §105），合规零风险。",
        safe=True,
        allowed_args=["--query", "--limit"],
    ),
    "voa": Connector(
        key="voa",
        module="scripts.fetch_voa_learning_english",
        display_name="VOA Learning English 官网",
        license_note="美国政府出版物，公有领域（17 U.S.C. §105），自带分级与逐字稿。",
        safe=True,
        allowed_args=["--limit", "--url"],
    ),
    "mit_ocw": Connector(
        key="mit_ocw",
        module="scripts.fetch_mit_ocw",
        display_name="MIT OpenCourseWare",
        license_note="MIT OCW 采用 CC BY-NC-SA 授权；非商用 + 署名 + 同源协议。",
        safe=True,
        allowed_args=["--limit", "--offset"],
    ),
    "internet_archive": Connector(
        key="internet_archive",
        module="scripts.fetch_internet_archive",
        display_name="Internet Archive 公有领域收藏",
        # IA 收藏混杂：脚本内有白名单，但 agent 仍应对其产出做合规复核。
        license_note="Internet Archive 收藏版权混杂；连接器内置 PD 收藏白名单，"
                     "但单条仍可能版权模糊，需 LLM 复核。",
        safe=False,
        allowed_args=["--collection", "--limit"],
    ),
}


def get(key: str) -> Connector:
    """按 key 取连接器；未登记直接报错（防 backlog 写错源名）。"""
    if key not in REGISTRY:
        raise KeyError(f"未登记的连接器：{key}（可用：{sorted(REGISTRY)}）")
    return REGISTRY[key]


def run_connector(
    conn: Connector,
    args: dict[str, str],
    out_path: Path,
    timeout: float = 600.0,
) -> list[dict]:
    """以 subprocess 跑一个连接器 CLI，读回它产的 fetch JSON。

    参数:
      conn:     连接器元数据
      args:     backlog 透传的命令行参数（如 {"--query": "Mars", "--limit": "3"}）；
                只有在 conn.allowed_args 白名单内的键才会被透传。
      out_path: 连接器 --out 输出路径。
      timeout:  子进程超时（秒）。

    返回: 候选条目数组（连接器产的 fetch JSON 解析结果）。
    """
    cmd = [sys.executable, "-m", conn.module, "--out", str(out_path)]
    for k, v in args.items():
        if k not in conn.allowed_args:
            print(f"  [connector] 跳过未授权参数 {k}（{conn.key} 仅允许 {conn.allowed_args}）",
                  file=sys.stderr)
            continue
        cmd.extend([k, str(v)])

    print(f"  [connector] 调用 {conn.key}: {' '.join(cmd)}", file=sys.stderr)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # 连接器的进度日志走 stderr，这里直通给用户看；stdout 留给可能的数据。
    result = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        timeout=timeout,
        capture_output=True,
        text=True,
    )
    if result.stderr:
        # 连接器日志逐行转发，加缩进区分层级
        for ln in result.stderr.rstrip().splitlines():
            print(f"    {ln}", file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"连接器 {conn.key} 退出码 {result.returncode}\n{result.stdout}"
        )
    if not out_path.exists():
        raise RuntimeError(f"连接器 {conn.key} 未产出 {out_path}")
    return json.loads(out_path.read_text(encoding="utf-8"))
