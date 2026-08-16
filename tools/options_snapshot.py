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
import math
import re
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from tech_snapshot import tier_of, fetch_ohlcv, realized_vol   # noqa: E402

# OCC 合约代码：ROOT + YYMMDD + C/P + 8 位行权价（千分之一美元）
OCC = re.compile(r"^(?P<root>[A-Z0-9]+)(?P<y>\d{2})(?P<m>\d{2})(?P<d>\d{2})(?P<cp>[CP])(?P<k>\d{8})$")

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


def parse_contract(row):
    m = OCC.match(row.get("option", ""))
    if not m:
        return None
    g = m.groupdict()
    try:
        exp = datetime(2000 + int(g["y"]), int(g["m"]), int(g["d"]), tzinfo=timezone.utc)
    except ValueError:
        return None
    bid, ask = row.get("bid") or 0.0, row.get("ask") or 0.0
    mid = (bid + ask) / 2 if (bid and ask) else (row.get("last_trade_price") or 0.0)
    spread_pct = ((ask - bid) / mid * 100) if (mid and ask and bid) else None
    return {
        "symbol": row["option"], "type": g["cp"], "strike": int(g["k"]) / 1000.0,
        "expiry": exp.strftime("%Y-%m-%d"),
        "bid": bid, "ask": ask, "mid": mid, "spread_pct": spread_pct,
        "iv": row.get("iv"), "delta": row.get("delta"), "gamma": row.get("gamma"),
        "theta": row.get("theta"), "vega": row.get("vega"),
        "oi": row.get("open_interest") or 0, "volume": row.get("volume") or 0,
    }


def atm_iv(contracts, spot):
    """最接近平值的 call 与 put 的 IV 均值。用两边平均是为了削掉偏斜的影响。"""
    out = []
    for cp in ("C", "P"):
        side = [c for c in contracts if c["type"] == cp and c["iv"]]
        if side:
            out.append(min(side, key=lambda c: abs(c["strike"] - spot))["iv"])
    return sum(out) / len(out) if out else None


def straddle_expected_move(contracts, spot):
    """预期波动幅度 ≈ 平值跨式中间价 × 0.85。

    比用 IV×√(T/365) 更稳——它直接读市场为这个到期日实际付出的价格，
    不依赖年化假设，也自动含了偏斜。0.85 是业内常用的近似系数。
    """
    c = [x for x in contracts if x["type"] == "C" and x["mid"]]
    p = [x for x in contracts if x["type"] == "P" and x["mid"]]
    if not c or not p:
        return None
    k = min({x["strike"] for x in c} & {x["strike"] for x in p},
            key=lambda s: abs(s - spot), default=None)
    if k is None:
        return None
    cm = next(x["mid"] for x in c if x["strike"] == k)
    pm = next(x["mid"] for x in p if x["strike"] == k)
    return (cm + pm) * 0.85


def build(ticker, dte_lo=7, dte_hi=120):
    data = fetch_chain(ticker)
    if not data:
        return None
    spot = data.get("current_price")
    rows = [parse_contract(r) for r in (data.get("options") or [])]
    rows = [r for r in rows if r]
    if not rows or not spot:
        return None

    today = datetime.now(timezone.utc).date()
    for r in rows:
        r["dte"] = (datetime.strptime(r["expiry"], "%Y-%m-%d").date() - today).days

    # 已实现波动率：拿来和隐含波动率比。CBOE 不给 IV rank（需要 52 周 IV 历史），
    # 用 IV/HV 作**替代指标**——必须如实标注它不是 IV rank，只是"隐含 vs 近期实际"。
    bars, _ = fetch_ohlcv(ticker)
    hv20 = realized_vol([b["adjclose"] for b in bars], 20) if len(bars) > 25 else None

    by_exp = {}
    for r in rows:
        if dte_lo <= r["dte"] <= dte_hi:
            by_exp.setdefault(r["expiry"], []).append(r)

    expiries = []
    for exp in sorted(by_exp):
        cs = by_exp[exp]
        iv = atm_iv(cs, spot)
        em = straddle_expected_move(cs, spot)
        liquid = [c for c in cs if c["oi"] >= 50 and c["spread_pct"] is not None
                  and c["spread_pct"] <= 15]
        expiries.append({
            "expiry": exp, "dte": cs[0]["dte"], "atm_iv": iv,
            "atm_iv_pct": iv * 100 if iv else None,
            "expected_move": em,
            "expected_move_pct": (em / spot * 100) if em else None,
            "contracts": len(cs), "liquid_contracts": len(liquid),
            "total_oi": sum(c["oi"] for c in cs),
        })

    # 期限结构：正常是升水（远月 IV > 近月）。倒挂 = 近月有事件（财报/宏观）。
    # 这是**不需要财报日历**就能读出事件的办法，也是日历价差最大的雷。
    backwardation = None
    if len(expiries) >= 2:
        a, b = expiries[0], expiries[1]
        if a["atm_iv"] and b["atm_iv"]:
            backwardation = {
                "front": a["expiry"], "front_iv": a["atm_iv_pct"],
                "back": b["expiry"], "back_iv": b["atm_iv_pct"],
                "inverted": a["atm_iv"] > b["atm_iv"] * 1.05,
            }

    return {
        "ticker": ticker.upper(), "spot": spot,
        "tier": tier_of(ticker)[0], "tier_note": tier_of(ticker)[1],
        "as_of": data.get("last_trade_time") or str(today),
        "hv20_pct": hv20,
        "expiries": expiries,
        "backwardation": backwardation,
        "chain": rows,
    }


