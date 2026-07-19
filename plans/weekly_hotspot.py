# -*- coding: utf-8 -*-
"""本周热点选股流水线

流程:
  1. 分析本周热点版块 (概念板块实时排名, 过滤风格/地域/市值类)
  2. 自选股 = 步骤3策略选出的候选池 (热点板块内按 classify_stage 形态识别, 不再取"涨幅前2")
  3. 按新选股逻辑 (analysis/breakout.classify_stage 形态识别) 在热点版块内选股
  4. 生成报告并推送到企业微信智能机器人 (aibot 通道, 与用户沟通的唯一通道)

用法:
  python plans/weekly_hotspot.py                       # 默认 8 个版块, 自选股=策略候选池, 突破扫描每版块 15 只
  python plans/weekly_hotspot.py --concepts 10 --per 20
  python plans/weekly_hotspot.py --no-push              # 只生成报告不推送
  python plans/weekly_hotspot.py --json                 # 输出 JSON
  python plans/weekly_hotspot.py --runup-days 20 --runup-pct 40   # 前期大涨不追阈值(前N日涨幅>%剔除)

策略微调 (与 backtest_hotspot 回测结论一致):
  · 大盘三状态路由 (实战核心): 先判上证 MA20 vs MA60 得 多头/震荡/空头,
    再蒸馏不同选股策略 (见 REGIME_STRATEGY / apply_regime_strategy):
      - 多头(MA20>MA60): 顺势低吸 = 回调低吸(S5) ∪ 强趋势低波动(S6) ∪ 箱体突破(S9)
      - 震荡(MA20≈MA60): 高胜率共振(S3) ∪ 箱体突破(S9), 薄边降仓(0.6×)
      - 空头(MA20<MA60): 空仓观望, 不出股 (回测无可靠策略, 避免买点窗口虚假信号)
  · 箱体突破(S9): 长期盘整(箱体)后放量向上突破箱顶——量能(量比≥1.5)+价位(新鲜突破)+
    共振(金叉/多头)三重确认, 只买"刚突破"不追"已突破大涨"(详见报告【三b】)。
  · 前期大涨不追: 截至今天前 runup_days 日累计涨幅 > runup_pct% 直接剔除推荐 (等回踩不追高)
  · 每只推荐自带纪律买卖: 买点 / 止损价 / 目标价(take_profit) / 仓位 / 卖出提示(移动止损锁利)
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")
WATCHLIST_PATH = os.path.join(DATA_DIR, "watchlist.json")

from collectors.concept import concept_rank_sina, merge_duplicate_concepts
from collectors.quote import realtime as _realtime
from plans.concept_analysis import filter_concepts
from plans.breakout_scan import run as run_breakthrough, format_report as fmt_breakthrough, _kline_cached
from core.cli import save_watchlist
from analysis.breakout import STAGE_LABELS, classify_stage as _classify_stage


# ───────────────── 形态成功率 / 买入计划 ─────────────────
# 科学退出参数 (与 backtest_weekly_hotspot.settle_smart 同源口径):
#   初始硬止损 -7% → 涨+12%减仓半仓锁利 → 减仓后破MA20(趋势线)或回撤10%清仓, 止损上移保本。
SMART_EXIT = {
    "stop_pct": 0.07,        # 初始硬止损 (突破-7% / 其他-8%)
    "scale_out_pct": 0.12,   # 涨幅达 +12% 触发减仓 (平衡: 不过早减仓)
    "scale_out_frac": 0.5,   # 减仓比例 (半仓)
    "trailing_pct": 0.10,    # 减仓后移动止损 (从峰值回撤 10%, 让利润奔跑)
    "use_ma20": True,        # 破 MA20 = 破趋势线 → 清仓
}


def _estimate_win_rate(stage, signals):
    """依据形态状态 + 信号共振, 估算形态成功率 (0~1)。

    信号组合 → 成功率 经验映射 (非历史回测, 供参考):
      - 突破启动 + 放量 + 均线多头 + 突破平台 : 0.68
      - 趋势中 + VCP + MACD金叉 + KDJ金叉      : 0.60
      - 趋势中 + 布林收缩 + 双金叉              : 0.56
      - 趋势中 + VCP + 单KDJ                   : 0.55
      - 趋势中 + 均线多头(仅)                  : 0.50
      - 趋势中 + 布林收缩(仅)                  : 0.48
    """
    sig = " ".join(signals or [])
    has = lambda k: k in sig
    if stage == "breakout":
        if has("放量") and has("均线多头") and has("突破平台"):
            return 0.68
        return 0.60
    rate = 0.45
    if has("VCP"):
        rate += 0.07
    if has("MACD金叉"):
        rate += 0.05
    if has("KDJ金叉"):
        rate += 0.04
    if has("布林带极致收缩"):
        rate += 0.04
    if has("均线多头"):
        rate += 0.04
    if has("放量"):
        rate += 0.03
    return round(min(rate, 0.65), 2)


def _rating(score, win_rate, stage):
    """综合评级: 重点 / 关注 / 观察 / 暂避。"""
    if stage == "breakout" and win_rate >= 0.60:
        return "重点"
    if win_rate >= 0.55:
        return "关注"
    if win_rate >= 0.50:
        return "观察"
    return "暂避"


def _ma(closes, n):
    """简单移动平均 (最近 n 根收盘)。数据不足返回 None。"""
    if not closes or len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 3)


def _consec_above_ma5(closes):
    """连续站在 MA5 上方的交易日数 (从最新往回数, 跌破即止)。"""
    n = len(closes)
    cnt = 0
    for i in range(n - 1, -1, -1):
        lo = max(0, i - 4)
        window = closes[lo:i + 1]
        if len(window) < 2:
            break
        m = sum(window) / len(window)
        if closes[i] >= m - 1e-9:
            cnt += 1
        else:
            break
    return cnt


def _trend_state(closes, price):
    """均线 + 趋势状态, 供 A+B 买点 与 选股策略门控。

    选股策略 = 趋势 + 突破 + 共振, 仅在以下两种情形给回踩低吸买点:
      1) 多头排列 (bull):  MA5 > MA10 > MA20 且 现价 >= MA5
      2) 调整后站稳5日线 (steady): 现价 >= MA5 且 连续 >=3 日收盘 >= MA5
    其余(破位/无多头排列/在MA5下方) → 不给追高买点, 等放量收复MA5。
    """
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    if None in (ma5, ma10, ma20):
        return {"ma5": ma5, "ma10": ma10, "ma20": ma20,
                "bull": False, "steady": False, "above_ma5": False}
    above_ma5 = price >= ma5 - 1e-9
    bull = above_ma5 and (ma5 > ma10 > ma20)
    steady = above_ma5 and _consec_above_ma5(closes) >= 3
    return {"ma5": ma5, "ma10": ma10, "ma20": ma20,
            "bull": bool(bull), "steady": bool(steady), "above_ma5": bool(above_ma5)}


def _build_plan(c, win_rate, rating, ma=None):
    """生成买点 / 止损 / 仓位 (A+B: 真均线 + 回踩带)。

    ma: _trend_state 返回的均线状态 (含 ma5 / bull / steady)。
      - ma5 可用且 (bull or steady): 买点 = 真 MA5 支撑(数值), 文字给 MA5 回踩带
        (区间 = [MA5, 现价*0.98], 分批低吸); 止损 = 支撑下方 7%/8%。
      - ma5 可用但 非多头排列且未站稳 (破位/弱势): 不给买点 (buy_level=None),
        文字提示"等放量收复5日线"。
      - ma5 缺失 (K线不足): 降级为旧固定百分比, 避免报告空白。
    返回值新增第 5 项 buy_level (None 表示当前不可买)。
    """
    price = float(c.get("price") or 0)
    stage = c.get("stage")
    ma5 = (ma or {}).get("ma5")
    bull = (ma or {}).get("bull")
    steady = (ma or {}).get("steady")

    if ma5 is not None and (bull or steady):
        support = ma5
        band_high = round(max(support, price * 0.98), 2)
        if stage == "breakout":
            buy = "突破回踩5日线≈¥%.2f低吸(¥%.2f–¥%.2f区间分批)" % (support, support, band_high)
        else:
            buy = "缩量回踩5日线≈¥%.2f低吸(¥%.2f–¥%.2f区间分批)" % (support, support, band_high)
        stop = round(support * (0.93 if stage == "breakout" else 0.92), 2)
        buy_level = round(support, 2)
    elif ma5 is not None:
        # 弱势/破位: 不给追高买点, 等放量收复 MA5
        buy = "现价¥%.2f 跌破/未站稳5日线(MA5≈¥%.2f), 等放量收复5日线再考虑低吸" % (price, ma5)
        stop = round(price * 0.93, 2)
        buy_level = None
    else:
        # 无均线数据, 降级旧固定百分比(避免报告空白)
        if stage == "breakout":
            buy = "突破回踩5日线≈¥%.2f低吸" % (price * 0.95)
            stop = round(price * 0.93, 2)
            buy_level = round(price * 0.95, 2)
        else:
            buy = "缩量回踩¥%.2f–%.2f低吸" % (price * 0.95, price * 0.97)
            stop = round(price * 0.92, 2)
            buy_level = round(price * 0.96, 2)

    stop_pct = round((stop / price - 1) * 100, 1) if (price and stop) else 0
    position = {"重点": 8, "关注": 8, "观察": 6, "暂避": 5}.get(rating, 5)
    return buy, round(stop, 2), stop_pct, position, buy_level


def _prior_runup(symbol, lookback=20, ref_date=None):
    """截至 ref_date(默认最新交易日) 前 lookback 个交易日累计涨幅 — 捕捉'前期已大幅上涨'。

    返回涨幅% 或 None (无数据/历史不足)。与 backtest_hotspot.prior_runup 同源逻辑。
    """
    try:
        kl = _kline_cached(symbol)
    except Exception:
        return None
    if not kl:
        return None
    if ref_date is None:
        ref_date = kl[-1]["date"][:10]
    idx = None
    for i, b in enumerate(kl):
        if b["date"][:10] <= ref_date:
            idx = i
        else:
            break
    if idx is None:
        return None
    base_i = max(0, idx - lookback)
    base = kl[base_i]["close"]
    price = kl[idx]["close"]
    if base and price:
        return round((price - base) / base * 100, 2)
    return None


def _sell_hint(buy_price, stage, stop_loss):
    """生成卖出提示 (科学止盈/止损): 涨+10%减仓半仓锁利; 减仓后破MA20(趋势线)
    或回撤8%清仓; 破止损价离场 (减仓后止损上移保本)。"""
    so = SMART_EXIT["scale_out_pct"]
    scale_price = round(buy_price * (1 + so), 2)
    tp_pct = round(so * 100)
    tr = int(SMART_EXIT["trailing_pct"] * 100)
    hint = (f"涨+{tp_pct}%减仓半仓(¥{scale_price})锁利; 减仓后破MA20(趋势线)或回撤{tr}%清仓; "
            f"破¥{stop_loss}止损(减仓后上移保本)")
    return scale_price, hint


def enrich_watchlist(codes, verbose=False):
    """人工自选股 = data/watchlist.json 的纯代码列表; 补全 名称/现价/涨跌幅/形态分析,
    使「自选股」栏目呈现「综合分析」而非仅代码。

    单只失败不影响其它 (实时行情/缓存缺失均降级为仅代码)。零触网部分走 K线缓存。
    """
    out = []
    for code in (codes or []):
        code = str(code).strip()
        if not code:
            continue
        item = {
            "code": code, "name": code, "price": None, "change_pct": None,
            "stage": None, "score": None, "signals": [], "details": {},
            "win_rate": None, "rating": None, "buy_point": None,
            "stop_loss": None, "stop_pct": None, "take_profit": None, "sell_hint": None,
        }
        # 名称 + 现价 (腾讯实时, 单只失败不影响其它)
        try:
            q = _realtime(code)
            item["name"] = q.get("name") or code
            item["price"] = q.get("price")
            item["change_pct"] = q.get("change_pct")
        except Exception as e:
            if verbose:
                print(f"    [watchlist] {code} 实时行情失败: {e}")
        # 形态分析 (K线缓存 + classify_stage, 零触网)
        try:
            kl = _kline_cached(code)
            if kl:
                closes = [b["close"] for b in kl]
                highs = [b["high"] for b in kl]
                lows = [b["low"] for b in kl]
                vols = [b["volume"] for b in kl]
                price = item["price"] if item["price"] else (closes[-1] if closes else 0)
                res = _classify_stage(closes, highs, lows, vols, price=price)
                item["stage"] = res.get("stage")
                item["score"] = res.get("score")
                item["signals"] = res.get("signals", [])
                item["details"] = res.get("details", {})
                wr = _estimate_win_rate(res.get("stage"), res.get("signals", []))
                item["win_rate"] = wr
                item["rating"] = _rating(res.get("score", 0), wr, res.get("stage"))
                ma = _trend_state(closes, price) if (closes and len(closes) >= 20) else None
                bp, sl, sp, pos, bl = _build_plan(
                    {"price": price, "stage": res.get("stage"),
                     "change_pct": item["change_pct"] or 0}, wr, item["rating"], ma)
                item["buy_point"] = bp
                item["buy_level"] = bl
                item["stop_loss"] = sl if bl is not None else None
                item["stop_pct"] = sp if bl is not None else 0
                item["take_profit"], item["sell_hint"] = _sell_hint(price, res.get("stage"), sl)
        except Exception as e:
            if verbose:
                print(f"    [watchlist] {code} 形态分析失败: {e}")
        out.append(item)
    return out


def enrich_pool_entries(entries, verbose=False):
    """策略股票池条目注入实时 当前价/当日涨幅, 并**按实时价重算买点/止损/止盈**
    (不持久化到 stock_pool.json, 仅报告展示用)。

    关键修复: 原 stock_pool.json 的 buy_level/stop_level/tp_level 在 refresh 时按当时
    K线收盘价冻结, 与报告展示的实时「现价」时点错配 → 股价波动后买点严重偏离现价、
    且数值买点与 buy_point 文字自相矛盾(数值=追高, 文字=回踩)。这里用实时价重算,
    保证 买点/止损/止盈 三方同基准、符合"回踩低吸"逻辑。单只失败自动降级(留空)。"""
    for e in (entries or []):
        sym = str(e.get("symbol", "")).strip()
        if not sym:
            continue
        try:
            q = _realtime(sym)
            price = q.get("price")
            cp = q.get("change_pct")
            if isinstance(price, (int, float)):
                e["price"] = price
            if isinstance(cp, (int, float)):
                e["change_pct"] = cp
            # 用实时价 + 真实 K线均线 重算数值关卡 (A+B: 真MA5 + 回踩带 + 选股门控)
            if isinstance(price, (int, float)) and price > 0:
                stage = e.get("stage")
                rating = e.get("rating") or "暂避"
                wr = e.get("win_rate")
                if wr is None:
                    wr = _estimate_win_rate(stage, e.get("signals", []) or [])
                # 真实均线: 从 K线缓存取收盘, 算 MA5/10/20 + 趋势状态
                ma = None
                try:
                    kl = _kline_cached(sym)
                    if kl:
                        closes = [float(b["close"]) for b in kl if b.get("close") is not None]
                        if len(closes) >= 20:
                            ma = _trend_state(closes, price)
                except Exception as ex:
                    if verbose:
                        print(f"    [pool] {sym} K线/均线失败: {ex}")
                buy_text, stop_base, stop_pct, position, bl = _build_plan(
                    {"price": price, "stage": stage, "change_pct": cp or 0,
                     "signals": e.get("signals", []) or [], "score": e.get("score")},
                    wr, rating, ma)
                tp_base = bl if bl is not None else price
                tp, _ = _sell_hint(tp_base, stage, stop_base)
                e["buy_level"] = bl
                e["buy_point"] = buy_text
                # 不可买(破位/弱势) → 止损/止盈置空, 卡片显示 '—'
                e["stop_level"] = round(float(stop_base), 2) if bl is not None else None
                e["stop_pct"] = stop_pct if bl is not None else 0
                e["position"] = position
                e["tp_level"] = round(float(tp), 2) if (bl is not None and tp) else None
                e["take_profit"] = e["tp_level"]
        except Exception as ex:
            if verbose:
                print(f"    [pool] {sym} 实时行情失败: {ex}")
    return entries


def enrich_candidates(breakthrough, runup_days=20, runup_pct=40):
    """对突破候选去重 (同股票跨多热点) + 叠加 win_rate / rating / plan 字段。

    策略微调 (与回测一致):
      · 前期大涨不追: 截至今天前 runup_days 日累计涨幅 > runup_pct% 视为'前期已大幅上涨,
        从推荐列表剔除, 计入 excluded (不追)。
      · 每只推荐附带 take_profit(目标价) + sell_hint(卖出提示: 目标价+移动止损)。
    """
    if not breakthrough or "candidates" not in breakthrough or "error" in breakthrough:
        return breakthrough
    seen = {}
    excluded = []
    for c in breakthrough["candidates"]:
        sym = c.get("symbol")
        if not sym:
            continue
        # ── 前期大涨不追 ──
        runup = _prior_runup(sym, lookback=runup_days)
        if runup is not None and runup >= runup_pct:
            excluded.append({
                "symbol": sym, "name": c.get("name", sym),
                "price": c.get("price"), "prior_runup": runup,
                "concepts": [c.get("concept")] if c.get("concept") else [],
            })
            continue
        if sym in seen:
            # 合并概念 (同一股票出现在多个热点版块)
            if c.get("concept") and c["concept"] not in seen[sym]["concepts"]:
                seen[sym]["concepts"].append(c["concept"])
            continue
        # 取K线供蒸馏策略(strat_*)使用: 回调低吸需 ma20/价格, 与回测同源
        try:
            kl = _kline_cached(sym)
        except Exception:
            kl = None
        ma = None
        if kl:
            closes_c = [float(b["close"]) for b in kl if b.get("close") is not None]
            if len(closes_c) >= 20:
                ma = _trend_state(closes_c, float(c.get("price") or 0))
        wr = _estimate_win_rate(c.get("stage"), c.get("signals", []))
        rating = _rating(c.get("score", 0), wr, c.get("stage"))
        buy, stop, stop_pct, position, bl = _build_plan(c, wr, rating, ma)
        tp, sell_hint = _sell_hint(float(c.get("price") or 0), c.get("stage"), stop)
        seen[sym] = {
            **c,
            "kl": kl,
            "price_b": float(c.get("price") or 0),
            "concepts": [c.get("concept")],
            "win_rate": wr,
            "rating": rating,
            "buy_level": bl,
            "buy_point": buy,
            "stop_loss": stop,
            "stop_pct": stop_pct,
            "position": position,
            "prior_runup": runup,
            "take_profit": tp,
            "sell_hint": sell_hint,
        }
    enriched = sorted(seen.values(), key=lambda x: -x["score"])
    return {**breakthrough, "candidates": enriched, "count": len(enriched),
            "excluded": excluded, "excluded_count": len(excluded)}


# ───────────────── 大盘三状态 × 蒸馏策略路由 ─────────────────
# 来自 backtest_hotspot walk-forward 蒸馏 (2024-06→2026-07, 按大盘三状态拆胜率):
#   多头(MA20>MA60): S5 趋势回调低吸 77%/+6.96% ⊕ S6 强趋势低波动 76%/+6.52%
#   震荡(MA20≈MA60): S3 高胜率共振 56%/+2.97% (唯一正期望, 薄边降仓)
#   空头(MA20<MA60): 无可靠策略(最佳仅53%且样本小, 多数40-50%) → 空仓
REGIME_NEUTRAL_BAND = 0.03  # |MA20-MA60|/MA60 <= 3% 视为纠缠(震荡)

REGIME_STRATEGY = {
    "多头": {
        "label": "顺势低吸(回调+低波)",
        "desc": "大盘多头排列, 顺势做多。实证: 回调低吸胜率77%/均收益+6.96%, "
                "强趋势低波动76%/+6.52% (S5∪S6)。",
        "position_scale": 1.0,
    },
    "震荡": {
        "label": "高胜率共振",
        "desc": "大盘纠缠震荡, 仅高胜率共振(S3)有正期望(56%/+2.97%), 边薄。降仓位、严止损。",
        "position_scale": 0.6,
    },
    "空头": {
        "label": "空仓观望",
        "desc": "大盘空头排列, 无可靠策略(最佳仅53%且样本小, 多数40-50%)。"
                "空仓不出股, 规避买点窗口虚假信号。",
        "position_scale": 0.0,
    },
    "未知": {
        "label": "未知(按震荡处理)",
        "desc": "大盘数据不足, 保守按震荡处理。",
        "position_scale": 0.6,
    },
}


def market_regime():
    """判定当前大盘(上证000001)三状态: 多头/震荡/空头。基于 MA20 vs MA60。

    返回 (状态, MA20相对MA60偏离% 或 None)。与回测 _market_weak 同源口径。
    """
    try:
        kl = _kline_cached("000001", days=250)
        closes = [b["close"] for b in kl if b.get("close")]
        if len(closes) < 60:
            return "未知", None
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        diff = (ma20 - ma60) / ma60
        if diff > REGIME_NEUTRAL_BAND:
            return "多头", round(diff * 100, 2)
        if diff < -REGIME_NEUTRAL_BAND:
            return "空头", round(diff * 100, 2)
        return "震荡", round(diff * 100, 2)
    except Exception:
        return "未知", None


def apply_regime_strategy(candidates, regime, buy_date):
    """按大盘状态路由到蒸馏策略, 返回最终推荐候选 (空头返回空列表=空仓)。

    直接复用 backtest_hotspot 的 strat_* 函数 (与回测同源, 保证口径一致)。
    """
    from plans.backtest_hotspot import strat_pullback, strat_steady, strat_highwr, strat_box_breakout
    if regime == "空头":
        return []
    if regime == "多头":
        # 顺势低吸: 回调低吸(S5) ∪ 强趋势低波动(S6) ∪ 箱体突破(S9), 去重
        pb = [c for c in candidates if c.get("kl")]
        picks = strat_pullback(pb, runup_pct=40, buy_date=buy_date)
        picks += strat_steady(candidates, runup_pct=40, buy_date=buy_date)
        picks += strat_box_breakout(candidates, runup_pct=40, buy_date=buy_date)
    else:  # 震荡 / 未知 → 高胜率共振(S3) ∪ 箱体突破(S9)
        picks = strat_highwr(candidates, runup_pct=40, buy_date=buy_date)
        picks += strat_box_breakout(candidates, runup_pct=40, buy_date=buy_date)
    seen = set()
    out = []
    for c in picks:
        if c["symbol"] not in seen:
            seen.add(c["symbol"])
            out.append(c)
    return out


def _bare(symbol: str) -> str:
    """成分股 symbol 常带 sh/sz 前缀, 自选股统一存 6 位裸码。"""
    return symbol[2:] if symbol[:2] in ("sh", "sz") else symbol


def get_hotspots(top_n: int = 8) -> list:
    """取本周热点版块 (过滤后 Top N)。

    返回: [{name, code, change_pct, amount, leader, leader_code, leader_pct}, ...]
    """
    raw = concept_rank_sina(limit=top_n * 3)
    mapped = [{
        "name": c["name"],
        "code": c["code"],
        "change_pct": c.get("change_pct", 0),
        "amount": c.get("amount", 0),
        "leader": c.get("leader_name", ""),
        "leader_code": c.get("leader_code", ""),
        "leader_pct": c.get("leader_pct", 0),
    } for c in raw]
    return merge_duplicate_concepts(filter_concepts(mapped))[:top_n]


def build_watchlist_from_candidates(breakthrough: dict, verbose: bool = True) -> list:
    """自选股 = 最终推荐的买 list (breakthrough["final"])。

    不再取"涨幅前2" (那会把已涨停的票塞进监控池, 与策略"不追已大涨"相悖),
    也不再跨板块去重丢弃 (跨板块共振=双重确认, 是最强信号, 应保留其全部所属板块)。
    final 是经大盘三状态蒸馏 (apply_regime_strategy) 后的最终买 list:
      多头→全量候选; 震荡→薄边降仓; 空头→空 list。
    若 final 缺失则回退到 candidates (形态识别候选池), 不回退到更早的"涨幅前2"。
    enrich_candidates 已对候选按 symbol 合并概念 (concepts 字段含全部所属板块),
    final 元素同样带 concepts 字段。

    返回: [{code, name, pct, concept}, ...] (concept 为"板块1、板块2"合并串)
    """
    final = breakthrough.get("final")
    if not final:
        final = breakthrough.get("candidates", []) or []
    seen = set()
    picks = []
    for c in final:
        sym = _bare(c.get("symbol", ""))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        cons = c.get("concepts") or ([c.get("concept")] if c.get("concept") else [])
        picks.append({
            "code": sym,
            "name": c.get("name", sym),
            "pct": round(float(c.get("change_pct", 0) or 0), 2),
            "concept": "、".join(cons),
        })
    # 注: 自选股(人工)不再由本函数自动覆盖; 策略精选改由 main 加入股票池(stock_pool)
    wl_codes = [p["code"] for p in picks]
    if verbose:
        print(f"  ✓ 策略精选候选 {len(wl_codes)} 只 (已交由股票池累积, 不覆盖人工自选股)")
    return picks


def build_report(date_str: str, hotspots: list, human_watchlist: list,
                 pool_entries: list, breakthrough: dict, regime: str = None) -> str:
    regime = regime or breakthrough.get("regime", "未知")
    rinfo = REGIME_STRATEGY.get(regime, REGIME_STRATEGY["未知"])
    final = breakthrough.get("final", breakthrough.get("candidates", []))
    is_bear = (regime == "空头")
    rdiff = breakthrough.get("regime_diff")
    lines = [f"\n{'='*50}",
             f"  📊 本周热点选股报告 ({date_str})",
             f"{'='*50}"]

    # 〇、大盘行情状态与策略路由
    lines.append(f"\n【〇、大盘行情状态与策略路由】")
    rdiff_s = f"{rdiff:+.2f}%" if isinstance(rdiff, (int, float)) else "—"
    lines.append(f"  大盘(上证): {regime}  (MA20相对MA60 {rdiff_s})")
    lines.append(f"  采用策略: 【{rinfo['label']}】")
    lines.append(f"  策略说明: {rinfo['desc']}")
    if is_bear:
        lines.append(f"  ⚠️ 空头排列 → 本期空仓观望, 不出股 (候选池 {breakthrough.get('count',0)} 只全部放弃)")
    else:
        lines.append(f"  蒸馏精选: 候选池 {breakthrough.get('count',0)} 只 → 精选 {len(final)} 只")

    # 一、热点版块
    lines.append(f"\n【一、本周热点版块 Top {len(hotspots)}】")
    for i, c in enumerate(hotspots, 1):
        amt_yi = round(c.get("amount", 0) / 1e8, 1) if c.get("amount") else 0
        lead = f" 龙头:{c['leader']}({c['leader_pct']:+.1f}%)" if c.get("leader") else ""
        lines.append(f"  {i}. {c['name']} {c['change_pct']:+.2f}%  成交{amt_yi}亿{lead}")

    # 二、自选股 (人工维护, 已补全名称/现价/形态分析)
    lines.append(f"\n【二、自选股】")
    if not human_watchlist:
        lines.append(f"  （空 — 由你人工维护, 系统不再自动覆盖; 可在 data/watchlist.json 加入股票代码）")
    else:
        lines.append(f"  共 {len(human_watchlist)} 只 (名称/现价/形态分析):")
        for i, w in enumerate(human_watchlist, 1):
            if isinstance(w, dict):
                name = w.get("name", w.get("code", "?"))
                code = w.get("code", "")
                price = w.get("price")
                price_s = f" ¥{price}" if isinstance(price, (int, float)) else ""
                cp = w.get("change_pct")
                cp_s = f" {cp:+.2f}%" if isinstance(cp, (int, float)) else ""
                stg = STAGE_LABELS.get(w.get("stage"), w.get("stage")) if w.get("stage") else "—"
                score = w.get("score")
                wr = w.get("win_rate")
                wr_s = f" 成功率{wr*100:.0f}%" if isinstance(wr, (int, float)) else ""
                rating = w.get("rating") or "—"
                lines.append(f"  {i}. {name}({code}){price_s}{cp_s} | {stg} 评分{score}{wr_s} 评级{rating}")
                if w.get("signals"):
                    lines.append(f"     信号: {'、'.join(w['signals'])}")
            else:
                lines.append(f"  {i}. {w}")

    # 二b、策略股票池 (机器维护, 每日 refresh 更新数值关卡 + 移动止损)
    lines.append(f"\n【二b、策略股票池 ({len(pool_entries)} 只)】")
    if not pool_entries:
        lines.append(f"  （空 — 运行突破扫描后自动累积入池）")
    else:
        lines.append(f"  共 {len(pool_entries)} 只 (入选日 | 现价 | 涨幅 | 形态 | 评分 | 状态 | 买点 | 止损 | 止盈 | 来源):")
        for e in pool_entries:
            if e.get("exited"):
                st = "已退出"
            elif e.get("entered"):
                st = "已建仓"
            else:
                st = "观察中"
            stage_lbl = STAGE_LABELS.get(e.get("stage"), e.get("stage"))
            price = e.get("price")
            price_s = f" ¥{price}" if isinstance(price, (int, float)) else ""
            cp = e.get("change_pct")
            cp_s = f" {cp:+.2f}%" if isinstance(cp, (int, float)) else ""
            reason = e.get("reason", "")
            reason_s = f" | {reason}" if reason and "迁移" not in reason else ""
            lines.append(
                f"  · {e.get('name','')}({e['symbol']}){price_s}{cp_s} 入选{e.get('entry_date','')} "
                f"| {stage_lbl} | 评分{e.get('score','')} | {st} "
                f"| 买{e.get('buy_level','—')} 止损{e.get('stop_level','—')} 盈{e.get('tp_level','—')}"
                f"{reason_s}")

    # 三、蒸馏策略精选
    lines.append(f"\n【三、蒸馏策略精选 ({rinfo['label']}, 大盘{regime})】")
    if "error" in breakthrough:
        lines.append(f"  ❌ 突破扫描失败: {breakthrough['error']}")
    elif is_bear:
        lines.append(f"  🛑 空头排列行情 → 空仓观望, 不出股。")
        lines.append(f"  · 理由: {rinfo['desc']}")
        lines.append(f"  · 候选池仍有 {breakthrough.get('count',0)} 只, 但空头无可靠策略, 全部放弃。")
    else:
        lines.append(f"  候选池 {breakthrough.get('count',0)} 只 (已剔除前期大涨不追 "
                     f"{breakthrough.get('excluded_count', 0)} 只) → 蒸馏精选 {len(final)} 只:")
        for i, c in enumerate(final[:15], 1):
            sig = " | ".join(c.get("signals", [])[:3])
            wr = c.get("win_rate")
            wr_str = f" 成功率{wr*100:.0f}%" if isinstance(wr, (int, float)) else ""
            ru = c.get("prior_runup")
            ru_s = f" 前期{ru:+.0f}%" if isinstance(ru, (int, float)) else ""
            lines.append(
                f"  {i}. {c['name']}({c['symbol']}) ¥{c['price']} {c['change_pct']:+.2f}% "
                f"| {STAGE_LABELS.get(c['stage'], c['stage'])} 评分{c['score']}{wr_str}{ru_s}")
            lines.append(f"     热点:{'、'.join(c.get('concepts', [c.get('concept')]))} | {sig}")
        # 被'前期大涨不追'剔除的清单
        exc = breakthrough.get("excluded") or []
        if exc:
            lines.append(f"\n  🚫 前期大涨不追 (已剔除, 等回踩不追高):")
            for e in exc[:15]:
                ru = e.get("prior_runup")
                ru_s = f"{ru:+.0f}%" if isinstance(ru, (int, float)) else "—"
                lines.append(f"    · {e['name']}({e['symbol']}) ¥{e.get('price','—')} 前期{ru_s} "
                             f"热点:{'、'.join(e.get('concepts', []))}")

    # 三b、箱体突破原理与选股逻辑
    lines.append(f"\n【三b、箱体突破原理与选股逻辑 (S9)】")
    lines.append(f"  · 长期盘整(箱体): 股价在相对狭窄区间(箱底支撑~箱顶阻力)内上下波动, "
                 f"波动逐步收敛、成交量萎缩——这是供需再平衡、浮筹沉淀的过程, 也是主力吸筹的常见形态。")
    lines.append(f"  · 向上突破: 放量站上箱顶(阻力位)=买方力量压倒卖方, 突破临界点; "
                 f"量能确认突破有效性(无量上冲多为假突破, 易回落箱体内)。")
    lines.append(f"  · 选股门槛(量价共振): ①箱体已建成(平台波动<15%) ②新鲜突破(近15日刚站上箱顶, "
                 f"非已大涨) ③放量确认(量比≥1.5) ④均线多头或MACD/KDJ金叉共振。")
    lines.append(f"  · 退出: 沿用科学止盈/止损(涨+12%减仓半仓→破MA20/回撤10%清仓), 让突破后的趋势充分奔跑。")

    # 四、买入建议与计划
    lines.append(f"\n【四、买入建议与计划 (形态成功率 + 综合评级 + 纪律买卖)】")
    if "error" in breakthrough:
        lines.append("  (无数据)")
    elif is_bear:
        lines.append("  🛑 空仓观望: 本期不给出买入计划。")
        lines.append("  · 空头排列下任何买点胜率均不足 (回测最佳仅53%且样本小), 持币等待多头信号。")
    else:
        lines.append("  评级: 重点 > 关注 > 观察 > 暂避 | 仓位为单标的上限建议"
                     + (" (震荡薄边已按0.6×降仓)" if regime == "震荡" else ""))
        lines.append("  纪律(科学止盈/止损): 破止损价离场; 涨+12%减仓半仓锁利; "
                     "减仓后破MA20(趋势线)或回撤10%清仓, 减仓后止损上移保本")
        for i, c in enumerate(final[:12], 1):
            wr = c.get("win_rate")
            wr_str = f"{wr*100:.0f}%" if isinstance(wr, (int, float)) else "—"
            lines.append(
                f"  {i}. 【{c.get('rating','—')}】{c['name']}({c['symbol']}) "
                f"成功率{wr_str} 模型分{c['score']} 仓位{c.get('position','—')}%")
            lines.append(f"     📍 买点: {c.get('buy_point','—')}")
            lines.append(f"     🛡 止损: ¥{c.get('stop_loss','—')} ({c.get('stop_pct','—')}%)")
            lines.append(f"     🎯 目标: ¥{c.get('take_profit','—')}  | {c.get('sell_hint','—')}")

    lines.append(f"\n{'='*50}")
    lines.append("报告生成完毕")
    return "\n".join(lines)


def build_wechat_card(date_str, hotspots, pool_entries, breakthrough, regime):
    """企微摘要卡片: markdown 卡片式(标题/加粗/引用块), 统一 ①②③④ 章节编号,
    格式化不堆文字; 详细买卖计划见 HTML 周报。"""
    b = breakthrough or {}
    rg = b.get("regime", regime or "未知")
    rdiff = b.get("regime_diff")
    rlabel = b.get("strategy_label", "")
    diff_s = f"{rdiff:+.2f}%" if isinstance(rdiff, (int, float)) else "—"
    is_bear = (rg == "空头")
    count = b.get("count", 0)
    dot = "🔴" if is_bear else ("🟢" if rg == "多头" else "🟡")

    L = [f"## 📊 本周热点选股 · {date_str}"]

    # ① 大盘状态与策略
    L.append("")
    L.append("**① 大盘状态**")
    L.append(f"> {dot} 上证 **{rg}** (MA20-MA60 {diff_s})")
    L.append(f"> 策略: {rlabel or '—'}")
    if is_bear:
        L.append(f"> ⚠️ 空头排列 → 空仓观望, 候选池 {count} 只全部放弃")

    # ② 本周热点板块
    L.append("")
    L.append(f"**② 本周热点 Top{len(hotspots)}**")
    for i, c in enumerate(hotspots, 1):
        amt = c.get("amount")
        amt_s = f"{amt / 1e8:.1f}亿" if amt else "0"
        cp = c.get("change_pct") or 0
        arrow = "▲" if cp >= 0 else "▼"
        lp = c.get("leader_pct")
        lead = ""
        if c.get("leader"):
            lp_s = f" {lp:+.1f}%" if isinstance(lp, (int, float)) else ""
            lead = f" ｜龙头 {c['leader']}{lp_s}"
        L.append(f"> {i}. {c['name']}  {arrow}{abs(cp):.2f}%  💰{amt_s}{lead}")

    # ③ 蒸馏策略精选 (空头不产出)
    final = b.get("final", []) or []
    L.append("")
    if is_bear:
        L.append("**③ 蒸馏精选**")
        L.append("> 🛑 空头观望, 本期不出股")
    elif final:
        L.append(f"**③ 蒸馏精选 {len(final)} 只** (候选 {count})")
        for i, c in enumerate(final[:5], 1):
            wr = c.get("win_rate")
            wr_s = f"  成功率{wr * 100:.0f}%" if isinstance(wr, (int, float)) else ""
            L.append(f"> {i}. {c['name']}({c['symbol']})  {c['score']}分{wr_s}")
        if len(final) > 5:
            L.append(f"> …另 {len(final) - 5} 只见 HTML 周报")
    else:
        L.append("**③ 蒸馏精选**")
        L.append(f"> 候选池 {count} 只均未通过当前策略")

    # ④ 策略股票池摘要
    pe = pool_entries or []
    n_total = len(pe)
    n_entered = sum(1 for e in pe if e.get("entered") and not e.get("exited"))
    n_exited = sum(1 for e in pe if e.get("exited"))
    n_watch = n_total - n_entered - n_exited
    L.append("")
    L.append(f"**④ 策略池 {n_total} 只**")
    L.append(f"> 已建仓 {n_entered} ｜观察中 {n_watch} ｜已退出 {n_exited}")
    L.append("")
    L.append("📄 完整买卖计划见 HTML 周报")
    return "\n".join(L)


def main():
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="本周热点选股流水线")
    ap.add_argument("--concepts", type=int, default=8, help="热点版块数量 (默认8)")
    ap.add_argument("--per", type=int, default=15, help="突破扫描每版块成分股数 (默认15)")
    ap.add_argument("--runup-days", type=int, default=20, help="前期涨幅回看交易日数 (默认20)")
    ap.add_argument("--runup-pct", type=float, default=40, help="前期涨幅超此%则'不追'剔除 (默认40)")
    ap.add_argument("--no-push", action="store_true", help="仅生成报告, 不推送微信")
    ap.add_argument("--html", action="store_true", help="生成 HTML 报告文件 (输出 HTML_REPORT:<path>, 供 bot 附带发送)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    date_str = datetime.now().strftime("%Y-%m-%d")
    print(f"[{date_str}] 本周热点选股流水线启动...", flush=True)

    # 1. 热点版块
    print("  ▶ 步骤1: 分析本周热点版块...", flush=True)
    hotspots = get_hotspots(top_n=args.concepts)
    if not hotspots:
        print("  ❌ 无法获取热点版块 (网络异常或休市)", flush=True)
        return 1
    print(f"    热点版块 {len(hotspots)} 个: " + "、".join(c["name"] for c in hotspots), flush=True)

    # 2. 自选股改由步骤3(策略选股)后的候选池重建, 此处不再单独取"涨幅前2"

    # 3. 新选股逻辑筛选 (突破扫描)
    print("  ▶ 步骤3: 按新选股逻辑 (classify_stage) 在热点版块内选股...", flush=True)
    breakthrough = run_breakthrough(
        top_concepts=args.concepts,
        top_per_concept=args.per,
        verbose=True,
    )
    # 3b. 去重 + 叠加 形态成功率 / 综合评级 / 买入计划 + 前期大涨不追过滤
    breakthrough = enrich_candidates(breakthrough,
                                     runup_days=args.runup_days,
                                     runup_pct=args.runup_pct)
    # 2(延后). 策略精选 → 加入股票池(累积+TTL+去重), 不再覆盖人工自选股
    from plans.stock_pool import add_entries as _pool_add, load_pool as _pool_load
    final_picks = breakthrough.get("final", [])
    pool_new = []
    for c in final_picks:
        sym = _bare(c.get("symbol", ""))
        if not sym:
            continue
        cons = c.get("concepts") or ([c.get("concept")] if c.get("concept") else [])
        pool_new.append({
            "symbol": sym,
            "name": c.get("name", sym),
            "concepts": cons,
            "reason": "热点突破精选(%s)" % regime,
        })
    if pool_new:
        _pool_add(pool_new, reason_default="热点突破精选(%s)" % regime)
        print(f"    策略精选 {len(pool_new)} 只已加入股票池(累积+TTL+去重)", flush=True)
    # 人工自选股(只读, 系统不写) → 补全名称/现价/形态分析
    try:
        _raw_wl = json.load(open(WATCHLIST_PATH, encoding="utf-8")) or []
    except Exception:
        _raw_wl = []
    human_watchlist = enrich_watchlist(_raw_wl, verbose=args is not None)
    if human_watchlist:
        print(f"    自选股 {len(human_watchlist)} 只已补全名称/现价/形态分析", flush=True)
    # 股票池(供报告展示) → 注入实时 当前价/当日涨幅
    pool_entries = _pool_load().get("entries", [])
    enrich_pool_entries(pool_entries, verbose=args is not None)

    print(f"    选入 {breakthrough.get('count', 0)} 只, "
          f"剔除前期大涨不追 {breakthrough.get('excluded_count', 0)} 只 "
          f"(前{args.runup_days}日>{args.runup_pct}%不追)", flush=True)

    # 3c. 大盘三状态判定 + 蒸馏策略路由 (实战核心: 不同行情用不同选股策略)
    regime, regime_diff = market_regime()
    rinfo = REGIME_STRATEGY.get(regime, REGIME_STRATEGY["未知"])
    final = apply_regime_strategy(breakthrough.get("candidates", []), regime, date_str)
    scale = rinfo["position_scale"]
    if scale and scale != 1.0:
        for c in final:
            try:
                c["position"] = max(1, round((c.get("position") or 5) * scale))
            except Exception:
                pass
    breakthrough["regime"] = regime
    breakthrough["regime_diff"] = regime_diff
    breakthrough["strategy_label"] = rinfo["label"]
    breakthrough["strategy_desc"] = rinfo["desc"]
    breakthrough["final"] = final
    breakthrough["final_count"] = len(final)
    print(f"    大盘状态: {regime} (MA20-MA60 {regime_diff:+.2f}%) → 策略[{rinfo['label']}] "
          f"精选 {len(final)} 只 (候选池 {breakthrough.get('count',0)} 只)", flush=True)

    # 4. 报告
    report = build_report(date_str, hotspots, human_watchlist, pool_entries, breakthrough, regime=regime)

    # 4a. 企微摘要卡片 (统一数据源: 定时推送与 bot 路径共用同一张卡片)
    card = build_wechat_card(date_str, hotspots, pool_entries, breakthrough, regime)
    # 用标记块输出到 stdout, 供 wecom_bot(--no-push 路径)提取 → 微信只推卡片, 不推全文报告
    print("<<<WECHAT_CARD_START>>>")
    print(card)
    print("<<<WECHAT_CARD_END>>>")

    # 4b. HTML 报告 (统一机制: core/html_renderer.render + HTML_REPORT:<path> 约定)
    if args.html:
        try:
            from core.html_renderer import render
            html_path = render(
                {
                    "date": date_str,
                    "hotspots": hotspots,
                    "human_watchlist": human_watchlist,
                    "stock_pool": pool_entries,
                    "breakthrough": breakthrough,
                },
                "weekly_hotspot_report",
                filename=f"weekly_hotspot_report_{date_str}.html",
            )
            print(f"HTML_REPORT:{html_path}")
        except Exception as e:
            print(f"[HTML] 报告生成失败: {e}", file=sys.stderr)

    # 报告落盘 (无论是否推送都保留交付物)
    report_path = os.path.join(DATA_DIR, f"weekly_hotspot_report_{date_str}.md")
    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception:
        report_path = None

    if args.json:
        out = {
            "date": date_str,
            "hotspots": hotspots,
            "human_watchlist": human_watchlist,
            "stock_pool": pool_entries,
            "breakthrough": breakthrough,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(report)

    if report_path:
        print(f"\n[REPORT] 报告已保存: {report_path}", flush=True)

    # 5. 推送微信 (仅经智能机器人 aibot 通道, 与用户沟通)
    #    企微只推格式化摘要卡片, 详细买卖计划留在 HTML 周报 / 落盘文件
    if not args.no_push:
        try:
            from notify.wecom_bot import push_markdown_via_bot
            ok = push_markdown_via_bot(card)
            if ok:
                print("\n[AIBOT] 已推送摘要卡片到企业微信智能机器人", flush=True)
            else:
                print("\n[AIBOT] 推送未成功 (详见上方错误), 报告已落盘。",
                      file=sys.stderr, flush=True)
        except Exception as e:
            print(f"\n[AIBOT] 推送异常: {e}", file=sys.stderr, flush=True)
            print("       报告已落盘, 配置好智能机器人后可重跑或手动发送。",
                  file=sys.stderr, flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
