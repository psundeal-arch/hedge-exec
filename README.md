# hedge-bot

Kraken 现货多 + Coinbase CDE 永续空，两条腿的对冲工具。

**当前阶段：只读监控 + 模拟盘，不会下任何真实订单。** 所有 API key 都只要只读权限。

现货腿可以分散在 Kraken（可质押）和 Robinhood，`account` 会把它们加总后与 CDE 空单对齐。
Robinhood 只做持仓读取；`paper` 模拟盘的现货腿目前只按 Kraken 的订单簿成交。

和 [perp-track](../perp-track) 的分工：perp-track 只看 Coinbase 那一腿，管保证金、资金费、
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

## 执行流程（模拟盘和将来的实盘共用）

1. **预检**：CDE 必须开市；按当前订单簿算两条腿各自的成交均价，任一腿偏离中间价超过
   `max_slippage_bps`，整笔都不做。
2. **先下永续，再下现货。** CDE 有闭市时段、只能整张成交、深度也薄，是更容易失败的一腿。
   它失败时现货还没动，不需要撤任何东西。
3. **第二条腿失败，就反向平掉第一条腿（回滚）**。回滚产生的手续费和价差会计为一笔亏损。
   如果回滚也失败，会报出**裸敞口**，`status` 里的净 delta 会显示不为 0，必须人工处理。

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
- **进程本身崩溃** → `hedge-bot-failed.service` 用裸 curl 发 Pushover，6 小时内最多一次。
- 通知凭据读 `~/perp-track/.env`，不复制密钥。报警状态在 `data/watch.db`。

## 真实下单（live）

```bash
hedge live open LTC --spot coinbase --contracts 2        # 只预检：余额、保证金、交易所预检、费用
hedge live open LTC --spot coinbase --contracts 2 --go   # 真实执行，要手输「LTC 2」确认
hedge live close LTC --spot kraken --usd 3000 --go
```

现货挂 post-only 限价单，每成交满一张合约的量就吃单跟进一张 CDE。任意时刻没对冲的敞口不到
一张。挂单超过 `max_wait_s` 没成交完：`--finish complete`（默认）吃单补齐，`--finish trim`
把凑不满一张的零头退掉。空单连续 3 次跟不上就撤单停手，并报告裸腿。Ctrl-C 也会先撤单、
再对冲已成交的整张。每笔成交实时写入 `data/live.db`。

交易 key 放 `.env.trade`，只留本机（deploy 只传 `.env`）。

**保证金：加密现货不计入 CDE 保证金，不管币在 Coinbase 还是 Kraken。** 只有现金
（USD/USDC）计入。所以 `--spot coinbase` 买现货花掉的 USD 会直接从 CDE 购买力里扣掉；
`--spot kraken` 不动 Coinbase 的现金。预检会按「花掉的现金 + 空单隔夜保证金」检查购买力。
币价上涨时现货的浮盈也补不到空单那边，需要手动卖币换 USD 补保证金。

## 下一步（实盘）

- 先用最小规模（LTC 1 张 ≈ $335）真实跑通一次开、一次平，核对 data/live.db 与交易所记录。
- Robinhood 现货腿（现有 BTC/ETH/LTC 在那边）还不能自动平仓。
