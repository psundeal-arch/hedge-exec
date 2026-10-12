#!/usr/bin/env bash
# 部署 hedge-bot 到服务器 ~/hedge-exec（2026-10-12 前叫 ~/hedge-bot），装上它的 systemd user 单元。
#
#   ./deploy/deploy.sh [user@host] [远程目录]     默认 rhserver hedge-exec
#
#   hedge-bot-watch.timer     两腿对齐巡检（每 15 分钟 :05/:20/:35/:50）
#   hedge-exec.service        日内执行 worker（常驻；交易 key 用 ~/perp-grid-live/.env.trade，不在本目录）
#   hedge-bot-failed@.service 上面两个异常退出时的独立传呼
#
# perp-track（保证金/资金费监控、保证金闸门）2026-10-11 拆成另一个仓，部署在 ~/perp-track，
# 有自己的 deploy.sh。hedge-bot 只经子进程用它：保证金闸门跑 `perp-track gate`（PERP_TRACK_BIN）。
#
# 交易 key（.env.trade）【绝不上服务器的 hedge-bot 目录】。
# 凭据正本在服务器 ~/vault（2026-10-06 集中，目录 700、文件 600）：部署【不推任何凭据】，
# 只校验各单元要挂的 vault 文件在、权限对、键齐 —— 少了就停，不让单元带着缺变量起来。
#
# 三道关（审查 H3/H11，见 docs/code-review.md）：
#   1. 只部署已提交的 HEAD（git archive），本机测试不过什么都不动
#   2. 服务器上的 config.toml 被手改过 → 停下给 diff；确认覆盖用 FORCE_CONFIG=1
#   3. 服务器上 pip + 测试 + 冒烟在 set -euo pipefail 里跑，不过就不重启 hedge-exec
# 被覆盖/删除的文件备份在服务器 .deploy-backup/<时间>（留 10 份）；当前版本记在 .deployed/COMMIT
set -euo pipefail

TARGET="${1:-rhserver}"
REMOTE_DIR="${2:-hedge-exec}"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
UNITS="hedge-bot-watch.service hedge-bot-watch.timer hedge-bot-failed@.service hedge-exec.service"
TIMERS="hedge-bot-watch.timer"

CONFIGS="config.toml"
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

# --- 拆仓守卫：perp-track 不能在本部署目录里 -----------------------------------
# 下面的 rsync --delete 会删掉部署目录里不属于本仓的东西（src/perptrack 等）。perp-track 还在
# 这里跑的话，它的定时任务下一轮就找不到代码 —— 保证金监控静默停掉。所以先确认它已在 ~/perp-track。
echo "==> perp-track 是否已搬出 ~/$REMOTE_DIR"
if ! ssh "$TARGET" bash -s -- "$REMOTE_DIR" <<'REMOTE'
set -uo pipefail
bad=0
[ -e ~/"$1"/perp.db ] && { echo "    !! ~/$1/perp.db 还在 —— perp-track 的活库还没搬到 ~/perp-track"; bad=1; }
if grep -lsE "%h/$1/|/$1/\.venv/bin/perp-track" ~/.config/systemd/user/perp-track*.service >/dev/null; then
  echo "    !! 还有 perp-track 单元指向 ~/$1：$(grep -lsE "%h/$1/" ~/.config/systemd/user/perp-track*.service | xargs -n1 basename | tr '\n' ' ')"; bad=1
fi
[ -x ~/perp-track/.venv/bin/perp-track ] || { echo "    !! ~/perp-track/.venv/bin/perp-track 不存在 —— 保证金闸门会一律拦单"; bad=1; }
[ $bad = 0 ] && echo "    perp-track 已在 ~/perp-track ✓"
exit $bad
REMOTE
then
  echo "!! perp-track 还没搬完 —— 什么都没动。先按拆仓步骤把它部署到 ~/perp-track"
  exit 1
fi

