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
#   hedge-exec.service        日内执行 worker（常驻；交易 key 用 ~/perp-grid-live/.env.trade，不在本目录）
# 保证金传呼是安全网，交易代码改得勤 —— 所以各自独立的进程，互不拖累。
#
# 交易 key（.env.trade）【绝不上服务器的 hedge-bot 目录】。
# 凭据正本在服务器 ~/vault（2026-10-06 集中，目录 700、文件 600）：部署【不推任何凭据】，
# 只校验各单元要挂的 vault 文件在、权限对、键齐 —— 少了就停，不让单元带着缺变量起来。
# perp.db（资金费历史、快照、告警幂等）远程已有就【绝不覆盖】。
#
# 三道关（审查 H3/H11）：
#   1. 只部署已提交的 HEAD（git archive），本机测试不过什么都不动
#   2. 服务器上的 config.toml / perp-track.toml 被手改过 → 停下给 diff；确认覆盖用 FORCE_CONFIG=1
#   3. 服务器上 pip + 测试 + 冒烟在 set -euo pipefail 里跑，不过就不重启 hedge-exec
# 被覆盖/删除的文件备份在服务器 .deploy-backup/<时间>（留 10 份）；当前版本记在 .deployed/COMMIT
set -euo pipefail

TARGET="${1:-rhserver}"
REMOTE_DIR="${2:-hedge-bot}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="perp-track.service perp-track.timer perp-track-funding.service perp-track-funding.timer \
perp-track-watch.service perp-track-watch.timer perp-track-pager-failed.service \
hedge-bot-watch.service hedge-bot-watch.timer hedge-bot-failed.service hedge-exec.service"
TIMERS="perp-track.timer perp-track-funding.timer perp-track-watch.timer hedge-bot-watch.timer"

CONFIGS="config.toml perp-track.toml"
# 各单元挂的 vault 文件及其必须有的键（与 deploy/*.service 的 EnvironmentFile 对应）
VAULT_NEEDS="coinbase-view.env:COINBASE_API_KEY_NAME,COINBASE_API_PRIVATE_KEY \
kraken-view.env:KRAKEN_API_KEY,KRAKEN_API_SECRET \
robinhood-view.env:RH_API_KEY,RH_PRIVATE_KEY \
notify.env:PUSHOVER_TOKEN,PUSHOVER_USER,DISCORD_WEBHOOK_URL"
TS="$(date -u +%Y%m%d-%H%M%S)"

# --- 关卡一：部署的必须正好是某个提交，且本机测试通过（H3）---------------------
# 原来 rsync 同步的是工作目录：没提交的改动、未跟踪的文件都会上去，服务器上跑的是
# 哪个版本无从知道。现在从 git archive HEAD 打包，未提交的改动直接拒绝。
cd "$LOCAL_DIR"
if ! git diff --quiet HEAD --; then
  echo "!! 有未提交的改动 —— 部署的是 HEAD，这些改动不会上去。先提交再部署："
  git status --short | grep -v '^??' || true
  exit 1
fi
COMMIT="$(git rev-parse --short HEAD)"
echo "==> 部署提交 $COMMIT  $(git log -1 --format=%s)"

echo "==> 本机测试（不过就什么都不动）"
"$LOCAL_DIR/.venv/bin/python" -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -1

echo "==> 远程 python 版本"
ssh "$TARGET" 'python3 -c "import sys; assert sys.version_info>=(3,11); print(sys.version.split()[0])"'

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
git archive HEAD | tar -x -C "$STAGE"

# --- 关卡二：服务器上的配置有没有被手改（H11）------------------------------------
# 原来 rsync --delete 静默覆盖 config.toml / perp-track.toml / .env —— 服务器上的手改
# 被还原过两次（0d030e6、95c4bf3）。每次部署把发上去的版本存到 .deployed/，下次
# 先比「服务器现状」和「上次发的」：不一样就是有人在服务器上改过，停下给 diff。
# 确认要覆盖：FORCE_CONFIG=1 ./deploy/deploy.sh
echo "==> 服务器配置是否被手改过"
mkdir -p "$STAGE/.remote"
drift=0
for f in $CONFIGS; do
  if ! ssh "$TARGET" "test -f $REMOTE_DIR/$f"; then continue; fi
  ssh "$TARGET" "cat $REMOTE_DIR/$f" > "$STAGE/.remote/$f"
  if ssh "$TARGET" "test -f $REMOTE_DIR/.deployed/$f"; then
    ssh "$TARGET" "cat $REMOTE_DIR/.deployed/$f" > "$STAGE/.remote/$f.base"
    cmp -s "$STAGE/.remote/$f" "$STAGE/.remote/$f.base" && continue
    echo "    !! $f 在服务器上被改过（和上次部署的不一样）"
  else
    cmp -s "$STAGE/.remote/$f" "$STAGE/$f" && continue
    echo "    !! $f 服务器版本和本机不同（还没有部署记录，分不清是谁改的）"
  fi
  drift=1
  diff -u --label "服务器 $f" --label "本机 $f" "$STAGE/.remote/$f" "$STAGE/$f" | sed 's/^/       /' || true
