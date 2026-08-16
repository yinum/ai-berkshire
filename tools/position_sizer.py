#!/usr/bin/env python3
"""
position_sizer.py — 把「拍脑袋的仓位百分比」变成「风险推导的仓位」

设计哲学（重要，别搞反）：
    价值投资者的 alpha 在【预期收益】——那是研究员的活，历史数据算不出来。
    量化能贡献的是【风险结构】——协方差、相关性、集中度，这些历史数据能算。
    所以本工具默认【不】用历史收益做均值方差优化（那是垃圾进垃圾出），
    而是用只需要协方差矩阵的方法（逆波动率 / HRP）给出风险中性的基准配置，
    再让你把自己的 conviction 叠上去，并显示两者的差距。

用法：
    # 1) 只看风险结构（不需要任何观点）
    python3 tools/position_sizer.py --tickers AAPL MSFT JNJ XOM KO

    # 2) 带上你自己的手工仓位，看看它在风险上意味着什么
    python3 tools/position_sizer.py --tickers AAPL MSFT JNJ XOM KO \\
        --manual 30 25 20 15 10

    # 3) 带上你自己的预期收益（来自 DCF / 内在价值），做最大夏普
    python3 tools/position_sizer.py --tickers AAPL MSFT JNJ XOM KO \\
        --manual 30 25 20 15 10 --expected 8 7 10 12 9

注意第 3 种用法的陷阱：均值方差优化是「误差最大化器」，
它会把仓位全部堆到预期收益最高的那一两只上（常见结果：有效持仓数掉到 3 以下）。
本工具默认展示前两种（只需协方差），第 3 种仅供对照，不建议直接采用其输出。

依赖：~/.venvs/berkshire-quant/bin/python（quantstats / PyPortfolioOpt / yfinance）
"""

import argparse
import warnings
import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


def fetch(tickers, years):
    end = pd.Timestamp.today()
    start = end - pd.DateOffset(years=years)
    df = yf.download(tickers, start=start, end=end, auto_adjust=True,
                     progress=False, threads=True)["Close"]
    if isinstance(df, pd.Series):
        df = df.to_frame(tickers[0])
    df = df.dropna(axis=1, how="all").dropna()
    missing = set(tickers) - set(df.columns)
    if missing:
        print(f"[warn] 无数据，已剔除：{sorted(missing)}")
    return df


def inverse_vol(rets):
    v = rets.std() * np.sqrt(252)
    w = (1 / v) / (1 / v).sum()
    return w


def hrp(rets):
    """Hierarchical Risk Parity（López de Prado 2016）——只用协方差，不需要预期收益。

    自行实现而非调用 PyPortfolioOpt.HRPOpt：后者在 scipy>=1.18 下因
    scipy.cluster.hierarchy._LINKAGE_METHODS 被移除而报错。
    """
    from scipy.cluster.hierarchy import linkage, to_tree
    cov, corr = rets.cov(), rets.corr()
    dist = np.sqrt(np.clip((1 - corr) / 2, 0, 1))          # 相关性 → 距离
    link = linkage(dist.values[np.triu_indices(len(corr), 1)], "single")

    # 准对角化：按聚类树的叶子顺序重排
    order = [n.id for n in to_tree(link, rd=False).pre_order(lambda x: x)]
    tickers = [corr.columns[i] for i in order]

    # 递归二分，按逆方差在两簇间分配
    w = pd.Series(1.0, index=tickers)
    clusters = [tickers]
    while clusters:
        nxt = []
        for c in clusters:
            if len(c) <= 1:
                continue
            mid = len(c) // 2
            for left, right in ((c[:mid], c[mid:]),):
                def cluster_var(items):
                    sub = cov.loc[items, items]
                    iv = 1 / np.diag(sub)
                    iv = iv / iv.sum()
                    return float(iv @ sub.values @ iv)
                vl, vr = cluster_var(left), cluster_var(right)
                alpha = 1 - vl / (vl + vr)
                w[left] *= alpha
                w[right] *= 1 - alpha
                nxt += [left, right]
        clusters = nxt
    return (w / w.sum()).reindex(rets.columns)


def max_sharpe(rets, expected):
    """用【你自己的】预期收益，而不是历史收益"""
    from pypfopt import EfficientFrontier, risk_models
    S = risk_models.CovarianceShrinkage(rets, returns_data=True).ledoit_wolf()
    mu = pd.Series(expected, index=rets.columns) / 100.0
    ef = EfficientFrontier(mu, S, weight_bounds=(0, 0.40))
    ef.max_sharpe(risk_free_rate=0.04)
    return pd.Series(ef.clean_weights())


