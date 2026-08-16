#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tech_snapshot.py — 短线技术面确定性快照

**为什么要有这个文件**：`zubair-trabzada/ai-trading-claude`（MIT，本仓库短线 skill 的结构来源）
让模型用 WebSearch 去搜「TICKER RSI MACD」，读网页上印着的数字。那是最难发现的错误——
所有推理都对，只有一个基础数字是幻觉。TradingAgents 的 `market_data_validator.py` 修的是同一个
issue（#830：模型编造「历史验证过的支撑位反弹」）。

所以短线判断开始之前，先用纯 Python 从 Yahoo 日线算出一张已核验快照，塞给模型，
附一句狠话：**以此为准；若其他工具或搜索结果与之冲突，标记冲突，不要自己编一个调和后的数字。**

只用标准库；价格走 curl（和 `stock_screener.py`、`decision_log.py` 同一路子）。

用法：
    python3 tools/tech_snapshot.py AAOI
    python3 tools/tech_snapshot.py AAOI --json
    python3 tools/tech_snapshot.py AAOI --benchmark SPY
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# 可交易分层。Yahoo 对 600519.SS / 300285.SZ / 2492.TW 照给数据不误——
# 这正是 TradingAgents 最危险的地方：它的架构里没有「买不到」这个概念。
# 给一个 Robinhood 买不到的标的算出入场止损价位是有害的，它会让人以为可以行动。
TIER_BY_SUFFIX = {
    ".HK": ("T2", "港股：Robinhood 不行；Schwab/Fidelity 国际账户可能支持，需自行核实"),
    ".T": ("T2", "日股：Robinhood 不行；Schwab/Fidelity 国际账户可能支持，需自行核实"),
    ".SS": ("T3", "A 股（沪）：美国零售券商基本都不支持"),
    ".SZ": ("T3", "A 股（深）：美国零售券商基本都不支持"),
    ".TW": ("T3", "台股（上市）：美国零售券商基本都不支持"),
    ".TWO": ("T3", "台股（上柜）：美国零售券商基本都不支持"),
}

# 非股票标的：指数、期货、外汇、加密。给这些算"入场/止损位"没有意义，
# 而且它们大多根本不在可投资范围里。
NON_EQUITY = (
    ("^", "指数（不可直接买，找对应 ETF）"),
    ("=F", "期货合约"),
    ("=X", "外汇对"),
    ("-USD", "加密货币"),
)


def tier_of(ticker):
    """白名单：**只有没有交易所后缀的美股代码才是 T1**。

    原来这里是黑名单（列了 5 个后缀，其余一律 T1），交叉评审实测穿过去的有：
    SHOP.TO / 6488.TWO / 005930.KS / BP.L / SAP.DE / BHP.AX，
    以及 ^GSPC / GC=F / BTC-USD / EURUSD=X。
    其中 .TWO 是台股上柜——CLAUDE.md 第二节点名的 T3，黑名单偏偏漏了它。
    未知的东西默认不可交易，才是安全的方向。
    """
    t = ticker.upper()
    for mark, why in NON_EQUITY:
        if (t.startswith(mark) if mark == "^" else t.endswith(mark)):
            return "T3", f"非股票标的：{why}"
    for suf, (tier, why) in TIER_BY_SUFFIX.items():
        if t.endswith(suf):
            return tier, why
    if "." in t:
        suf = "." + t.rsplit(".", 1)[1]
        return "T3", f"未知交易所后缀 {suf}：不在已核实的可交易范围内，按不可买处理"
    # 美股用 `-` 表示股份类别（BRK-B、BF-B），不是交易所后缀
    return "T1", ""


WARNING = (
    "**以此表为准。** 若 WebSearch、券商页面或任何二手来源给出的数字与本表冲突，"
    "**标记冲突并同时列出两个数字**，不要自己编一个调和后的中间值。"
    "本表由 `tools/tech_snapshot.py` 从 Yahoo 日线（复权）纯 Python 计算，无模型参与。"
)


# ============================================================
# 取数
# ============================================================

