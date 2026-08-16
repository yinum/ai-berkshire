#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decision_log.py — 决策日志 + alpha 回填 + 反思注入

解决的问题：`falsifiable-predictions` 表里的「事后结果」列永远写着「待判定」，
`history.md` 只追加但没人回头读。设计都在，闭环从没合上——框架无法从自己的错误里学。

这个工具把闭环合上：
  1. add      每次出研究结论时写一条 pending 记录（含当日 adjclose 与基准价）
  2. due      列出持有期已满、该回填的记录
  3. resolve  拉最新价，算 **相对基准的 alpha**，把 pending 改成已判定
  4. reflect  写 2-4 句反思（模型写，工具只负责原子落盘）
  5. context  输出「同标的最近 5 条 + 跨标的最近 3 条教训」，注入下一次研究的 prompt

设计要点（抄 TradingAgents 的工程纪律，不抄它的 agent 名册）：
  * 只追加。已有条目只在「结果」「反思」两个字段上被改写，理由字段永不重写——
    事后改理由就是事后合理化，那正是这个日志要防的东西。
  * `<!-- ENTRY_END -->` 做硬分隔符：LLM 散文里不可能出现这个串。
  * 写入前扫一遍防重复（同标的 + 同决策日 + 同来源报告 = 重复）。
  * 临时文件 + os.replace() 原子写，中途崩了不会留半个日志。
  * **收益一律记 alpha，不记原始收益。** 赚 18% 而同期 SPY 涨 25%，是论文错了不是赚了。
    只看原始收益会系统性高估自己。

依赖：只用标准库；价格走 curl（绕开 Python SSL 问题，和 stock_screener.py 一致）。

用法：
    python3 tools/decision_log.py add --ticker AAOI --rating Overweight \\
        --thesis "800G 光模块产能爬坡兑现" --kill "Q3 毛利率仍低于 20%" \\
        --source reports/AAOI投资研究报告-20260805.md --tier T1
    python3 tools/decision_log.py due
    python3 tools/decision_log.py resolve --id 20260805-AAOI-01
    python3 tools/decision_log.py reflect --id 20260805-AAOI-01 --text "..."
    python3 tools/decision_log.py context --ticker AAOI
    python3 tools/decision_log.py list [--ticker AAOI] [--status pending]
    python3 tools/decision_log.py score      # 已判定条目的 alpha 汇总
"""

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

# ============================================================
# 常量
# ============================================================

DEFAULT_LOG = os.environ.get(
    "AIB_DECISION_LOG",
    os.path.expanduser("~/ai-berkshire/reports/_decisions/log.md"),
)

ENTRY_END = "<!-- ENTRY_END -->"
ENTRY_START = "<!-- ENTRY_START -->"

# 五档评级，四个调用点共用同一常量，防止各处漂移（星级不可证伪，已废弃）
RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]
RATING_BEHAVIOR = {
    "Buy": "建议建仓，可给到目标仓位",
    "Overweight": "建议逐步加仓，先给半仓",
    "Hold": "不加不减；无仓位则继续观察",
    "Underweight": "建议减仓，不再补",
    "Sell": "建议清仓 / 明确不买",
}
# 容错解析：星级、中文、口语说法都收敛到五档
RATING_ALIASES = {
    "buy": "Buy", "strongbuy": "Buy", "强烈推荐": "Buy", "买入": "Buy", "★★★★★": "Buy",
    "overweight": "Overweight", "accumulate": "Overweight", "增持": "Overweight",
    "推荐": "Overweight", "★★★★☆": "Overweight", "★★★★": "Overweight",
    "hold": "Hold", "neutral": "Hold", "持有": "Hold", "观望": "Hold", "中性": "Hold",
    "★★★☆☆": "Hold", "★★★": "Hold",
    "underweight": "Underweight", "reduce": "Underweight", "减持": "Underweight",
    "★★☆☆☆": "Underweight", "★★": "Underweight",
    "sell": "Sell", "avoid": "Sell", "卖出": "Sell", "回避": "Sell", "不买": "Sell",
    "★☆☆☆☆": "Sell", "★": "Sell",
    # 去劣筛选的判定词——它出的就是「明确不买」，等价于 Sell
    "不通过": "Sell", "排除": "Sell", "淘汰": "Sell", "fail": "Sell",
    # 「通过去劣筛选」不是买入建议，是「可以进入深度研究」。别往五档里塞。
}
NON_CALLS = {"通过", "pass", "进入深度研究", "观察名单"}

# 基准一律用**可复权的 ETF**，不用价格指数。
# ^HSI / ^N225 / 000300.SS 都没有 adjclose，会回落到收盘价 = 价格收益；
# 而标的那边是全收益。两者相减，alpha 会被指数股息率系统性抬高
# （恒指约 3–4%/年 → 180 天 1–2 个点），且方向恒定对多头有利。
BENCHMARKS = {"us": "SPY", "hk": "2800.HK", "jp": "1321.T",
              "cn": "510300.SS", "tw": "0050.TW"}

TIERS = ["T1", "T2", "T3"]
STATUSES = ["pending", "resolved", "void"]

# 元数据字段：可见的 markdown 键值行，工具与人读同一份文本，不存隐藏副本
META_KEYS = [
    "id", "ticker", "market", "tier", "rating", "decided_on",
    "horizon_class", "horizon_days",
    "benchmark", "entry_date", "entry_price", "bench_entry", "status", "source", "retro",
    "resolved_on", "exit_price", "bench_exit", "raw_return", "bench_return",
    "alpha", "call_alpha", "held_days",
]

# 方向：看多的 alpha 为正才算对，看空的 alpha 为负才算对。
# 不做这个转换的话，一条「回避」结论对应的标的涨了，记分板会把它算成赢——
# 而 `去劣筛选` 出的全是这类结论，等于在最该被打脸的地方给自己发奖。
DIRECTION = {"Buy": 1, "Overweight": 1, "Hold": 0, "Underweight": -1, "Sell": -1}

# 长线和短线是两套判断，必须分开记分。混在一起两边都读不出信号：
# 一条 180 天的「值不值得长期持有」和一条 30 天的「这周该不该进」，
# 用同一个窗口评判，等于把两种技艺的成绩搅成一碗。
HORIZON_CLASSES = {"long": 180, "short": 30}
HORIZON_LABEL = {"long": "长线", "short": "短线"}

META_LINE = re.compile(r"^- `([a-z_]+)`:\s*(.*?)\s*$", re.MULTILINE)

HEADER = """# 决策日志（只追加）

