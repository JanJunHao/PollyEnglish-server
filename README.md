# polly-server

Polly 后端 MVP — 内容 manifest API + OpenAI 代理。

对应 plan：`/Users/macmini/.claude/plans/demo-virtual-cookie.md`，"数据架构与合规演进" + "AI 自动化打标流水线" 章节。

## 端点

| Method | Path | 作用 |
|---|---|---|
| GET | `/healthz` | 探活 + DB 连通性检查 |
| GET | `/v1/contents/latest?since=<iso8601>` | 增量拉取内容 manifest，iOS 首页用 |
| POST | `/v1/ai/explain` | AI 句子深度讲解（OpenAI 代理） |
| POST | `/v1/ai/classify` | gpt-4o 自动分类（入库流水线用） |

## 起步

```bash
cd polly-server
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

cp .env.example .env
# 编辑 .env 填 OPENAI_API_KEY

python scripts/seed.py                 # 写 3 个 demo 视频进 SQLite
uvicorn app.main:app --reload          # http://127.0.0.1:8000/docs
```

## 切到 PostgreSQL

已演练通过（2026-05-14），零代码改动：

```bash
brew install postgresql@16
brew services start postgresql@16
createdb polly
psql -d polly -c "CREATE USER polly WITH PASSWORD 'polly'; GRANT ALL PRIVILEGES ON DATABASE polly TO polly; GRANT ALL ON SCHEMA public TO polly;"

pip install -e ".[postgres]"
# 改 .env：DATABASE_URL=postgresql+psycopg://polly:polly@localhost:5432/polly
python -m scripts.seed
uvicorn app.main:app --reload
```

## ingest 流水线

把 Polly iOS 工程里 demo 视频的资源（mp4 / 字幕 / 缩略图）通过 storage 抽象上传，再写入 contents 表：

```bash
python -m scripts.ingest --all                # 入库全部
python -m scripts.ingest --slug julian-treasure
```

`USE_R2=false`（默认）：拷到 `cdn-staging/`，由 `/static/` 暴露。
`USE_R2=true`：上传到 Cloudflare R2，URL 走 `R2_PUBLIC_BASE`。

## AI 讲解预生成

逐句调 OpenAI 灌库到 `explanations` 表。iOS 调 `/v1/ai/explain` 时优先命中此表（`cached: true`），不烧 token：

```bash
python -m scripts.pregenerate_explanations --slug julian-treasure
python -m scripts.pregenerate_explanations --all --concurrency 5
python -m scripts.pregenerate_explanations --slug X --limit 5      # 只跑前 5 句
python -m scripts.pregenerate_explanations --slug X --force        # 覆写已有缓存
```

## 鉴权 + 限速

所有 `/v1/*` 端点要求 `Authorization: Bearer <POLLY_API_KEY>`。空 `POLLY_API_KEY` = 关闭鉴权（仅本地 dev）。

限速：每 IP 滑动窗口。`REDIS_URL` 配上后切到 Redis ZSET 后端（多 worker 共享窗口）；为空时走进程内内存（单 worker dev 用）。

```ini
POLLY_API_KEY=...               # python -c "import secrets; print(secrets.token_urlsafe(32))"
RATE_LIMIT_AI=60/hour
RATE_LIMIT_CONTENTS=300/hour
REDIS_URL=redis://127.0.0.1:6379/0    # 生产必填
```

**重要**：生产用 `--workers 4` 跑 uvicorn 时不配 `REDIS_URL` 限速会失效——4 个 worker 各自一个独立内存桶，实际上限变 4 倍。

## 切到 R2（上线前）

```bash
pip install -e ".[r2]"
# .env：
USE_R2=true
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=polly
R2_PUBLIC_BASE=https://cdn.polly.app   # R2 后台配自定义域名

python -m scripts.ingest --all  # 这次会上传到 R2，URL 自动用 R2_PUBLIC_BASE
```

## 生产部署 checklist

按顺序做完即可上线：

```bash
# 1) 系统包
sudo apt install caddy postgresql redis-server python3-venv

# 2) DB
sudo -u postgres createuser polly --pwprompt
sudo -u postgres createdb polly -O polly
sudo systemctl enable --now postgresql redis-server

# 3) 代码 + venv
cd /srv && sudo git clone <repo> polly-server && cd polly-server
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[postgres,redis,r2]'

# 4) .env（关键项）
cat > .env <<EOF
DATABASE_URL=postgresql+psycopg://polly:<pwd>@localhost:5432/polly
REDIS_URL=redis://127.0.0.1:6379/0
POLLY_API_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
USE_R2=true
R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_PUBLIC_BASE=https://cdn.polly.app
OPENAI_API_KEY=sk-...
PUBLIC_BASE_URL=https://api.polly.app
CORS_ORIGINS=https://polly.app
EOF

# 5) 内容入库 + 预生成 AI 讲解
python -m scripts.seed
python -m scripts.ingest --all
python -m scripts.pregenerate_explanations --all --concurrency 5

# 6) systemd + Caddy
sudo cp deploy/polly-server.service /etc/systemd/system/
sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl daemon-reload
sudo systemctl enable --now polly-server
sudo systemctl reload caddy

# 7) iOS 切线
# Secrets.xcconfig：POLLY_SERVER_URL=https://api.polly.app
# Info.plist 通过 project.yml 移除 127.0.0.1 / localhost 的 ATS exception
# xcodegen generate 后 release build → IPA 不含 TED mp4（已 exclude）
```

**坑位提醒**：
- `--workers 4` 不配 `REDIS_URL`，限速会被分桶 4 倍失效
- R2 自定义域名要在 Cloudflare 后台 R2 → custom domain 绑好，证书自动
- HSTS 启用前先确认域名稳定，被 preload 后改不回 HTTP
- `pregenerate_explanations --all` 会按 contents 表里 published 视频跑；先 ingest 再 pregenerate
