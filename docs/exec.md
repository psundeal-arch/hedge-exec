# hedge exec：日内执行算法

**你定量，程序挑时机。** 给一个母单（对冲加 / 减几张，或现货买 / 卖多少）和截止时间，后台 worker 在时间窗里分批做完，
目标是比"一次 `hedge live` / 一次市价"成交得更便宜：挂单代替吃单、日内逢低 / 逢高多做、对冲按基差择时。
它**不判断该不该加减仓**（日内择时在 CDE 费用下没有稳定优势，perp-grid 的研究已经验证过），只负责执行。

## 命令（在服务器上跑）

```
cd ~/hedge-exec
.venv/bin/hedge exec open  ZEC 2 --venue coinbase --by 21:00          # 对冲加 2 张（现货买 + CDE 空），UTC 21:00 前
.venv/bin/hedge exec close ZEC 1 --venue coinbase --by 4h             # 对冲减 1 张，4 小时内
.venv/bin/hedge exec buy   BTC --usd 2000 --venue coinbase --by 6h    # 纯现货买
.venv/bin/hedge exec sell  ETH --qty 0.5  --venue coinbase --by 6h --limit 4200
.venv/bin/hedge exec status            # 任务列表（含暂停原因）
.venv/bin/hedge exec report 3          # 任务 #3 日志
.venv/bin/hedge exec cancel 3          # 取消（已成交部分保留；对冲两腿始终对齐）
```

提交时跑和 `hedge live` 一样的预检（名义上限、保证金、余额、会话风险），打印执行计划，然后要你**手输 `EXEC ZEC 2`** 确认。
加 `--dry` 就是演练：盯真实行情、用模拟腿成交，不下单、不用确认。
演练的成交记在 `data/live.db`（`runs.mode = paper`），**不进 `hedge paper` 的模拟盘**（审查 M3/M8：
`hedge paper` 是另一套引擎 `executor.run_pair`，练的不是实盘那套）。看演练结果用
`hedge exec status` / `hedge exec report <id>` / `hedge exec quality --mode paper`。

| 参数 | 默认 | 含义 |
|---|---|---|
| `--by` | 必填 | 截止：`4h` / `90m` / `21:00`（UTC，已过则明天）/ ISO 时间 |
| `--start` | 现在 | 推迟开始 |
| `--limit` | 开始价 ±2% | 价格保护：买不高于 / 卖不低于；超出暂停并 Pushover，不追 |
| `--finish` | complete | 截止时剩余：`complete` 吃单补齐（现货最多比对手价差 1%）/ `cancel` 撤掉剩余 |
| `--slices` | 4 | 至少分几份（单份还受 `clip_usd` 限制） |
| `--dip-k` / `--chase-k` | 0.5 / 0.5 | 逢低超前 / 高位不追的门槛（单位：1 小时平均振幅 ATR） |
| `--basis-min` | 近 24h 中位数 | 对冲：加仓要 永续溢价 ≥ 它，减仓要 ≤ 它（bp） |
| （固定）追价上限 | 15bp | 每份子单挂单价最多比首次挂价追 15bp；一点没成交就放弃这一份、冷却 5 分钟；很急时不限 |

拒绝：`grid_only` 名单里的标的（SUI / XRP，perp-grid 实盘网格在用）；同一标的已有未完成任务。

## 排程规则（`src/hedgebot/exec_sched.py`）

每 15 秒一轮：
1. **进度线**：目标完成量 = 已过时间 / (窗口 × 85%) × 总量 —— 在窗口 85% 处走完，最后一份也有时间挂单，不必在截止时吃单。
   落后一份以上就下一份；**很急** = 剩余时间 < 15% 窗口，或落后 ≥ 2 份。
2. **价格保护**：现价越过 `--limit` → 暂停（通知一次）。子单内部也是硬约束：现货挂单、改价、超时补齐、只吃单流程都不越过它；
   对冲任务碰到上限时，已成交的整张照常对冲，凑不满一张的现货零头退掉。**空单对冲不受 `--limit` 约束**（只升级不放弃，放弃就是裸腿）。
