#!/bin/bash
# 本机定时任务：抓取数据 -> 有变化则提交并推送到 GitHub
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
if ! /Users/mengli/miniconda3/bin/python3 scrape.py >> "$LOG" 2>&1; then
  say "抓取失败（有城市无历史数据兜底），跳过提交"
  exit 1
fi

git add "江苏省2027省考报名情况.csv" "江苏省2027省考报名情况.xlsx"
if git diff --cached --quiet -- "江苏省2027省考报名情况.csv"; then
  say "数据无变化，结束"
  exit 0
fi

git commit -m "data: $(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M') 更新报名数据" >> "$LOG" 2>&1

for i in 1 2 3; do
  if git push origin main >> "$LOG" 2>&1; then
    say "推送成功"
    exit 0
  fi
  say "推送失败，第 $i 次重试"
  sleep 15
done

say "推送失败，已保留本地提交，下次运行会继续推送"
exit 1