def stats_for(w, rets):
    w = w.reindex(rets.columns).fillna(0)
    port = (rets * w).sum(axis=1)
    ann_vol = port.std() * np.sqrt(252)
    cum = (1 + port).cumprod()
    mdd = (cum / cum.cummax() - 1).min()
    hhi = (w ** 2).sum()               # 赫芬达尔集中度
    eff_n = 1 / hhi if hhi > 0 else 0  # 有效持仓数
    # 风险贡献集中度：单一标的贡献的组合方差占比最大值
    cov = rets.cov() * 252
    pvar = float(w @ cov @ w)
    mrc = (cov @ w) * w / pvar if pvar > 0 else w * 0
    return {"年化波动": ann_vol, "最大回撤": mdd, "有效持仓数": eff_n,
            "最大风险贡献": mrc.max(), "最大风险贡献标的": mrc.idxmax()}


def show(name, w, rets):
    s = stats_for(w, rets)
    print(f"\n  {name}")
    print("   " + "  ".join(f"{t}:{w.get(t,0)*100:.1f}%" for t in rets.columns))
    print(f"   年化波动 {s['年化波动']*100:.1f}%  |  最大回撤 {s['最大回撤']*100:.1f}%  |  "
          f"有效持仓数 {s['有效持仓数']:.2f}  |  最大单标的风险贡献 {s['最大风险贡献']*100:.1f}% "
          f"({s['最大风险贡献标的']})")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--manual", nargs="+", type=float,
                    help="你的手工仓位（百分比，顺序对应 --tickers）")
    ap.add_argument("--expected", nargs="+", type=float,
                    help="你的预期年化收益（百分比，来自 DCF/内在价值，不是历史）")
    ap.add_argument("--years", type=int, default=5)
    a = ap.parse_args()

    px = fetch(a.tickers, a.years)
    rets = px.pct_change().dropna()
    cols = list(rets.columns)
    print(f"\n样本：{len(rets)} 个交易日 / {len(cols)} 只标的 "
          f"({rets.index[0].date()} → {rets.index[-1].date()})")

    print("\n" + "=" * 78)
    print("  相关性矩阵（分散化的真实程度）")
    print("=" * 78)
    print((rets.corr() * 100).round(0).astype(int).to_string())
    avg_corr = rets.corr().values[np.triu_indices(len(cols), 1)].mean()
    print(f"\n  平均两两相关性：{avg_corr*100:.0f}%"
          f"{'  ← 高相关，分散化是假的' if avg_corr > 0.6 else ''}")

    print("\n" + "=" * 78)
    print("  各种配置方案对比")
    print("=" * 78)

    results = {}
    results["等权（基准）"] = show("等权（基准）",
                                pd.Series(1 / len(cols), index=cols), rets)
    results["逆波动率"] = show("逆波动率（风险平价的简化版）", inverse_vol(rets), rets)
    try:
        results["HRP"] = show("HRP 层次风险平价（只用协方差，无需预期收益）", hrp(rets), rets)
    except Exception as e:
        print(f"\n  [HRP 跳过：{e}]")

    if a.manual:
        m = pd.Series(a.manual, index=a.tickers).reindex(cols).fillna(0)
        m = m / m.sum()
        results["手工"] = show("★ 你的手工仓位", m, rets)

    if a.expected:
        try:
            results["最大夏普"] = show("最大夏普（用你的预期收益 + 收缩协方差）",
                                    max_sharpe(rets, a.expected), rets)
        except Exception as e:
            print(f"\n  [最大夏普 跳过：{e}]")

    if a.manual and "手工" in results:
        print("\n" + "=" * 78)
        print("  手工仓位 vs 风险推导仓位：差在哪")
        print("=" * 78)
        h = results["手工"]
        for k in ("等权（基准）", "逆波动率", "HRP"):
            if k not in results:
                continue
            r = results[k]
            dv = (h["年化波动"] - r["年化波动"]) * 100
            dd = (h["最大回撤"] - r["最大回撤"]) * 100
            print(f"  vs {k:<10} 波动 {dv:+.1f}pt   回撤 {dd:+.1f}pt   "
                  f"有效持仓 {h['有效持仓数']:.2f} vs {r['有效持仓数']:.2f}")
        print(f"\n  你的组合里，{h['最大风险贡献标的']} 一只贡献了 "
              f"{h['最大风险贡献']*100:.0f}% 的组合方差。")
        print("  若这个比例远高于它的仓位占比，说明你在不知情的情况下押注了它。")


if __name__ == "__main__":
    main()