> **这个文件是什么**：每次研究出结论时在这里写一条 `pending` 记录，持有期满后回填
> **相对基准的 alpha**，并写 2-4 句反思。下一次研究同一家公司时，
> `investment-research` / `investment-checklist` 会先把这里的历史读进去。
>
> **为什么记 alpha 不记原始收益**：赚 18% 而同期 SPY 涨 25%，是论文错了不是赚了。
> 只看原始收益会系统性高估自己。alpha = 标的收益 − 同期基准收益（算术差，含分红，用复权价）。
>
> **为什么用五档评级不用星级**：「推荐度 ★★★★☆」不可证伪——四星是什么意思？
> 和另一份报告的四星可比吗？五档（Buy / Overweight / Hold / Underweight / Sell）
> 每档有行为定义，能跨报告聚合，能算 Brier score。
>
> **长线和短线分开记**：`horizon_class: long` 是「值不值得长期持有」，判定窗口 180 天；
> `horizon_class: short` 是「这一两个月的择时」，窗口 30 天。记分板两边分开算，永不合并——
> 用同一个窗口评判两种技艺，等于把成绩搅成一碗，两边都读不出信号。
> 同一份报告同一天产出一长一短两条判断是正常的，不算重复。
>
> **规矩**：理由字段（`thesis` / `kill`）写下就不许改。只有「结果」和「反思」可以回填。
> 事后改理由就是事后合理化，那正是这个日志要防的东西。
>
> **用 `tools/decision_log.py` 读写，不要手改。** 手改**会**坏：少一个条目结尾的分隔注释，
> 就足以让一条记录消失、另一条顶着别人的理由。工具现在会在解析时发现这类问题并直接拒绝运行
> （宁可整条命令失败，也不在坏结构上继续读写），但它只能报警，不能替你还原丢掉的内容。
> ——顺带一提，这段话本身不能写出那个分隔符的字面形式，否则它就会被当成一个真的分隔符。