def fetch_ohlcv(ticker, days=500):
    """取日线 OHLCV。返回 [{date, open, high, low, close, adjclose, volume}]，按日期升序。"""
    end = datetime.now()
    start = end - timedelta(days=days)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={int(start.timestamp())}&period2={int(end.timestamp())}"
        f"&interval=1d&events=div%7Csplit"
    )
    try:
        r = subprocess.run(["curl", "-s", "-H", "User-Agent: Mozilla/5.0", url],
                           capture_output=True, text=True, timeout=25)
        if r.returncode != 0:
            return [], {}
        data = json.loads(r.stdout)
        res = (data.get("chart") or {}).get("result") or []
        if not res:
            return [], {}
        res = res[0]
        ts = res.get("timestamp") or []
        q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or q.get("close")
        meta = res.get("meta") or {}
        rows = []
        for i, t in enumerate(ts):
            c = (q.get("close") or [None] * len(ts))[i]
            h = (q.get("high") or [None] * len(ts))[i]
            l = (q.get("low") or [None] * len(ts))[i]
            if c is None or h is None or l is None:
                continue                      # 不完整的 bar 直接丢，不留半截数据
            a = (adj or [None] * len(ts))[i] if adj else None
            rows.append({
                "date": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                "open": (q.get("open") or [None] * len(ts))[i],
                "high": h, "low": l, "close": c,
                # adjclose 可能是 None 而 close 有值——直接存 None 会让后面的
                # ema/rsi 抛 TypeError（未捕获的 traceback，不是友好的"数据不足"）
                "adjclose": a if a is not None else c,
                "volume": (q.get("volume") or [0] * len(ts))[i] or 0,
            })
        # Yahoo 偶尔对同一天发两行；保后一行
        dedup = {r["date"]: r for r in rows}
        rows = [dedup[d] for d in sorted(dedup)]

        # 盘中运行时最后一根是**未收盘的实时 bar**：把它当收盘价会让 RSI/MACD/布林
        # 全部基于半根 bar，量比更是只统计了半天的量（10 点跑会显示 ~0.1x 被读成"缩量"）。
        provisional = False
        mkt_time, period = meta.get("regularMarketTime"), (meta.get("currentTradingPeriod") or {})
        reg_end = ((period.get("regular") or {}).get("end"))
        if rows and mkt_time and reg_end and mkt_time < reg_end:
            last_day = datetime.fromtimestamp(mkt_time, tz=timezone.utc).strftime("%Y-%m-%d")
            if rows[-1]["date"] == last_day:
                provisional = True
        return rows, {"currency": meta.get("currency") or "", "provisional_last_bar": provisional}
    except Exception:
        return [], {}


# ============================================================
# 指标（纯 Python，无第三方库）
# ============================================================

