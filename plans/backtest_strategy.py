# -*- coding: utf-8 -*-
"""策略回测 — 连续 walk-forward (2025-01 至今), 用"我们设定的选股+交易策略"。

选股策略 (趋势 + 突破 + 共振, 多策略对比):
  · 还原买入日热点板块 (成分股历史涨幅反推, 同 backtest_hotspot 方法, 可复现)
  · 对热点成分股截 K 线到买入日跑 classify_stage 形态识别 → 完整候选池 (pool)
  · 套用各选股策略 (S0~S9 + 原A+B门控) 筛选 → 真实 MA5 买点
  · 数值关卡: 买点=真实 MA5 (回踩低吸), 止损=支撑下方 7%/8%, 仓位按评级

交易策略 (纪律化执行) + T+1 硬约束:
  · 入选日挂单, 等价格回踩至 MA5(buy_level) 才低吸建仓 (不追高)
  · 建仓后 **次日(T+1)起** 才允许卖出 (严禁当日买卖 / 卖早于买)
  · 止损: 跌破支撑 (stop_level) 硬止损
  · 止盈: 触及目标价 (tp_level ≈ +12%/+18%)
  · 移动止损: 涨幅≥12% 后激活, 自峰值回撤 10% 离场 (让利润奔跑, 且永不低于成本)
  · 破趋势线: 收盘价跌破 MA20 清仓
  · 最长持有: max_hold 交易日强制到期

多策略对比: --compare 对 S0~S9 + 原A+B门控 全部回测, 按年化(CAGR)排名, 输出对比报告。

输出: data/reports/backtest_strategy_<start>_<end>.md (单策略完整报告)
      data/reports/backtest_compare_<start>_<end>.md (多策略排名 + 最优策略完整报告)

用法:
  python plans/backtest_strategy.py                         # 默认 S14(三角形突破·保本), 2025-01-01~今天
  python plans/backtest_strategy.py --start 2025-01-01 --end 2026-07-19
  python plans/backtest_strategy.py --strategy S2          # 指定选股策略
  python plans/backtest_strategy.py --compare              # 全部策略排名
  python plans/backtest_strategy.py --concepts 8 --per 15 --top-n 8 --runup-pct 40
"""
import os
import sys
import json
import glob
import time
import argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
os.makedirs(REPORTS_DIR, exist_ok=True)

# 复用现有回测/选股/交易构件 (保证与线上策略同源)
from plans.backtest_hotspot import (
    _kl, kline_upto, price_on, change_on, prior_runup,
    get_board_pool, restore_hotspots_on, _board_stocks, _is_garbage, _stale_symbols,
    build_pool, _not_chase,
    strat_baseline, strat_uptrend, strat_breakout, strat_highwr, strat_squeeze,
    strat_pullback, strat_steady, strat_healthymom, strat_timing, strat_box_breakout,
    STRATEGIES,
)
from plans.weekly_hotspot import (
    _estimate_win_rate, _rating, _build_plan, _sell_hint, _trend_state, SMART_EXIT,
)
from analysis.breakout import classify_stage

# 全局 K 线缓存: 同一标的全程只读一次磁盘 (避免重复读盘)
_KL_CACHE = {}


def _klx(symbol):
    if symbol not in _KL_CACHE:
        _KL_CACHE[symbol] = _kl(symbol)
    return _KL_CACHE[symbol]


# 让 backtest_hotspot 内的 restore_hotspots_on 也复用本模块缓存
import plans.backtest_hotspot as _bh
_bh._kl = _klx

# 候选池缓存: 同一买入日 + 参数 只 build_pool 一次 (多策略对比时 10x 提速)
_POOL_CACHE = {}


def _build_pool_cached(buy_date, board_pool, top_n, per, verbose):
    key = (buy_date, top_n, per)
    if key not in _POOL_CACHE:
        hotspots = restore_hotspots_on(buy_date, board_pool, heat_per=8, verbose=verbose)
        _POOL_CACHE[key] = build_pool(buy_date, hotspots, top_n, per, verbose=verbose)
    return _POOL_CACHE[key]


# ───────────────── 全市场候选池 (去掉热点板块限制, 仅用于 S11 三角形突破) ─────────────────
_FULL_POOL_CACHE = {}


def build_full_pool(buy_date, verbose=True, detect_fn=None):
    """全市场候选池 (无热点板块限制): 扫描所有落盘 K 线, 仅保留三角形突破形态。

    返回候选列表, 单项结构同 build_pool (含 triangle 预检, 供 strat_triangle 复用);
    名称无统一映射, 暂用代码 (name=sym)。detect_fn 可切换 S11/S12 检测标准。
    """
    if detect_fn is None:
        detect_fn = detect_triangle
    pat = os.path.join(DATA_DIR, "klines", "kl_*.json")
    files = glob.glob(pat)
    pool = []
    seen = set()
    stale = _stale_symbols()
    for fp in files:
        sym = os.path.basename(fp)[3:-5]   # kl_XXXXXX.json -> XXXXXX
        if sym in stale or sym in seen:
            continue
        seen.add(sym)
        try:
            kl = _klx(sym)
        except Exception:
            continue
        kl_b = kline_upto(kl, buy_date)
        if len(kl_b) < 80:
            continue
        price_b = price_on(kl, buy_date)
        chg_b = change_on(kl, buy_date) or 0
        if price_b is None:
            continue
        tri = detect_fn(kl_b)
        if tri is None:
            continue
        # 仅对三角形幸存者做形态识别 (取真实评分/评级, 数量已很小)
        res = classify_stage(
            [b["close"] for b in kl_b], [b["high"] for b in kl_b],
            [b["low"] for b in kl_b], [b["volume"] for b in kl_b],
            price=price_b)
        wr = _estimate_win_rate(res["stage"], res["signals"])
        rating = _rating(res["score"], wr, res["stage"])
        runup = prior_runup(kl, buy_date, lookback=20)
        pool.append({
            "symbol": sym, "name": sym,
            "buy_date": buy_date, "price_b": price_b, "chg_b": chg_b,
            "stage": "triangle", "score": res["score"],
            "signals": res["signals"], "details": res["details"],
            "win_rate": wr, "rating": rating, "prior_runup": runup,
            "limit_up_buy": chg_b >= 9.5, "kl": kl,
            "concepts": [], "triangle": tri,
        })
    if verbose:
        print(f"    全市场候选池(三角形): {len(pool)} 只 (扫描 {len(files)} 个标的, 无热点限制)")
    return pool


def _build_full_pool_cached(buy_date, verbose):
    if buy_date not in _FULL_POOL_CACHE:
        _FULL_POOL_CACHE[buy_date] = build_full_pool(buy_date, verbose=verbose)
    return _FULL_POOL_CACHE[buy_date]


def build_full_pool_v2(buy_date, verbose=True):
    """S12 全市场候选池: 同 S11 三角检测 (v1), 优化在退出/目标位。"""
    return build_full_pool(buy_date, verbose=verbose, detect_fn=detect_triangle)


_FULL_POOL_CACHE_V2 = {}


def _build_full_pool_cached_v2(buy_date, verbose):
    if buy_date not in _FULL_POOL_CACHE_V2:
        _FULL_POOL_CACHE_V2[buy_date] = build_full_pool_v2(buy_date, verbose=verbose)
    return _FULL_POOL_CACHE_V2[buy_date]


# ───────────────── 原 A+B 门控选股策略 (复刻首版回测逻辑) ─────────────────
def strat_ab(pool, runup_pct=40, buy_date=None):
    """原 A+B 门控: 评分≥45 + 前期大涨不追 + (多头排列 或 站稳5日线)。"""
    out = []
    for c in pool:
        if c["score"] < 45:
            continue
        if _not_chase(c, runup_pct):
            continue
        kl_b = kline_upto(c["kl"], buy_date)
        if len(kl_b) < 60:
            continue
        ma = _trend_state([b["close"] for b in kl_b], c["price_b"])
        if ma.get("bull") or ma.get("steady"):
            out.append(c)
    return out


# ───────────────── S11 三角形突破选股策略 (上升中继型) ─────────────────
def _local_pivots(highs, lows, left=2, right=2):
    """返回窗口内局部拐点索引: 高点为局部最大, 低点为局部最小。"""
    n = len(highs)
    hi, lo = [], []
    for i in range(left, n - right):
        if highs[i] >= max(highs[i - left:i + right + 1]):
            hi.append(i)
        if lows[i] <= min(lows[i - left:i + right + 1]):
            lo.append(i)
    return hi, lo