---
"""


# ============================================================
# 价格（curl + Yahoo Finance，和 stock_screener.py 同一路子）
# ============================================================

def fetch_adjclose(ticker, start_date, end_date=None):
    """取 [start_date-10d, end_date] 区间的复权收盘价。返回 [(date, adjclose)]，按日期升序。

    用复权价（adjclose）而不是收盘价：分红和拆股都要算进总回报，
    否则高股息标的会被系统性低估，基准 SPY 也一样。
    """
    start = datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=10)
    end = datetime.now() if end_date is None else datetime.strptime(end_date, "%Y-%m-%d")
    end = end + timedelta(days=2)
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        f"?period1={int(start.timestamp())}&period2={int(end.timestamp())}"
        f"&interval=1d&events=div%7Csplit"
    )
    try:
        result = subprocess.run(
            ["curl", "-s", "-H", "User-Agent: Mozilla/5.0", url],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        chart = (data.get("chart") or {}).get("result") or []
        if not chart:
            return []
        chart = chart[0]
        timestamps = chart.get("timestamp") or []
        adj = ((chart.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
        if not adj:
            # 有些标的没有 adjclose（指数常见），退回收盘价
            adj = ((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close")
        if not adj:
            return []
        rows = []
        for ts, px in zip(timestamps, adj):
            if px is None:
                continue
            d = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            rows.append((d, float(px)))
        return rows
    except Exception:
        return []


def price_on_or_after(rows, date_str):
    """决策日当天的收盘价；当天不是交易日就取之后第一个交易日。返回 (date, price) 或 None。"""
    for d, px in rows:
        if d >= date_str:
            return (d, px)
    return None


def latest_price(rows):
    return rows[-1] if rows else None


def aligned_series(ticker, benchmark, start_date):
    """标的与基准的**同一次拉取、按共同交易日对齐**的价格序列。

    这个函数存在的理由是交叉评审里最严重的一条 bug：原来入场价是几个月前存下的
    adjclose，退出价是今天重新拉的 adjclose。Yahoo 的复权价是**回溯调整**的——
    每次分红或拆股，全部历史值都会被重算。两个快照的基准不同，相除得到的东西是错的：
      * 2:1 拆股 → 显示成 −50%，一条正确的多头判断被记成惨败；
      * 分红被静默丢掉 → 算出来其实是价格收益，而文件头明写着"含分红"。
    两端都从同一次响应里取，问题就不存在了。

    顺带修掉另外两条：两边各取各的最后一根 bar（停牌/退市时窗口不同），
    以及只按标的的交易日算天数。

    返回 (dates, smap, bmap)，dates 是两边都有报价的日期，升序。
    """
    smap = dict(fetch_adjclose(ticker, start_date))
    bmap = dict(fetch_adjclose(benchmark, start_date))
    return sorted(set(smap) & set(bmap)), smap, bmap


def pick_date(dates, on_or_after=None):
    """第一个 >= on_or_after 的共同交易日；不给就取最后一个。"""
    if not dates:
        return None
    if on_or_after is None:
        return dates[-1]
    for d in dates:
        if d >= on_or_after:
            return d
    return None


def measure(e, dates, smap, bmap, exit_date):
    """按 (入场日, 退出日) 两个共同交易日算收益。两端同源，基准一致。"""
    entry_date = pick_date(dates, e["decided_on"])
    if not entry_date or not exit_date:
        return None
    raw = (smap[exit_date] / smap[entry_date] - 1) * 100
    bench = (bmap[exit_date] / bmap[entry_date] - 1) * 100
    alpha = raw - bench
    d = DIRECTION.get(e["rating"], 0)
    return {
        "entry_date": entry_date, "exit_date": exit_date,
        "entry_px": smap[entry_date], "exit_px": smap[exit_date],
        "bench_entry": bmap[entry_date], "bench_exit": bmap[exit_date],
        "raw": raw, "bench": bench, "alpha": alpha,
        "call_alpha": alpha * d if d else None,
        "held": days_between(entry_date, exit_date),
    }


# ============================================================
# 解析 / 序列化
# ============================================================

class LogCorrupt(Exception):
    """日志结构被破坏。宁可整个命令失败，也不要在坏结构上继续读写——
    交叉评审里最严重的一条就是：少一个 ENTRY_END，一条记录会被静默吞掉，
    幸存的那条还会顶着别人的 thesis/kill。那正是这个日志要防的事后合理化。"""


REQUIRED_KEYS = ("id", "ticker", "rating", "decided_on", "status")


def parse_entries(text):
    """把日志切成条目列表。任何结构异常一律抛 LogCorrupt，不做"尽力而为"的解析。

    元数据**只从正文区之前**读（第一个 `### ` 之前）。否则 thesis / kill / 反思里
    出现一行 `- ``status``: resolved` 就会覆盖真元数据——模型写反思时写出这种行完全合理。
    """
    n_start, n_end = text.count(ENTRY_START), text.count(ENTRY_END)
    if n_start != n_end:
        raise LogCorrupt(
            f"分隔符不配对：{n_start} 个 ENTRY_START vs {n_end} 个 ENTRY_END。"
            f"多半是手改或粘贴时丢了一个 `{ENTRY_END}`。修好之前不动这个文件。")

    entries, seen_ids = [], {}
    for i, chunk in enumerate(text.split(ENTRY_END)):
        if ENTRY_START not in chunk:
            continue
        if chunk.count(ENTRY_START) > 1:
            raise LogCorrupt(f"第 {i + 1} 段里有 {chunk.count(ENTRY_START)} 个 ENTRY_START，"
                             f"说明两条记录粘在了一起。")
        body = chunk[chunk.index(ENTRY_START) + len(ENTRY_START):]

        # 元数据区 = 正文区（第一个 `### `）之前
        meta_region = body.split("\n### ", 1)[0]
        pairs = META_LINE.findall(meta_region)
        keys = [k for k, _ in pairs]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        if dups:
            raise LogCorrupt(f"元数据键重复：{dups}（第 {i + 1} 段）。")
        meta = dict(pairs)

        missing = [k for k in REQUIRED_KEYS if not meta.get(k) or meta[k] == "—"]
        if missing:
            raise LogCorrupt(f"第 {i + 1} 段缺必填字段 {missing}"
                             + (f"（id={meta['id']}）" if meta.get("id") else "（连 id 都没有）"))
        if meta["id"] in seen_ids:
            raise LogCorrupt(f"id 重复：{meta['id']}。resolve/reflect 会同时改到两条。")
        seen_ids[meta["id"]] = True

        meta["_raw"] = ENTRY_START + body + ENTRY_END
        meta["_thesis"] = section_text(body, "论文一句话")
        meta["_kill"] = section_text(body, "什么会证伪它")
        meta["_reflection"] = section_text(body, "反思")
        entries.append(meta)
    return entries


def section_text(body, title):
    m = re.search(rf"^### {re.escape(title)}\s*\n(.*?)(?=\n### |\n<!-- |\Z)", body,
                  re.MULTILINE | re.DOTALL)
    s = m.group(1).strip() if m else ""
    return "" if s == "—" else s   # 渲染占位符读回来算空，不算内容


META_LINE_ONE = re.compile(r"^\s*- `[a-z_]+`:")


def check_prose(field, s):
    """理由字段写进去之前先验一遍。渲染→解析必须能原样往返，否则下一次
    resolve/reflect 重渲染时这段文字会被截断或串位——而理由字段是不许改的。"""
    if not s:
        return
    for tok in (ENTRY_START, ENTRY_END):
        if tok in s:
            sys.exit(f"--{field} 里不能出现 `{tok}`——那是日志的硬分隔符，会把条目劈开。")
    for ln in s.splitlines():
        if ln.startswith("### "):
            sys.exit(f"--{field} 里不能有以 `### ` 开头的行——它会被当成新的小节，后面的内容会丢。")
        if ln.lstrip().startswith("<!--"):
            sys.exit(f"--{field} 里不能有 HTML 注释——小节解析会在这里截断。")
        if META_LINE_ONE.match(ln):
            sys.exit(f"--{field} 里不能有 `- `键`: 值` 形式的行——会被当成元数据。")


def hclass(meta):
    """老条目没有这个字段，一律按长线处理——决策日志上线时只有长线判断。"""
    v = (meta.get("horizon_class") or "").strip()
    return v if v in HORIZON_CLASSES else "long"


def render_entry(meta, thesis, kill, reflection):
    lines = [ENTRY_START, ""]
    tag = (f"{meta['decided_on']} | {meta['ticker']} | "
           f"{HORIZON_LABEL[hclass(meta)]} | {meta['rating']} | ")
    tag += ({"pending": "pending", "void": "void（已作废，不计分）"}.get(meta["status"])
            or fmt_result_tag(meta))
    lines.append(f"## [{tag}]")
    lines.append("")
    for k in META_KEYS:
        v = meta.get(k, "")
        if v == "" or v is None:
            v = "—"
        lines.append(f"- `{k}`: {v}")
    lines.append("")
    lines.append("### 论文一句话")
    lines.append("")
    lines.append(thesis or "—")
    lines.append("")
    lines.append("### 什么会证伪它")
    lines.append("")
    lines.append(kill or "—")
    lines.append("")
    lines.append("### 反思")
    lines.append("")
    lines.append(reflection or "待回填（持有期满后由 `decision_log.py reflect` 写入）。")
    lines.append("")
    lines.append(ENTRY_END)
    return "\n".join(lines)


def fmt_result_tag(meta):
    def pct(x):
        try:
            return f"{float(x):+.1f}%"
        except (TypeError, ValueError):
            return "NA"
    ca = meta.get("call_alpha")
    try:
        mark = " ✓" if float(ca) > 0 else " ✗"
    except (TypeError, ValueError):
        mark = ""   # Hold：不是方向性判断，不打对错
    return (f"{pct(meta.get('raw_return'))} | alpha {pct(meta.get('alpha'))} | "
            f"{meta.get('held_days', '?')}d{mark}")


# ============================================================
# 读写（原子）
# ============================================================

def read_log(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@contextmanager
def log_lock(path):
    """整个 读→校验→改→写 事务上锁。

    原子写只保证不留半个文件，不保证不丢更新：两个进程同时 read 到旧内容，
    后写的那个会静默覆盖先写的。单用户也会撞上——两个终端各跑一条 add 就够了。
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path + ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def safe_write(path, new_text, before, expect_delta=0):
    """写盘前把新文本重新解析一遍，验完不变量才落盘。

    验两件事：条目数对得上（没被吞、没被重复替换）；已存在条目的 thesis / kill
    一个字都没变。理由字段只许写一次——这是整个日志的立身之本，
    所以它不能只是"规矩"，得是写盘前会失败的检查。
    """
    after = parse_entries(new_text)          # 结构坏了这里就抛
    if len(after) != len(before) + expect_delta:
        raise LogCorrupt(f"条目数异常：写前 {len(before)} 条，预期 {len(before) + expect_delta} 条，"
                         f"实际渲染出 {len(after)} 条。已中止，日志未被修改。")
    old_by = {e["id"]: e for e in before}
    for e in after:
        o = old_by.get(e["id"])
        if not o:
            continue
        for fld, label in (("_thesis", "论文一句话"), ("_kill", "什么会证伪它")):
            if e[fld] != o[fld]:
                raise LogCorrupt(
                    f"{e['id']} 的「{label}」被改动了。理由字段只许写一次——"
                    f"事后改理由就是事后合理化。已中止，日志未被修改。")
    atomic_write(path, new_text)