3. **坏时段**：每个整点前后 2 分钟（CDE 资金费结算）、周五 20:30–22:00 UTC（CDE 闭市前后）不下新子单。
4. **基差闸门**（只对冲）：基差 = CDE 永续中价 / 现货中价 − 1。加对冲要永续偏贵、减对冲要永续偏便宜；很急时放宽。
5. **逢低 / 逢高**：现价相对当日 VWAP 的偏离（以 ATR 计）对我们有利超过 `dip_k` → 即使进度已到也可超前（最多超前 2 份）。
6. **不追**：偏离对我们不利超过 `chase_k` 且不急 → 等。

参考数据（`exec_signal.py`，公开接口）：Coinbase 现货 5 分钟线 → 当日 VWAP（UTC 0 点起；0 点后不足 2 小时用最近 24h）、
1 小时平均振幅；CDE 永续 5 分钟线 → 近 24h 基差中位数。每 5 分钟刷新，取不到就按纯进度线执行。

子单：
- **对冲**：每份复用 `Ladder`（现货挂单、每满一张跟 CDE 吃单、带回滚 / 紧急退出），最多等 `child_wait_s`（240 秒）后吃单补齐这一份 ——
  对冲子单总是做完整张，两腿不会长时间不齐。子单出错 → 任务 `error` + 高优先级通知，先 `hedge account` 核对。
- **纯现货（Coinbase）**：post-only 挂在最优价，被抢到前面就改价；超时撤单，没成交的留给后面的份。
- **只吃单场所（Robinhood，阶段 2）**：带保护价的吃单，挑时机但不省手续费。

## 后台与恢复

`hedge-exec.service`（systemd user，常驻，`deploy/deploy.sh` 安装并重启）：轮询 `live.db` 的 `exec_jobs` 表，一次做一个任务。
- 状态（进度、成本、挂着的子单）每步写库 —— 对冲子单里每一笔成交、每一次挂撤单都**当场**落盘（含挂单号），不等子单做完；进程重启（含 SIGKILL）→ **恢复**：按存下的挂单号对账、撤单、补记重启前没记上的成交；对冲任务把现货零头补齐到整张，再把"现货已成交未对冲"的部分补对冲。
- SIGTERM 停在安全点：落在启动任务（建连接）期间也一样，不会开跑新的排程器。
- 启动任务时配置/密钥不对（如服务器没有 Robinhood 交易 key）→ 任务直接 `error` + 传呼，不重试，队列往下走。
- 每个任务一条 `runs` 记录，成交写 `fills`（和 `hedge live` 同一张表，`hedge watch` 照常核对两腿）。
- worker 遇到异常（网络等）60 秒后重试，连续 3 次才把任务标 `error` 并通知。
- **结果不明的单子**（下单请求超时 / 查不到状态）先对账、不重试；**对不上账的任务直接 `error` + 高优先级传呼，不交给 worker 重试**（重试会走恢复流程再下单）。连续出错计数在任务成功跑完一轮后清零。
- 通知：开始、每 25%、价格保护暂停、结束报告（均价、比开始时中价贵 / 便宜多少 bp、成交基差、手续费）。标题和正文都带【模拟】/【实盘】前缀。
- **执行质量**：每个任务结束把到达价滑点、基差滑点、手续费 bp、合计成本 bp 存进 `runs.summary`，结束通知附同标的「最近 5 笔 vs 全部」的滚动值。
  `hedge exec quality [SYM] [--mode live|paper|all] [--detail]` 按标的看。成本 bp 正 = 比基准差，按成交额加权；
  对冲任务看「基差滑点 + 手续费」（两腿价格涨跌抵消），纯现货看「到达价滑点 + 手续费」（含窗口内价格漂移，看多笔均值）。

## 交易 key

服务器上 hedge-bot 目录**不放交易 key**（`deploy.sh` 会删掉那里的 `.env.trade`，这条不变）。worker 读 `HEDGE_EXEC_KEYS` 指向的文件，
默认 `~/perp-grid-live/.env.trade`：服务器 IP 绑定的 Coinbase View+Trade key（和 perp-grid 实盘共用一个账户，所以 SUI / XRP 一律拒绝）。
本机的 key 绑家里 IP，服务器用不了。Robinhood 要在服务器另生成一把绑服务器 IP 的 key（阶段 2）。

## 阶段

1. Coinbase 现货 + CDE 对冲、Coinbase 纯现货；先 `--dry` 演练，看报告里比开始时中价省多少。
2. Robinhood 现货与 RH 现货腿的对冲。
3. 用实盘基差数据定 `--basis-min` 默认值与 `dip_k`。
