#!/usr/bin/env bash
# 部署 hedge-bot 到服务器并挂上 15 分钟一次的巡检。
#
#   ./deploy/deploy.sh [user@host] [远程目录]     默认 rhserver hedge-bot
#
# 通知凭据读 perp-track 的 .env（同一台机器上已经部署了 perp-track）。
# .env 单独 scp 并设 600，不走 rsync。data/（模拟盘、报警状态）远程已有则不动。
set -euo pipefail

TARGET="${1:-rhserver}"
REMOTE_DIR="${2:-hedge-bot}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 远程检查：python ≥ 3.11、perp-track 的 .env（通知凭据）"
ssh "$TARGET" 'python3 -c "import sys; assert sys.version_info>=(3,11); print(sys.version.split()[0])" \
  && test -f ~/perp-track/.env && echo "perp-track/.env 在"'

echo "==> 同步代码"
rsync -az --delete \
  --exclude '.git/' --exclude '.claude/' --exclude '.pytest_cache/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '*.egg-info/' --exclude '.env' --exclude 'data/' \
  "$LOCAL_DIR"/ "$TARGET:$REMOTE_DIR/"

echo "==> 传 .env（600）"
scp -q "$LOCAL_DIR/.env" "$TARGET:$REMOTE_DIR/.env"
ssh "$TARGET" "chmod 600 $REMOTE_DIR/.env && mkdir -p $REMOTE_DIR/data"
echo "    键：$(sed -n 's/=.*//p' "$LOCAL_DIR/.env" | tr '\n' ' ')"

echo "==> venv + 测试"
ssh "$TARGET" "cd $REMOTE_DIR && python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' \
  && .venv/bin/python -m pytest tests/ -q"

echo "==> 冒烟：用 systemd 同样的环境跑 account（看三个来源在服务器上是否都读得到）"
ssh "$TARGET" "cd $REMOTE_DIR && set -a && . ~/perp-track/.env && . ./.env && set +a \
  && .venv/bin/hedge account"

echo "==> 安装 systemd user 单元"
ssh "$TARGET" "mkdir -p ~/.config/systemd/user && \
  cp $REMOTE_DIR/deploy/hedge-bot-watch.service $REMOTE_DIR/deploy/hedge-bot-watch.timer \
     $REMOTE_DIR/deploy/hedge-bot-failed.service ~/.config/systemd/user/ && \
  systemctl --user daemon-reload && systemctl --user enable --now hedge-bot-watch.timer"

echo "==> 立即跑一次单元，确认 systemd 环境端到端可用"
ssh "$TARGET" "systemctl --user start hedge-bot-watch.service; \
  systemctl --user is-active --quiet hedge-bot-watch.timer && echo 'timer 已激活' || { echo '!! timer 没激活'; exit 1; }; \
  journalctl --user -u hedge-bot-watch.service -n 8 --no-pager -o cat; \
  systemctl --user list-timers 'hedge-bot*' --no-pager | head -3; \
  loginctl show-user \$(whoami) -p Linger"
