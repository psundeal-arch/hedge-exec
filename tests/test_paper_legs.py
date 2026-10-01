"""模拟腿的成交判定。"""
import pytest

from hedgebot.book import Book
from hedgebot.paper_legs import PaperPerpLeg, PaperSpotLeg


class Real:
    name, tick, lot, min_qty = "coinbase", 0.01, 0.001, 0.001

    def __init__(self):
        self.bid, self.ask = 100.0, 100.1

    def book(self):
        return Book(bids=((self.bid, 50), (self.bid - 0.1, 50)),
                    asks=((self.ask, 50), (self.ask + 0.1, 50)))


def leg(trades):
    real = Real()
    clock = {"t": 1000.0}
    lg = PaperSpotLeg(real, maker_fee=0.0015, taker_fee=0.003,
                      trades_fn=lambda: trades, now=lambda: clock["t"])
    return lg, real


def test_post_only_rejected_when_crossing():
    lg, _ = leg([])
    st = lg.status(lg.place_post_only("buy", 1, 100.1))
    assert not st.open and st.filled == 0 and "post-only" in st.reason


def test_trade_through_fills_partially_then_fully():
    trades = []
    lg, _ = leg(trades)
    oid = lg.place_post_only("buy", 5, 100.0)          # 排在已有买一后面（没改善价格）
    trades.append(("t0", 999.0, 99.9, 9))              # 挂单之前的成交，不算
    trades.append(("t1", 1001.0, 100.0, 2))            # 同价：前面有人排队，不算
    trades.append(("t2", 1002.0, 99.95, 3))            # 更低价成交：一定先吃到我们
    st = lg.status(oid)
    assert st.filled == 3 and st.open
    assert st.fee == pytest.approx(3 * 100.0 * 0.0015)
    trades.append(("t3", 1003.0, 99.9, 10))
    st = lg.status(oid)
    assert st.filled == 5 and not st.open
    assert lg.status(oid).filled == 5                  # 同一笔成交不重复计


def test_improved_price_counts_equal_trades():
    trades = [("t1", 1001.0, 100.05, 2)]
    lg, _ = leg(trades)
    oid = lg.place_post_only("buy", 5, 100.05)         # 比买一好一档，价位上只有我们
    assert lg.status(oid).filled == 2


def test_book_crossing_fills_rest():
    lg, real = leg([])
    oid = lg.place_post_only("sell", 4, 100.1)
    real.bid = 100.15                                   # 买一越过了我们的卖价
    st = lg.status(oid)
    assert st.filled == 4 and not st.open


def test_take_respects_limit_and_depth():
    lg, _ = leg([])
    st = lg.take("buy", 80, limit_px=100.1)            # 限价内只有 50
    assert st.filled == 50 and st.avg == pytest.approx(100.1)
    perp = PaperPerpLeg(Real(), contract_size=5, taker_fee=0.0007, per_contract=0.12)
    st = perp.take("sell", 2, limit_px=99.0)
    assert st.filled == 2 and st.fee == pytest.approx(2 * 5 * 100.0 * 0.0007 + 2 * 0.12)
