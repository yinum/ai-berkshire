#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""决策日志与技术面快照的回归测试。

每个用例都对应一条交叉评审（Codex + Fable 5）实际发现的缺陷——
不是为了覆盖率凑数，是为了让那几条 bug 回不来。纯标准库，不联网。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import decision_log as D          # noqa: E402
import tech_snapshot as T         # noqa: E402
import options_snapshot as O      # noqa: E402


def make_meta(**kw):
    m = {"id": "20260101-AAA-L01", "ticker": "AAA", "market": "us", "tier": "T1",
         "rating": "Sell", "decided_on": "2026-01-01", "horizon_class": "long",
         "horizon_days": "180", "benchmark": "SPY", "entry_date": "2026-01-02",
         "entry_price": "10.0", "bench_entry": "100.0", "status": "pending",
         "source": "a.md", "retro": "false"}
    m.update(kw)
    return m


class RoundTrip(unittest.TestCase):
    """render → parse → render 必须原样往返。理由字段是只写一次的，
    往返一旦不等幂，下一次 resolve/reflect 就会悄悄改掉它。"""

    def rt(self, thesis, kill, reflection=""):
        text = D.HEADER + "\n\n" + D.render_entry(make_meta(), thesis, kill, reflection) + "\n"
        got = D.parse_entries(text)
        self.assertEqual(len(got), 1)
        return got[0]

    def test_plain(self):
        e = self.rt("论文", "证伪条件", "反思内容")
        self.assertEqual((e["_thesis"], e["_kill"], e["_reflection"]),
                         ("论文", "证伪条件", "反思内容"))

    def test_idempotent(self):
        e = self.rt("论文", "证伪条件", "反思")
        again = D.render_entry(e, e["_thesis"], e["_kill"], e["_reflection"])
        parsed = D.parse_entries(D.HEADER + "\n\n" + again + "\n")[0]
        self.assertEqual(parsed["_thesis"], "论文")
        self.assertEqual(parsed["_kill"], "证伪条件")
        self.assertEqual(parsed["_reflection"], "反思")

    def test_empty_placeholder_reads_back_empty(self):
        # 渲染成 "—" 的空字段，读回来必须是空串，否则 context 会打印「当初的论文：—」
        e = self.rt("", "")
        self.assertEqual(e["_thesis"], "")
        self.assertEqual(e["_kill"], "")


class HostileProse(unittest.TestCase):
    """会破坏结构的正文必须在写入前就被拒绝（Codex critical / Fable medium）。"""

    def test_rejects_metadata_shaped_line(self):
        # 模型写反思时写出 "- `kill`: 写得太宽" 是完全合理的，
        # 而它会以「后者赢」覆盖真元数据
        with self.assertRaises(SystemExit):
            D.check_prose("thesis", "开头\n- `status`: resolved\n结尾")

    def test_rejects_sentinels(self):
        for tok in (D.ENTRY_START, D.ENTRY_END):
            with self.assertRaises(SystemExit):
                D.check_prose("thesis", f"论文 {tok} 后面")

    def test_rejects_heading_and_comment(self):
        with self.assertRaises(SystemExit):
            D.check_prose("kill", "前面\n### 新小节\n后面")
        with self.assertRaises(SystemExit):
            D.check_prose("kill", "前面\n<!-- 注释 -->\n后面")

    def test_allows_normal_chinese_prose(self):
        D.check_prose("thesis", "毛利率连续两季低于 20%，且经营现金流为负。\n第二行也没问题。")

    def test_metadata_only_read_before_body(self):
        # 即使正文里混进了元数据形状的行，解析也只认正文区之前的
        text = D.HEADER + "\n\n" + D.render_entry(make_meta(), "论文", "证伪", "") + "\n"
        text = text.replace("### 反思\n\n待回填", "### 反思\n\n- `status`: resolved\n待回填")
        e = D.parse_entries(text)[0]
        self.assertEqual(e["status"], "pending")   # 没有被正文里那行覆盖