def ema(vals, n):
    if len(vals) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(vals[:n]) / n]
    for v in vals[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(vals, n):
    return [sum(vals[i - n + 1:i + 1]) / n for i in range(n - 1, len(vals))] if len(vals) >= n else []


def rsi(closes, n=14):
    """Wilder RSI。"""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if ag == 0 and al == 0:
        return 50.0     # 完全平盘（停牌/常数序列）：Wilder RSI 在 0/0 时无定义，
                        # 返回 100 会把一只停牌股标成"超买"。中性更诚实。
    if al == 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    ef, es = ema(closes, fast), ema(closes, slow)
    # 对齐：ema 返回的序列起点不同
    ef = ef[len(ef) - len(es):]
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    if not sig:
        return None
    line_t = line[len(line) - len(sig):]
    return {"macd": line_t[-1], "signal": sig[-1], "hist": line_t[-1] - sig[-1],
            "hist_prev": (line_t[-2] - sig[-2]) if len(sig) > 1 else None}


def atr(rows, n=14):
    """Wilder ATR。"""
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["high"], rows[i]["low"], rows[i - 1]["close"]
        if None in (h, l, pc):
            continue
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < n:
        return None
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def bollinger(closes, n=20, k=2):
    if len(closes) < n:
        return None
    w = closes[-n:]
    m = sum(w) / n
    var = sum((x - m) ** 2 for x in w) / n
    sd = var ** 0.5
    up, lo = m + k * sd, m - k * sd
    return {"upper": up, "mid": m, "lower": lo,
            "bandwidth": (up - lo) / m * 100 if m else None,
            "pctb": (closes[-1] - lo) / (up - lo) * 100 if up != lo else None}


def swing_levels(rows, window=5, lookback=180):
    """摆动高低点找支撑阻力。不做形态识别——形态是主观的，摆动点是客观的。"""
    r = rows[-lookback:] if len(rows) > lookback else rows
    highs, lows = [], []
    for i in range(window, len(r) - window):
        h = r[i]["high"]
        l = r[i]["low"]
        if h is None or l is None:
            continue
        if h == max(x["high"] for x in r[i - window:i + window + 1] if x["high"] is not None):
            highs.append(h)
        if l == min(x["low"] for x in r[i - window:i + window + 1] if x["low"] is not None):
            lows.append(l)
    px = r[-1]["close"]
    res = sorted({round(h, 2) for h in highs if h > px})[:3]
    sup = sorted({round(l, 2) for l in lows if l < px}, reverse=True)[:3]
    return {"support": sup, "resistance": res}


def realized_vol(closes, n=20):
    if len(closes) < n + 1:
        return None
    rets = [(closes[i] / closes[i - 1] - 1) for i in range(len(closes) - n, len(closes))]
    m = sum(rets) / len(rets)
    var = sum((x - m) ** 2 for x in rets) / len(rets)
    return (var ** 0.5) * (252 ** 0.5) * 100


def max_drawdown(closes):
    peak, mdd = closes[0], 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1)
    return mdd * 100


def perf(closes, n):
    return (closes[-1] / closes[-1 - n] - 1) * 100 if len(closes) > n else None


# ============================================================
# 组装
# ============================================================

def build(ticker, benchmark="SPY", drop_provisional=True):
    rows, meta = fetch_ohlcv(ticker)
    provisional = meta.get("provisional_last_bar", False)
    if provisional and drop_provisional and rows:
        rows = rows[:-1]          # 未收盘的当日 bar 一律剔除，宁可少一天
    if len(rows) < 60:
        return None
    closes = [r["adjclose"] for r in rows]
    vols = [r["volume"] for r in rows]
    px = closes[-1]

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    e20, e50, e200 = (e20[-1] if e20 else None), (e50[-1] if e50 else None), (e200[-1] if e200 else None)

    stack = "数据不足"
    if None not in (e20, e50, e200):
        if px > e20 > e50 > e200:
            stack = "多头排列（价 > EMA20 > EMA50 > EMA200）"
        elif px < e20 < e50 < e200:
            stack = "空头排列（价 < EMA20 < EMA50 < EMA200）"
        else:
            stack = "纠缠 / 转换中（均线未形成一致排列）"

    a = atr(rows, 14)
    bb = bollinger(closes, 20, 2)
    lv = swing_levels(rows)

    v20 = sum(vols[-20:]) / 20 if len(vols) >= 20 else None
    v50 = sum(vols[-50:]) / 50 if len(vols) >= 50 else None

    hi52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    lo52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)

    # 相对强弱：和基准比，不是绝对涨跌。跑赢大盘才叫强。
    # **必须按共同交易日对齐**：两边各数各的最后 21/63/126 根，遇到假期不同、
    # 停牌、或某一边数据陈旧时，起止日期就不一样了，却仍会算出一个看着很精确的相对收益。
    brows, _ = fetch_ohlcv(benchmark)
    if provisional and drop_provisional and brows:
        brows = brows[:-1]
    smap = {r["date"]: r["adjclose"] for r in rows}
    bmap = {r["date"]: r["adjclose"] for r in brows}
    common = sorted(set(smap) & set(bmap))
    rs = {}
    for label, n in (("1M", 21), ("3M", 63), ("6M", 126)):
        if len(common) > n:
            d0, d1 = common[-1 - n], common[-1]
            ps = (smap[d1] / smap[d0] - 1) * 100
            pb = (bmap[d1] / bmap[d0] - 1) * 100
            rs[label] = {"stock": ps, "bench": pb, "rel": ps - pb, "from": d0, "to": d1}
        else:
            rs[label] = {"stock": None, "bench": None, "rel": None, "from": None, "to": None}

    tier, tier_why = tier_of(ticker)
    return {
        "ticker": ticker.upper(), "benchmark": benchmark,
        "tier": tier, "tier_note": tier_why,
        "currency": meta.get("currency") or "",
        "provisional_last_bar_dropped": bool(provisional and drop_provisional),
        "as_of": rows[-1]["date"], "bars": len(rows),
        "price": px,
        "day_change_pct": (closes[-1] / closes[-2] - 1) * 100 if len(closes) > 1 else None,
        "ema20": e20, "ema50": e50, "ema200": e200, "ma_stack": stack,
        "rsi14": rsi(closes, 14),
        "macd": macd(closes),
        "atr14": a, "atr_pct": (a / px * 100) if a else None,
        "bollinger": bb,
        "levels": lv,
        "vol_today": vols[-1], "vol_avg20": v20, "vol_avg50": v50,
        "vol_ratio_20": (vols[-1] / v20) if v20 else None,
        "hi52": hi52, "lo52": lo52,
        "from_hi52_pct": (px / hi52 - 1) * 100, "from_lo52_pct": (px / lo52 - 1) * 100,
        "realized_vol_20d": realized_vol(closes, 20),
        "max_drawdown_1y": max_drawdown(closes[-252:] if len(closes) >= 252 else closes),
        "rs": rs,
    }


