#!/usr/bin/env python3
"""
momentum_layer_test_v2.py — 检验 stock_screener.py 第一层（动量发现）的信息量
v2 修正 v1 的两个方法论缺陷：
  1. universe 改用真实标普500成分股（v1 因 lxml 缺失回退到手工清单）
  2. 前瞻窗口重叠会严重高估 t 值 → 改用 Fama-MacBeth 式月度截面

方法（Fama-MacBeth）：
  每个月末，计算该月内「触发信号的股票」与「全部股票」的平均前瞻收益之差 = 当月超额；
  再对这一列月度超额序列做 t 检验。
  这样：① 同月内的横截面相关性被吸收进单个月度观测；
        ② 月度序列近似独立 → t 值可信。
  代价是样本量从「股票×交易日」降到「月份数」，t 值必然远小于 v1——这是正确的。

被检验规则（tools/stock_screener.py:122-157）：
  triggered = (60日新高 or 10日内出现过60日新高) and (5日均量/20日均量 > 1.5)
  该层为硬门槛：不触发 → grade_signal 直接 SKIP，基本面根本不被评估。
"""

import warnings
import numpy as np
import pandas as pd
import yfinance as yf
from scipy import stats

warnings.filterwarnings("ignore")

HORIZONS = [21, 63, 126, 252]
START, END = "2005-01-01", "2026-07-31"


def _universe_html(cache="/tmp/sp500_constituents.html"):
    """Wikipedia 拒绝 urllib 默认 UA，用 curl 落盘后再解析（24h 缓存）"""
    import os, subprocess, time
    if not os.path.exists(cache) or time.time() - os.path.getmtime(cache) > 86400:
        subprocess.run(["curl", "-sL", "-A",
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
                        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
                        "-o", cache], check=True)
    return cache


def get_universe():
    tbl = pd.read_html(_universe_html())[0]
    tks = sorted({t.replace(".", "-") for t in tbl["Symbol"].tolist()})
    print(f"universe：标普500现成分股 {len(tks)} 只（Wikipedia）")
    print("⚠️  幸存者偏差：这是【当前】成分股。但信号组与基准组取自同一 universe，")
    print("    偏差同等作用于两组，做差后基本抵消——这正是条件/无条件设计的价值。\n")
    return tks


def load(tickers):
    df = yf.download(tickers, start=START, end=END, auto_adjust=True,
                     progress=False, threads=True)
    close, high, vol = df["Close"], df["High"], df["Volume"]
    ok = close.notna().sum() > 700
    print(f"有效标的 {ok.sum()} 只 / {close.shape[0]} 个交易日\n")
    return close.loc[:, ok], high.loc[:, ok], vol.loc[:, ok]


def signals(close, high, vol):
    is_high = close > high.shift(1).rolling(60).max()
    vr = vol.rolling(5).mean() / vol.rolling(20).mean()
    is_vol = vr > 1.5
    recent = is_high.rolling(10).max().astype(bool)
    return {
        "原实现（新高 or 近10日新高）+ 放量": (is_high | recent) & is_vol,
        "仅 60日新高（价格突破）": is_high,
        "仅 放量 1.5x（成交量）": is_vol,
    }


def fama_macbeth(close, sig, h):
    """每月一个截面观测：当月信号股前瞻收益均值 − 当月全样本前瞻收益均值"""
    fwd = close.shift(-h) / close - 1.0
    valid = fwd.notna()
    monthly = []
    periods = fwd.index.to_period("M")
    for _, idx in pd.Series(fwd.index, index=periods).groupby(level=0):
        idx = pd.DatetimeIndex(idx.values)
        if len(idx) == 0:
            continue
        f = fwd.loc[idx]
        s = sig.loc[idx] & valid.loc[idx]
        sr = f.where(s).stack().dropna()
        br = f.where(valid.loc[idx]).stack().dropna()
        if len(sr) < 3 or len(br) < 30:
            continue
        monthly.append(sr.mean() - br.mean())
    m = pd.Series(monthly).dropna()
    if len(m) < 12:
        return None
    t, p = stats.ttest_1samp(m, 0.0)
    return {"n_months": len(m), "mean_edge": m.mean(), "t": t, "p": p,
            "pct_positive": (m > 0).mean()}


def main():
    close, high, vol = load(get_universe())
    sigs = signals(close, high, vol)

    for name, s in sigs.items():
        freq = s.sum().sum() / s.notna().sum().sum() * 100
        print(f"\n{'='*82}\n  {name}   （触发频率 {freq:.2f}% 的股票-交易日）\n{'='*82}")
        print(f"{'前瞻':<8}{'月度观测':>9}{'月均超额':>11}{'t值':>9}{'p值':>10}{'超额为正的月份占比':>20}")
        for h in HORIZONS:
            r = fama_macbeth(close, s, h)
            if not r:
                continue
            flag = "" if abs(r["t"]) >= 2 else "   ← 不显著"
            print(f"{str(h)+'d':<8}{r['n_months']:>9}{r['mean_edge']*100:>10.2f}%"
                  f"{r['t']:>9.2f}{r['p']:>10.3f}{r['pct_positive']*100:>17.1f}%{flag}")

    print(f"\n{'='*82}")
    print("  判读：|t| < 2 → 无法拒绝『该信号无信息量』的原假设")
    print(f"{'='*82}")


if __name__ == "__main__":
    main()