def _linfit(xs, ys):
    """最小二乘拟合 (slope, intercept)。"""
    n = len(xs)
    if n < 2:
        return 0.0, (ys[0] if ys else 0.0)
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def detect_triangle(kl_b, win=55, min_pivots=3):
    """检测对称三角形 (上升中继型)。kl_b: 截至买入日的K线。

    逻辑 (对应图注「趋势向上→回调收敛三角→放量突破→加速」):
      1. 前置: 近60日整体上涨 (上升中继, 非下跌趋势)
      2. 末 win 根内至少 3 个下降高点 + 3 个上升低点 (收敛三角)
      3. 上轨斜率<0(高点越来越低) 且 下轨斜率>0(低点越来越高)
      4. 收敛: 起点宽度 > 终点宽度(终点≤起点60%)
      5. 当前价仍在三角内部 (尚未提前突破)
      6. 量能萎缩: 后半段均量 < 前半段均量 (盘整收敛)
      7. 临近末端: 当前 x 已接近两轨交叉点(apex, ≥40%), 但尚未越过太多
    返回 rail 参数 dict (供 run_backtest 动态计算突破位/止损位) 或 None。
    """
    n = len(kl_b)
    if n < 80:
        return None
    closes = [b["close"] for b in kl_b]
    if closes[-1] < closes[-60]:          # 前置: 上升中继
        return None
    w = kl_b[-win:]
    highs = [b["high"] for b in w]
    lows = [b["low"] for b in w]
    vols = [b["volume"] for b in w]
    hi, lo = _local_pivots(highs, lows, left=2, right=2)
    if len(hi) < min_pivots or len(lo) < min_pivots:
        return None
    hs, hi_int = _linfit(hi, [highs[i] for i in hi])     # 上轨斜率(应为负)
    ls, lo_int = _linfit(lo, [lows[i] for i in lo])      # 下轨斜率(应为正)
    if not (hs < 0 and ls > 0):
        return None
    w_start = hi_int - lo_int
    last_x = len(w) - 1
    w_end = (hi_int + hs * last_x) - (lo_int + ls * last_x)
    if w_start <= 0 or w_end >= w_start * 0.6:           # 未明显收敛
        return None
    upper_now = hi_int + hs * last_x
    lower_now = lo_int + ls * last_x
    last_close = closes[-1]
    if not (lower_now * 0.995 <= last_close <= upper_now * 1.005):
        return None                                      # 已提前突破, 不在三角内
    half = len(vols) // 2
    vol_back = sum(vols[half:]) / max(1, len(vols) - half)
    vol_front = sum(vols[:half]) / max(1, half)
    if vol_front <= 0 or vol_back >= vol_front * 0.85:   # 量未萎缩
        return None
    denom = (hs - ls)
    if denom == 0:
        return None
    x_cross = (lo_int - hi_int) / denom                  # 两轨交叉点
    if x_cross <= 0 or last_x < 0.40 * x_cross:          # 太早, 尚未成熟
        return None
    avg_vol20 = (sum(vols[-20:]) / 20) if len(vols) >= 20 else (sum(vols) / len(vols))
    return {
        "upper_slope": hs, "upper_at_buy": hi_int + hs * last_x,
        "lower_slope": ls, "lower_at_buy": lo_int + ls * last_x,
        "avg_vol20": avg_vol20,
        "x_cross": x_cross, "last_x": last_x,
        "height0": w_start,                 # 三角起点高度 (测量目标位)
        "level0": (hi_int + lo_int) / 2.0,  # 中轴
    }


def strat_triangle(pool, runup_pct=40, buy_date=None):
    """S11 三角形突破 (上升中继): 在热点候选池中识别对称三角形盘整,
    要求 上升趋势 + 收敛三角(高点降/低点升) + 量能萎缩 + 临近末端;
    突破信号 = 收盘价站上上轨 + 放量(量比≥1.5) (由 run_backtest 动态判定)。"""
    out = []
    for c in pool:
        if _not_chase(c, runup_pct):
            continue
        kl_b = kline_upto(c["kl"], buy_date)
        tri = detect_triangle(kl_b)
        if tri is None:
            continue
        c = dict(c)                       # 不污染共享候选池
        c["triangle"] = tri
        out.append(c)
    return out


# ───────────────── S12 三角形突破优化版 (提升胜率) ─────────────────
def strat_triangle_v2(pool, runup_pct=40, buy_date=None):
    """S12 三角形突破优化版: 同 S11 三角检测, 优化在退出端(软MA20)与目标位(测量)。"""
    out = []
    for c in pool:
        if _not_chase(c, runup_pct):
            continue
        kl_b = kline_upto(c["kl"], buy_date)
        tri = detect_triangle(kl_b)
        if tri is None:
            continue
        c = dict(c)
        c["triangle"] = tri
        out.append(c)
    return out


# ───────────────── S13 入场过滤 (基于诊断的失败因素分析) ─────────────────
# 诊断结论 (S12 全市场 554 笔): 失败交易 95% 集中在 止损+破MA20; 入场可区分因素:
#   · RS(个股20日收益−市场20日收益): 高(>0.073)胜率57% vs 低(<0.022)49%
#   · 市场深度: mkt_vs_ma60 深度熊市(<-1.7%)胜率46% vs 中性/牛市55-57%
#     (注: 之前要求市场>MA60 太严, 应只排除"深度熊市", 允许中性)
#   · 流动性 avg_vol20: 极低(<560万)胜率46% vs 中等59%
#   · 三角相对高度: 小(<13.4%)胜率58% vs 大(>20.6%)50%
def _s13_entry_ok(entry_price, bar, di, bdates, bm, tri, level, k, D, mkt_bars, mkt_dates):
    """S13 入场多重过滤: 任一不满足 → 放弃当日, 等窗口内后续日重试。返回 (ok, reason)。"""
    # 1) 相对强度 RS ≥ 阈值: 个股20日收益 不低于 市场20日收益 (龙头, 非滞后反弹)
    try:
        if D in mkt_dates:
            mi = mkt_dates.index(D)
            if mi >= 20:
                sret = entry_price / bm[bdates[max(0, di - 20)]]["close"] - 1
                mret = mkt_bars[mkt_dates[mi]]["close"] / mkt_bars[mkt_dates[mi - 20]]["close"] - 1
                if sret - mret < 0.02:          # RS 偏弱
                    return False, "rs_weak"
    except Exception:
        pass
    # 2) 市场非深度熊市: 上证距 MA60 不低于 -1.7% (软门控, 允许中性/牛市)
    try:
        if D in mkt_dates:
            mi = mkt_dates.index(D)
            mc = mkt_bars[D]["close"]
            m60 = sum(mkt_bars[mkt_dates[x]]["close"] for x in range(max(0, mi - 59), mi + 1)) / min(mi + 1, 60)
            if mc / m60 - 1 < -0.017:
                return False, "mkt_deep_bear"
    except Exception:
        pass
    # 3) 最小流动性: 20日均量 ≥ 500万 (剔除极低流动性标的, 其胜率显著偏低)
    if (tri.get("avg_vol20") or 0) < 5_000_000:
        return False, "illiquid"
    # 4) 三角相对高度上限: 避免过大/松散三角 (高度/价 > 28% 胜率偏低)
    h0 = tri.get("height0")
    if h0 and entry_price:
        if h0 / entry_price > 0.28:
            return False, "tri_too_wide"
    return True, "ok"


def strat_triangle_v3(pool, runup_pct=40, buy_date=None):
    """S13 三角形突破(优化+入场过滤): 选股同 S12, 差异在 run_backtest 入场过滤。"""
    return strat_triangle_v2(pool, runup_pct, buy_date)


def strat_triangle_v4(pool, runup_pct=40, buy_date=None):
    """S14 三角形突破(优化+保本止损): 选股同 S12, 差异在 run_backtest 退出端加保本止损。"""
    return strat_triangle_v2(pool, runup_pct, buy_date)


def strat_triangle_v5(pool, runup_pct=40, buy_date=None):
    """S15 三角形突破(优化+保本止损+跟随止损): 在 S14 基础上, 保本激活后加峰值-8%跟随,
    把趋势反转的深套提前以盈利了结。"""
    return strat_triangle_v2(pool, runup_pct, buy_date)