def fmt(v, unit="", nd=2):
    if v is None:
        return "数据不足"
    return f"{v:,.{nd}f}{unit}"


def render(s):
    L = []
    # 分层警告必须在 **stdout 的第一行**。原来只写到 stderr，而模型读的是 stdout——
    # 拿到的快照和 T1 的长得一模一样，"不许据此给价位"那条指令就落空了。
    if s["tier"] != "T1":
        L.append(f"> 🔴 **{s['ticker']} 是 {s['tier']}（{s['tier_note']}）。"
                 f"以下数据仅供理解产业链，不得用于任何买卖建议、入场价、止损价。**\n")
    L.append(f"# {s['ticker']} 技术面确定性快照（截至 {s['as_of']}，{s['bars']} 根日线）\n")
    L.append(f"> {WARNING}\n")
    if s.get("provisional_last_bar_dropped"):
        L.append("> ℹ️ 当前是盘中时段，**未收盘的当日 bar 已剔除**——本表基于最后一根已收盘日线。\n")

    L.append("## 价格与趋势\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    cur = f" {s['currency']}" if s.get('currency') else ""
    L.append(f"| 最新收盘（复权） | {fmt(s['price'], cur)} |")
    L.append(f"| 当日涨跌 | {fmt(s['day_change_pct'], '%')} |")
    L.append(f"| EMA20 / 50 / 200 | {fmt(s['ema20'])} / {fmt(s['ema50'])} / {fmt(s['ema200'])} |")
    L.append(f"| 均线排列 | {s['ma_stack']} |")
    L.append(f"| 距 52 周高 / 低 | {fmt(s['from_hi52_pct'], '%')} / {fmt(s['from_lo52_pct'], '%')} "
             f"（高 {fmt(s['hi52'])} / 低 {fmt(s['lo52'])}） |")
    L.append(f"| 近一年最大回撤 | {fmt(s['max_drawdown_1y'], '%')} |\n")

    L.append("## 动量与波动\n")
    L.append("| 项 | 值 | 读法 |")
    L.append("|---|---|---|")
    r = s["rsi14"]
    rr = "数据不足" if r is None else ("超买区（>70）" if r > 70 else "超卖区（<30）" if r < 30 else "中性区")
    L.append(f"| RSI(14) | {fmt(r)} | {rr} |")
    m = s["macd"]
    if m:
        d = "柱体放大" if (m["hist_prev"] is not None and abs(m["hist"]) > abs(m["hist_prev"])) else "柱体收敛"
        L.append(f"| MACD / 信号 / 柱 | {fmt(m['macd'])} / {fmt(m['signal'])} / {fmt(m['hist'])} | "
                 f"{'金叉状态' if m['hist'] > 0 else '死叉状态'}，{d} |")
    else:
        L.append("| MACD | 数据不足 | — |")
    L.append(f"| ATR(14) | {fmt(s['atr14'])}（{fmt(s['atr_pct'], '%')}） | "
             f"日均波动幅度，止损宽度的下限参考 |")
    L.append(f"| 20 日年化波动率 | {fmt(s['realized_vol_20d'], '%')} | 已实现波动，不是隐含波动 |")
    bb = s["bollinger"]
    if bb:
        L.append(f"| 布林带 上/中/下 | {fmt(bb['upper'])} / {fmt(bb['mid'])} / {fmt(bb['lower'])} | "
                 f"带宽 {fmt(bb['bandwidth'], '%')}，%B {fmt(bb['pctb'], '%')} |")
    L.append("")

    L.append("## 量能\n")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 当日成交量 | {fmt(s['vol_today'], '', 0)} |")
    L.append(f"| 20 日 / 50 日均量 | {fmt(s['vol_avg20'], '', 0)} / {fmt(s['vol_avg50'], '', 0)} |")
    L.append(f"| 当日 / 20 日均量 | {fmt(s['vol_ratio_20'], 'x')} |\n")

    L.append("## 摆动高低点（客观计算，非形态识别）\n")
    lv = s["levels"]
    L.append(f"- **上方阻力**：{', '.join(fmt(x) for x in lv['resistance']) or '近 180 根日线内无更高摆动高点'}")
    L.append(f"- **下方支撑**：{', '.join(fmt(x) for x in lv['support']) or '近 180 根日线内无更低摆动低点'}")
    L.append("\n> 这些是过去 **180 根日线**（约 8.5 个月）内的局部极值（前后各 5 根确认），**不是**「历史验证过会反弹的位置」。"
             "不要把它们描述成有预测力的位置。\n")

    L.append(f"## 相对强弱（vs {s['benchmark']}）\n")
    L.append("| 区间 | 标的 | 基准 | 相对 |")
    L.append("|---|---|---|---|")
    for k in ("1M", "3M", "6M"):
        v = s["rs"][k]
        L.append(f"| {k} | {fmt(v['stock'], '%')} | {fmt(v['bench'], '%')} | {fmt(v['rel'], '%')} |")
    L.append("")
    L.append("> ⚠️ **本仓库已回测过的教训**：单独的「60 日新高」动量信号经检验是**显著负 alpha**，"
             "只有叠加放量条件才有效（见 `tools/signal_layer_test.py`）。"
             "看到上面的相对强弱为正，不构成买入理由。")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser(description="短线技术面确定性快照（纯 Python 计算，防编数字）")
    p.add_argument("ticker")
    p.add_argument("--benchmark", default="SPY")
    p.add_argument("--json", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="T2/T3 标的仍然出快照（只为理解产业链时用，不许据此给价位）")
    args = p.parse_args()

    tier, why = tier_of(args.ticker)
    if tier != "T1" and not args.force:
        sys.exit(
            f"❌ {args.ticker.upper()} 是 {tier} 标的——{why}。\n"
            f"   短线判断依赖入场 / 止损 / 减仓三个价位可执行，买不到的标的给这些价位是有害的。\n"
            f"   Yahoo 对它照给数据不误，这正是不能只信数据源的原因。\n"
            f"   只是想理解产业链走势：加 --force，但**不许**据此写任何价位或买卖建议。"
        )
    if tier != "T1":
        print(f"> ⚠️ **{args.ticker.upper()} 是 {tier}（{why}）。以下数据仅供理解产业链，"
              f"不得用于任何买卖建议或价位。**\n", file=sys.stderr)

    s = build(args.ticker, args.benchmark)
    if not s:
        sys.exit(f"取不到 {args.ticker} 的足够日线数据（需要 ≥60 根）。"
                 f"T3 标的和部分海外票 Yahoo 覆盖不全。")
    print(json.dumps(s, indent=2, ensure_ascii=False) if args.json else render(s))


if __name__ == "__main__":
    main()