def atomic_write(path, text):
    """临时文件 + os.replace：中途崩了不会留半个日志。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".log-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def ensure_header(text):
    return text if text.strip() else HEADER


# ============================================================
# 工具函数
# ============================================================

def normalize_rating(s):
    if not s:
        return None
    key = s.strip()
    if key in RATINGS:
        return key
    k = key.lower().replace(" ", "").replace("-", "")
    return RATING_ALIASES.get(k) or RATING_ALIASES.get(key)


def guess_market(ticker):
    t = ticker.upper()
    if t.endswith(".HK"):
        return "hk"
    if t.endswith(".T"):
        return "jp"
    if t.endswith((".SS", ".SZ")):
        return "cn"
    if t.endswith(".TW"):
        return "tw"
    return "us"


def guess_tier(market):
    return {"us": "T1", "hk": "T2", "jp": "T2", "cn": "T3", "tw": "T3"}.get(market, "T1")


def next_id(entries, ticker, date_str, hc="long"):
    """按已存在的 id 逐个试，不用计数——手删过一条的话计数式会撞号，
    撞号之后 resolve --id 会同时改到两条。"""
    stem = f"{date_str.replace('-', '')}-{ticker.upper()}-{hc[0].upper()}"
    used = {e["id"] for e in entries}
    n = 1
    while f"{stem}{n:02d}" in used:
        n += 1
    return f"{stem}{n:02d}"


def days_between(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


def num(v, default=None):
    """空字段在日志里渲染成 '—'，读回来要能安全转数字。

    NaN / inf 一律当无效：手改坏的条目应该被排除或报错，
    不能悄悄变成一个看起来合理的数字去污染汇总。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def today():
    return datetime.now().strftime("%Y-%m-%d")


# ============================================================
# 子命令
# ============================================================

def cmd_add(args):
    path = args.log
    check_prose("thesis", args.thesis)
    check_prose("kill", args.kill)
    with log_lock(path):
        _add_locked(args, path)


def _add_locked(args, path):
    text = ensure_header(read_log(path))
    entries = parse_entries(text)

    ticker = args.ticker.upper()
    decided_on = args.date or today()
    if args.rating.strip().lower() in NON_CALLS or args.rating.strip() in NON_CALLS:
        sys.exit(f"'{args.rating}' 不是方向性判断——「通过去劣筛选」只是说可以进入深度研究，"
                 f"不是买入建议。别记进决策日志，否则记分板会被一堆非决策稀释。")
    rating = normalize_rating(args.rating)
    if not rating:
        sys.exit(f"评级无法解析：'{args.rating}'。只收五档：{' / '.join(RATINGS)}（星级和中文也认，见 RATING_ALIASES）")

    hc = args.horizon_class
    # 写入前扫一遍防重复。**horizon_class 必须进 key**——双轨研究下同一份报告
    # 同一天本来就会产出一长一短两条判断，那不是重复。
    for e in entries:
        if (e["ticker"].upper() == ticker and e["decided_on"] == decided_on
                and e.get("source", "") == (args.source or "—")
                and hclass(e) == hc):
            sys.exit(f"重复：{e['id']} 已经是同标的 + 同决策日 + 同来源报告 + 同"
                     f"{HORIZON_LABEL[hc]}。要改评级请新开一条，不要覆盖。")

    market = args.market or guess_market(ticker)
    tier = args.tier or guess_tier(market)
    if tier not in TIERS:
        sys.exit(f"tier 只能是 {TIERS}")
    benchmark = args.benchmark or BENCHMARKS.get(market, "SPY")

    # 取入场价：按**共同交易日**取，窗口给到决策日 +14 天。
    # 原来窗口只到 +2 天，周六决策碰上长周末就取不到任何 bar，
    # 条目照记但 entry_price 为空 → 永远无法判定。
    dates, smap, bmap = aligned_series(ticker, benchmark, decided_on)
    entry_date = pick_date(dates, decided_on)
    if not entry_date:
        print(f"⚠️  {decided_on} 之后取不到 {ticker} 与 {benchmark} 的共同交易日——"
              f"条目照记，但 alpha 回填不了。（T3 标的和部分海外票 Yahoo 覆盖不全）",
              file=sys.stderr)
    elif entry_date != decided_on:
        print(f"ℹ️  {decided_on} 不是交易日，入场价按 {entry_date} 计。", file=sys.stderr)

    horizon = args.horizon if args.horizon else HORIZON_CLASSES[hc]
    meta = {
        "id": next_id(entries, ticker, decided_on, hc),
        "ticker": ticker,
        "market": market,
        "tier": tier,
        "rating": rating,
        "decided_on": decided_on,
        "horizon_class": hc,
        "horizon_days": str(horizon),
        "benchmark": benchmark,
        "entry_date": entry_date or "",
        "entry_price": f"{smap[entry_date]:.4f}" if entry_date else "",
        "bench_entry": f"{bmap[entry_date]:.4f}" if entry_date else "",
        "status": "pending",
        "source": args.source or "—",
        "retro": "true" if args.retro else "false",
    }
    entry = render_entry(meta, args.thesis, args.kill, "")
    safe_write(path, text.rstrip() + "\n\n" + entry + "\n", entries, expect_delta=1)

    print(f"✅ {meta['id']}  {ticker} {HORIZON_LABEL[hc]} {rating} [{tier}]  基准 {benchmark}")
    if entry_date:
        print(f"   建仓参考 {entry_date} @ {smap[entry_date]:.2f}，基准 @ {bmap[entry_date]:.2f}")
    print(f"   判定日 ≥ {(datetime.strptime(decided_on, '%Y-%m-%d') + timedelta(days=horizon)).strftime('%Y-%m-%d')}"
          f"（{horizon} 天）")
    if args.retro:
        print("   ⚠️  标了 retro=true：回溯录入，不是当时真做的决策。统计时要和实时决策分开看。")


