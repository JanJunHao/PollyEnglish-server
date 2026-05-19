# sourcing agent —— Polly 内容采集 agent（阶段 2 / Q3）

LLM 驱动的内容采集 agent。它**包住**阶段 0/1 已有的确定性平台连接器
（`scripts/fetch_*.py`），负责「动脑判断」的部分：决定采哪个源、判断模糊版权是否合规、
对候选做质量评审、把不确定的转人工。

> 概念区分（务必理解）：
> - **连接器 / pipeline**：确定性脚本，规则写死，不变通。已存在，本目录不修改它们。
> - **sourcing agent**：LLM 驱动的程序，做判断与编排。它把连接器当**工具**调用，不替换。
> - 关系：**agent 包住 pipeline**。

## 架构

```
        ┌──────────────────── agent.py（编排器 / run 入口）────────────────────┐
        │                                                                      │
  state.py            connectors.py                  judge.py                  │
  backlog/复核队列     连接器登记表 + subprocess       LLM 合规判定 + 质量评审    │
        │                    │                            │                    │
        ▼                    ▼                            ▼                    │
  ① 选 backlog 项 ──▶ ② 调连接器 CLI 抓候选 ──▶ ③ LLM 合规 ──▶ ④ LLM 质量 ──┐  │
                          (fetch_*.py)            allow/        pass/review/  │  │
                                                  deny/         reject        │  │
                                                  uncertain                   │  │
        ┌─────────────────────────────────────────────────────────────────────┘  │
        ▼                                                                         │
  ⑤ pass 的候选 ──▶ ingest.py --from-fetch 入库 ──▶ Q1 打分器自动打分            │
  ⑥ 更新 backlog 状态 + 写 run_log ───────────────────────────────────────────────┘

  deny      → 丢弃
  uncertain → 人工复核队列（review_queue.json），不入库
  review    → 人工复核队列，不入库
```

### 文件

| 文件 | 职责 |
|---|---|
| `agent.py` | 主编排器；`run` / `backlog` / `review-queue` 三个 CLI 子命令 |
| `connectors.py` | 连接器登记表；以 subprocess 跑连接器 CLI、读回 fetch JSON |
| `judge.py` | LLM 判断：模糊版权合规判定 + 候选质量评审（复用 `app/config.py` OpenAI 配置） |
| `state.py` | JSON 持久化：采集 backlog、人工复核队列、run 日志 |
| `state/` | 运行时生成：`backlog.json` / `review_queue.json` / `run_log.json` / `tmp/` |

### 合规设计

- **硬规则禁区**：来源/标题/描述命中 `bbc / ted talk / ted-ed / cnn / netflix` → 直接 `deny`，不问 LLM。
- **LLM 合规判定**：公有领域（NASA/VOA 等政府出版物）/ 明确 CC → `allow`；版权模糊 → `uncertain`。
- **保守优先**：LLM 拿不准、返回异常一律降级为 `uncertain`（合规）/ `review`（质量），绝不乐观放行。
- 连接器登记表中 `fetch_ted_channel.py` **故意不登记**——TED 主频道属禁区。

## 运行

工程根目录：`polly-server/`。Python 一律用 `.venv/bin/python`。

```bash
# 跑一轮发现 + 采集（定时调度的入口）
.venv/bin/python -m scripts.sourcing_agent.agent run

# 只跑发现 + LLM 判断，不真正入库（调试用）
.venv/bin/python -m scripts.sourcing_agent.agent run --dry-run

# 查看采集 backlog 状态
.venv/bin/python -m scripts.sourcing_agent.agent backlog

# 查看人工复核队列
.venv/bin/python -m scripts.sourcing_agent.agent review-queue
```

一轮 `run` 只处理 backlog 中优先级最高的一个待办源（采完标 `done`）。
要采下一个源，再跑一次 `run` 即可——天然适合定时反复触发。

## 采集 backlog

`state/backlog.json`，可手改。每项：

```json
{
  "id": "nasa-science",
  "connector": "nasa",
  "args": {"--query": "ScienceCasts", "--limit": "3"},
  "priority": 20,
  "status": "pending",
  "note": "说明文字"
}
```

- `connector`：必须是 `connectors.REGISTRY` 的 key（`nasa` / `voa` / `mit_ocw` / `internet_archive`）。
- `args`：透传给连接器 CLI 的参数；只有在该连接器 `allowed_args` 白名单内的才生效。
- `priority`：数字越大越先采。
- `status`：`pending` 待采 / `done` 已采 / `failed` 失败。
- 首次运行若文件不存在，会用内置默认 backlog 初始化。

## 人工复核队列

`state/review_queue.json`。LLM 判为 `uncertain`（合规存疑）或 `review`（质量存疑）的候选
**不自动入库**，写进此文件。每项含候选原始数据、连接器、判定阶段、LLM 理由。
人工审过后可手动把合格条目整理成 fetch JSON 交 `ingest.py` 入库。

## 接定时调度

agent 已设计为「跑一轮即退出」，接 cron / launchd 只需周期性执行 `run`，无需改代码。

cron（每天 03:00 跑一轮）：

```cron
0 3 * * * cd /path/to/polly-server && .venv/bin/python -m scripts.sourcing_agent.agent run >> /var/log/polly-sourcing.log 2>&1
```

macOS launchd 同理（`launchd` plist 里 `ProgramArguments` 指向同一条命令）。
backlog 全部 `done` 后 `run` 会安静地空转返回 `status: idle`，可放心高频触发。

## 验证

阶段 2 出口验证：跑一轮 `run`，agent 从 backlog 取一个安全公有领域源（NASA），
调连接器抓 3 条候选 → LLM 合规判定（3 条均 `allow`，公有领域）→ LLM 质量评审
（1 条 `pass`、2 条 `review` 进复核队列）→ `pass` 候选交 `ingest.py` 入库并经 Q1 打分。
`GET /v1/contents/latest` 返回正常、无回归。