class Framing(unittest.TestCase):
    """分隔符缺失必须抛错，不能"尽力而为"地解析（两位评审都点名）。"""

    def two_entries(self):
        a = D.render_entry(make_meta(), "第一条论文", "第一条证伪", "")
        b = D.render_entry(make_meta(id="20260102-BBB-L01", ticker="BBB",
                                     decided_on="2026-01-02"), "第二条论文", "第二条证伪", "")
        return D.HEADER + "\n\n" + a + "\n\n" + b + "\n"

    def test_healthy_log_parses(self):
        self.assertEqual(len(D.parse_entries(self.two_entries())), 2)

    def test_missing_end_marker_raises(self):
        # 修复前：静默变成 1 条，且幸存那条顶着另一条的 thesis/kill
        broken = self.two_entries().replace(D.ENTRY_END, "", 1)
        with self.assertRaises(D.LogCorrupt):
            D.parse_entries(broken)

    def test_duplicate_id_raises(self):
        dup = self.two_entries().replace("20260102-BBB-L01", "20260101-AAA-L01")
        with self.assertRaises(D.LogCorrupt):
            D.parse_entries(dup)

    def test_missing_required_field_raises(self):
        bad = self.two_entries().replace("- `rating`: Sell", "- `rating`: —", 1)
        with self.assertRaises(D.LogCorrupt):
            D.parse_entries(bad)


class Direction(unittest.TestCase):
    """看空判断涨了要算错。不做这个转换，去劣筛选出的结论会被系统性记成赢。"""

    def measure(self, rating, s_ret, b_ret):
        dates = ["2026-01-02", "2026-06-01"]
        smap = {"2026-01-02": 100.0, "2026-06-01": 100.0 * (1 + s_ret)}
        bmap = {"2026-01-02": 100.0, "2026-06-01": 100.0 * (1 + b_ret)}
        return D.measure(make_meta(rating=rating), dates, smap, bmap, "2026-06-01")

    def test_sell_that_rallies_is_wrong(self):
        m = self.measure("Sell", 0.20, 0.05)
        self.assertAlmostEqual(m["alpha"], 15.0, places=6)
        self.assertLess(m["call_alpha"], 0)          # 判错

    def test_sell_that_falls_is_right(self):
        m = self.measure("Sell", -0.10, 0.05)
        self.assertGreater(m["call_alpha"], 0)

    def test_buy_up_but_lagging_is_wrong(self):
        # 赚了 18% 而基准 25% —— 论文错了，不是赚了
        m = self.measure("Buy", 0.18, 0.25)
        self.assertGreater(m["raw"], 0)
        self.assertLess(m["call_alpha"], 0)

    def test_hold_not_scored(self):
        self.assertIsNone(self.measure("Hold", 0.20, 0.05)["call_alpha"])


class PriceBasis(unittest.TestCase):
    """两端价格必须来自同一份序列（Codex critical：复权基准错配）。"""

    def test_split_does_not_look_like_a_50pct_loss(self):
        # 模拟 2:1 拆股后 Yahoo 回溯调整：全部历史 adjclose 减半。
        # 只要入场价和退出价都从这份**新**序列里取，收益就仍然是 +10%。
        dates = ["2026-01-02", "2026-06-01"]
        smap = {"2026-01-02": 50.0, "2026-06-01": 55.0}      # 已回溯调整
        bmap = {"2026-01-02": 100.0, "2026-06-01": 100.0}
        m = D.measure(make_meta(entry_price="100.0"), dates, smap, bmap, "2026-06-01")
        self.assertAlmostEqual(m["raw"], 10.0, places=6)
        # 若误用条目里存的旧基准入场价 100.0，会算成 -45%
        self.assertNotAlmostEqual(m["raw"], -45.0, places=1)

    def test_uses_common_dates_only(self):
        dates = ["2026-01-02"]           # 只有一个共同交易日
        self.assertEqual(D.pick_date(dates, "2026-01-01"), "2026-01-02")
        self.assertIsNone(D.pick_date(dates, "2026-02-01"))
        self.assertIsNone(D.pick_date([], None))