def maturity_date(e):
    """约定的判定日 = 决策日 + horizon_days。"""
    return (datetime.strptime(e["decided_on"], "%Y-%m-%d")
            + timedelta(days=int(num(e.get("horizon_days"), 0)))).strftime("%Y-%m-%d")


def cmd_resolve(args):
    path = args.log
    with log_lock(path):
        text = ensure_header(read_log(path))
        entries = parse_entries(text)
        targets = [e for e in entries if e["id"] == args.id] if args.id else \
                  [e for e in entries if e["status"] == "pending"
                   and today() >= maturity_date(e)]
        if not targets:
            print("没有需要回填的条目。" if not args.id else f"找不到 id={args.id}")
            return

        new_text, n = text, 0
        for e in targets:
            if e["status"] != "pending":
                print(f"跳过 {e['id']}：状态已是 {e['status']}")
                continue

            due = maturity_date(e)
            if today() < due and not args.early:
                print(f"跳过 {e['id']}：判定日是 {due}，还没到。"
                      f"提前判定会让分数取决于你哪天跑 resolve——真要提前用 --early。")
                continue

            dates, smap, bmap = aligned_series(e["ticker"], e["benchmark"], e["decided_on"])
            if not dates:
                print(f"跳过 {e['id']}：{e['ticker']} 与基准 {e['benchmark']} 没有共同交易日。")
                continue

            # 判定价取**约定到期日当天或之后的第一个共同交易日**，不是"今天最新价"。
            # 用最新价的话，晚一个月跑 resolve 就得到不同的分数——那不叫判定，叫挑日子。
            exit_date = pick_date(dates, due) if not args.early else pick_date(dates)
            if not exit_date:
                print(f"跳过 {e['id']}：到期日 {due} 之后还没有共同交易日"
                      f"（最后一个是 {dates[-1]}）。停牌或退市了？可以 `void --id {e['id']}`。")
                continue

            stale = days_between(exit_date, today())
            if stale > 10 and not args.early:
                print(f"⚠️  {e['id']}：判定日用的是 {exit_date}，距今 {stale} 天——"
                      f"数据可能停在停牌前。")

            m = measure(e, dates, smap, bmap, exit_date)
            meta = {k: e.get(k, "") for k in META_KEYS}
            meta.update({
                "status": "resolved", "resolved_on": m["exit_date"],
                # 入场价也用判定时这一次拉取的序列重算——这是复权基准错配的修复点：
                # 记录里存的那个是当初快照下的值，和今天的退出价基准不同。
                "entry_date": m["entry_date"],
                "entry_price": f"{m['entry_px']:.4f}", "bench_entry": f"{m['bench_entry']:.4f}",
                "exit_price": f"{m['exit_px']:.4f}", "bench_exit": f"{m['bench_exit']:.4f}",
                "raw_return": f"{m['raw']:.2f}", "bench_return": f"{m['bench']:.2f}",
                "alpha": f"{m['alpha']:.2f}",
                "call_alpha": f"{m['call_alpha']:.2f}" if m["call_alpha"] is not None else "n/a",
                "held_days": str(m["held"]),
            })
            rebuilt = render_entry(meta, e["_thesis"], e["_kill"], e["_reflection"])
            new_text = new_text.replace(e["_raw"], rebuilt, 1)
            n += 1

            ca = m["call_alpha"]
            verdict = ("Hold 不是方向性判断，不计入胜率" if ca is None
                       else "论文兑现" if ca > 0 else "论文没兑现")
            print(f"📊 {e['id']}  {e['ticker']} {e['rating']}  "
                  f"{m['entry_date']} → {m['exit_date']}（{m['held']}d）")
            print(f"   原始 {m['raw']:+.1f}%   基准({e['benchmark']}) {m['bench']:+.1f}%   "
                  f"**alpha {m['alpha']:+.1f}%**"
                  + (f"   方向修正后 {ca:+.1f}%" if DIRECTION.get(e["rating"]) == -1 else "")
                  + f"  → {verdict}")
            d = DIRECTION.get(e["rating"], 0)
            if d == 1 and m["raw"] > 0 and m["alpha"] < 0:
                print(f"   ⚠️  赚了 {m['raw']:+.1f}% 但跑输基准 {abs(m['alpha']):.1f}%——"
                      f"这是论文错了，不是赚了。")
            if d == -1 and m["alpha"] > 0:
                print(f"   ⚠️  当初的结论是「{e['rating']}」，标的却跑赢基准 "
                      f"{m['alpha']:+.1f}%——判错了。")

        if n:
            safe_write(path, new_text, entries)
            print("\n下一步：给每条写 2-4 句反思 —— "
                  "`decision_log.py reflect --id <ID> --text \"...\"`。不写反思，下次研究就读不到教训。")


