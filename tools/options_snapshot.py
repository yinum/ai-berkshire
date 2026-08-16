#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
options_snapshot.py — 期权链确定性快照（含真实 Greeks）

**为什么要有这个文件**：`Viprasol-Tech/options-strategy-analyzer`（MIT，本仓库期权 skill 的
结构来源）在自己的正文里写明：*"You cannot fetch the chain… never invent prices, deltas,
or volatilities."* 它只能推理用户手输的数字。这个脚本把那一半补上——
从 CBOE 延迟报价拉真实链，纯 Python 算派生量，模型不碰任何数字。

数据源：`cdn.cboe.com/api/global/delayed_quotes/options/{TICKER}.json`
免费、不需鉴权，带 bid/ask/IV/OI/volume 和**交易所自己算的 delta/gamma/theta/vega/rho**。
（Yahoo 的 v7 期权接口现在要 crumb，已不可用。）

**注意是延迟报价**，不是实时。用来做研究和结构筛选够，用来卡价下单不够。

用法：
    python3 tools/options_snapshot.py AAOI
    python3 tools/options_snapshot.py AAOI --dte 20-60
    python3 tools/options_snapshot.py AAOI --json
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from tech_snapshot import tier_of, fetch_ohlcv, realized_vol   # noqa: E402

# OCC 合约代码：ROOT + YYMMDD + C/P + 8 位行权价（千分之一美元）
OCC = re.compile(r"^(?P<root>[A-Z0-9]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<k>\d{8})$")

# 期权到期是**美国交易所日期**，不是 UTC 日期。用 UTC 的话，美东晚 8 点之后
# 整个 DTE 列、窗口过滤、以及 skill 让用户填的 --horizon 全部差一天。
ET = ZoneInfo("America/New_York")

# 「流动」只有一个阈值，代码和正文共用。原来代码用 15% 而正文要求 10%，
# 结果「流动合约 17/196」这个被 skill 称为"比任何策略讨论都重要"的数字，
# 里面混着 skill 明令不许推荐的合约。
MAX_SPREAD_PCT = 10.0
MIN_OI = 50

WARNING = (
    "**以此表为准。** 下面的 delta/gamma/theta/vega、IV、买卖价、未平仓量全部来自 CBOE 延迟报价，"
    "由 `tools/options_snapshot.py` 直接读取，**模型没有参与任何一个数字的产生**。"
    "若你在别处看到不同的数字，标记冲突并同时列出两个，不要自己编一个调和后的中间值。"
)


