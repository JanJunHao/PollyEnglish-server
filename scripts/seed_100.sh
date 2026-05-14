#!/usr/bin/env bash
# 一键拉 100 条 TED + 入库 + 自动分类 + 字幕生成。
# 跑前确认 .env 里 OPENAI_API_KEY 有效；DATABASE_URL 指向目标库（dev = SQLite）。
#
# 用法：
#   bash scripts/seed_100.sh             # 默认 @TED 100 条
#   LIMIT=20 CHANNEL=@TED-Ed bash scripts/seed_100.sh
#   NO_SUBTITLES=1 bash scripts/seed_100.sh   # 跳过字幕，省时间

set -euo pipefail

cd "$(dirname "$0")/.."

CHANNEL="${CHANNEL:-@TED}"
LIMIT="${LIMIT:-100}"
FETCH_OUT="cdn-staging/ted-fetch.json"

echo "==> 1) fetch $CHANNEL × $LIMIT"
python -m scripts.fetch_ted_channel --channel "$CHANNEL" --limit "$LIMIT" --out "$FETCH_OUT"

echo
echo "==> 2) ingest（含 classify + subtitle）"
extra_args=""
if [[ "${NO_SUBTITLES:-0}" == "1" ]]; then
  extra_args="--no-subtitles"
fi
python -m scripts.ingest --from-fetch "$FETCH_OUT" $extra_args

echo
echo "==> 3) 库内汇总"
DB_FILE="${DATABASE_URL#sqlite:///}"
if [[ -f "$DB_FILE" ]]; then
  sqlite3 "$DB_FILE" "SELECT status, count(*) FROM contents GROUP BY status"
fi