echo "==> 同步代码（被覆盖/删除的文件备份到 .deploy-backup/$TS）"
rsync -az --delete --backup --backup-dir=".deploy-backup/$TS" \
  --exclude '.git/' --exclude '.claude/' --exclude '.pytest_cache/' --exclude '.venv/' \
  --exclude '__pycache__/' --exclude '*.egg-info/' --exclude 'data/' \
  --exclude '.env' --exclude '.env.*' --exclude '.remote/' \
  --exclude '.deployed/' --exclude '.deploy-backup/' \
  --exclude 'perp.db' --exclude 'perp.db-*' --exclude 'perp.db.*' --exclude 'rhearn.db*' \
  --exclude 'snapshots/' --exclude 'cron.log' --exclude 'cron.err' --exclude 'uv.lock' \
  "$STAGE"/ "$TARGET:$REMOTE_DIR/"

# 交易 key 绝不上服务器。--exclude '.env' 只匹配这一个文件名，
# 2026-10-02 发现 .env.trade 因此被同步过去 —— 现在排除 .env.*，并在这里兜底删除
# （exec worker 的交易 key 在 ~/perp-grid-live/.env.trade，不在这个目录，不受影响）
ssh "$TARGET" "for f in .env.trade .env.local; do \
  if [ -f $REMOTE_DIR/\$f ]; then mkdir -p $REMOTE_DIR/.deploy-backup/$TS && \
    mv $REMOTE_DIR/\$f $REMOTE_DIR/.deploy-backup/$TS/ && echo \"    !! 移走了 $REMOTE_DIR/\$f（备份在 .deploy-backup/$TS）\"; fi; done"

echo "==> 凭据（服务器 ~/vault，不推送，只校验）"
# ssh 会把参数拼成一行重新分词 —— 不加内层引号，远程 $1 只拿到第一项（第一次部署就这样漏检了三个文件）
if ! ssh "$TARGET" bash -s -- "'$VAULT_NEEDS'" <<'REMOTE'
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

ROLLBACK="回滚：git checkout <上一个提交> && ./deploy/deploy.sh；被覆盖的旧文件在服务器 $REMOTE_DIR/.deploy-backup/$TS"

# --- 关卡三：服务器上再测一遍 + 冒烟（H3）----------------------------------------
# 原来这两步的退出码都被管道吃掉了（远程 shell 没有 pipefail，取的是 tail 的 0），
# 测试失败照样往下走、重启实盘的 hedge-exec。现在整段在远程 set -euo pipefail 里跑。
echo "==> venv + 测试（服务器）"
if ! ssh "$TARGET" bash -s -- "$REMOTE_DIR" <<'REMOTE'
set -euo pipefail
cd "$1"
python3 -m venv .venv
.venv/bin/pip install -q -e '.[dev]' -c deploy/constraints.txt
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
export PERP_TRACK_BIN="$HOME/perp-track/.venv/bin/perp-track"
# 两条进程边界：perp-track 能读到 grid_only、hedge-bot 能问到保证金闸门（--no-intent，不登记）
.venv/bin/hedge grid-only --json || { echo "!! hedge grid-only 失败"; exit 1; }
out="$(.venv/bin/python -c "from hedgebot.live import margin_gate; print(margin_gate(add_notional=0, margin_rate=None, spend_cash=0, note='部署冒烟', register=False))" 2>&1)"
echo "    闸门：$out"
if echo "$out" | grep -qE "Traceback|闸门出错"; then
  echo "!! 保证金闸门调不通"; exit 1
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
  rm -f ~/.config/systemd/user/hedge-bot-failed.service && \
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
ssh "$TARGET" "systemctl --user list-timers 'hedge-bot*' --no-pager | head -4; \
  loginctl show-user \$(whoami) -p Linger"

echo
echo "==> 完成。若 Linger=no：sudo loginctl enable-linger \$(whoami)，否则注销后定时任务会停"
