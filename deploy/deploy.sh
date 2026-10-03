#!/usr/bin/env bash
# 部署 hedge-bot（含并入的 perp-track）到服务器，装上全部 systemd user 定时任务。
#
#   ./deploy/deploy.sh [user@host] [远程目录]     默认 rhserver hedge-bot
#
# 进程分开、仓库合一（2026-10-02 并入）：
#   perp-track.timer          日报（每天 17:00 PT）
#   perp-track-funding.timer  全场 CDE 资金费采样（每小时 :02）
#   perp-track-watch.timer    保证金率监控 + Pushover 传呼（每 15 分钟）
#   hedge-bot-watch.timer     两腿对齐巡检（每 15 分钟 :05/:20/:35/:50）
# 保证金传呼是安全网，交易代码改得勤 —— 所以各自独立的进程，互不拖累。
#
# 交易 key（.env.trade）【绝不上服务器】；服务器只放只读 key + 通知凭据（.env，600）。
# perp.db（资金费历史、快照、告警幂等）远程已有就【绝不覆盖】。
set -euo pipefail

TARGET="${1:-rhserver}"
REMOTE_DIR="${2:-hedge-bot}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="perp-track.service perp-track.timer perp-track-funding.service perp-track-funding.timer \
perp-track-watch.service perp-track-watch.timer perp-track-pager-failed.service \
hedge-bot-watch.service hedge-bot-watch.timer hedge-bot-failed.service"
TIMERS="perp-track.timer perp-track-funding.timer perp-track-watch.timer hedge-bot-watch.timer"

echo "==> 远程 python 版本"
ssh "$TARGET" 'python3 -c "import sys; assert sys.version_info>=(3,11); print(sys.version.split()[0])"'

echo "==> 同步代码（不碰 .env / 数据库 / 运行产物）"
rsync -az --delete \
  --exclude '.git/' --exclude '.claude/' --exclude '.pytest_cache/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '*.egg-info/' --exclude 'data/' \
  --exclude '.env' --exclude '.env.*' \
  --exclude 'perp.db' --exclude 'perp.db-*' --exclude 'rhearn.db*' \
  --exclude 'snapshots/' --exclude 'cron.log' --exclude 'cron.err' --exclude 'uv.lock' \
  "$LOCAL_DIR"/ "$TARGET:$REMOTE_DIR/"

# 交易 key 绝不上服务器。--exclude '.env' 只匹配这一个文件名，
# 2026-10-02 发现 .env.trade 因此被同步过去 —— 现在排除 .env.*，并在这里兜底删除
ssh "$TARGET" "rm -f $REMOTE_DIR/.env.trade $REMOTE_DIR/.env.local"

echo "==> 传 .env（600）"
scp -q "$LOCAL_DIR/.env" "$TARGET:$REMOTE_DIR/.env"
ssh "$TARGET" "chmod 600 $REMOTE_DIR/.env && mkdir -p $REMOTE_DIR/data"
echo "    键：$(grep -oE '^[A-Z_]+=' "$LOCAL_DIR/.env" | tr -d '=' | tr '\n' ' ')"

echo "==> 数据库"
if ssh "$TARGET" "test -f $REMOTE_DIR/perp.db"; then
  echo "    远程已有 perp.db，保留不动"
else
  echo "    !! 远程没有 perp.db —— 首次部署请先迁移历史库（见 docs/perp-track.md「迁移」），"
  echo "    !! 否则 perp-track 会从空库开始，资金费均值与告警幂等全部丢失"
fi

echo "==> venv + 测试"
ssh "$TARGET" "cd $REMOTE_DIR && python3 -m venv .venv && .venv/bin/pip install -q -e '.[dev]' \
  && .venv/bin/python -m pytest tests/ -q 2>&1 | tail -1"

echo "==> 冒烟（与 systemd 同样的环境）"
ssh "$TARGET" "cd $REMOTE_DIR && set -a && . ./.env && set +a \
  && .venv/bin/perp-track status 2>&1 | grep -E '账户保证金率|能扛住' ; .venv/bin/hedge account 2>&1 | tail -3"

echo "==> 安装 systemd user 单元"
ssh "$TARGET" "mkdir -p ~/.config/systemd/user && \
  for u in $UNITS; do cp $REMOTE_DIR/deploy/\$u ~/.config/systemd/user/; done && \
  systemctl --user daemon-reload && \
  for t in $TIMERS; do systemctl --user enable --now \$t; done"

echo "==> 断言定时任务都激活了（漏装 unit 不会报错 —— 代码在、测试过、就是没跑）"
for t in $TIMERS; do
  if ssh "$TARGET" "systemctl --user is-active --quiet $t"; then
    echo "    $t 已激活"
  else
    echo "    !! $t 没有激活 —— 部署失败"
    exit 1
  fi
done
ssh "$TARGET" "systemctl --user list-timers 'perp-track*' 'hedge-bot*' --no-pager | head -6; \
  loginctl show-user \$(whoami) -p Linger"

echo
echo "==> 完成。若 Linger=no：sudo loginctl enable-linger \$(whoami)，否则注销后定时任务会停"
