# hedge-bot 代码审查（2026-10-05）

**范围**：`src/hedgebot/` + `src/perptrack/` 全部 37 个模块（约 9.7k 行）、6 个测试文件、`deploy/`、文档与配置。

**方法**：实盘路径（`live` / `ladder` / `exec_sched` / `trade_api`）逐模块通读；跑完整测试（**263 passed**，全离线、无网络、无随机、无真实 sleep）；对最严重的几条**用真实代码 + 真实数据库复现**（`data/perp-server.db`、`perp.db`），不靠读代码下结论。

**结论**：整体质量明显在平均线以上 —— 注释是踩坑踩出来的，失败语义想得很清楚，纯函数/测试的切分很克制。问题集中在**一个地方**：**和交易所之间的那道缝**。那里把「结果不明」（单子可能已经存在）当成了「干净失败」（什么都没发生）来处理。这道缝没有任何测试，而它恰好是真金白银会出事的地方。

> 本文只读审查，未改动任何代码。

---

## 一、严重（Critical）

### C1. 成交没被观测到 → 对冲重复下单，而程序报告「零敞口」 🐞 已实测复现

位置：[`ladder.py:226-271`](../src/hedgebot/ladder.py#L226-L271) · [`trade_api.py:148-163`](../src/hedgebot/trade_api.py#L148-L163) · [`trade_api.py:309-318`](../src/hedgebot/trade_api.py#L309-L318)

`CoinbaseLeg.take()` / `KrakenLeg.take()` 先下单，再轮询 `_wait_final(leg, oid)`。这个轮询一旦抛错（20 秒超时、429、4xx、偶发 5xx），**`oid` 就丢了**，调用方只看到一个笼统的 `SourceError`。`Ladder._hedge()` 把它当硬失败计入 `hard_failures`，然后**用同样的数量重试** —— 可那笔单子已经发出去了，可能已经成交。每重试一次，真实仓位就多一份。

用真实的 `Ladder` + 一个「IOC 在交易所成交了、但轮询报错」的假腿（就是现实里「发出去了、看不见」的那种情形）实测：

```
status=unwound  spot_net=+0  perp_recorded=0
perp_ACTUALLY_SOLD_at_exchange=9   residual_reported=+0
```

程序退出时报的是**「回到零敞口」**、净 delta `+0`、`fills` 表里一条记录都没有 —— 而账户上是一个**9 张的裸空**（3 次重试 × 3 张），现货一股没有。

现货腿是同一个毛病：`_sync()` 里的 `leg.status()` 没有保护，status 一报错就中止主循环 → `_emergency` → `_hedge_all()` 按**过期且偏小**的 `spot_qty` 去对冲，结果是裸多现货。

**修法**：让订单号在异常里活下来（返回它，或抛出携带它的异常）；把「状态不明」当成**停手并对账** —— 用**确定性**的 `client_order_id` 回查订单、或拉最近成交 —— 绝不能当成「重试」。现在 `client_order_id` 是每次新生成的 `uuid4()`（[`trade_api.py:117`](../src/hedgebot/trade_api.py#L117)），连人工对账都无从下手。

### C2. 可用资金归零的 CRITICAL 传呼发不出来 —— 命令先崩了 🐞 已实测复现

位置：[`cli.py:873-876`](../src/perptrack/cli.py#L873-L876) · [`cli.py:915`](../src/perptrack/cli.py#L915) · [`sources.py:743-755`](../src/perptrack/sources.py#L743-L755)

保证金被打光时，`fetch_margin_lite` 是**故意**返回 `ok=1, margin_ratio=None` 的（源码注释：「funds == 0 走到这里：请求全都成功了，所以 ok=1…不能当成取数失败」）。接着 `cmd_margin_watch` 里：

```python
r = row["margin_ratio"]
line = (f"[{row['ts']}] 保证金率 {r:.2%}   "   # ← margin_ratio 为 None 时 TypeError
```

而真正处理 `zero_funds → Decision("open","critical")` 的 `_page_if_needed`（[`marginwatch.py:221-228`](../src/perptrack/marginwatch.py#L221-L228)）在同一函数的**最末尾** `return` 处（第 915 行），**根本走不到**。

于是「马上要被强平」这一个状态产生的结果是：没有 `margin_events` 记录、没有 Pushover 传呼、`margin_fail_streak()` 因为该行是 `ok=1` 而保持 0，所以连「监控失明」告警也不会响。纯函数有测试，CLI 接线处没有。

**修法**：格式化时防 None（`"—" if r is None`），并把 `_page_if_needed` 提到任何格式化之前。

### C3. 所有「24 小时」窗口实际是约 40 小时（TEXT 与 datetime 比较） 🐞 已实测复现

位置：[`store.py:371,384,403,434,444,454,482,502,520,539,612,626`](../src/perptrack/store.py#L371) · [`chart.py:85`](../src/perptrack/chart.py#L85)

`ts` 写入的是 `isoformat()` → `2026-10-05T16:02:34+00:00`，而 `datetime('now','-24 hours')` 产出 `2026-10-04 17:14:22`。SQLite 按**字节**比较，`'T'`(0x54) > `' '`(0x20)，所以**凡是落在截止日那一天的记录，不管几点，全部命中**：

```
'2026-10-04T01:00:00+00:00' >= '2026-10-04 17:14:22'  ->  1   （按时间应该是 0）

data/perp-server.db:  现有谓词  1189 行，跨度 2026-10-04T00:02 → 2026-10-05T16:02（40.0 小时）
                      真实 24h  667 行
```

受影响的是日报里的「24 小时均 / N 点」、`funding_series`（进而 `screen_min_samples`），以及 `margin_ticks` 打印的「近 N 小时」。**数字全错，而且不报错。**

**修法**：`ts` 改存 `'YYYY-MM-DD HH:MM:SS'`，或比较改写成 `julianday(ts) >= julianday('now', ?)`。

---

## 二、高（High）

### H1. `hedge exec close --dry` 永远做不了 —— 预检查的是一个 exec 从不写的库 🐞 已实测复现

位置：[`exec_cli.py:56-58`](../src/hedgebot/exec_cli.py#L56-L58) · [`live.py:180-186`](../src/hedgebot/live.py#L180-L186)

`submit()` 把 `paper=dry` 传进 `preflight`，于是 `action == "close"` 那条分支拿 `paper.db` 做校验。而 `exec_sched.py` 全程不碰 `paper.db`（它的 `on_fill` 只经 `Store.fill` 写 `live.db`）。实测：

```
dry-run hedge close blockers: ['模拟盘里 ZEC 只有 0 张空单 / 0 现货，不够平 1 张']
```

每一条 `hedge exec close … --dry` 都被拦住；万一碰巧放行（因为以前跑过 `hedge paper open`），校验的也是完全无关的陈旧状态。

### H2. 挂单超时会在交易所留下一张没人管的活单，没有对冲、没有记录 🐞 已实测复现

位置：[`ladder.py:134-167`](../src/hedgebot/ladder.py#L134-L167) · [`ladder.py:442-448`](../src/hedgebot/ladder.py#L442-L448)

POST 超时的时候，单子可能已经在交易所了。此时 `self.cur` 还是 `None`，`_safe_cancel()` 直接空转：

```
status=error  error='SourceError: kraken 挂单 buy ... ReadTimeout'
cancel calls made: []   spot order still live at exchange: True
```

代码里**没有任何**对账手段（`grep`：没有查挂单列表、没有按 `client_order_id` 回查）。这张买一之后可能成交，成交后**没有对冲、也没有记录**。[`README.md:98`](../README.md#L98) 写的是「接口超时/5xx/限频等任何意外 → 撤现货挂单」，只有订单号已知时才成立。

重启也一样：`Scheduler.resume()` 只认 `state.child_oid`（纯现货子单），**不认**对冲子单里 Ladder 的那张挂单 —— 被 SIGKILL 打断就是一个孤儿单。

### H3. 部署唯一的质检关卡是失效的

位置：[`deploy.sh:56-61`](../deploy/deploy.sh#L56-L61)

本机的 `set -euo pipefail` **不过 ssh**；远程 shell 没有 `-e`/`pipefail`，而 `&&` 的优先级高于 `|`，所以远程实际执行的是 `… && (pytest | tail -1)` —— 退出码取自 `tail`，恒为 0。**测试失败的 commit 照样部署**，然后第 68 行还会把实盘的 `hedge-exec.service` 重启一遍。第 60-61 行更糟：`… | grep … ; … | tail -3` 把冒烟检查的退出码整个丢掉。（`pip install` 失败**会**中断，只有测试失败不会。）

### H4. `SystemExit` 逃出 worker 的 `except Exception` → 静默无限崩溃循环，所有任务被堵死

位置：[`exec_cli.py:188-199`](../src/hedgebot/exec_cli.py#L188-L199) · [`live.py:48-102`](../src/hedgebot/live.py#L48-L102)

`trade_auths()` / `build()` 抛的是 `SystemExit`，它继承自 `BaseException`。`worker()` 只捕获 `Exception`。于是文档里明确支持的 `hedge exec open ZEC 1 --venue robinhood`，在服务器没有 RH 交易 key 时（deploy **故意**删掉 `.env.trade`）：worker 退出 → `Restart=on-failure` / `RestartSec=60` 重启 → 同一个任务又被选中（`exec_cli.py:181`）→ 永远循环。

任务永远不会变成 `error`，`n >= 3` 的传呼分支不可达，`StartLimitIntervalSec` 也够不着（60 秒 > 默认 10 秒窗口）所以 `OnFailure` 不触发，**排在其后的所有任务永久阻塞**。只有 journald 知道。

### H5. `build()` 期间的 SIGTERM 被忽略 —— 新建的排程器停不下来

位置：[`exec_cli.py:171-198`](../src/hedgebot/exec_cli.py#L171-L198)

`_term` 设了 `stop["flag"]`，但那时 `stop["sched"]` 还是 `None`；`build()` 返回后新建的 `Scheduler` 的 `self.stop = False`，中间没有重新检查。`systemctl restart hedge-exec.service` 正好落在这个窗口里，任务会继续交易到 `TimeoutStopSec=330` 才被 SIGKILL 打断在子单中间 —— 与 unit 自己的注释（「SIGTERM 后排程器停在安全点」）完全相反。

### H6. 对冲任务不执行 `--limit` 价格保护

位置：[`exec_sched.py:291-297`](../src/hedgebot/exec_sched.py#L291-L297) · [`exec_sched.py:456`](../src/hedgebot/exec_sched.py#L456) · [`ladder.py:281-308`](../src/hedgebot/ladder.py#L281-L308)

`_ladder()` 从不把任务的 limit 传进 `Ladder`，而 `Ladder._finish` 的档位最高到 `MARKET_BPS = 2000` —— 也就是买单可以比参考价**高 20%**。`decide()` 只在**启动**一份 240 秒子单之前拿中价和 `p.limit` 比一次。而 [`docs/exec.md:27`](exec.md#L27) 把它写成硬约束（「买不高于 / 卖不低于；超出暂停并 Pushover，不追」）。

纯现货那条路**是**夹住的（[`exec_sched.py:333-334`](../src/hedgebot/exec_sched.py#L333-L334)），所以同一个参数 `buy` 有效、`open` 无效，反差更大。

### H7. Robinhood 对冲子单会留下无人管的 GTC 挂单

位置：[`rh_trade.py:167-182`](../src/hedgebot/rh_trade.py#L167-L182)

`RobinhoodLeg.take` 下的是 **GTC** 限价单，最多等 10 秒再撤。进程在这窗口里死掉、或撤单本身失败，单子就留在交易所；成交不会被记账，`resume()` 也看不见。

叠加问题：`min_qty = p["step"]`（[`rh_trade.py:148`](../src/hedgebot/rh_trade.py#L148)）忽略了按**美元**计的 `min_order_amount` —— `min_usd` 算出来了，但在 `src/` 里**从来没有被用过**，所以低于交易所美元下限的分片单会被发出去然后被拒。

### H8. perp-track 两条「静默降级」路径

* **取数失败被当成健康**：组合余额取数失败被归为 *soft* 错误（[`sources.py:391-403`](../src/perptrack/sources.py#L391-L403)），于是快照 `ok=1` 但 `perp_margin_ratio` 缺失；`alerts.evaluate` 的 `ratio is not None` 守卫（[`alerts.py:89-90`](../src/perptrack/alerts.py#L89-L90)）为假 → **没有保证金告警、退出码 0、`fail_streak` 不受影响**。唯一的痕迹是邮件正文里一行「取数失败」。
* **升级会丢掉旧的 Pushover 回执**：`receipt` 只有一列（[`store.py:139`](../src/perptrack/store.py#L139)），升级时被覆盖（[`cli.py:1054-1059`](../src/perptrack/cli.py#L1054-L1059)），而 `cancel` 只找最新的那个（[`cli.py:997-1002`](../src/perptrack/cli.py#L997-L1002)）。warn→critical 之后**两条** priority=2 消息同时重复响，旧的会一直响到自己的 expire；若升级那次发送失败，`receipt=None` 会把还在响的旧回执**永久抹掉**，再也撤不掉。相关：expire 后的重开依赖一次回执查询，而它的失败被吞掉（[`cli.py:960-974`](../src/perptrack/cli.py#L960-L974)），没有基于时间的兜底 —— 危险持续存在却可以彻底安静下来。

### H9. exec worker 的「连续出错」计数器从不重置

位置：[`exec_cli.py:169-210`](../src/hedgebot/exec_cli.py#L169-L210)

`fails` 每次异常 +1，**成功时从不清零**（`grep` 全文件只有两处）。于是早上两次网络抖动、晚上再一次 → 任务被标 `error` 并发 priority=1 传呼，而它并不是连续失败三次。这也是 H4 的引爆点。

### H10. 文档里的执行流程，描述的是一个实盘根本不用的引擎

位置：[`README.md:36-45`](../README.md#L36-L45) · [`config.toml:44`](../config.toml#L44) · [`executor.py:1-9`](../src/hedgebot/executor.py#L1-L9)

README「执行流程」和 `config.toml [risk] max_slippage_bps`（「任一腿成交均价偏离中间价超过这么多基点，**整笔都不做**」）描述的是 `executor.run_pair`：先永续后现货、两腿成交、失败回滚。

而这个引擎**只有 `hedge paper open/close` 会调到**（`grep`：`run_pair` 只有一个调用点，[`paper.py:188`](../src/hedgebot/paper.py#L188)）。实盘走的是 [`ladder.py`](../src/hedgebot/ladder.py)：**现货先挂**、每满一张跟一张永续，而且**从不读 `max_slippage_bps`** —— 它自己的放宽档位是故意容忍 30→100→300bp 然后发**真正的市价单**。

结果是：读配置的人以为有一道 30bp 的滑点闸门在保护实盘，实际上空单腿最后一档是**无上限**的。同一根因的另一面：`hedge paper` 演练的已经不是实盘那套引擎了。

### H11. 部署会毁掉服务器本地状态，且没有 diff、备份、回滚

位置：[`deploy.sh:30-43`](../deploy/deploy.sh#L30-L43)

`rsync -az --delete` 会同步 `config.toml` 和 `perp-track.toml`，而 `--exclude` 里**没有**它们，于是服务器上的手改被静默还原 —— 仓库历史里正好有这两次事故：`0d030e6 grid_only 加 XRP（与服务器 10-04 16:36 的手改同步，避免下次部署被覆盖）`、`95c4bf3 …一天就漂了一次`。

第 43 行无条件覆盖 `.env`；第 40 行 `rm -f` 掉 `.env.trade`（密钥不出本机是对的，但按 [`docs/exec.md:68`](exec.md#L68) 在服务器上另生成的 RH key 会一起消失，且没有任何提示）。没有 `--dry-run`、没有备份、没有版本记录（`.git/` 被排除，也没有写任何 commit id）。

### H12. 传呼依赖的 `.env` 正是它要报告的那类故障源；日报单元没有失败告警

位置：[`hedge-bot-failed.service:6-8`](../deploy/hedge-bot-failed.service#L6-L8) · [`perp-track.service:1-11`](../deploy/perp-track.service#L1-L11)

`hedge-bot-failed.service` 用的是 `EnvironmentFile=%h/hedge-bot/.env` —— 只要这个文件里有一行语法坏掉，**watch 单元和它的传呼单元会一起加载失败 → 一条都发不出来**。它的节流标记还是一个全局文件，被 `hedge-bot-watch` 和 `hedge-exec` 共用：exec 崩过一次会压掉 watch 的传呼，而且文案照样写「hedge watch 进程异常退出」，把排查引到错的 unit。

隔壁的 [`perp-track-pager-failed.service`](../deploy/perp-track-pager-failed.service) 做对了（裸 curl、在 bash 里 `. ./.env`），hedge 这边应该照抄。

另外 `perp-track.service` **没有 `OnFailure`**，而 `RestartSec=300` 大于 systemd 默认的 10 秒启动限制窗口 —— 它会永远重启、永远进不了 `failed`：Python 侧的故障会静默地终结日报和所有告警。

---

## 三、中（Medium）

| # | 位置 | 问题 |
|---|---|---|
| M1 | [`exec_sched.py:508-530`](../src/hedgebot/exec_sched.py#L508-L530) | `resume()` 里的 `ld._hedge_all()` 没有任何保护；`HedgeStuck` 会一路抛出 `run()`，任务留在 `running` 且跳过 `save()`。 |
| M2 | [`exec_sched.py:298-315`](../src/hedgebot/exec_sched.py#L298-L315) · [`ladder.py:322-338`](../src/hedgebot/ladder.py#L322-L338) | `_finish` 在 `_spot_take` 只成交一部分时照样置 `done`/`trimmed`，未对冲的现货零头被报成成功。`Ladder` 会打印警告，但 exec 的任务报告从不暴露 `r.residual`。 |
| M3 | [`exec_sched.py:204-206`](../src/hedgebot/exec_sched.py#L204-L206) | `--dry` 的成交只写 `live.db`，`paper.db` 一个字都没有，所以 exec 演练任务从不出现在 `hedge paper status` 里。 |
| M4 | [`account.py:36-73`](../src/hedgebot/account.py#L36-L73) · [`watch.py:120-129`](../src/hedgebot/watch.py#L120-L129) | **缺凭据被算成取数失败**。在有意不配某个场所的主机上，`fail_streak` 一路涨，45 分钟后 `hedge watch` 永久报「监控看不见」。应区分「永久未配置」和「临时取数错误」。 |
| M5 | [`live.py:317-319`](../src/hedgebot/live.py#L317-L319) vs [`live.py:391-394`](../src/hedgebot/live.py#L391-L394) | `perp_only` 在订单循环里重新取簿子，却没检查 `best_ask/best_bid` 是否为 `None` → 成交一部分之后 `TypeError` 崩掉，任务留下 `running`。`Ladder` 这里是处理对的。 |
| M6 | [`coinbase.py:52-53`](../src/hedgebot/coinbase.py#L52-L53) | `cde_perps()` 是单次 `limit=250`，没有分页；某个 CDE 永续一旦被挤出去，所有命令都会报「CDE 没有 X 的永续」。 |
| M7 | [`market.py:47`](../src/hedgebot/market.py#L47) | `kraken.pair_info()` 在逐个标的的 `try/except` **之前**抛错，所以 `config.toml` 里一个错的交易对就能让整张 `hedge market` 表全灭，与该函数「单个失败不影响其它」的注释矛盾。 |
| M8 | [`paper.py:171-193`](../src/hedgebot/paper.py#L171-L193) | 两套并行的模拟引擎（这套 vs `Ladder` + `paper_legs`）。文档里让人「演练」的那条路，练的已经不是实盘引擎了。 |
| M9 | [`tests/`](../tests) | **下单那一层完全没有测试**：引用 `CoinbaseLeg`/`KrakenLeg` 的文件 0 个；`preflight`/`isatty`/`perp_only` 出现 0 次；`hedgebot/notify.py` 无测试；`watch.run()` 的状态机无测试（`test_watch.py` 只 import 了纯函数 `plan`）。[`test_stress.py:286`](../tests/test_stress.py#L286) 那个断言实际只等于长度检查（它引用了被测列表自己的 `perp.takes[0][2]`）。 |
| M10 | [`sources.py:338-343`](../src/perptrack/sources.py#L338-L343) | `fees_cum` 只汇总每个合约最近 200 笔成交，却当成「累计手续费」展示并参与净盈亏 → 成交一多，净盈亏被系统性高估。 |
| M11 | [`sources.py:50-58`](../src/perptrack/sources.py#L50-L58) | 429 分支覆盖了 `attempts=1`；实测一个文档声明不可重试的签名请求**发了 4 次** HTTP。对 Kraken 就是 `EAPI:Invalid nonce`，余额静默变成 `{}`。 |
| M12 | [`store.py:402-409`](../src/perptrack/store.py#L402-L409) | `funding_board` 漏了其它所有聚合都带的 `session_open` 过滤；一个闭市时段的陈旧 `apr` 可以领跑邮件里「全场 carry 前 5」。 |
| M13 | [`store.py:328-330`](../src/perptrack/store.py#L328-L330) | `GROUP BY run_date` 配裸列，取到的是任意一行：生产库上 09-29 画的是 58.6%，而当天最后一条快照是 36.4%；`recent_values` 还喂给告警的 streak。 |
| M14 | [`store.py:665-674`](../src/perptrack/store.py#L665-L674) · [`chart.py:185-188`](../src/perptrack/chart.py#L185-L188) | PT 偏移硬编码 `-7`，`report_timezone = America/Los_Angeles` 从未被读 → 整个冬令时日期分桶都是错的。资金费图最多画 6 个合约，而且是按字母序挑的。 |
| M15 | [`perp-track.toml:288`](../perp-track.toml#L288) | `fail_streak` 数的是行数，但 `Restart=on-failure`/300 秒会把一次凭据故障变成每 ~5 分钟一条失败快照：「连续 2 次」实际约 10 分钟，并且每天多出 ~288 行垃圾。与配置注释互相矛盾。 |
| M16 | [`sources.py:170-177`](../src/perptrack/sources.py#L170-L177) | `_f_or_none(x or None)` —— `_num` 自己的文档明确禁止这个写法 —— 在 CDE 费率上还在用，合法的 `0` 费率会把合约整个从样本集里丢掉。 |
| M17 | [`README.md:5`](../README.md#L5) · [`.env.example:1`](../.env.example#L1) | 「只读监控 + 模拟盘，不会下任何真实订单」被同一份 README（`live --go`）、`hedge-exec.service`、`hedge rh-order --go` 三处推翻。`.env.example` 漏掉了全部通知/邮件键（`PUSHOVER_TOKEN/USER`、`DISCORD_WEBHOOK_URL`、`SMTP_*`），而 [`README.md:74`](../README.md#L74) 指向的 `~/perp-track/.env` 也不是实际部署的 `~/hedge-bot/.env` —— 照文档做的人会得到**静默失效**的告警。 |
| M18 | [`deploy/`](../deploy) | 依赖不锁版本、每次部署都重装（`requests`、`pyjwt[crypto]`、`matplotlib`，而 `uv.lock` 被排除）；持有 View+Trade key 的 unit 没有任何 systemd 沙箱 / `MemoryMax` / `StartLimitBurst`；perp-track 没有 WAL 也没有保留策略（Kraken 一年约 190 万行，4.5 天已经 7.8 MB）；watch 单元的 `TimeoutStartSec=300` 低于最坏情况的通知预算（**每条告警**最坏约 111 秒，且是串行发送）。 |
| M19 | [`.git`](../.git) | `perp.db`（286,720 字节，真实保证金/持仓/资金费历史）在**16 个历史 blob** 里，尽管 `eaa481d` 写着「perp.db 不再跟踪」—— `.gitignore` 只管住了以后，没管住过去。当前没有配置任何 remote（仓库只有 2.5 MB），所以是潜在风险；一旦 push 就是泄漏。 |

---

## 四、低 / Nit

* `perp_only` 的确认串是 `"ZEC 2 合约"`，`execute` 是 `"ZEC 2"`，不一致但无害（[`live.py:369`](../src/hedgebot/live.py#L369)）。
* `preflight`：`contracts = args.contracts or math.floor(args.usd / …)`，`--contracts 0` 会直接抛 traceback（[`live.py:153`](../src/hedgebot/live.py#L153)）。
* `_wait_final` 以 0.5 秒轮询，忽略 Kraken 的 `min_poll_s = 3`（[`trade_api.py:309-318`](../src/hedgebot/trade_api.py#L309-L318)）；`_KrakenAuth.call` 会对 `AddOrder` 做限频重试 —— 注释里论证过是安全的，但这是唯一「重试可能下成两单」的地方，值得加一道硬保护（[`trade_api.py:203-216`](../src/hedgebot/trade_api.py#L203-L216)）。
* `cmd_rh_order` 自己重抄了一遍 `runs`/`fills` 的 DDL，没有 import `live.SCHEMA`（[`cli.py:446-450`](../src/hedgebot/cli.py#L446-L450)）；它的「还没成交完」判断漏了 `queued`（应该用 `OPEN_STATES`）。
* 未使用的 import（`trade_api.py`: `base64/hashlib/hmac/urllib.parse`；`report_html.py`: `json`；`sources.py`: `dataclasses`, `ZoneInfo`）与死代码（`report_html._status/_delta`、`store.kraken_funding_series`、永远为 0 的 `rearm_count`）。
* perp-track：`report_html.py:329` 是 HTML 邮件里**唯一**未转义的数据路径（`root_unit`）；`chart.py` 的中文标签在邮件里是豆腐块（测试跑出 52 条字形警告）；几条注释仍在描述**已知错误**的 `total_balance` 分母（[`store.py:106`](../src/perptrack/store.py#L106)、[`docs/perp-track.md:27`](perp-track.md#L27)），并与 [`docs/perp-track.md:110`](perp-track.md#L110) 自相矛盾 —— 这尤其危险，因为代码注释里明确写了这个公式曾把比值低估约 2 倍。
* 硬编码的 `rhserver`/`hedge-bot` 在 `cli.py` 与 `deploy.sh` 里重复；`deploy.sh` 的「远程目录」参数在其它的 unit 里全被硬编码成 `%h/hedge-bot`，参数化是假的。

---

## 五、做得好的地方

这部分值得写下来，因为代码整体是克制的，上面那些问题属于例外：

* **失败语义是想清楚了的。** `unwound`（已回滚）/ `aborted`（回滚也失败，裸腿，如实上报）/ `error` / `interrupted` 是分开的状态，而且只有一条安全出口，且一定先撤挂单（[`ladder.py:414-448`](../src/hedgebot/ladder.py#L414-L448)）。空单腿**只在硬错误时放弃，从不因价格放弃** —— 这个取舍是对的。
* **真实下单的门是真的**：实盘路径要求 TTY **并且**手输确认（[`live.py:432-436`](../src/hedgebot/live.py#L432-L436)、[`exec_cli.py:109-114`](../src/hedgebot/exec_cli.py#L109-L114)）；`--go` 与 `--paper` 互斥；POST 故意不自动重试（[`trade_api.py:11-12`](../src/hedgebot/trade_api.py#L11-L12)）；Kraken nonce 严格递增并重签名。
* **测试是离线、确定、且对抗性的** —— 注入时钟与 sleep，假腿把「整块被一口吃掉」「零深度」「CDE 部分成交」「两种 post-only 拒绝形态」「Ctrl-C」都编码进去了；[`test_stress.py`](../tests/test_stress.py) 逐条对应 README 的压力表，还有一个对照组证明 `clip_usd` 确实限制住了裸敞口。跑两遍结果一致（263 passed）。
* **配置在启动时大声失败**（百分数/小数陷阱、阈值顺序、Pushover API 限制）—— 对无人值守系统来说这是正确的位置。告警幂等（`seen()` + `ON CONFLICT … WHERE status='error'`）与失明退避（3/6/12/24/48）都是对的。
* **密钥卫生是对的**：`.env`/`.env.trade` 未跟踪、600、不同步到服务器（既排除又删除）、错误信息里没有密钥材料、`load_env` 不覆盖已有环境变量。保证金数学、资金费符号/周期/年化、以及 screen 的各项分母，都与官方公式一致（已独立重推）。

---

## 六、建议的修复顺序

1. **C1** —— 订单身份 + 「状态不明就停手对账，绝不重试」。如果一次 429 就能把空单悄悄放大 3 倍，别的问题都不重要。
2. **C2** —— 一行守卫，直接解锁整条强平传呼链路。
3. **C3** —— 修时间戳格式/窗口；它现在正在污染每一个展示出来的均值。
4. **H2 / H4 / H5** —— 孤儿挂单（持久化订单号、启动时对账）、worker 里的 `SystemExit`、SIGTERM 竞态。
5. **H3 / H11** —— 让部署的质检真的能拦，停止覆盖服务器配置。
6. **H6 / H1 / H9** —— 对冲子单执行 `--limit`、修 `exec --dry close`、失败计数器归零。
7. 然后是上面那张中级表，外加**给下单那一层补测试** —— 这个缺口正是 C1/H1/H2/H5 能活过一次 263 个用例的测试的原因。

两点范围说明：perp-track 在代码里已经不是「只采不判」了，所以日报单元（H12）值得配上和 margin-watch 同等的两层传呼设计；另外这个仓库没有 CI，`deploy.sh` 就是唯一的关卡 —— 这正是 H3 要紧的原因。

---

## 附：已实测复现的缺陷

都是只读的，用仓库自带的 venv 就能跑。

### C1 —— 未观测到的成交导致重复对冲

造一个「IOC 在交易所成交了、但 status 报错」的假空单腿，喂给真实的 `Ladder`：

```
status=unwound  spot_net=+0  perp_recorded=0
perp_ACTUALLY_SOLD_at_exchange=9   residual_reported=+0
```

程序认为自己在零敞口；交易所上是 9 张裸空。

### C2 —— 可用资金归零时命令先崩

`fetch_margin_lite` 在 `funds == 0` 时返回 `ok=1, margin_ratio=None`，`cmd_margin_watch` 随后的
`f"保证金率 {r:.2%}"` 直接抛 `TypeError`，而 `_page_if_needed` 在同一函数末尾。把
`margin_ratio=None` 的那一行喂给 `cmd_margin_watch` 即可看到崩溃，且 `margin_ticks` 有行、
`margin_events` 无行。

### C3 —— 24 小时窗口实际 40 小时

这里是 TEXT 比较，不是时间比较：

```text
'2026-10-04T01:00:00+00:00' >= '2026-10-04 17:14:22'   ->  1     # 按时间应为 0

data/perp-server.db  现有谓词   1189 行 / 跨度 40.0 小时
                     真实 24h    667 行
```

### H1 —— exec 演练的平仓预检

```text
dry-run hedge close blockers: ['模拟盘里 ZEC 只有 0 张空单 / 0 现货，不够平 1 张']
```