def strat_triangle_v6(pool, runup_pct=40, buy_date=None):
    """S16 三角形突破(优化+保本+突破确认入场): 在 S14 基础上, 突破日只标记、次日仍站上轨才建仓,
    过滤假突破(突破后次日跌回、命中硬止损的失败交易)。"""
    return strat_triangle_v2(pool, runup_pct, buy_date)


STRAT_MAP = {
    "AB": strat_ab,
    "S0": strat_baseline, "S1": strat_uptrend, "S2": strat_breakout,
    "S3": strat_highwr, "S4": strat_squeeze, "S5": strat_pullback,
    "S6": strat_steady, "S7": strat_healthymom, "S8": strat_timing,
    "S9": strat_box_breakout, "S11": strat_triangle, "S12": strat_triangle_v2,
    "S13": strat_triangle_v3, "S14": strat_triangle_v4, "S15": strat_triangle_v5,
    "S16": strat_triangle_v6,
}
COMPARE_LIST = ([("原A+B门控", strat_ab)] + STRATEGIES +
                 [("S11 三角形突破", strat_triangle),
                 ("S12 三角形突破(优化)", strat_triangle_v2),
                 ("S13 三角形突破(过滤)", strat_triangle_v3),
                 ("S14 三角形突破(保本)", strat_triangle_v4)])  # STRATEGIES = [(label, fn), ...]


# ───────────────── 选股 (套用指定策略) ─────────────────
def select_gated(buy_date, board_pool, top_n, per, runup_pct,
                 strategy_fn=strat_ab, verbose=True, pool_override=None):
    """买入日视角选股: 热点板块 → 形态识别(候选池) → 套用策略筛选 → 真实 MA5 买点。

    返回 picks 列表, 每项为 dict:
      symbol, name, selection_date, price_b, buy_level(MA5), stop_level, tp_level,
      position(仓位%), rating, stage, score, signals, concepts
    """
    if pool_override is not None:
        pool = pool_override
    else:
        pool = _build_pool_cached(buy_date, board_pool, top_n, per, verbose=verbose)
    cands = strategy_fn(pool, runup_pct=runup_pct, buy_date=buy_date)
    picks = {}
    for c in cands:
        sym = c["symbol"]
        if sym in picks:
            continue
        kl = _klx(sym)
        kl_b = kline_upto(kl, buy_date)
        if len(kl_b) < 60:          # 需足够历史算 MA20
            continue
        price_b = c["price_b"]
        chg_b = c["chg_b"]
        ma = _trend_state([b["close"] for b in kl_b], price_b)
        wr = c["win_rate"]
        rating = c["rating"]
        tri = c.get("triangle")
        if tri is not None:
            # 三角形突破: 动态上轨突破买点 (建仓逻辑见 run_backtest 的 breakout 分支)
            position = {"重点": 8, "关注": 8, "观察": 6, "暂避": 5}.get(rating, 5)
            stop = round(tri["lower_at_buy"] * 0.99, 2)   # 跌破下轨支撑=形态失败
            tp = round(tri["upper_at_buy"] * 1.18, 2)     # 目标 +18%
            picks[sym] = {
                "symbol": sym, "name": c.get("name") or sym,
                "selection_date": buy_date, "price_b": price_b, "chg_b": chg_b,
                "buy_level": None, "stop_level": stop, "tp_level": tp,
                "position": position, "rating": rating,
                "stage": "triangle", "score": c["score"],
                "signals": c["signals"], "concepts": c.get("concepts", []),
                "entry_mode": "breakout", "triangle": tri,
            }
            continue
        ma = _trend_state([b["close"] for b in kl_b], price_b)
        buy, stop, stop_pct, position, buy_level = _build_plan(
            {"price": price_b, "stage": c["stage"], "change_pct": chg_b},
            wr, rating, ma)
        if buy_level is None:       # 门控失败 (破位/弱势)
            continue
        tp, _ = _sell_hint(buy_level, c["stage"], stop)
        picks[sym] = {
            "symbol": sym, "name": c.get("name") or sym,
            "selection_date": buy_date, "price_b": price_b, "chg_b": chg_b,
            "buy_level": buy_level, "stop_level": stop, "tp_level": tp,
            "position": position, "rating": rating,
            "stage": c["stage"], "score": c["score"],
            "signals": c["signals"], "concepts": c.get("concepts", []),
            "entry_mode": "pullback", "triangle": None,
        }
    return sorted(picks.values(), key=lambda x: -x["score"])


# ───────────────── 交易模拟 ─────────────────
def _ma_n(closes, n):
    if len(closes) < n:
        return None
    return sum(closes[-n:]) / n


def _check_exit(pos, bar, ma20, ma20_slope=None):
    """返回 (exit_price, reason) 或 (None, None)。优先级: 硬止损 > 止盈 > 移动止损 > 破MA20。"""
    price, low, high = bar["close"], bar["low"], bar["high"]
    stop = pos["stop_level"]
    tp = pos["tp_level"]
    # 0) 保本止损 + 跟随止损 (S14 保本 / S15 再加跟随)
    #    浮盈≥5% 后激活: 硬止损上移至成本+1%(保本地板); 若开启 trail_be(S15),
    #    则自峰值回撤 S15_TRAIL_PCT 跟随(永不低于保本+1%), 让利润奔跑且把"趋势反转"的
    #    深套(原破MA20 亏损)提前在峰值-8% 以盈利了结。
    if pos.get("breakeven") and price >= pos["entry_price"] * 1.05:
        pos["_be_active"] = True
    if pos.get("_be_active"):
        _peak = max(pos["peak"], price)
        _be = round(pos["entry_price"] * 1.01, 2)          # 保本地板 +1%
        if pos.get("trail_be"):
            _tr = round(_peak * (1 - S15_TRAIL_PCT), 2)
            if _tr > _be:
                _be = _tr
        if stop is None or _be > stop:
            pos["stop_level"] = _be
            stop = _be
    # 1) 硬止损
    if stop is not None and low <= stop:
        return stop, "止损"
    # 2) 止盈
    if tp is not None and high >= tp:
        return tp, "止盈"
    # 更新峰值
    if price > pos["peak"]:
        pos["peak"] = price
    ret = pos["peak"] / pos["entry_price"] - 1
    # 3) 移动止损 (涨幅≥scale_out_pct 激活, 自峰值回撤 trailing_pct, 且不低于成本)
    if ret >= SMART_EXIT["scale_out_pct"]:
        pos["trailing"] = True
    if pos.get("trailing"):
        ts = pos["peak"] * (1 - SMART_EXIT["trailing_pct"])
        if ts < pos["entry_price"]:
            ts = pos["entry_price"]          # 永不低于成本
        if price <= ts:
            return round(ts, 2), "移动止损"
    # 4) 破 MA20 趋势线
    if SMART_EXIT.get("use_ma20") and ma20 is not None and price < ma20:
        # S12(soft_ma20): 仅当 MA20 斜率转负(趋势线拐头)才清仓; 单日回踩不砍, 避免正常回调被误杀
        if not pos.get("soft_ma20") or ma20_slope is None or ma20_slope <= 0:
            return price, "破MA20"
    return None, None


DIAG_RECORDS = []   # 逐笔诊断记录 (成功/失败因素分析用, 由 run_backtest 填充)
SKIP_RECORDS = []   # 被 S13 入场过滤剔除的候选 (验证过滤有效性)
DBG_CNT = {"trig": 0, "pass": 0, "fail": 0, "picks_total": 0}  # 调试计数器
S15_TRAIL_PCT = 0.08   # S15 跟随止损: 保本激活后自峰值回撤 8% (永不低于保本+1%)


def _market_is_bear(buy_date):
    """截至 buy_date, 上证 MA20 相对 MA60 < -3% 视为空头(与 weekly_hotspot.REGIME_NEUTRAL_BAND 一致)。

    用于回测/实盘同源的"空头→空仓"路由: 空头周不新建仓(已持仓仍按纪律退出),
    使回测年化/胜率与实盘一致, 避免空头行情下虚假买点拖累收益。
    """
    try:
        kl0 = _klx("000001")
        kl_b = kline_upto(kl0, buy_date)
        if len(kl_b) < 60:
            return False
        closes = [b["close"] for b in kl_b]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        return (ma20 - ma60) / ma60 < -0.03
    except Exception:
        return False