class Ratings(unittest.TestCase):
    def test_five_tier_aliases(self):
        for raw, want in [("★★★★☆", "Overweight"), ("回避", "Sell"), ("不通过", "Sell"),
                          ("排除", "Sell"), ("买入", "Buy"), ("观望", "Hold"),
                          ("Underweight", "Underweight")]:
            self.assertEqual(D.normalize_rating(raw), want, raw)

    def test_pass_is_not_a_call(self):
        self.assertIn("通过", D.NON_CALLS)

    def test_next_id_avoids_collision(self):
        existing = [{"id": "20260101-AAA-L01"}, {"id": "20260101-AAA-L02"}]
        self.assertEqual(D.next_id(existing, "AAA", "2026-01-01", "long"), "20260101-AAA-L03")
        # 中间被手删一条也不能撞号
        gapped = [{"id": "20260101-AAA-L02"}]
        self.assertNotIn(D.next_id(gapped, "AAA", "2026-01-01", "long"), {"20260101-AAA-L02"})

    def test_num_rejects_nan_inf(self):
        self.assertIsNone(D.num("nan"))
        self.assertIsNone(D.num("inf"))
        self.assertIsNone(D.num("—"))
        self.assertEqual(D.num("1.5"), 1.5)


class Tiers(unittest.TestCase):
    """白名单：未知后缀一律不可交易（两位评审都点名的最高频漏洞）。"""

    def test_us_symbols_are_t1(self):
        for t in ("AAOI", "BRK-B", "BF-B", "SPY"):
            self.assertEqual(T.tier_of(t)[0], "T1", t)

    def test_unknown_suffixes_are_not_t1(self):
        for t in ("SHOP.TO", "005930.KS", "BP.L", "SAP.DE", "BHP.AX", "ITX.MC"):
            self.assertNotEqual(T.tier_of(t)[0], "T1", t)

    def test_taiwan_otc_is_t3(self):
        # CLAUDE.md 明写台股「上市/上柜」都是 T3，旧的黑名单漏了 .TWO
        self.assertEqual(T.tier_of("6488.TWO")[0], "T3")
        self.assertEqual(T.tier_of("2492.TW")[0], "T3")

    def test_non_equities_rejected(self):
        for t in ("^GSPC", "GC=F", "BTC-USD", "EURUSD=X"):
            self.assertEqual(T.tier_of(t)[0], "T3", t)

    def test_known_tiers_unchanged(self):
        self.assertEqual(T.tier_of("0700.HK")[0], "T2")
        self.assertEqual(T.tier_of("7203.T")[0], "T2")
        self.assertEqual(T.tier_of("300285.SZ")[0], "T3")


class Indicators(unittest.TestCase):
    def test_rsi_all_gains_is_100(self):
        self.assertEqual(T.rsi([100 + i for i in range(30)], 14), 100.0)

    def test_rsi_flat_series_is_neutral(self):
        # 0/0 时 Wilder RSI 无定义；返回 100 会把停牌股标成「超买」
        self.assertEqual(T.rsi([100.0] * 30, 14), 50.0)

    def test_rsi_wilder_reference(self):
        # Wilder《New Concepts》经典 14 日算例的收盘价序列
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
                  45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
        self.assertAlmostEqual(T.rsi(closes, 14), 70.46, delta=0.2)

    def test_macd_alignment_matches_naive(self):
        closes = [100 + (i % 7) - (i % 3) * 1.5 + i * 0.3 for i in range(120)]
        got = T.macd(closes)
        ef, es = T.ema(closes, 12), T.ema(closes, 26)
        ef = ef[len(ef) - len(es):]
        line = [a - b for a, b in zip(ef, es)]
        sig = T.ema(line, 9)
        self.assertAlmostEqual(got["macd"], line[-1], places=9)
        self.assertAlmostEqual(got["signal"], sig[-1], places=9)
        self.assertAlmostEqual(got["hist"], line[-1] - sig[-1], places=9)

    def test_atr_is_positive_and_bounded(self):
        rows = [{"high": 10 + i * 0.1, "low": 9 + i * 0.1, "close": 9.5 + i * 0.1}
                for i in range(40)]
        a = T.atr(rows, 14)
        self.assertGreater(a, 0)
        self.assertLess(a, 5)

    def test_bollinger_population_sigma(self):
        closes = [float(i) for i in range(1, 21)]
        bb = T.bollinger(closes, 20, 2)
        m = sum(closes) / 20
        sd = (sum((x - m) ** 2 for x in closes) / 20) ** 0.5   # 总体 σ，不是样本 σ
        self.assertAlmostEqual(bb["mid"], m, places=9)
        self.assertAlmostEqual(bb["upper"], m + 2 * sd, places=9)

    def test_swing_levels_handles_plateau_and_none(self):
        rows = [{"high": 10.0, "low": 9.0, "close": 9.5} for _ in range(30)]
        rows[15]["high"] = None          # 缺失的 bar 不能让它崩
        rows[16]["low"] = None
        lv = T.swing_levels(rows)
        self.assertIn("support", lv)
        self.assertIn("resistance", lv)

    def test_max_drawdown_sign(self):
        self.assertAlmostEqual(T.max_drawdown([100, 50]), -50.0, places=6)
        self.assertAlmostEqual(T.max_drawdown([100, 110]), 0.0, places=6)


