#!/usr/bin/env bash
# watch 单元异常退出时传呼。6 小时内最多一次 —— 配置坏了会每 15 分钟失败一次，
# 每次都响会让人静音，静音之后真正的「没对齐」也收不到。
set -u
MARK="$HOME/hedge-bot/data/.failed-paged"
if [ -n "$(find "$MARK" -mmin -360 2>/dev/null)" ]; then
  exit 0
fi
curl -sf --max-time 20 \
  -F "token=${PUSHOVER_TOKEN:-}" -F "user=${PUSHOVER_USER:-}" \
  -F "priority=0" \
  -F "title=hedge-bot 巡检单元失败" \
  -F "message=hedge watch 进程异常退出（普通取数失败不会走到这里）。两腿对齐现在【看不见】，不是【没问题】。查：journalctl --user -u hedge-bot-watch.service -n 30" \
  https://api.pushover.net/1/messages.json >/dev/null \
  && mkdir -p "$(dirname "$MARK")" && touch "$MARK"