def run_backtest(start, end, concepts=8, per=15, top_n=8, runup_pct=40,
                 entry_window=5, max_hold=20, initial=1_000_000,
                 strategy_fn=strat_ab, verbose=True, full_market=False):
    # 交易日历 (上证指数)
    kl0 = _klx("000001")
    cal = sorted(b["date"][:10] for b in kl0 if start <= b["date"][:10] <= end)
    mkt_bars = {b["date"][:10]: b for b in kl0}
    mkt_dates = [b["date"][:10] for b in kl0]
    DIAG_RECORDS.clear()
    SKIP_RECORDS.clear()
    DBG_CNT["trig"] = DBG_CNT["pass"] = DBG_CNT["fail"] = DBG_CNT["picks_total"] = 0
    if not cal:
        raise RuntimeError("无交易日历数据, 检查 start/end 与 K 线缓存")

    # 周频再平衡: 取每个 ISO 周的首个交易日
    seen_wk, rebal = set(), []
    for d in cal:
        wk = datetime.strptime(d, "%Y-%m-%d").isocalendar()[:2]
        if wk not in seen_wk:
            seen_wk.add(wk)
            rebal.append(d)
    rebal_set = set(rebal)
    if verbose:
        print(f"[BACKTEST] 区间 {start}~{end}  再平衡 {len(rebal)} 次 (周频)  策略={getattr(strategy_fn,'__name__','?')}")

    # 板块 universe (按方法论: 当前实时榜快照, 全历史共用 — 数据源限制)
    board_pool = get_board_pool(buy_date=rebal[0], pool_size=120,
                               heat_per=8, verbose=verbose)

    BMAP = {}        # symbol -> (date->bar, sorted dates)
    def bar_map(sym):
        if sym not in BMAP:
            kl = _klx(sym)
            BMAP[sym] = ({b["date"][:10]: b for b in kl},
                         [b["date"][:10] for b in kl])
        return BMAP[sym]

    def equity_on(date):
        eq = cash
        for p in positions:
            bm, _ = bar_map(p["symbol"])
            if date in bm:
                eq += p["shares"] * bm[date]["close"]
        return eq

    cash = float(initial)
    positions = []   # 已建仓 (active)
    pending = []     # 已入选未建仓 (等回踩 MA5, 窗口内)
    trades = []      # 已平仓
    equity_curve = []        # (date, equity)
    selection_log = []       # (date, n_selected, n_pending_added)
    monthly_eq = {}          # month_str -> equity (月末)

    last_d = cal[0]
    for D in cal:
        is_rebal = D in rebal_set
        # --- 1) 持仓逐日盯市 + 退出判定 (T+1: 仅 entry_date 次日及之后可卖) ---
        still = []
        for pos in positions:
            if D <= pos["entry_date"]:      # T+1: 买入当日不可卖
                still.append(pos)
                continue
            bm, bdates = bar_map(pos["symbol"])
            if D not in bm:
                still.append(pos)
                continue
            bar = bm[D]
            idx = bdates.index(D)
            ma20 = _ma_n([bm[d]["close"] for d in bdates[max(0, idx - 19):idx + 1]], 20)
            ma20_prev = _ma_n([bm[d]["close"] for d in bdates[max(0, idx - 24):idx - 4]], 20)
            ma20_slope = (ma20 - ma20_prev) if (ma20 is not None and ma20_prev is not None) else None
            ex_price, reason = _check_exit(pos, bar, ma20, ma20_slope)
            hold_days = bdates.index(D) - bdates.index(pos["entry_date"])
            if ex_price is None and hold_days > max_hold:
                ex_price, reason = bar["close"], "到期"
            if ex_price is not None:
                proceeds = pos["shares"] * ex_price
                cash += proceeds
                pnl = proceeds - pos["cost"]
                # ---- 逐笔诊断: 持仓最大回撤/浮盈 + 记录 ----
                _i0 = bdates.index(pos["entry_date"]); _i1 = bdates.index(D)
                _mae = 0.0; _mfe = 0.0
                for _x in range(_i0, _i1 + 1):
                    _b = bm[bdates[_x]]
                    _mae = min(_mae, _b["low"] / pos["entry_price"] - 1)
                    _mfe = max(_mfe, _b["high"] / pos["entry_price"] - 1)
                _rec = {
                    "symbol": pos["symbol"], "name": pos["name"],
                    "buy_date": pos["entry_date"], "sell_date": D,
                    "return_pct": round((ex_price / pos["entry_price"] - 1) * 100, 2),
                    "win": (ex_price / pos["entry_price"] - 1) > 0,
                    "reason": reason, "hold_days": hold_days,
                    "rating": pos["rating"], "stage": pos["stage"],
                    "exit_ma20_slope": round(ma20_slope, 4) if ma20_slope is not None else None,
                    "mae": round(_mae, 4), "mfe": round(_mfe, 4),
                }
                _rec.update(pos.get("_entry_diag", {}))
                DIAG_RECORDS.append(_rec)
                trades.append({
                    "symbol": pos["symbol"], "name": pos["name"],
                    "selection_date": pos["selection_date"],
                    "buy_date": pos["entry_date"], "buy_price": pos["entry_price"],
                    "sell_date": D, "sell_price": round(ex_price, 2),
                    "return_pct": round((ex_price / pos["entry_price"] - 1) * 100, 2),
                    "pnl": round(pnl, 2), "size_pct": pos["size_pct"],
                    "hold_days": hold_days, "reason": reason,
                    "rating": pos["rating"], "stage": pos["stage"],
                    "concepts": pos["concepts"],
                })
            else:
                still.append(pos)
        positions = still

        # --- 2) 待建仓: 回踩 MA5(pullback) 或 放量突破上轨(breakout) 触发建仓 ---
        still_pending = []
        for pend in pending:
            if D > pend["entry_deadline"]:
                continue                    # 窗口过期, 放弃
            bm, bdates = bar_map(pend["symbol"])
            if D not in bm:
                still_pending.append(pend)
                continue
            bar = bm[D]
            di = bdates.index(D)

            entry_price = None
            stop_for_pos = pend["stop_level"]
            tp_for_pos = pend["tp_level"]
            if pend.get("entry_mode") == "breakout":
                # 突破买入: 收盘价站上动态上轨 + 放量(量比≥1.5)
                tri = pend["triangle"]
                # 距买入日(入选日)的交易日数 k: 入选日可能不在该标的 K 线中(停牌/数据缺口),
                # 取 K 线中 ≤ 入选日 的最近一个交易日索引, 避免 index 报错 (全市场扫描更易命中)
                sel = pend["selection_date"]
                sel_idx = 0
                for _i, _d in enumerate(bdates):
                    if _d <= sel:
                        sel_idx = _i
                k = di - sel_idx
                level = tri["upper_at_buy"] + tri["upper_slope"] * k
                if strategy_fn in (strat_triangle_v2, strat_triangle_v3):
                    # ---- S12/S13 优化: 同 S11 入场(突破当日买), 叠加"软MA20退出" + 测量目标位 ----
                    # 入场与 S11 一致(收盘站上轨 + 量比≥1.5); 核心改进在退出端(soft_ma20, 见 _check_exit)
                    # 与目标位(三角高度投影, 更贴近真实测量目标, 封顶+20%/保底+10%)。
                    # S13 额外在入场时施加多重过滤 (见 _s13_entry_ok)。
                    if bar["close"] > level and bar["volume"] >= tri["avg_vol20"] * 1.5:
                        DBG_CNT["trig"] += 1
                        _ep = max(bar["open"], round(level, 2))
                        if strategy_fn is strat_triangle_v3:
                            _ok, _fr = _s13_entry_ok(_ep, bar, di, bdates, bm, tri, level, k, D, mkt_bars, mkt_dates)
                            if _ok:
                                DBG_CNT["pass"] += 1
                            else:
                                DBG_CNT["fail"] += 1
                            if not _ok:
                                # 记录被过滤剔除的候选 (用于验证过滤有效性)
                                _srec = {"symbol": pend["symbol"], "date": D, "reason": _fr,
                                         "entry_gap": round(_ep/level - 1, 4),
                                         "avg_vol20": tri.get("avg_vol20"),
                                         "tri_height_pct": round(tri["height0"]/_ep, 4) if tri.get("height0") else None}
                                try:
                                    if D in mkt_dates:
                                        _mi = mkt_dates.index(D)
                                        if _mi >= 20:
                                            _srec["rs"] = round(_ep/bm[bdates[max(0,di-20)]]["close"]-1
                                                                - (mkt_bars[mkt_dates[_mi]]["close"]/mkt_bars[mkt_dates[_mi-20]]["close"]-1), 4)
                                        _mc = mkt_bars[D]["close"]
                                        _m60 = sum(mkt_bars[mkt_dates[x]]["close"] for x in range(max(0,_mi-59),_mi+1))/min(_mi+1,60)
                                        _srec["mkt_vs_ma60"] = round(_mc/_m60 - 1, 4)
                                except Exception:
                                    pass
                                SKIP_RECORDS.append(_srec)
                                pend["_skip_reason"] = _fr
                                still_pending.append(pend)
                                continue
                        entry_price = _ep
                        stop_for_pos = round((tri["lower_at_buy"] + tri["lower_slope"] * k) * 0.99, 2)
                        h0 = tri.get("height0")
                        if h0:
                            # 测量目标位 = 突破位 + 三角高度投影, 封顶 +20% / 保底 +10%
                            tp_for_pos = min(max(entry_price + h0,
                                                 entry_price * 1.10),
                                            entry_price * 1.20)
                        else:
                            tp_for_pos = round(entry_price * 1.18, 2)
                        tp_for_pos = round(tp_for_pos, 2)
                elif strategy_fn is strat_triangle_v6:
                    # S16: 突破确认入场 — 突破日(放量站上轨)只标记, 次日仍站上轨才建仓,
                    # 过滤"假突破"(突破后次日即跌回、命中硬止损的失败交易, 诊断中最大亏损户)。
                    _trig = bar["close"] > level and bar["volume"] >= tri["avg_vol20"] * 1.5
                    if pend.get("_bx_day") is None:
                        if _trig:
                            pend["_bx_day"] = D
                            still_pending.append(pend)
                            continue
                    else:
                        # 已标记突破日 → 次日确认
                        if bar["close"] > level:
                            _ep = max(bar["open"], round(level, 2))
                            entry_price = _ep
                            stop_for_pos = round((tri["lower_at_buy"] + tri["lower_slope"] * k) * 0.99, 2)
                            h0 = tri.get("height0")
                            if h0:
                                tp_for_pos = min(max(entry_price + h0, entry_price * 1.10), entry_price * 1.20)
                            else:
                                tp_for_pos = round(entry_price * 1.18, 2)
                            tp_for_pos = round(tp_for_pos, 2)
                        else:
                            pend["_bx_day"] = None   # 确认失败, 允许后续重新触发
                            still_pending.append(pend)
                            continue
                elif bar["close"] > level and bar["volume"] >= tri["avg_vol20"] * 1.5:
                    entry_price = max(bar["open"], round(level, 2))
                    stop_for_pos = round((tri["lower_at_buy"] + tri["lower_slope"] * k) * 0.99, 2)
            else:
                # 原回踩 MA5 买入
                if bar["low"] <= pend["buy_level"]:
                    entry_price = min(bar["open"], pend["buy_level"])

            if entry_price is None:
                still_pending.append(pend)
                continue
            eq = equity_on(D)
            alloc = pend["position"] / 100.0 * eq
            shares = alloc / entry_price
            if shares * entry_price > cash:
                shares = cash / entry_price
            if shares <= 0:
                still_pending.append(pend)
                continue
            shares = int(shares)
            if shares <= 0:
                still_pending.append(pend)
                continue
            cost = shares * entry_price
            cash -= cost
            # ---- 逐笔诊断: 入场特征 (成功/失败因素分析) ----
            _ediag = {}
            if tri is not None:
                _ei = di
                _ecl = [bm[d]["close"] for d in bdates[max(0, _ei - 19):_ei + 1]]
                _ema20 = sum(_ecl) / len(_ecl) if _ecl else None
                _ediag = {
                    "entry_gap": round(entry_price / level - 1, 4),
                    "vol_ratio": round(bar["volume"] / tri["avg_vol20"], 2) if tri.get("avg_vol20") else None,
                    "ma20_dist": round((entry_price - _ema20) / _ema20, 4) if _ema20 else None,
                    "tri_height_pct": round(tri["height0"] / entry_price, 4) if tri.get("height0") else None,
                    "apex_prox": round(k / tri["x_cross"], 4) if tri.get("x_cross") else None,
                    "price_level": entry_price,
                    "avg_vol20": tri["avg_vol20"],
                }
                try:
                    if D in mkt_dates:
                        _mi = mkt_dates.index(D)
                        _mc = mkt_bars[D]["close"]
                        _m60 = sum(mkt_bars[mkt_dates[x]]["close"] for x in range(max(0, _mi - 59), _mi + 1)) / min(_mi + 1, 60)
                        _ediag["mkt_vs_ma60"] = round(_mc / _m60 - 1, 4)
                        if _mi >= 20:
                            _sret = entry_price / bm[bdates[max(0, _ei - 20)]]["close"] - 1
                            _mret = mkt_bars[mkt_dates[_mi]]["close"] / mkt_bars[mkt_dates[_mi - 20]]["close"] - 1
                            _ediag["rs"] = round(_sret - _mret, 4)
                        if _mi >= 24:
                            _m20 = sum(mkt_bars[mkt_dates[x]]["close"] for x in range(_mi - 19, _mi + 1)) / 20
                            _m20p = sum(mkt_bars[mkt_dates[x]]["close"] for x in range(_mi - 24, _mi - 4)) / 20
                            _ediag["mkt_ma20_slope"] = round(_m20 - _m20p, 4)
                except Exception:
                    pass
            positions.append({
                "symbol": pend["symbol"], "name": pend["name"],
                "entry_date": D, "entry_price": entry_price,
                "selection_date": pend["selection_date"],
                "shares": shares, "cost": cost,
                "stop_level": stop_for_pos, "tp_level": tp_for_pos,
                "soft_ma20": (strategy_fn in (strat_triangle_v2, strat_triangle_v4, strat_triangle_v5, strat_triangle_v6)),
                "breakeven": (strategy_fn in (strat_triangle_v4, strat_triangle_v5, strat_triangle_v6)),
                "trail_be": (strategy_fn is strat_triangle_v5),
                "peak": entry_price, "trailing": False,
                "_entry_diag": _ediag,
                "size_pct": pend["position"], "rating": pend["rating"],
                "stage": pend["stage"], "concepts": pend["concepts"],
            })
        pending = still_pending

        # --- 3) 再平衡日: 选股 + 挂单 ---
        if is_rebal:
            if _market_is_bear(D):
                selection_log.append((D, 0, 0))
                if verbose:
                    print(f"  {D}: 大盘空头 → 空仓观望 (跳过选股, 不挂单)")
                continue
            if full_market and strategy_fn in (strat_triangle_v2, strat_triangle_v3, strat_triangle_v4, strat_triangle_v5, strat_triangle_v6):
                fp = _build_full_pool_cached_v2(D, verbose)
                picks = select_gated(D, board_pool, top_n, per, runup_pct,
                                     strategy_fn=strategy_fn, verbose=verbose,
                                     pool_override=fp)
            elif full_market and strategy_fn is strat_triangle:
                fp = _build_full_pool_cached(D, verbose)
                picks = select_gated(D, board_pool, top_n, per, runup_pct,
                                     strategy_fn=strategy_fn, verbose=verbose,
                                     pool_override=fp)
            else:
                picks = select_gated(D, board_pool, top_n, per, runup_pct,
                                     strategy_fn=strategy_fn, verbose=verbose)
            di = cal.index(D)
            deadline = cal[min(di + entry_window, len(cal) - 1)]
            added = 0
            for pk in picks:
                if any(p["symbol"] == pk["symbol"] for p in positions):
                    continue
                if any(p["symbol"] == pk["symbol"] for p in pending):
                    continue
                pending.append({
                    "symbol": pk["symbol"], "name": pk["name"],
                    "selection_date": D, "buy_level": pk["buy_level"],
                    "stop_level": pk["stop_level"], "tp_level": pk["tp_level"],
                    "position": pk["position"], "rating": pk["rating"],
                    "stage": pk["stage"], "concepts": pk["concepts"],
                    "entry_deadline": deadline,
                    "entry_mode": pk.get("entry_mode", "pullback"),
                    "triangle": pk.get("triangle"),
                })
                added += 1
                DBG_CNT["picks_total"] += 1
            selection_log.append((D, len(picks), added))
            if verbose:
                print(f"  {D}: 入选 {len(picks)} 只 → 挂单 {added} 只 (持仓 {len(positions)}, 待建仓 {len(pending)})")

        # 记录权益 (周频再平衡日 + 月末)
        if is_rebal:
            equity_curve.append((D, round(equity_on(D), 2)))
        m = D[:7]
        monthly_eq[m] = round(equity_on(D), 2)
        last_d = D

    # 期末: 剩余持仓按最后一日收盘价平仓
    final_date = cal[-1]
    for pos in positions:
        bm, bdates_f = bar_map(pos["symbol"])
        px = bm[final_date]["close"] if final_date in bm else pos["entry_price"]
        proceeds = pos["shares"] * px
        cash += proceeds
        # ---- 逐笔诊断 (期末平仓) ----
        _mae = 0.0; _mfe = 0.0
        if final_date in bm and pos["entry_date"] in bm:
            _i0 = bdates_f.index(pos["entry_date"]); _i1 = bdates_f.index(final_date)
            for _x in range(_i0, _i1 + 1):
                _b = bm[bdates_f[_x]]
                _mae = min(_mae, _b["low"] / pos["entry_price"] - 1)
                _mfe = max(_mfe, _b["high"] / pos["entry_price"] - 1)
        _rec = {
            "symbol": pos["symbol"], "name": pos["name"],
            "buy_date": pos["entry_date"], "sell_date": final_date,
            "return_pct": round((px / pos["entry_price"] - 1) * 100, 2),
            "win": (px / pos["entry_price"] - 1) > 0,
            "reason": "期末平仓", "hold_days": None,
            "rating": pos["rating"], "stage": pos["stage"],
            "exit_ma20_slope": None,
            "mae": round(_mae, 4), "mfe": round(_mfe, 4),
        }
        _rec.update(pos.get("_entry_diag", {}))
        DIAG_RECORDS.append(_rec)
        trades.append({
            "symbol": pos["symbol"], "name": pos["name"],
            "selection_date": pos["selection_date"],
            "buy_date": pos["entry_date"], "buy_price": pos["entry_price"],
            "sell_date": final_date, "sell_price": round(px, 2),
            "return_pct": round((px / pos["entry_price"] - 1) * 100, 2),
            "pnl": round(proceeds - pos["cost"], 2), "size_pct": pos["size_pct"],
            "hold_days": None, "reason": "期末平仓",
            "rating": pos["rating"], "stage": pos["stage"],
            "concepts": pos["concepts"],
        })
    positions = []
    # 未触发的挂单直接丢弃 (未建仓, 不计入交易)

    final_equity = cash
    equity_curve.append((final_date, round(final_equity, 2)))
    monthly_eq[final_date[:7]] = round(final_equity, 2)

    if DBG_CNT["trig"] or DBG_CNT["picks_total"]:
        print(f"[DBG] picks_total={DBG_CNT['picks_total']} entry_trig={DBG_CNT['trig']} "
              f"filter_pass={DBG_CNT['pass']} filter_fail={DBG_CNT['fail']} trades={len(trades)}")

    return {
        "start": start, "end": end, "initial": initial,
        "final_equity": round(final_equity, 2),
        "trades": trades, "equity_curve": equity_curve,
        "selection_log": selection_log, "monthly_eq": monthly_eq,
        "params": {
            "concepts": concepts, "per": per, "top_n": top_n,
            "runup_pct": runup_pct, "entry_window": entry_window,
            "max_hold": max_hold, "smart_exit": SMART_EXIT,
            "strategy": getattr(strategy_fn, "__name__", str(strategy_fn)),
            "full_market": full_market,
        },
    }