done
if [ "$drift" = 1 ] && [ "${FORCE_CONFIG:-0}" != 1 ]; then
  echo "    先把服务器上的改动合进本机仓库（或确认可以丢掉），再部署；"
  echo "    确认用本机版本覆盖：FORCE_CONFIG=1 ./deploy/deploy.sh"
  exit 1
fi

echo "==> 同步代码（被覆盖/删除的文件备份到 .deploy-backup/$TS）"
rsync -az --delete --backup --backup-dir=".deploy-backup/$TS" \
  --exclude '.git/' --exclude '.claude/' --exclude '.pytest_cache/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '*.egg-info/' --exclude 'data/' \
  --exclude '.env' --exclude '.env.*' --exclude '.remote/' \
  --exclude '.deployed/' --exclude '.deploy-backup/' \
  --exclude 'perp.db' --exclude 'perp.db-*' --exclude 'rhearn.db*' \
  --exclude 'snapshots/' --exclude 'cron.log' --exclude 'cron.err' --exclude 'uv.lock' \
  "$STAGE"/ "$TARGET:$REMOTE_DIR/"

# 交易 key 绝不上服务器。--exclude '.env' 只匹配这一个文件名，
# 2026-10-02 发现 .env.trade 因此被同步过去 —— 现在排除 .env.*，并在这里兜底删除
# （exec worker 的交易 key 在 ~/perp-grid-live/.env.trade，不在这个目录，不受影响）
ssh "$TARGET" "for f in .env.trade .env.local; do \
  if [ -f $REMOTE_DIR/\$f ]; then mkdir -p $REMOTE_DIR/.deploy-backup/$TS && \
    mv $REMOTE_DIR/\$f $REMOTE_DIR/.deploy-backup/$TS/ && echo \"    !! 移走了 $REMOTE_DIR/\$f（备份在 .deploy-backup/$TS）\"; fi; done"