class Options(unittest.TestCase):
    """期权链解析。行权价放大 1000 倍存在 OCC 代码里，解错一位就是 10 倍的价位。"""

    def test_occ_symbol_parsed(self):
        c = O.parse_contract({"option": "AAOI260911P00150000", "bid": 16.4, "ask": 19.3,
                              "iv": 1.103, "delta": -0.4325, "open_interest": 5, "volume": 15})
        self.assertEqual(c["type"], "P")
        self.assertEqual(c["strike"], 150.0)
        self.assertEqual(c["expiry"], "2026-09-11")
        self.assertAlmostEqual(c["mid"], 17.85, places=6)

    def test_fractional_strike(self):
        c = O.parse_contract({"option": "AAOI260911C00152500", "bid": 1.0, "ask": 1.2})
        self.assertEqual(c["strike"], 152.5)
        self.assertEqual(c["type"], "C")

    def test_numeric_root_ok(self):
        # 有些标的代码带数字，正则不能因此拒绝
        self.assertIsNotNone(O.parse_contract({"option": "BRKB260918C00500000",
                                               "bid": 1.0, "ask": 1.1}))

    def test_malformed_symbol_returns_none(self):
        for bad in ("", "NOTANOPTION", "AAOI2609P00150000", "AAOI269911C00150000"):
            self.assertIsNone(O.parse_contract({"option": bad}), bad)

    def test_spread_pct(self):
        c = O.parse_contract({"option": "AAOI260911C00150000", "bid": 9.0, "ask": 11.0})
        self.assertAlmostEqual(c["mid"], 10.0, places=6)
        self.assertAlmostEqual(c["spread_pct"], 20.0, places=6)   # (11−9)/10

    def test_atm_iv_uses_nearest_strike_both_sides(self):
        cs = [{"type": "C", "strike": 100.0, "iv": 0.40},
              {"type": "C", "strike": 150.0, "iv": 0.60},
              {"type": "P", "strike": 150.0, "iv": 0.80}]
        self.assertAlmostEqual(O.atm_iv(cs, 149.0), 0.70, places=6)   # (0.60+0.80)/2

    def test_expected_move_from_straddle(self):
        cs = [{"type": "C", "strike": 100.0, "mid": 6.0},
              {"type": "P", "strike": 100.0, "mid": 4.0}]
        self.assertAlmostEqual(O.straddle_expected_move(cs, 100.0), 8.5, places=6)

    def test_expected_move_needs_both_sides(self):
        self.assertIsNone(O.straddle_expected_move([{"type": "C", "strike": 1.0, "mid": 1.0}], 1.0))

    def test_tier_gate_shared_with_tech_snapshot(self):
        # 期权工具复用同一个白名单，不能各判各的
        self.assertEqual(O.tier_of("300285.SZ")[0], "T3")
        self.assertEqual(O.tier_of("AAOI")[0], "T1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
