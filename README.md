# hedge-bot

现货多（Coinbase / Kraken / Robinhood）+ Coinbase CDE 永续空，两条腿的对冲工具。

**会下真实订单**（审查 M17：原来这里写着「只读、不下单」，早已不成立）：
- `hedge live open|close … --go`、`hedge rh-order … --go`：在**本机**跑，交易 key 在本机 `.env.trade`，终端手输确认。
- `hedge exec`（服务器常驻 `hedge-exec.service`）：用服务器 IP 绑定的 Coinbase 交易 key（`HEDGE_EXEC_KEYS`）。
- 其余（`account` / `market` / `scan` / `watch` / `paper`）只读或模拟。

现货腿可以分散在 Coinbase、Kraken（可质押）和 Robinhood，`account` 会把它们加总后与 CDE 空单对齐。

**perp-track 是另一个仓**（2026-10-02 并入、2026-10-11 拆开，各自带历史）：保证金 / 资金费监控、日报、保证金闸门都在那边，
服务器上部署在 `~/perp-track`。本仓只经子进程用它：开仓前跑 `perp-track gate`（位置由 `PERP_TRACK_BIN` 指定），
`scan --perp-track` / `funding` 读它的 `perp-track.toml` / `perp.db`（本机位置由 `PERP_TRACK_HOME` 指定）。
反过来 perp-track 经 `hedge grid-only --json` 读本仓的网格名单。
代码注释里「审查 C1 / H2 / M18…」这类编号出自 2026-10-05 的代码审查，见 [docs/code-review.md](docs/code-review.md)（H8、H11 等 perp-track 的条目已在那边修掉）。

**hedge exec**（2026-10-05）：日内执行算法 —— 你定量，程序在时间窗内挑时机分批做完对冲加减仓或现货买卖，见 [docs/exec.md](docs/exec.md)。

和 perp-track 的分工：perp-track 只看 Coinbase 那一腿，管保证金、资金费、
强平距离以及报警；这个项目看的是**两条腿合在一起**的情况，包括是否对齐、开平一轮的成本，
并在模拟盘里演练执行流程。

## 用法

```bash
hedge market                    # 全部标的：两边中价、基差、资金费年化、开平一轮成本、回本天数
hedge market LTC AVAX --usd 10000
hedge account                   # 实盘只读：现货（Kraken 含质押 + Robinhood）vs CDE 空单，标出没对齐的
hedge watch [--dry-run]         # 巡检：对齐报警 + 模拟盘记资金费（服务器上每 15 分钟跑）
hedge watch --test-notify       # 发一条测试通知，验证 Discord / Pushover 通道
hedge rh-keygen                 # 生成 Robinhood API 密钥对（私钥写入 .env，只打印公钥）
hedge paper open LTC --usd 2000 # 模拟：卖永续 + 买等量现货
hedge paper close LTC --pct 50  # 模拟反向平仓：买平空单 + 卖现货
hedge paper tick                # 记资金费，每小时跑一次
hedge paper status              # 模拟仓位、净 delta、浮盈、手续费、资金费、年化
hedge paper history             # 成交明细与已平仓记录
```

`hedge` 指的是 `.venv/bin/hedge`。模拟盘的数据存在 `data/paper.db`，想重来就删掉这个文件。

## 执行流程

**实盘和模拟盘用的是两套引擎**（审查 H10）—— 别拿下面一套的保护去推断另一套。

### 实盘：`hedge live` / `hedge exec`（`ladder.py`）

1. **预检**：CDE 开市、不临近交易日切换/维护；名义 ≤ `[live] max_notional_usd`；开仓时每张固定费
   ≤ `max_fixed_fee_bp`；余额、保证金、Robinhood 残留挂单。
2. **现货先挂单**（Coinbase/Kraken post-only；Robinhood 没有挂单优惠，逐张吃单），
   **每凑满一张就立刻吃单开/平一张空单**。
3. **空单跟进只升级不放弃**：限价逐档放宽 `hedge_escalation_bps`（30 → 100 → 300bp），
   用完后发**市价单，没有上限**。这是有意的：放弃对冲就是在最坏的时刻留下裸腿。
   **`[risk] max_slippage_bps` 不作用于实盘。** 价格保护只在现货腿上：`hedge exec --limit`
   （子单内部也是硬约束）和 `reprice_cap_bps` 追价上限。
4. 结果不明的单子先对账、不重试；对不上账就停手（`status=unknown`）。

### 模拟盘：`hedge paper open|close`（`executor.run_pair`）

1. **预检**：按当前订单簿算两条腿的成交均价，任一腿偏离中间价超过 `max_slippage_bps`，整笔都不做。
2. **先下永续，再下现货**；第二条腿失败就反向平掉第一条腿（回滚）。
   回滚也失败会报出**裸敞口**，`status` 里的净 delta 不为 0。

想用实盘那套引擎演练，用 `hedge live ... --paper` 或 `hedge exec ... --dry`（盯真实行情、模拟成交）。

仓位规模以整张合约为准：现货数量 = 张数 × 合约乘数，所以两条腿的数量严格相等。

## 模拟盘的近似

- 成交按**下单那一刻的订单簿快照**吃单计算，不考虑延迟，也不考虑自己的成交对盘口的冲击。
  规模越大越乐观。