echo "==> 凭据（服务器 ~/vault，不推送，只校验）"
if ! ssh "$TARGET" bash -s -- "$VAULT_NEEDS" <<'REMOTE'
set -uo pipefail
bad=0
[ "$(stat -c %a ~/vault 2>/dev/null)" = 700 ] || { echo "    !! ~/vault 不存在或权限不是 700"; bad=1; }
for item in $1; do
  f="${item%%:*}"; keys="${item#*:}"
  p=~/vault/$f
  if [ ! -f "$p" ]; then echo "    !! 缺 vault/$f"; bad=1; continue; fi
  [ "$(stat -c %a "$p")" = 600 ] || { echo "    !! vault/$f 权限不是 600"; bad=1; }
  for k in ${keys//,/ }; do
    grep -qE "^$k=.+" "$p" || { echo "    !! vault/$f 缺 $k"; bad=1; }
  done
  echo "    vault/$f ✓"
done
# 交易 key 不该出现在 vault 以外的 hedge-bot 目录；RH 交易 key 按决定只留本机（2026-10-06）
[ -f ~/vault/robinhood-trade.env ] && { echo "    !! vault 里又出现了 robinhood-trade.env（决定是 RH 交易 key 只留本机）"; bad=1; }
exit $bad
REMOTE
then
  echo "!! vault 校验没过 —— 什么都还没动（单元没装、服务没重启）"
  exit 1
fi

echo "==> 数据库"
if ssh "$TARGET" "test -f $REMOTE_DIR/perp.db"; then
  echo "    远程已有 perp.db，保留不动"
else
  echo "    !! 远程没有 perp.db —— 首次部署请先迁移历史库（见 docs/perp-track.md「迁移」），"
  echo "    !! 否则 perp-track 会从空库开始，资金费均值与告警幂等全部丢失"
fi

ROLLBACK="回滚：git checkout <上一个提交> && ./deploy/deploy.sh；被覆盖的旧文件在服务器 $REMOTE_DIR/.deploy-backup/$TS"

# --- 关卡三：服务器上再测一遍 + 冒烟（H3）----------------------------------------
# 原来这两步的退出码都被管道吃掉了（远程 shell 没有 pipefail，取的是 tail 的 0），
# 测试失败照样往下走、重启实盘的 hedge-exec。现在整段在远程 set -euo pipefail 里跑。
echo "==> venv + 测试（服务器）"
if ! ssh "$TARGET" bash -s -- "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
cd "$1"
python3 -m venv .venv
.venv/bin/pip install -q -e '.[dev]'
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -1
REMOTE
then
  echo "!! 服务器上安装或测试没过 —— 代码已同步，但 hedge-exec 没有重启；定时任务下一轮会跑到新代码"
  echo "!! $ROLLBACK"
  exit 1
fi

echo "==> 冒烟（与 systemd 同样的环境；出现 Traceback 或缺关键输出就停）"
if ! ssh "$TARGET" bash -s -- "$REMOTE_DIR" <<'REMOTE'
set -uo pipefail
cd "$1"
set -a; for f in coinbase-view kraken-view robinhood-view notify misc; do . ~/vault/$f.env; done; set +a
out="$(.venv/bin/perp-track status 2>&1)"
echo "$out" | grep -E '账户保证金率|能扛住' || true
if echo "$out" | grep -q Traceback || ! echo "$out" | grep -q '账户保证金率'; then
  echo "$out" | tail -15; echo "!! perp-track status 异常"; exit 1
fi
out="$(.venv/bin/hedge account 2>&1)"
echo "$out" | tail -3
if echo "$out" | grep -q Traceback; then
  echo "$out" | tail -15; echo "!! hedge account 崩了"; exit 1
fi
REMOTE
then
  echo "!! 冒烟没过 —— hedge-exec 没有重启"
  echo "!! $ROLLBACK"
  exit 1
fi

echo "==> 安装 systemd user 单元"
ssh "$TARGET" "mkdir -p ~/.config/systemd/user && \
  for u in $UNITS; do cp $REMOTE_DIR/deploy/\$u ~/.config/systemd/user/; done && \
  systemctl --user daemon-reload && \
  for t in $TIMERS; do systemctl --user enable --now \$t; done && \
  systemctl --user enable hedge-exec.service && systemctl --user restart hedge-exec.service"

echo "==> 断言定时任务都激活了（漏装 unit 不会报错 —— 代码在、测试过、就是没跑）"
for t in $TIMERS; do
  if ssh "$TARGET" "systemctl --user is-active --quiet $t"; then
    echo "    $t 已激活"
  else
    echo "    !! $t 没有激活 —— 部署失败"
    exit 1
  fi
done
if ssh "$TARGET" "systemctl --user is-active --quiet hedge-exec.service"; then
  echo "    hedge-exec.service 运行中"
else
  echo "    !! hedge-exec.service 没在跑 —— journalctl --user -u hedge-exec -n 30"
  exit 1
fi
# 记下这次发上去的配置和提交号：下次部署据此判断服务器上有没有手改；也能一眼看出跑的是哪个版本
ssh "$TARGET" "mkdir -p $REMOTE_DIR/.deployed && chmod 700 $REMOTE_DIR/.deployed && \
  for f in $CONFIGS; do cp -p $REMOTE_DIR/\$f $REMOTE_DIR/.deployed/; done && \
  echo '$COMMIT $TS' > $REMOTE_DIR/.deployed/COMMIT && \
  ls -1d $REMOTE_DIR/.deploy-backup/* 2>/dev/null | sort | head -n -10 | xargs -r rm -rf"
echo "    服务器现在跑的是 $COMMIT（记在 $REMOTE_DIR/.deployed/COMMIT）"
ssh "$TARGET" "systemctl --user list-timers 'perp-track*' 'hedge-bot*' --no-pager | head -6; \
  loginctl show-user \$(whoami) -p Linger"

echo
echo "==> 完成。若 Linger=no：sudo loginctl enable-linger \$(whoami)，否则注销后定时任务会停"
