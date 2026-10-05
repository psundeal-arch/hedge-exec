"""开仓硬约束与出场容量：纯计算，不联网。"""
import pytest

from hedgebot.config import load_config
from hedgebot.live import fixed_fee_blocker, fixed_fee_bp
from hedgebot.scan import walk_bp

CFG = load_config()


def test_fixed_fee_is_a_hard_limit_on_small_contracts():
    """AVAX 一张 $109：$0.12 固定费 = 11bp/边，往返 22bp —— 拒绝，而不是只在表里印个成本。"""
    assert fixed_fee_bp(CFG, 109) == pytest.approx(CFG.fees.cde_per_contract / 109 * 1e4)
    why = fixed_fee_blocker(CFG, 109)
    assert why and "bp/边" in why and "max_fixed_fee_bp" in why
    assert fixed_fee_blocker(CFG, 1320) is None                  # ZEC 0.9bp 放行
    assert fixed_fee_blocker(CFG, 0) is not None                 # 没价格不能当成免费


def test_walk_bp_measures_own_exit_not_entry_depth():
    """出场 $25k：第一档 100 张 @ 中价+5bp（$10,005），第二档 200 张 @ 中价+20bp → 吃到第二档。"""
    mid = 100.0
    asks = ((100.05, 100.0), (100.20, 200.0))                    # 数量是张，每张 1 枚
    bp = walk_bp(asks, 25_000, mid, cs=1.0)
    coins = 100 + (25_000 - 100 * 100.05) / 100.20               # 第一档吃光，余下在第二档
    assert bp == pytest.approx((25_000 / coins / mid - 1) * 1e4)
    assert 5 < bp < 20
    assert walk_bp(asks, 50_000, mid) is None, "簿子吃不完要说出来，不能当成没冲击"
    # 卖盘（bids）方向：成交价低于中价，冲击仍是正数
    assert walk_bp(((99.9, 1000.0),), 10_000, mid) == pytest.approx(10, rel=1e-6)