def fetch_chain(ticker):
    url = f"https://cdn.cboe.com/api/global/delayed_quotes/options/{ticker.upper()}.json"
    try:
        r = subprocess.run(["curl", "-s", "-H", "User-Agent: Mozilla/5.0", url],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            return None
        return (json.loads(r.stdout) or {}).get("data") or None
    except Exception:
        return None


def norm_root(ticker):
    """标准合约的根代码：去掉美股股份类别的 `-` / `.`（BRK-B → BRKB）。"""
    return ticker.upper().replace("-", "").replace(".", "")


def parse_contract(row, expect_root=None):
    """解析一张合约。`expect_root` 给了就**只收标准合约**。

    交易所会为拆股 / 并购 / 特别分红调整过的合约分配带序号的根代码
    （`AAOI1`、`XYZ2`），迷你合约用 `AAPL7`。这些的**交割物不是 100 股**，
    甚至可能含现金。它们和标准合约同到期日同行权价并存，报价却完全不同。
    混进来的后果：平值 IV、跨式预期波动、流动性统计全被污染，
    而近月表里会出现两行同行权价、价格差一截、且没有任何说明。
    所以默认整张丢掉，只在汇总里报个数。**丢弃是安全的，误解析是致命的。**
    """
    m = OCC.match(row.get("option") or "")
    if not m:
        return None
    g = m.groupdict()
    if expect_root is not None and g["root"] != expect_root:
        return {"_nonstandard": True, "root": g["root"]}
    try:
        exp = datetime(2000 + int(g["y"]), int(g["m"]), int(g["d"]), tzinfo=timezone.utc)
    except ValueError:
        return None

    bid, ask = row.get("bid"), row.get("ask")
    bid = bid if isinstance(bid, (int, float)) else None
    ask = ask if isinstance(ask, (int, float)) else None

    # 只有**双边且不交叉**的报价才算真报价。交叉报价（bid > ask，延迟行情里真会出现）
    # 原来会算出负的价差百分比，然后大摇大摆通过 `<= 15` 的流动性判定——
    # 最坏的合约被标成最好的。
    two_sided = bool(bid and ask and bid > 0 and ask >= bid)
    if two_sided:
        mid, mid_source = (bid + ask) / 2, "quote"
        spread_pct = (ask - bid) / mid * 100 if mid else None
    else:
        # 成交价可能是几天前的。**绝不能冒充报价中值**，只留着并标明出处。
        lt = row.get("last_trade_price")
        mid = lt if isinstance(lt, (int, float)) and lt > 0 else None
        mid_source = "last_trade" if mid else None
        spread_pct = None

    iv = row.get("iv")
    return {
        "symbol": row["option"], "root": g["root"], "type": g["cp"],
        "strike": int(g["k"]) / 1000.0, "expiry": exp.strftime("%Y-%m-%d"),
        "bid": bid, "ask": ask, "mid": mid, "mid_source": mid_source,
        "two_sided": two_sided, "spread_pct": spread_pct,
        "iv": iv if isinstance(iv, (int, float)) and iv > 0 else None,
        "delta": row.get("delta"), "gamma": row.get("gamma"),
        "theta": row.get("theta"), "vega": row.get("vega"),
        "oi": row.get("open_interest") or 0, "volume": row.get("volume") or 0,
        "last_trade_time": row.get("last_trade_time"),
    }


def is_liquid(c):
    return (c["two_sided"] and c["spread_pct"] is not None
            and c["spread_pct"] <= MAX_SPREAD_PCT and c["oi"] >= MIN_OI)


MAX_ATM_OFFSET_PCT = 5.0    # 最近的共同行权价离现价超过这个比例就不算平值


def common_atm_strike(contracts, spot):
    """离现价最近、且 call 与 put **都有双边报价**的那个行权价。

    原来 call 和 put 各自找各自最近的行权价，两边可能落在不同的行权价上，
    偏斜一存在，两者平均出来的东西就不是平值 IV。
    另外加了距离上限：跳空之后最近的共同行权价可能离现价 10–20%，
    那时跨式里含大量内在价值，"预期波动幅度"会被严重高估。
    """
    cs = {x["strike"] for x in contracts if x["type"] == "C" and x["two_sided"]}
    ps = {x["strike"] for x in contracts if x["type"] == "P" and x["two_sided"]}
    both = cs & ps
    if not both:
        return None
    k = min(both, key=lambda s: abs(s - spot))
    return k if abs(k - spot) / spot * 100 <= MAX_ATM_OFFSET_PCT else None


def atm_iv(contracts, spot):
    """同一个共同平值行权价上，call 与 put 的 IV 均值（两边平均削掉偏斜）。

    两边任一缺 IV 就返回 None——宁可报「数据不足」，不要用单边冒充平值 IV。
    """
    k = common_atm_strike(contracts, spot)
    if k is None:
        return None
    out = []
    for cp in ("C", "P"):
        hit = [c for c in contracts
               if c["type"] == cp and c["strike"] == k and c["two_sided"] and c["iv"]]
        if not hit:
            return None
        out.append(hit[0]["iv"])
    return sum(out) / len(out)


def straddle_expected_move(contracts, spot):
    """平值跨式中间价 × 0.85。

    ⚠️ **这是「中位数级别的波幅」，不是 1 倍标准差。**
    平值跨式价 ≈ 0.8·S·σ√T，所以 0.85×跨式 ≈ 0.68σ，对应的包含概率约 50%——
    到期时大约有一半的概率会超出这个幅度。新手极容易把「±X」读成 68%（1σ）区间，
    据此把卖方的行权价定得太近。1σ 约等于 1.25×跨式价，两个都输出。

    只用双边报价的合约：陈旧的成交价冒充中值会直接污染这个数。
    """
    k = common_atm_strike(contracts, spot)
    if k is None:
        return None
    mids = {}
    for cp in ("C", "P"):
        hit = [c for c in contracts
               if c["type"] == cp and c["strike"] == k and c["two_sided"] and c["mid"]]
        if not hit:
            return None
        mids[cp] = hit[0]["mid"]
    straddle = mids["C"] + mids["P"]
    return {"strike": k, "straddle": straddle,
            "median_move": straddle * 0.85,     # ≈0.68σ，包含概率约 50%
            "one_sigma": straddle * 1.25}       # ≈1σ，包含概率约 68%


def summarize(cs, spot, exp):
    em = straddle_expected_move(cs, spot)
    iv = atm_iv(cs, spot)
    return {
        "expiry": exp, "dte": cs[0]["dte"], "atm_iv": iv,
        "atm_iv_pct": iv * 100 if iv else None,
        "atm_strike": em["strike"] if em else None,
        "straddle": em["straddle"] if em else None,
        "median_move": em["median_move"] if em else None,
        "median_move_pct": (em["median_move"] / spot * 100) if em else None,
        "one_sigma": em["one_sigma"] if em else None,
        "one_sigma_pct": (em["one_sigma"] / spot * 100) if em else None,
        "contracts": len(cs), "liquid_contracts": sum(1 for c in cs if is_liquid(c)),
        "total_oi": sum(c["oi"] for c in cs),
    }


def build(ticker, dte_lo=7, dte_hi=120):
    data = fetch_chain(ticker)
    if not data:
        return None
    spot = data.get("current_price")
    root = norm_root(ticker)

    parsed = [parse_contract(r, expect_root=root) for r in (data.get("options") or [])]
    nonstandard = sorted({p["root"] for p in parsed if p and p.get("_nonstandard")})
    rows = [p for p in parsed if p and not p.get("_nonstandard")]
    if not rows or not spot:
        return None

    today = datetime.now(ET).date()
    for r in rows:
        r["dte"] = (datetime.strptime(r["expiry"], "%Y-%m-%d").date() - today).days
    rows = [r for r in rows if r["dte"] >= 1]     # 当日到期和已过期的一律丢掉
    if not rows:
        return None

    # 已实现波动率。**复用 tech_snapshot 的盘中未收盘 bar 剔除**——
    # 之前这里 `bars, _ =` 把 meta 丢了，等于把上一轮修好的东西又退回去了。
    bars, meta = fetch_ohlcv(ticker)
    if meta.get("provisional_last_bar") and bars:
        bars = bars[:-1]
    hv20 = realized_vol([b["adjclose"] for b in bars], 20) if len(bars) > 25 else None

    # 期限结构必须看**整条链**，不能只看 --dte 窗口内的。
    # skill 默认跑 --dte 20-60，那么 10 天后的财报会同时抬高 21 天和 28 天两个到期日，
    # 二者呈升水 → 原来会打印「近月窗口内没有明显的事件溢价」——
    # 一句在有财报时依然为真的假话，而且新手会照着它行动。
    all_by_exp = {}
    for r in rows:
        all_by_exp.setdefault(r["expiry"], []).append(r)
    ladder = [summarize(all_by_exp[e], spot, e) for e in sorted(all_by_exp)]
    ladder = [x for x in ladder if x["atm_iv"]]

    term = None
    if len(ladder) >= 2:
        a, b = ladder[0], ladder[1]
        ratio = a["atm_iv"] / b["atm_iv"]
        state = ("inverted" if ratio > 1.05 else
                 "contango" if ratio < 0.95 else "flat")
        term = {"front": a["expiry"], "front_iv": a["atm_iv_pct"], "front_dte": a["dte"],
                "back": b["expiry"], "back_iv": b["atm_iv_pct"], "back_dte": b["dte"],
                "ratio": ratio, "state": state}

    expiries = [x for x in ladder if dte_lo <= x["dte"] <= dte_hi]

    return {
        "ticker": ticker.upper(), "spot": spot,
        "tier": tier_of(ticker)[0], "tier_note": tier_of(ticker)[1],
        "as_of": data.get("last_trade_time") or str(today),
        "hv20_pct": hv20,
        "expiries": expiries, "ladder": ladder, "term": term,
        "nonstandard_roots": nonstandard,
        "chain": rows,
    }


def near_money(s, exp, n=6):
    """某个到期日、离现价最近的 n 档行权价上的全部合约。

    这里**不做流动性过滤**——流动性是要给人看的判断依据，
    滤掉了反而看不见"这一片全是宽价差"。是否可交易由 `is_liquid` 单独判断。
    """
    cs = [c for c in s["chain"] if c["expiry"] == exp]
    ks = sorted({c["strike"] for c in cs}, key=lambda k: abs(k - s["spot"]))[:n * 2]
    return sorted([c for c in cs if c["strike"] in ks], key=lambda c: (c["type"], c["strike"]))


def fmt(v, unit="", nd=2):
    return "—" if v is None else f"{v:,.{nd}f}{unit}"


def render(s):
    L = []
    if s["tier"] != "T1":
        L.append(f"> 🔴 **{s['ticker']} 是 {s['tier']}（{s['tier_note']}）。"
                 f"美国零售券商买不到它的期权，以下内容不得用于任何交易建议。**\n")
    L.append(f"# {s['ticker']} 期权链快照（现价 {fmt(s['spot'])}，{s['as_of']}）\n")
    L.append(f"> {WARNING}\n")
    L.append("> ⚠️ **CBOE 延迟报价，不是实时。** 用于研究和结构筛选够用，卡价下单不够。\n")

    if s["nonstandard_roots"]:
        L.append(f"> ℹ️ 已跳过非标准根代码 {', '.join(s['nonstandard_roots'])} 的合约"
                 f"（拆股/并购调整后或迷你合约，**交割物不是 100 股**，"
                 f"与标准合约不可混算）。\n")

    L.append("## 各到期日总览\n")
    L.append("| 到期日 | DTE | 平值 IV | 中位波幅(≈50%) | 1σ(≈68%) | 流动合约 | 总未平仓 |")
    L.append("|---|---|---|---|---|---|---|")
    for e in s["expiries"]:
        L.append(f"| {e['expiry']} | {e['dte']} | {fmt(e['atm_iv_pct'], '%')} | "
                 f"±{fmt(e['median_move'])}（{fmt(e['median_move_pct'], '%')}） | "
                 f"±{fmt(e['one_sigma'])}（{fmt(e['one_sigma_pct'], '%')}） | "
                 f"{e['liquid_contracts']}/{e['contracts']} | {e['total_oi']:,.0f} |")
    L.append("")
    L.append(f"> **中位波幅**＝平值跨式中间价×0.85。**它不是 1 倍标准差**——"
             f"到期时大约有**一半**概率会超出这个幅度。把「±」当成 68% 区间是新手最常见的误读，"
             f"会把卖方的行权价定得太近。1σ 那一列（≈1.25×跨式）才是约 68% 的包含区间。\n")
    L.append(f"> 「流动合约」= 双边报价、价差 ≤{MAX_SPREAD_PCT:.0f}%、未平仓 ≥{MIN_OI}。"
             f"未平仓是**筛选下限，不是能成交的保证**——它是存量，不是盘口深度。\n")

    if s["hv20_pct"] and s["expiries"] and s["expiries"][0]["atm_iv_pct"]:
        front = s["expiries"][0]
        ratio = front["atm_iv_pct"] / s["hv20_pct"]
        L.append("## 隐含 vs 已实现波动率（描述，不是结论）\n")
        L.append(f"- 近月（{front['expiry']}，DTE {front['dte']}）平值 IV "
                 f"**{fmt(front['atm_iv_pct'], '%')}** vs 过去 20 日已实现波动率 "
                 f"**{fmt(s['hv20_pct'], '%')}** → 比值 **{ratio:.2f}**")
        L.append("")
        L.append("> 🔴 **这个比值不能推出「买方占优」或「卖方占优」。** 三个理由："
                 "① 两边的时间窗根本不同（前瞻的到期日 IV vs 回看 20 日）；"
                 "② 它忽略了波动率风险溢价——IV 长期略高于已实现是**正常**的，不是便宜或贵；"
                 "③ 近月有财报时，高 IV 完全合理，而不是「贵」。")
        L.append("")
        L.append("> ⚠️ **更不是 IV rank。** IV rank 要 52 周 IV 历史，CBOE 免费接口不给。"
                 "报告里**不许**写成 IV rank 或 IV percentile，也不许据此单独决定买还是卖权利金。\n")

    t = s.get("term")
    if t:
        L.append("## 期限结构\n")
        L.append(f"近月 {t['front']}（DTE {t['front_dte']}）IV {fmt(t['front_iv'], '%')} · "
                 f"次月 {t['back']}（DTE {t['back_dte']}）IV {fmt(t['back_iv'], '%')} · "
                 f"比值 {t['ratio']:.2f}")
        L.append("")
        if t["state"] == "inverted":
            L.append("🔴 **明显倒挂**（近月 IV 高出 5% 以上）。常见原因是近月有事件"
                     "（财报、FDA、宏观数据）。两个后果：① 近月期权贵是有原因的，事件一过 IV 会崩；"
                     "② 日历价差和对角价差在这种结构下最容易爆——你卖的那条腿正好骑在事件上。")
        elif t["state"] == "contango":
            L.append("正常升水（远月 IV 更高）。")
        else:
            L.append("基本持平，无法判断。")
        L.append("")
        L.append("> 🔴 **不能用它来证明「没有事件」。** 这只比较了整条链最近的两个到期日；"
                 "若财报落在这两个到期日**之外**（或同时抬高了两者），曲线看起来照样正常。"
                 "**持有窗口内有没有财报，必须去查财报日历或公司 IR 页面确认，不能靠这条曲线推断。**\n")

        if len(s["ladder"]) > 2:
            L.append("完整 IV 阶梯（用来自己看曲线形状）：\n")
            L.append("| 到期日 | DTE | 平值 IV |")
            L.append("|---|---|---|")
            for x in s["ladder"][:8]:
                L.append(f"| {x['expiry']} | {x['dte']} | {fmt(x['atm_iv_pct'], '%')} |")
            L.append("")

    if s["expiries"]:
        exp = s["expiries"][0]["expiry"]
        L.append(f"## 近月平值附近合约（{exp}，DTE {s['expiries'][0]['dte']}）\n")
        L.append("| 类型 | 行权价 | 买价 | 卖价 | 价差% | IV | Δ | Θ/日 | ν | 未平仓 | 成交 | 报价 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in near_money(s, exp):
            src = "双边" if c["two_sided"] else ("⚠️成交价" if c["mid_source"] else "无报价")
            L.append(f"| {'Call' if c['type'] == 'C' else 'Put'} | {fmt(c['strike'])} | "
                     f"{fmt(c['bid'])} | {fmt(c['ask'])} | {fmt(c['spread_pct'], '%', 1)} | "
                     f"{fmt(c['iv'] * 100 if c['iv'] is not None else None, '%', 1)} | "
                     f"{fmt(c['delta'], '', 3)} | "
                     f"{fmt(c['theta'], '', 3)} | {fmt(c['vega'], '', 3)} | "
                     f"{c['oi']:,.0f} | {c['volume']:,.0f} | {src} |")
        L.append("")
        L.append(f"> **价差% 是流动性的硬指标**：>{MAX_SPREAD_PCT:.0f}% 的合约，"
                 f"你一进一出光滑点就吃掉大半收益。"
                 f"「报价」列标 ⚠️成交价 的，说明它没有双边报价，那个中值可能是**几天前**的成交，"
                 f"不可用于任何计算。\n")
        L.append("> **Δ 只是单腿「到期价内」的粗略代理**（0.25 delta ≈ 25% 概率到期价内）。"
                 "它**不是盈利概率**——盈亏平衡点因为权利金的关系不等于行权价；"
                 "而且对**卖方**结构，短腿的 |Δ| 大致是**亏损**的概率，胜率约为 1 − |Δ|。"
                 "价差组合的胜率不能直接由单腿 Δ 得出。\n")
        L.append("> **Θ 是模型给出的局部敏感度**（其他条件不变时每天的价值变化），"
                 "不是保证每天到账/付出的钱。多腿要把各腿的 Θ 加总看。\n")

    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description="期权链确定性快照（CBOE 延迟报价，真实 Greeks）")
    p.add_argument("ticker")
    p.add_argument("--dte", default="7-120", help="只看这个 DTE 区间，默认 7-120")
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true", help="T2/T3 也出（只为理解，不得给建议）")
    args = p.parse_args()

    tier, why = tier_of(args.ticker)
    if tier != "T1" and not args.force:
        sys.exit(f"❌ {args.ticker.upper()} 是 {tier} 标的——{why}。\n"
                 f"   美国零售券商买不到它的期权。加 --force 只为理解，不得据此给任何建议。")

    try:
        lo, hi = (int(x) for x in args.dte.split("-"))
    except ValueError:
        sys.exit("--dte 格式是 低-高，例如 20-60")
    if lo > hi:
        sys.exit(f"--dte {args.dte}：低值大于高值，会得到空报告。")
    lo = max(lo, 1)   # 当日到期和已过期的合约永远不出

    s = build(args.ticker, lo, hi)
    if not s:
        sys.exit(f"取不到 {args.ticker.upper()} 的期权链。"
                 f"（CBOE 只覆盖美国上市期权；小盘股可能根本没有期权）")
    print(json.dumps(s, indent=2, ensure_ascii=False) if args.json else render(s))


if __name__ == "__main__":
    main()