def cmd_void(args):
    """作废一条：退市、停牌、并购、或者当初就没取到价——留着记录，但不进记分板。"""
    path = args.log
    with log_lock(path):
        text = ensure_header(read_log(path))
        entries = parse_entries(text)
        hit = next((e for e in entries if e["id"] == args.id), None)
        if not hit:
            sys.exit(f"找不到 id={args.id}")
        if hit["status"] == "void":
            sys.exit(f"{args.id} 已经是 void。")
        meta = {k: hit.get(k, "") for k in META_KEYS}
        meta["status"] = "void"
        refl = (hit["_reflection"] + "\n\n" if hit["_reflection"] else "") + f"作废：{args.reason}"
        rebuilt = render_entry(meta, hit["_thesis"], hit["_kill"], refl)
        safe_write(path, text.replace(hit["_raw"], rebuilt, 1), entries)
        print(f"✅ {args.id} 已作废：{args.reason}（记录保留，不进记分板）")


def cmd_reflect(args):
    check_prose("text", args.text)
    with log_lock(args.log):
        _reflect_locked(args)


def _reflect_locked(args):
    path = args.log
    text = ensure_header(read_log(path))
    entries = parse_entries(text)
    hit = next((e for e in entries if e["id"] == args.id), None)
    if not hit:
        sys.exit(f"找不到 id={args.id}")
    existing = hit["_reflection"]
    if existing and not existing.startswith("待回填") and not args.append:
        sys.exit(f"{args.id} 已有反思。要补充用 --append，不要覆盖——"
                 f"覆盖旧反思等于抹掉当时的认知，那正是这个日志要防的。")
    new = (existing + "\n\n" + args.text) if (args.append and existing
           and not existing.startswith("待回填")) else args.text
    meta = {k: hit.get(k, "") for k in META_KEYS}
    rebuilt = render_entry(meta, hit["_thesis"], hit["_kill"], new.strip())
    safe_write(path, text.replace(hit["_raw"], rebuilt, 1), entries)
    print(f"✅ 反思已写入 {args.id}")


def interim(e):
    """未到判定日的临时 alpha。只作参照，**不写回日志、不进记分板**——
    提前落盘就等于允许自己挑一个好看的日子把 pending 结掉。
    但下一次研究要看到它：'上次我说回避，它至今跑赢基准 20%' 是最该被读到的一句话。

    和 cmd_resolve 走同一条计算路径（aligned_series + measure），差别只在退出日：
    这里取最后一个共同交易日，那里取约定到期日。复权基准对齐的修复因此只有一处。
    """
    dates, smap, bmap = aligned_series(e["ticker"], e["benchmark"], e["decided_on"])
    exit_date = pick_date(dates)
    if not exit_date:
        return None
    return measure(e, dates, smap, bmap, exit_date)


def cmd_peek(args):
    """所有 pending 条目的临时战况。到期回填用 resolve，这个只是看一眼。"""
    entries = [e for e in parse_entries(read_log(args.log)) if e["status"] == "pending"]
    if not entries:
        print("没有 pending 条目。")
        return
    rows = []
    for e in entries:
        r = interim(e)
        if r:
            rows.append((e, r))
        else:
            print(f"⚠️  {e['id']} 取不到价格，跳过", file=sys.stderr)
    if not rows:
        return
    rows.sort(key=lambda x: -(abs(x[1]["call_alpha"]) if x[1]["call_alpha"] is not None else 0))
    print("# 临时战况（未到判定日，不计入记分板）\n")
    print(f"{'ID':<22} {'标的':<8} {'期限':<6} {'评级':<12} {'原始':>8} {'基准':>8} "
          f"{'alpha':>9} {'天数':>6}  判断")
    print("-" * 102)
    for e, m in rows:
        ca = m["call_alpha"]
        mark = "—" if ca is None else ("暂时对" if ca > 0 else "暂时错")
        print(f"{e['id']:<22} {e['ticker']:<8} {HORIZON_LABEL[hclass(e)]:<6} {e['rating']:<12} "
              f"{m['raw']:>7.1f}% {m['bench']:>7.1f}% {m['alpha']:>8.1f}% {m['held']:>5}d  {mark}")
    wrong = [e for e, m in rows if m["call_alpha"] is not None and m["call_alpha"] < 0]
    if wrong:
        print(f"\n⚠️  {len(wrong)} 条目前站在错的一边："
              f"{', '.join(e['ticker'] for e in wrong)}。"
              f"\n   这不代表判断错了——判定日还没到，短期价格本来就是噪音。"
              f"\n   但下一次研究这几家时，报告开头必须解释：论文哪里变了，还是市场还没反应过来。")


def cmd_due(args):
    entries = parse_entries(read_log(args.log))
    rows = []
    for e in entries:
        if e["status"] != "pending":
            continue
        held = days_between(e["decided_on"], today())
        horizon = int(num(e.get("horizon_days"), 0))
        if today() >= maturity_date(e):
            rows.append((e, held, horizon))
    if not rows:
        n = sum(1 for e in entries if e["status"] == "pending")
        print(f"没有到期的。共 {n} 条 pending 还在持有期内。")
        return
    print(f"到期待回填（{len(rows)} 条）：\n")
    for e, held, horizon in sorted(rows, key=lambda r: -r[1]):
        print(f"  {e['id']}  {e['ticker']:8s} {e['rating']:12s} "
              f"决策 {e['decided_on']}  已 {held}d / 约定 {horizon}d")
    print(f"\n跑：decision_log.py resolve       # 一次回填全部到期条目")