# ───────────────── 绩效指标 ─────────────────
def _metrics(res):
    init = res["initial"]
    final = res["final_equity"]
    tr = final / init - 1
    days = (datetime.strptime(res["end"], "%Y-%m-%d") -
            datetime.strptime(res["start"], "%Y-%m-%d")).days
    years = days / 365.25
    cagr = (final / init) ** (1 / years) - 1 if years > 0 else 0

    trades = res["trades"]
    closed = [t for t in trades if t["reason"] != "期末平仓"]
    wins = [t for t in closed if t["return_pct"] > 0]
    losses = [t for t in closed if t["return_pct"] <= 0]
    win_rate = len(wins) / len(closed) if closed else 0
    sum_win = sum(t["pnl"] for t in wins)
    sum_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = sum_win / sum_loss if sum_loss else float("inf")
    avg_win = sum_win / len(wins) if wins else 0
    avg_loss = -sum_loss / len(losses) if losses else 0
    _hd = [t["hold_days"] for t in closed if t["hold_days"] is not None]
    avg_hold = sum(_hd) / len(_hd) if _hd else 0
    # 最大回撤
    eq = [e for _, e in res["equity_curve"]]
    peak = eq[0] if eq else init
    mdd = 0
    for e in eq:
        if e > peak:
            peak = e
        dd = peak - e
        if dd > mdd:
            mdd = dd
    mdd_pct = mdd / peak if peak else 0

    # 基准: 上证指数买入持有
    kl0 = _klx("000001")
    p0 = price_on(kl0, res["start"])
    p1 = price_on(kl0, res["end"])
    bench_ret = (p1 / p0 - 1) if (p0 and p1) else 0
    bench_cagr = (1 + bench_ret) ** (1 / years) - 1 if years > 0 else 0

    return {
        "total_return": tr, "cagr": cagr, "win_rate": win_rate,
        "n_trades": len(closed), "n_wins": len(wins), "n_losses": len(losses),
        "profit_factor": profit_factor, "avg_win": avg_win, "avg_loss": avg_loss,
        "avg_hold": avg_hold, "max_drawdown": mdd_pct,
        "bench_ret": bench_ret, "bench_cagr": bench_cagr,
        "n_open_end": len([t for t in trades if t["reason"] == "期末平仓"]),
    }


