#!/bin/bash
# 本机定时任务：抓取报名数据并写入 Neon PostgreSQL（每次保存一份全量快照）
set -u

export PATH="/opt/homebrew/bin:/Users/mengli/miniconda3/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "/Users/mengli/jiangsu2027" || exit 1

mkdir -p logs
LOG="logs/update.log"
# 日志超过 1MB 后轮转
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1048576 ]; then
  mv "$LOG" "$LOG.1"
fi

say() { echo "[$(TZ=Asia/Shanghai date '+%F %T')] $*" >> "$LOG"; }

say "===== 开始抓取 ====="
if /Users/mengli/miniconda3/bin/python3 scrape.py >> "$LOG" 2>&1; then
  say "抓取并入库完成"
  exit 0
fi

say "抓取或入库失败，请查看日志"
exit 1