def cmd_list(args):
    entries = parse_entries(read_log(args.log))
    if args.ticker:
        entries = [e for e in entries if e["ticker"].upper() == args.ticker.upper()]
    if args.status:
        entries = [e for e in entries if e["status"] == args.status]
    if args.horizon_class:
        entries = [e for e in entries if hclass(e) == args.horizon_class]
    if not entries:
        print("没有匹配的记录。")
        return
    print(f"{'ID':<22} {'标的':<9} {'期限':<6} {'层级':<5} {'评级':<12} {'状态':<9} {'alpha':>9}  来源")
    print("-" * 110)
    for e in entries:
        a = num(e.get("alpha"))
        a = f"{a:+.1f}%" if a is not None else "—"
        retro = " (回溯)" if e.get("retro") == "true" else ""
        print(f"{e['id']:<22} {e['ticker']:<9} {HORIZON_LABEL[hclass(e)]:<6} {e['tier']:<5} "
              f"{e['rating']:<12} {e['status']:<9} {a:>9}  "
              f"{os.path.basename(e.get('source', '—'))}{retro}")


def cmd_context(args):
    """输出注入下一次研究 prompt 的历史块。这是整个工具的存在理由。"""
    entries = parse_entries(read_log(args.log))
    ticker = args.ticker.upper() if args.ticker else None

    same = [e for e in entries if ticker and e["ticker"].upper() == ticker]
    same = same[-5:]
    resolved = [e for e in entries
                if e["status"] == "resolved"
                and e["_reflection"] and not e["_reflection"].startswith("待回填")
                and (not ticker or e["ticker"].upper() != ticker)]
    cross = resolved[-3:]

    if not same and not cross:
        print("## 决策日志：无历史记录\n")
        print(f"日志里还没有{'这家公司的' if ticker else ''}记录。"
              f"这份报告出结论后，用 `decision_log.py add` 记一条 pending，"
              f"否则下一次研究还是从零开始。")
        return

    print("## 先读这段：我在这家公司 / 这套方法上过去错在哪\n")
    print("> 下面是决策日志的自动注入。**报告开头必须明确回应它**——"
          "上次的判断兑现了没有、这次哪里改了、为什么这次不会重犯。\n")

    if ticker:
        print(f"### 同标的历史（{ticker}，最近 {len(same)} 条）\n")
        if not same:
            print(f"无。这是第一次记录 {ticker}。\n")
        for e in same:
            head = f"**{e['decided_on']} · {HORIZON_LABEL[hclass(e)]} · {e['rating']}**"
            if e["status"] == "resolved":
                ca = num(e.get("call_alpha"))
                verdict = "" if ca is None else ("　**判对了**" if ca > 0 else "　**判错了**")
                head += (f" → 原始 {num(e['raw_return'], 0):+.1f}% / "
                         f"基准 {num(e['bench_return'], 0):+.1f}% / "
                         f"**alpha {num(e['alpha'], 0):+.1f}%**（{e['held_days']}d）{verdict}")
            else:
                m = None if args.no_fetch else interim(e)
                if m:
                    ca = m["call_alpha"]
                    stand = "" if ca is None else ("　暂时站在对的一边" if ca > 0
                                                   else "　**暂时站在错的一边**")
                    head += (f" → 尚未到判定日（约定 {maturity_date(e)}）；至今原始 "
                             f"{m['raw']:+.1f}% / 基准 {m['bench']:+.1f}% / "
                             f"**alpha {m['alpha']:+.1f}%**（{m['held']}d）{stand}")
                else:
                    head += "  → 尚未判定"
            print(f"- {head}")
            if e["_thesis"]:
                print(f"  - 当初的论文：{e['_thesis']}")
            if e["_kill"]:
                print(f"  - 当初写的证伪条件：{e['_kill']}")
            if e["_reflection"] and not e["_reflection"].startswith("待回填"):
                print(f"  - 反思：{e['_reflection']}")
            print()

        # 长短线打架的历史，是这段注入里最该被读到的东西
        longs = [e for e in same if hclass(e) == "long" and DIRECTION.get(e["rating"], 0)]
        shorts = [e for e in same if hclass(e) == "short" and DIRECTION.get(e["rating"], 0)]
        if longs and shorts:
            dl = DIRECTION[longs[-1]["rating"]]
            ds = DIRECTION[shorts[-1]["rating"]]
            if dl != ds:
                print(f"> ⚠️ **上次长短线结论就是相反的**："
                      f"长线 {longs[-1]['rating']}（{longs[-1]['decided_on']}）"
                      f" vs 短线 {shorts[-1]['rating']}（{shorts[-1]['decided_on']}）。"
                      f"这次必须先说清楚：上次那次分歧后来是谁对了，为什么。\n")

    print(f"### 跨标的教训（最近 {len(cross)} 条已判定）\n")
    if not cross:
        print("无。还没有任何一条决策被回填并写过反思——"
              "在此之前这个框架无法从自己的错误里学。\n")
    for e in cross:
        ca = num(e.get("call_alpha"))
        verdict = "" if ca is None else ("（判对）" if ca > 0 else "（判错）")
        print(f"- **{e['ticker']} {e['decided_on']} {e['rating']}** "
              f"alpha {num(e['alpha'], 0):+.1f}%{verdict}（{e['held_days']}d）：{e['_reflection']}")
    print()


