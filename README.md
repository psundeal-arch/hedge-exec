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

`config.toml` 里 `cde_taker` / `cde_maker` 是估计值，**还没核对**。第一笔真实成交之后，
用 commission ÷ 名义金额校准。

## 下一步（实盘）

- 加一个 Venue 类实现真实下单：Kraken 走 AddOrder，CDE 走 Advanced Trade `/orders`，
  这时需要另外申请带交易权限的 key。
- Kraken 那一腿改成 post-only 限价单，成交后再用市价单对冲 CDE 那一腿。
- 先用最小规模跑通一次开平，再放大。