- 资金费在每个整点按**当时的**费率记账。漏掉的小时会用当前费率补记，所以 tick 要每小时跑一次。
  闭市的整点按 0 记。
- 估值按“立即平仓”的价格：现货看买一，空单看卖一。估值不含退出时的手续费。

## 成本

`market` 的“一轮成本”= 开仓 + 平仓、两条腿全部吃单。按当前默认费率大约是名义金额的 1%，
其中 Kraken 的 0.40% × 2 占了大头。改成在 Kraken 挂单成交（0.25%）能明显缩短回本时间，
这是接实盘时第一个要做的优化。

CDE 手续费 = 比例费 + 每张固定费（2026-10-01 用 91 笔真实 maker 成交拟合：0.0677% + $0.0917/张）。
固定费让小合约吃亏（AVAX 实际 0.165%，BTC 0.079%）。taker 还没有实测样本。

## 巡检与报警（watch）

部署在 rhserver（与 perp-track 同机）：`./deploy/deploy.sh`，systemd timer 每 15 分钟一次
（:05/:20/:35/:50 UTC，错开整点结算和 perp-track）。

- **没对齐**：标 ⚠ 且净敞口 ≥ `min_alert_usd`（$200）→ Pushover priority 1 + Discord。
  持续存在按 1h/3h/6h/12h/24h 退避重发，消失时发一条恢复通知。
- **取数有任何一边失败 → 本轮不评估对齐**。否则 Robinhood 一抖，现货读成 0，就是一条假的
  「裸空」报警。已有的报警记录原样保留，连续 3 轮（45 分钟）失败才报「监控看不见」。
- **进程本身崩溃** → `hedge-bot-failed@<单元>.service` 用裸 curl 发 Pushover，文案和节流按单元分开，每个单元 6 小时内最多一次（watch、exec 都接了）。
- 凭据在服务器 `~/vault`（2026-10-06 集中，目录 700、文件 600）：单元按需挂 `coinbase-view` /
  `kraken-view` / `robinhood-view` / `notify`，部署不推送任何凭据，只校验（见 `deploy/deploy.sh`）。
  本机照旧用仓库根的 `.env`（模板 `.env.example`）。报警状态在 `data/watch.db`。

## 真实下单（live）

```bash
hedge live open LTC --spot coinbase --perp cde --contracts 2        # 只预检：余额、保证金、交易所预检、费用
hedge live open LTC --spot coinbase --perp cde --contracts 2 --go   # 真实执行，要手输「LTC 2」确认
hedge live close LTC --spot kraken --perp cde --usd 3000 --go
hedge live open HBAR --spot robinhood --perp cde --contracts 6 --go   # Robinhood v2：逐张吃单 + 跟进
```

现货挂 post-only 限价单，**分块挂**：每块最多 `clip_usd`（$2000，至少一张合约）。每成交满
一张合约的量就吃单跟进一张 CDE。挂单超过 `max_wait_s` 还没成交完：`--finish complete`（默认）
吃单补齐，`--finish trim` 把凑不满一张的零头退掉。每笔成交实时写入 `data/live.db`。

### 剧烈行情下的处理（tests/test_stress.py 逐条覆盖）

| 情况 | 处理 | 结果状态 |
|---|---|---|
| 一块挂单被一口吃掉（暴跌时最常见，逆向选择） | 最大裸敞口就是这一块；靠 `clip_usd` 控制大小 | done |
| CDE 簿子被砸薄，吃单滑点超限 | **不放弃**，当场放宽限价 30 → 100 → 300bp → 市价 | done |
| CDE 只成交一部分 | 同一轮里放宽一档继续吃 | done |
| CDE 熔断/停牌/保证金不足（硬错误）连续 3 次 | 撤现货挂单，把没对冲的现货反向退掉，回到零敞口 | unwound |
| 上面这步现货也退不掉 | 如实报告裸腿数量，需要人工处理 | aborted |
| 接口超时/5xx/限频等任何意外 | 撤现货挂单 → 对冲已成交的整张 → 报告剩余 | error |
| Ctrl-C | 同上 | interrupted |
| **下单请求超时 / 下单后查不到状态**（结果不明） | **不重试**：按订单号或客户端订单号回查，挂着就撤，按实际成交记账 | 照常继续 |
| 上面这步查不清（对不上账） | **停手，不再下任何单**（不对冲、不回滚），报出订单号去交易所核对 | unknown |
| post-only 被拒（下单瞬间价格变了） | 按新价重挂 | done |

结果里会打印「运行中最大未对冲」，就是这一轮实际承受过的单腿风险。

交易 key 放 `.env.trade`，只留本机（deploy 只传 `.env`）。

**保证金：加密现货不计入 CDE 保证金，不管币在 Coinbase 还是 Kraken。** 只有现金
（USD/USDC）计入。所以 `--spot coinbase` 买现货花掉的 USD 会直接从 CDE 购买力里扣掉；
`--spot kraken` 不动 Coinbase 的现金。预检会按「花掉的现金 + 空单隔夜保证金」检查购买力。
币价上涨时现货的浮盈也补不到空单那边，需要手动卖币换 USD 补保证金。

## 下一步（实盘）

- 先用最小规模（LTC 1 张 ≈ $335）真实跑通一次开、一次平，核对 data/live.db 与交易所记录。
- Robinhood 现货腿（现有 BTC/ETH/LTC 在那边）还不能自动平仓。