def cmd_score(args):
    """已判定条目的汇总。回答的是「我到底行不行」，不是「我感觉如何」。"""
    entries = [e for e in parse_entries(read_log(args.log)) if e["status"] == "resolved"]
    if not entries:
        print("还没有已判定的条目，算不了。")
        return
    groups = []
    for hc in ("long", "short"):
        sel = [e for e in entries if hclass(e) == hc]
        if sel:
            groups.append((HORIZON_LABEL[hc], sel))

    def summarize(name, group):
        if not group:
            return
        print(f"\n### {name}（{len(group)} 条）")
        # 方向性判断（Buy/Overweight/Sell/Underweight）才计胜率；Hold 不是方向性判断
        directional = [e for e in group if num(e.get("call_alpha")) is not None]
        holds = len(group) - len(directional)
        if directional:
            cas = [num(e["call_alpha"]) for e in directional]
            wins = sum(1 for a in cas if a > 0)
            print(f"  判断正确率    {wins}/{len(directional)}  ({wins/len(directional)*100:.0f}%)"
                  f"   ← 看多要跑赢基准，看空要跑输基准")
            print(f"  平均论文 alpha {sum(cas)/len(cas):+.1f}%   （方向修正后）")
        if holds:
            print(f"  Hold          {holds} 条，不计入胜率")
        alphas = [num(e["alpha"], 0.0) for e in group]
        raws = [num(e["raw_return"], 0.0) for e in group]
        print(f"  平均原始 alpha {sum(alphas)/len(alphas):+.1f}%   （未做方向修正）")
        print(f"  平均原始收益  {sum(raws)/len(raws):+.1f}%")
        fake = sum(1 for e in group
                   if DIRECTION.get(e["rating"]) == 1
                   and num(e["raw_return"], 0) > 0 > num(e["alpha"], 0))
        if fake:
            print(f"  ⚠️  其中 {fake} 条「赚了钱但跑输基准」——只看原始收益会把这些当成功。")

    print("# 决策日志记分板")
    for label, sel in groups:
        live = [e for e in sel if e.get("retro") != "true"]
        retro = [e for e in sel if e.get("retro") == "true"]
        print(f"\n## {label}")
        summarize(f"{label} · 实时决策", live)
        summarize(f"{label} · 回溯录入（不算数，只作参照）", retro)

    if len(groups) == 2:
        print("\n## 长短线对照")
        print("  两边分开算，不合并。长线赢短线输 = 生意看对了择时看错了；反过来 = 在赌 beta。")
        print("  真正要盯的是**同一标的长短线结论相反、而短线那边赢**的情况——"
              "那说明你的长线论文可能只是在解释一个短期现象。")
    print("\n注：alpha = 标的收益 − 同期基准收益（复权，含分红）。样本 < 20 条时不要当结论看。")


# ============================================================
# CLI
# ============================================================

def main():
    p = argparse.ArgumentParser(
        description="决策日志：记 pending、回填 alpha、写反思、注入下一次研究",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="评级五档：" + " / ".join(f"{r}（{RATING_BEHAVIOR[r]}）" for r in RATINGS),
    )
    p.add_argument("--log", default=DEFAULT_LOG, help=f"日志路径（默认 {DEFAULT_LOG}）")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="记一条 pending 决策")
    a.add_argument("--ticker", required=True)
    a.add_argument("--rating", required=True, help="五档之一；星级/中文也认")
    a.add_argument("--thesis", default="", help="一句话论文：这次为什么是这个结论")
    a.add_argument("--kill", default="", help="什么会证伪它（写不出来说明论文不可证伪）")
    a.add_argument("--source", default="", help="来源报告路径")
    a.add_argument("--date", default="", help="决策日 YYYY-MM-DD（默认今天）")
    a.add_argument("--tier", default="", choices=[""] + TIERS, help="可交易分层，默认按后缀猜")
    a.add_argument("--market", default="", help="us/hk/jp/cn/tw，默认按后缀猜")
    a.add_argument("--benchmark", default="", help="默认按市场选：" + str(BENCHMARKS))
    a.add_argument("--horizon-class", default="long", choices=list(HORIZON_CLASSES),
                   dest="horizon_class",
                   help="long = 值不值得长期持有（默认 180 天）；short = 这一两个月的择时（默认 30 天）")
    a.add_argument("--horizon", type=int, default=0,
                   help="判定窗口天数；不给就按 --horizon-class 的默认值")
    a.add_argument("--retro", action="store_true", help="回溯录入，不是当时真做的决策")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("resolve", help="回填 alpha（不给 --id 就回填所有到期条目）")
    r.add_argument("--id", default="")
    r.add_argument("--early", action="store_true",
                   help="未到判定日也强行结掉，并用最新价。会让分数取决于你哪天跑，慎用")
    r.set_defaults(func=cmd_resolve)

    v = sub.add_parser("void", help="作废一条（退市/停牌/并购/当初没取到价）")
    v.add_argument("--id", required=True)
    v.add_argument("--reason", required=True)
    v.set_defaults(func=cmd_void)

    f = sub.add_parser("reflect", help="写 2-4 句反思")
    f.add_argument("--id", required=True)
    f.add_argument("--text", required=True)
    f.add_argument("--append", action="store_true", help="追加而不是拒绝覆盖")
    f.set_defaults(func=cmd_reflect)

    d = sub.add_parser("due", help="列出到期待回填的条目")
    d.set_defaults(func=cmd_due)

    l = sub.add_parser("list", help="列出记录")
    l.add_argument("--ticker", default="")
    l.add_argument("--status", default="", choices=[""] + STATUSES)
    l.add_argument("--horizon-class", default="", dest="horizon_class",
                   choices=[""] + list(HORIZON_CLASSES))
    l.set_defaults(func=cmd_list)

    c = sub.add_parser("context", help="输出注入下一次研究的历史块")
    c.add_argument("--ticker", default="")
    c.add_argument("--no-fetch", action="store_true", help="不联网算临时 alpha（离线时用）")
    c.set_defaults(func=cmd_context)

    pk = sub.add_parser("peek", help="所有 pending 条目的临时战况（不写回日志）")
    pk.set_defaults(func=cmd_peek)

    s = sub.add_parser("score", help="已判定条目的 alpha 汇总")
    s.set_defaults(func=cmd_score)

    args = p.parse_args()
    try:
        args.func(args)
    except LogCorrupt as ex:
        sys.exit(f"❌ 日志结构有问题，已中止（文件未被修改）：\n   {ex}\n"
                 f"   日志：{args.log}")


if __name__ == "__main__":
    main()