def near_money(s, exp, n=6):
    """某个到期日、平值上下各 n 档、且够流动的合约。"""
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

    L.append("## 各到期日总览\n")
    L.append("| 到期日 | DTE | 平值 IV | 预期波动幅度 | 流动合约 | 总未平仓 |")
    L.append("|---|---|---|---|---|---|")
    for e in s["expiries"]:
        L.append(f"| {e['expiry']} | {e['dte']} | {fmt(e['atm_iv_pct'], '%')} | "
                 f"±{fmt(e['expected_move'])}（{fmt(e['expected_move_pct'], '%')}） | "
                 f"{e['liquid_contracts']}/{e['contracts']} | {e['total_oi']:,.0f} |")
    L.append("")
    L.append("> **预期波动幅度**＝平值跨式中间价×0.85，是市场为这个到期日**实际付的钱**"
             "隐含的涨跌幅。你的判断如果落在这个幅度以内，买期权基本是白付时间价值——"
             "因为这个幅度已经被定价了。\n")

    if s["hv20_pct"]:
        front = s["expiries"][0] if s["expiries"] else None
        if front and front["atm_iv_pct"]:
            ratio = front["atm_iv_pct"] / s["hv20_pct"]
            judge = ("隐含明显贵于近期实际波动 → 卖方占优" if ratio > 1.3 else
                     "隐含明显便宜于近期实际波动 → 买方占优" if ratio < 0.8 else
                     "隐含与近期实际接近 → 波动率上没有明显便宜或贵")
            L.append("## 隐含 vs 已实现波动率\n")
            L.append(f"- 近月平值 IV **{fmt(front['atm_iv_pct'], '%')}** vs "
                     f"过去 20 日已实现波动率 **{fmt(s['hv20_pct'], '%')}** "
                     f"→ 比值 **{ratio:.2f}**（{judge}）")
            L.append("")
            L.append("> ⚠️ **这不是 IV rank。** IV rank 要 52 周 IV 历史，CBOE 免费接口不给。"
                     "这里用「隐含 ÷ 近期已实现」作替代指标——方向性参考可以，"
                     "**不要在报告里把它写成 IV rank 或 IV percentile**。\n")

    b = s.get("backwardation")
    if b:
        L.append("## 期限结构\n")
        if b["inverted"]:
            L.append(f"🔴 **倒挂**：近月 {b['front']} IV {fmt(b['front_iv'], '%')} "
                     f"> 次月 {b['back']} IV {fmt(b['back_iv'], '%')}。")
            L.append("")
            L.append("> 正常应该是远月 IV 更高（升水）。**倒挂几乎总是意味着近月有事件**"
                     "（财报、FDA、宏观数据）。两个后果：①  近月期权贵是有原因的，事件一过 IV 会崩；"
                     "② 日历价差和对角价差在这种结构下最容易爆——你卖的那条腿正好骑在事件上。")
        else:
            L.append(f"正常升水：近月 {b['front']} IV {fmt(b['front_iv'], '%')} "
                     f"≤ 次月 {b['back']} IV {fmt(b['back_iv'], '%')}。近月窗口内没有明显的事件溢价。")
        L.append("")

    if s["expiries"]:
        exp = s["expiries"][0]["expiry"]
        L.append(f"## 近月平值附近合约（{exp}，DTE {s['expiries'][0]['dte']}）\n")
        L.append("| 类型 | 行权价 | 买价 | 卖价 | 价差% | IV | Δ | Θ/日 | ν | 未平仓 | 成交 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for c in near_money(s, exp):
            L.append(f"| {'Call' if c['type'] == 'C' else 'Put'} | {fmt(c['strike'])} | "
                     f"{fmt(c['bid'])} | {fmt(c['ask'])} | {fmt(c['spread_pct'], '%', 1)} | "
                     f"{fmt((c['iv'] or 0) * 100, '%', 1)} | {fmt(c['delta'], '', 3)} | "
                     f"{fmt(c['theta'], '', 3)} | {fmt(c['vega'], '', 3)} | "
                     f"{c['oi']:,.0f} | {c['volume']:,.0f} |")
        L.append("")
        L.append("> **价差% 是流动性的硬指标**：买卖价差占中间价 >10% 的合约，"
                 "你一进一出光滑点就吃掉大半收益。未平仓量 <50 的更是想平都平不掉。\n")
        L.append("> **Δ 可当作到期价内的粗略概率**（0.25 delta ≈ 25%）。"
                 "**Θ 是每天流逝的钱**——买方每天付这个数，卖方每天收这个数。\n")

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

    s = build(args.ticker, lo, hi)
    if not s:
        sys.exit(f"取不到 {args.ticker.upper()} 的期权链。"
                 f"（CBOE 只覆盖美国上市期权；小盘股可能根本没有期权）")
    print(json.dumps(s, indent=2, ensure_ascii=False) if args.json else render(s))


if __name__ == "__main__":
    main()