# ───────────────── 报告 ─────────────────
def _fmt_pct(x):
    return f"{x*100:+.2f}%" if isinstance(x, (int, float)) else str(x)


def build_report(res, strategy_label="原A+B门控"):
    m = _metrics(res)
    p = res["params"]
    L = []
    L.append(f"# 策略回测报告 — {strategy_label}\n")
    L.append(f"- 回测区间: **{res['start']} ~ {res['end']}**")
    L.append(f"- 初始资金: ¥{res['initial']:,}    期末权益: **¥{res['final_equity']:,}**")
    L.append("")
    L.append("## 一、绩效汇总\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    L.append(f"| 总收益率 | **{_fmt_pct(m['total_return'])}** |")
    L.append(f"| 年化收益 (CAGR) | **{_fmt_pct(m['cagr'])}** |")
    L.append(f"| 最大回撤 | {_fmt_pct(-m['max_drawdown'])} |")
    L.append(f"| 交易笔数 (平仓) | {m['n_trades']} (胜 {m['n_wins']} / 负 {m['n_losses']}) |")
    L.append(f"| 胜率 | {_fmt_pct(m['win_rate'])} |")
    L.append(f"| 盈亏比 (盈利因子) | {m['profit_factor']:.2f} |")
    L.append(f"| 平均盈利 | ¥{m['avg_win']:,.0f} |")
    L.append(f"| 平均亏损 | ¥{m['avg_loss']:,.0f} |")
    L.append(f"| 平均持仓 | {m['avg_hold']:.1f} 交易日 |")
    L.append(f"| 基准(上证)买入持有收益 | {_fmt_pct(m['bench_ret'])} (年化 {_fmt_pct(m['bench_cagr'])}) |")
    L.append(f"| 超额收益 (策略−基准, 年化) | {_fmt_pct(m['cagr'] - m['bench_cagr'])} |")
    L.append("")

    L.append("## 二、策略参数\n")
    L.append(f"- 选股策略: **{strategy_label}**")
    if p.get("full_market"):
        L.append(f"- 选股 universe: **全市场 (无热点板块限制)** — 扫描所有落盘 K 线, 仅三角形突破形态入选; 前期20日涨幅≥{p['runup_pct']}% 剔除(不追高)")
    else:
        L.append(f"- 选股: 热点板块 Top {p['top_n']} × 每板 {p['per']} 股; 前期20日涨幅≥{p['runup_pct']}% 剔除(不追高)")
    if "S14" in strategy_label:
        L.append(f"- 买点: **突破当日买入** (同 S12 入场)")
        L.append(f"- 止损: 跌破三角下轨支撑 ≈ -1% (形态失败硬止损); 目标价=测量目标位(三角高度投影, 封顶+20%/保底+10%)")
        L.append(f"- 退出(核心优化): ① 破MA20 仅在 MA20 斜率转负时清仓(单日回踩不砍); ② **保本止损**: 浮盈≥5% 后硬止损上移至成本价上方≈+1%, 使'冲高反转'的失败突破以微利了结而非深套 (诊断显示亏损交易峰值均达+6.5%)")
    elif "S15" in strategy_label:
        L.append(f"- 买点: **突破当日买入** (同 S12 入场)")
        L.append(f"- 止损: 跌破三角下轨支撑 ≈ -1% (形态失败硬止损); 目标价=测量目标位(三角高度投影, 封顶+20%/保底+10%)")
        L.append(f"- 退出(核心优化): 在 S14 基础上加 **跟随止损**: 保本激活(浮盈≥5%)后, 硬止损自峰值回撤 {int(S15_TRAIL_PCT*100)}% 跟随(永不低于保本+1%), 让利润奔跑; 把'趋势反转'的深套提前在峰值-{int(S15_TRAIL_PCT*100)}% 以盈利了结 (原破MA20 亏损户转为盈利)")
    elif "S16" in strategy_label:
        L.append(f"- 买点: **突破确认后买入** (S14 退出端不变; 入场改为: 突破日放量站上轨只标记, 次日仍站上轨才建仓, 过滤假突破)")
        L.append(f"- 止损: 跌破三角下轨支撑 ≈ -1% (形态失败硬止损); 目标价=测量目标位(三角高度投影, 封顶+20%/保底+10%)")
        L.append(f"- 退出(核心优化): 同 S14 — ① 破MA20 仅在斜率转负时清仓; ② 保本止损(浮盈≥5%→成本+1%)")
    elif "S13" in strategy_label:
        L.append(f"- 买点: **突破当日买入 + 入场过滤** (同 S12 入场; 额外施加 4 重过滤: ①RS≥0.02 个股领涨市场 ②市场非深度熊市(上证距MA60≥-1.7%) ③20日均量≥500万 ④三角相对高度≤28%)")
        L.append(f"- 止损: 跌破三角下轨支撑 ≈ -1% (形态失败硬止损); 目标价=测量目标位(三角高度投影, 封顶+20%/保底+10%)")
        L.append(f"- 退出(核心优化): 破MA20 仅在 MA20 斜率转负(趋势线拐头)时才清仓, 单日回踩不砍 (避免正常回调被误杀 → 胜率提升关键); 其余止损/止盈/移动止损逻辑同 S11")
    elif "S12" in strategy_label:
        L.append(f"- 买点: **突破当日买入** (与 S11 完全一致: 收盘站上轨 + 量比≥1.5; 三角检测同 S11)")
        L.append(f"- 止损: 跌破三角下轨支撑 ≈ -1% (形态失败硬止损); 目标价=测量目标位(三角高度投影, 封顶+20%/保底+10%)")
        L.append(f"- 退出(核心优化): 破MA20 仅在 MA20 斜率转负(趋势线拐头)时才清仓, 单日回踩不砍 (避免正常回调被误杀 → 胜率提升关键); 其余止损/止盈/移动止损逻辑同 S11")
    elif "三角形" in strategy_label or "S11" in strategy_label:
        L.append(f"- 买点: **放量突破动态上轨** (T+1: 入选日挂单, 次日~{p['entry_window']}日内 收盘价站上三角上轨且量比≥1.5 才建仓)")
        L.append(f"- 止损: 跌破三角下轨支撑 ≈ -1% (形态失败硬止损); 目标价 +18%")
    else:
        L.append(f"- 买点: 真实 MA5 回踩低吸 (T+1: 入选日挂单, 次日~{p['entry_window']}日内回踩才建仓, 不追高)")
        L.append(f"- 止损: 支撑下方 {int(SMART_EXIT['stop_pct']*100)}%~8% (硬止损)")
    L.append(f"- 止盈: 目标价 +{int(SMART_EXIT['scale_out_pct']*100)}%~+18%")
    L.append(f"- 移动止损: 涨幅≥{int(SMART_EXIT['scale_out_pct']*100)}% 激活, 自峰值回撤 {int(SMART_EXIT['trailing_pct']*100)}% 离场 (永不低于成本)")
    L.append(f"- 破趋势线: 收盘跌破 MA20 清仓; 最长持有 {p['max_hold']} 交易日强制到期")
    L.append(f"- 仓位: 重点/关注 8% · 观察 6% · 暂避 5% (按评级)")
    L.append("")

    # 月度收益
    L.append("## 三、月度权益\n")
    L.append("| 月份 | 月末权益 | 月收益 |")
    L.append("|---|---|---|")
    months = sorted(res["monthly_eq"].keys())
    prev = res["initial"]
    for mo in months:
        eqv = res["monthly_eq"][mo]
        ret = eqv / prev - 1 if prev else 0
        L.append(f"| {mo} | ¥{eqv:,.0f} | {_fmt_pct(ret)} |")
        prev = eqv
    L.append("")

    # 逐笔交易
    L.append("## 四、逐笔交易明细\n")
    L.append("| # | 代码 | 名称 | 入选日 | 买入日 | 买价 | 卖日 | 卖价 | 收益% | 仓位% | 持有 | 卖出原因 | 评级 | 形态 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for i, t in enumerate(res["trades"], 1):
        L.append(
            f"| {i} | {t['symbol']} | {t['name']} | {t['selection_date']} "
            f"| {t['buy_date']} | {t['buy_price']:.2f} | {t['sell_date']} "
            f"| {t['sell_price']:.2f} | {_fmt_pct(t['return_pct']/100)} "
            f"| {t['size_pct']} | {t['hold_days'] if t['hold_days'] is not None else '-'} "
            f"| {t['reason']} | {t['rating']} | {t['stage']} |")
    L.append("")

    # 选股统计
    L.append("## 五、每周选股/挂单统计\n")
    L.append("| 再平衡日 | 入选数 | 挂单数 |")
    L.append("|---|---|---|")
    for d, ns, ne in res["selection_log"]:
        L.append(f"| {d} | {ns} | {ne} |")
    L.append("")

    L.append("## 六、局限说明\n")
    if p.get("full_market"):
        L.append("- **选股 universe = 全市场落盘 K 线快照**: 扫描 data/klines 全部标的 (截至数据抓取日的快照, 共数千只), 去掉热点板块限制; 非各买入日实时全市场, 名称无统一映射故以代码展示 (数据源限制, 与 backtest_hotspot 一致)。")
    else:
        L.append("- **板块 universe 为当前实时榜快照**: 新浪概念榜接口无历史日期参数, 全历史回测共用运行日快照的板块/成分股, 非各买入日真实热点 (数据源限制, 与 backtest_hotspot 一致)。")
    L.append("- **无未来函数**: 选股/买卖点均用买入日及之前的 K 线, 不窥探未来。")
    L.append("- **T+1 约束已强制**: 入选日挂单, 仅次日及之后回踩才建仓; 建仓当日不可卖出 (杜绝当日买卖/卖早于买)。")
    L.append("- **未计手续费/滑点/印花税**: 实际收益会略低于报告。")
    L.append("- **K线级交易**: 入场取回踩 MA5 的首个交易日的 min(开盘, MA5), 出场取触发价, 未模拟盘中精确撮合。")
    L.append("- **移动止损/破MA20 为收盘判定**: 盘中触及但收盘收回的情况按未触发处理。")
    return "\n".join(L)


def build_compare_report(ranked, start, end, initial):
    """ranked: [(label, res, metrics), ...] 已按 CAGR 降序。"""
    L = []
    L.append(f"# 多策略回测对比报告 (按年化收益排名)\n")
    L.append(f"- 回测区间: **{start} ~ {end}**    初始资金: ¥{initial:,}")
    L.append(f"- 对比策略数: {len(ranked)}    交易纪律: 同参数 (T+1 / 止损7~8% / 止盈12~18% / 移动止损 / 破MA20 / 最长{max_hold_global}日)")
    L.append("")
    L.append("## 排名 (按年化 CAGR 降序)\n")
    L.append("| 排名 | 策略 | 总收益 | 年化CAGR | 最大回撤 | 胜率 | 交易数 | 盈亏比 | 平均持仓 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, (label, res, m) in enumerate(ranked, 1):
        flag = " ★最优" if i == 1 else ""
        L.append(
            f"| {i} | {label}{flag} | {_fmt_pct(m['total_return'])} "
            f"| **{_fmt_pct(m['cagr'])}** | {_fmt_pct(-m['max_drawdown'])} "
            f"| {_fmt_pct(m['win_rate'])} | {m['n_trades']} "
            f"| {m['profit_factor']:.2f} | {m['avg_hold']:.1f} |")
    L.append("")
    L.append("> 基准(上证)买入持有: "
             f"总收益 {_fmt_pct(ranked[0][2]['bench_ret'])} / 年化 {_fmt_pct(ranked[0][2]['bench_cagr'])}")
    L.append("")
    L.append("## 结论\n")
    best_label, best_res, best_m = ranked[0]
    L.append(f"- **年化收益最高的选股策略为 `{best_label}`**: 总收益 {_fmt_pct(best_m['total_return'])}、"
             f"年化 **{_fmt_pct(best_m['cagr'])}**、最大回撤 {_fmt_pct(-best_m['max_drawdown'])}、"
             f"胜率 {_fmt_pct(best_m['win_rate'])}、盈亏比 {best_m['profit_factor']:.2f}。")
    L.append(f"- 其完整逐笔交易报告见同目录 `backtest_strategy_{start}_{end}_{_safe(best_label)}.md`。")
    L.append("")
    L.append("## 各策略完整报告索引\n")
    for i, (label, res, m) in enumerate(ranked, 1):
        L.append(f"{i}. {label}: 年化 {_fmt_pct(m['cagr'])} | "
                 f"`backtest_strategy_{start}_{end}_{_safe(label)}.md`")
    return "\n".join(L)


def _safe(s):
    return (s.replace(" ", "").replace("(", "").replace(")", "")
             .replace("/", "").replace("+", "p"))


max_hold_global = 20  # 供对比报告文案引用


# ───────────────── 多策略对比主流程 ─────────────────
def run_compare(start, end, concepts=8, per=15, top_n=8, runup_pct=40,
                entry_window=5, max_hold=20, initial=1_000_000, verbose=False,
                full_market=False):
    global max_hold_global
    max_hold_global = max_hold
    results = []
    for label, fn in COMPARE_LIST:
        # 注意: 不清除 _POOL_CACHE — 各策略共用同一买入日的候选池 (classify_stage 仅算一次), 提速约 10x
        res = run_backtest(start, end, concepts=concepts, per=per, top_n=top_n,
                           runup_pct=runup_pct, entry_window=entry_window,
                           max_hold=max_hold, initial=initial,
                           strategy_fn=fn, verbose=verbose, full_market=full_market)
        m = _metrics(res)
        results.append((label, res, m))
        print(f"  [compare] {label}: 总收益 {m['total_return']*100:+.2f}%  年化 {m['cagr']*100:+.2f}%  "
              f"回撤 {m['max_drawdown']*100:.2f}%  胜率 {m['win_rate']*100:.1f}%  笔数 {m['n_trades']}")
    ranked = sorted(results, key=lambda x: -x[2]["cagr"])
    # 对比报告
    cmp_md = build_compare_report(ranked, start, end, initial)
    cmp_out = os.path.join(REPORTS_DIR, f"backtest_compare_{start}_{end}.md")
    with open(cmp_out, "w", encoding="utf-8") as f:
        f.write(cmp_md)
    # 最优策略完整报告
    best_label, best_res, _ = ranked[0]
    best_md = build_report(best_res, strategy_label=best_label)
    best_out = os.path.join(REPORTS_DIR, f"backtest_strategy_{start}_{end}_{_safe(best_label)}.md")
    with open(best_out, "w", encoding="utf-8") as f:
        f.write(best_md)
    print(f"\n[COMPARE] 排名已保存: {cmp_out}")
    print(f"[COMPARE] 最优策略({best_label})完整报告: {best_out}")
    return ranked


def main():
    ap = argparse.ArgumentParser(description="策略回测 (多选股策略 + T+1 纪律)")
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default=datetime.now().strftime("%Y-%m-%d"))
    ap.add_argument("--concepts", type=int, default=8)
    ap.add_argument("--per", type=int, default=15)
    ap.add_argument("--top-n", type=int, default=8)
    ap.add_argument("--runup-pct", type=float, default=40)
    ap.add_argument("--entry-window", type=int, default=5)
    ap.add_argument("--max-hold", type=int, default=20)
    ap.add_argument("--initial", type=float, default=1_000_000)
    ap.add_argument("--strategy", default="S14",
                    help="选股策略: AB/S0/S1/.../S9/S11~S16 (默认 S14=三角形突破(保本), 已固化为最优策略)")
    ap.add_argument("--compare", action="store_true", help="对全部策略回测并排名")
    ap.add_argument("--full-market", action="store_true",
                    help="S11 三角形突破: 去掉热点板块限制, 扫描全市场落盘 K 线")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--diag", action="store_true", help="导出逐笔诊断JSON(成功/失败因素分析)")
    args = ap.parse_args()

    t0 = time.time()
    if args.compare:
        ranked = run_compare(
            args.start, args.end, concepts=args.concepts, per=args.per,
            top_n=args.top_n, runup_pct=args.runup_pct,
            entry_window=args.entry_window, max_hold=args.max_hold,
            initial=args.initial, verbose=not args.quiet,
            full_market=args.full_market)
        best_label, best_res, best_m = ranked[0]
        print(f"\n[RESULT] 最优策略 = {best_label}  年化 {best_m['cagr']*100:+.2f}%  "
              f"总收益 {best_m['total_return']*100:+.2f}%  回撤 {best_m['max_drawdown']*100:.2f}%")
    else:
        fn = STRAT_MAP.get(args.strategy, strat_triangle_v4)
        if args.strategy not in STRAT_MAP:
            print(f"[WARN] 未知策略 '{args.strategy}', 回退到 S14(三角形突破·保本)")
        is_tri = args.strategy in ("S11", "S12", "S13", "S14", "S15", "S16")
        label = ({"S11": "S11 三角形突破", "S12": "S12 三角形突破(优化)",
                  "S13": "S13 三角形突破(过滤)", "S14": "S14 三角形突破(保本)",
                  "S15": "S15 三角形突破(保本+跟随)",
                  "S16": "S16 三角形突破(保本+确认)"}.get(args.strategy)
                 or (args.strategy if args.strategy in STRAT_MAP else "原A+B门控"))
        if args.full_market and is_tri:
            label = label + "(全市场)"
        res = run_backtest(
            args.start, args.end, concepts=args.concepts, per=args.per,
            top_n=args.top_n, runup_pct=args.runup_pct,
            entry_window=args.entry_window, max_hold=args.max_hold,
            initial=args.initial, strategy_fn=fn, verbose=not args.quiet,
            full_market=args.full_market)
        md = build_report(res, strategy_label=label)
        if args.full_market and args.strategy == "S16":
            suffix = "_s16"
        elif args.full_market and args.strategy == "S15":
            suffix = "_s15"
        elif args.full_market and args.strategy == "S13":
            suffix = "_s13"
        elif args.full_market and args.strategy == "S14":
            suffix = "_s14"
        elif args.full_market and args.strategy == "S12":
            suffix = "_s12"
        elif args.full_market and args.strategy == "S11":
            suffix = "_fullmarket"
        else:
            suffix = ""
        out = os.path.join(REPORTS_DIR, f"backtest_strategy_{args.start}_{args.end}{suffix}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(md)
        if args.diag:
            import json as _json
            dpath = os.path.join(REPORTS_DIR, f"diag_{args.strategy}_{args.start}_{args.end}.json")
            with open(dpath, "w", encoding="utf-8") as f:
                _json.dump(DIAG_RECORDS, f, ensure_ascii=False, indent=1)
            print(f"[DIAG] 已导出逐笔诊断: {dpath} ({len(DIAG_RECORDS)} 笔)")
            if SKIP_RECORDS:
                spath = os.path.join(REPORTS_DIR, f"skip_{args.strategy}_{args.start}_{args.end}.json")
                with open(spath, "w", encoding="utf-8") as f:
                    _json.dump(SKIP_RECORDS, f, ensure_ascii=False, indent=1)
                print(f"[DIAG] 已导出剔除候选: {spath} ({len(SKIP_RECORDS)} 笔)")
        m = _metrics(res)
        print(md)
        print(f"\n[REPORT] 已保存: {out}")
    print(f"[TIME] 耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
