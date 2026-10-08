#!/usr/bin/env bash
# 单元异常退出时传呼。$1 = 失败的单元名（hedge-bot-failed@.service 的实例名）。
# 每个单元 6 小时内最多一次 —— 配置坏了会每 15 分钟失败一次，每次都响会让人静音，
# 静音之后真正的告警也收不到了。节流按单元分开：exec 崩过一次不能压掉 watch 的传呼。
set -u
UNIT="${1:-unknown.service}"
MARK="$HOME/hedge-bot/data/.failed-paged.$UNIT"
if [ -n "$(find "$MARK" -mmin -360 2>/dev/null)" ]; then
  exit 0
fi
case "$UNIT" in
  hedge-bot-watch.service)
    title="hedge-bot 巡检单元失败"
    what="hedge watch 进程异常退出（普通取数失败不会走到这里）。两腿对齐现在【看不见】，不是【没问题】。" ;;
  hedge-exec.service)
    title="hedge exec worker 失败"
    what="日内执行 worker 异常退出。可能有任务停在半路 —— 先 hedge exec status，再 hedge account 核对两腿。" ;;
  perp-track.service)
    title="perp-track 日报单元失败"
    what="日报快照连续失败（已自动重试）。今天的日报和基于快照的告警可能都没有出。" ;;
  *)
    title="$UNIT 失败"
    what="单元异常退出。" ;;
esac
curl -sf --max-time 20 \
  -F "token=${PUSHOVER_TOKEN:-}" -F "user=${PUSHOVER_USER:-}" \
  -F "priority=0" \
  -F "title=$title" \
  -F "message=$what 查：journalctl --user -u $UNIT -n 30" \
  https://api.pushover.net/1/messages.json >/dev/null \
  && mkdir -p "$(dirname "$MARK")" && touch "$MARK"
