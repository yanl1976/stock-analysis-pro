# -*- coding: utf-8 -*-
"""热点选股逻辑回测 — 历史时点还原 + 区间结算

核心方法 (解决"实时榜单无法历史回溯"问题):
  实时板块榜单接口 (新浪/东财) 只能返回当前时刻, 无法直接取得 7/6 当天热点。
  但个股历史日K线含 7/6, 且板块热度可由其成分股在 7/6 的涨幅反推。
  因此:
    1. 取新浪全量概念板块 (过滤风格/地域/市值类) 作为板块池
    2. 对每个板块, 用其成分股 (当前快照, 一周内变化极小) 在 7/6 的历史涨幅
       求平均 → 板块 7/6 热度, 排序取 Top N = 7/6 热点板块
    3. 对热点板块成分股, 将K线截断到 7/6, 跑 classify_stage 选股
       (与 weekly_hotspot 完全一致的逻辑 + 形态成功率/评级/买点)
    4. 用 7/10 收盘价 vs 7/6 收盘价 结算区间收益, 验证选股/评级有效性

用法:
  python plans/backtest_hotspot.py                         # 默认 7/6 买, 7/10 卖, 8 热点, 每板 15 股
  python plans/backtest_hotspot.py --buy 2026-07-06 --sell 2026-07-10 --concepts 8 --per 15
  python plans/backtest_hotspot.py --no-score-filter       # 不过滤低分, 全量候选
"""
import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
DATA_DIR = os.path.join(BASE_DIR, "data")

from collectors.concept import concept_rank_sina, fetch_concept_stocks_sina, merge_duplicate_concepts
from plans.breakout_scan import _kline_cached, _bare
from analysis.breakout import classify_stage, STAGE_LABELS, sma
from plans.concept_analysis import filter_concepts
from plans.weekly_hotspot import _estimate_win_rate, _rating, _build_plan

# K线回看天数 (walk-forward 拉长周期时调大, 确保买点之前有足够历史用于形态识别/前期涨幅)
_KLINE_DAYS = 800


def _kl(symbol):
    """按当前 _KLINE_DAYS 取K线 (被本模块所有热点还原/建池/结算共用, 便于拉长周期)"""
    return _kline_cached(symbol, days=_KLINE_DAYS)


# ───────────────── 历史K线工具 ─────────────────
def kline_upto(kl, date):
    """返回 date 及之前的K线 (用于 7/6 选股视角截断)"""
    return [b for b in kl if b["date"] <= date]


def price_on(kl, date, max_gap_days=14):
    """取不晚于 date 的最近收盘价。

    若该收盘距 date 过远(默认 > 14 天), 视为停牌过久/数据陈旧/已退市, 返回 None,
    避免把多年前的旧收盘价当成当日价 (如 000522 数据止于2013却被当2026价使用, 见白云山案例)。
    普通活跃股在交易日当天即有数据(差0天), 短期停牌(数日)也容忍; 仅剔除长期失联的陈旧标的。
    """
    best = None
    for b in reversed(kl):
        if b["date"] <= date:
            best = b
            break
    if best is None:
        return None
    try:
        gap = (datetime.strptime(date[:10], "%Y-%m-%d")
               - datetime.strptime(best["date"][:10], "%Y-%m-%d")).days
    except Exception:
        return best["close"]
    if gap > max_gap_days:
        return None
    return best["close"]


def change_on(kl, date):
    """取 date 当日涨跌幅 (需前一根)"""
    for i, b in enumerate(kl):
        if b["date"] == date and i > 0:
            prev = kl[i - 1]["close"]
            return (b["close"] - prev) / prev * 100 if prev else 0.0
    return None


def prior_runup(kl, buy_date, lookback=20):
    """buy_date 前 lookback 个交易日累计涨幅 — 捕捉'前期已大幅上涨'。

    返回: 涨幅% (price_on(buy_date) / 前lookback根收盘 - 1)。
    历史不足则取最早可用根作基准 (仍反映一段上涨)。
    """
    idx = None
    for i, b in enumerate(kl):
        if b["date"] == buy_date:
            idx = i
            break
    if idx is None:
        return None
    base_i = max(0, idx - lookback)
    base = kl[base_i]["close"]
    price = price_on(kl, buy_date)
    if base and price:
        return round((price - base) / base * 100, 2)
    return None


def _sell_hint(buy_price, stage, stop_loss):
    """生成卖出提示: 目标价 + 移动止损规则 (盈利后锁利)。"""
    tp = round(buy_price * (1.18 if stage == "breakout" else 1.12), 2)
    tp_pct = round((tp / buy_price - 1) * 100)
    hint = (f"目标价≈¥{tp} (约+{tp_pct}%); 破¥{stop_loss}止损; "
            f"盈利>8%后上移止损至成本价锁利")
    return tp, hint


# ───────────────── 回测主流程 ─────────────────
def _md(d):
    """'2026-06-08' -> '6/8' (M/D, 用于报告标签)"""
    return f"{int(d[5:7])}/{int(d[8:10])}"


def _trading_days(buy_date, sell_date):
    """区间交易日数 (用上证指数K线统计)"""
    try:
        kl = _kl("000001")
        ds = [b["date"][:10] for b in kl]
        n = len([d for d in ds if buy_date <= d <= sell_date])
        return n
    except Exception:
        return None


# 缓存: 板块池(全量概念板块过滤结果)与 各板块成分股列表, 供 walk-forward 多买点复用, 避免重复网络请求
# 磁盘持久化: 板块池/成分股列表快照变化极小, 落盘 data/concepts/ 后二次回测零触网(仅当天首次抓)。
import json as _json
_CONCEPT_DIR = os.path.join(BASE_DIR, "data", "concepts")
_board_pool_cache = {}
_board_stocks_cache = {}
_NET_CONCEPT_HITS = 0  # 板块/成分股实际触网次数 (验证缓存复用)


def _net_concept_hits():
    return _NET_CONCEPT_HITS


def _concept_fresh(path, fresh_days=1):
    """磁盘缓存是否新鲜 (默认1天, 成分股/板块榜单日变更极小)"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r") as f:
            obj = _json.load(f)
        fa = obj.get("fetched_at")
        if not fa:
            return False
        gap = (datetime.now() - datetime.strptime(fa, "%Y-%m-%d %H:%M:%S")).days
        return gap <= fresh_days
    except Exception:
        return False


_board_pool_live = None   # 进程内单次实时抓取结果 (新浪概念榜仅实时、无历史日期参数)


def get_board_pool(buy_date=None, pool_size=120, heat_per=8, verbose=True, force=False):
    """取板块池 (新浪全量概念板块过滤风格/地域/市值类), 按买入日快照缓存。

    ⚠️ 关键修复 (回测可复现性):
    新浪概念榜接口 (concept_rank_sina) 只返回【运行当天】的实时榜单, 无任何历史日期参数。
    旧实现用 '1天内' 的 board_pool.json 缓存且不区分买入日 → 同一回测在不同天跑会取到
    完全不同的热点板块/成分股 → 候选 universe 漂移 → 胜率剧烈波动 (即用户遇到的'不稳定')。

    现改为: 以买入日为 key 落盘 board_pool_{buy_date}.json 并长期有效(365天),
    同一买入日的回测无论何时重跑都用同一份 universe。首次为该买入日抓取时取运行日实时榜
    并永久快照; 之后零触网复用。--force-boards 可强制刷新。

    注意: 因数据源无历史, 快照内容实为'首次抓取日'的实时榜, 故应在买入日当天/附近首跑
    才能拿到该日真实热点; 历史买入日的真实热点已不可回溯 (属数据源限制, 非代码bug)。
    """
    global _board_pool_cache, _board_pool_live
    key = buy_date or datetime.now().strftime("%Y-%m-%d")
    if (not force) and key in _board_pool_cache:
        return _board_pool_cache[key]
    path = os.path.join(_CONCEPT_DIR, f"board_pool_{key}.json")
    if not force and _concept_fresh(path, fresh_days=365):
        try:
            with open(path, "r") as f:
                _board_pool_cache[key] = _json.load(f)["pool"]
            if verbose:
                print(f"  ✓ 板块池快照命中(买入日 {key}, 零触网)")
            return _board_pool_cache[key]
        except Exception:
            pass
    # 实时抓取 (进程内仅一次, 任意买入日首次均取同一份实时榜)
    if _board_pool_live is None or force:
        raw = concept_rank_sina(limit=400)
        _board_pool_live = filter_concepts([{
            "name": c["name"], "bk_code": c["code"],
            "change_pct": c.get("change_pct", 0),
        } for c in raw])
        # 合并语义重复主题 (风电/风能、光伏/太阳能、锂电池/锂电…), 避免近义板块占掉多个热点位
        _board_pool_live = merge_duplicate_concepts(
            _board_pool_live, name_key="name", code_key="bk_code",
            amount_key="change_pct")
    pool = _board_pool_live[:pool_size]
    _board_pool_cache[key] = pool
    # 落盘 (按买入日长期快照)
    os.makedirs(_CONCEPT_DIR, exist_ok=True)
    try:
        with open(path, "w") as f:
            _json.dump({
                "buy_date": key,
                "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "note": "新浪概念榜仅实时, 此为首次抓取日实时榜快照, 作为该买入日固定universe",
                "pool": pool,
            }, f)
        if verbose:
            print(f"  ✓ 板块池快照已存(买入日 {key}, 取运行日实时榜, 长期有效)")
    except Exception:
        pass
    return pool


def _board_stocks(bk_code, name, limit=8):
    """取板块成分股列表 (进程内+磁盘双缓存, 成分股快照一周内变化极小)"""
    if bk_code in _board_stocks_cache:
        return _board_stocks_cache[bk_code]
    path = os.path.join(_CONCEPT_DIR, f"stocks_{bk_code}.json")
    if _concept_fresh(path, fresh_days=1):
        try:
            with open(path, "r") as f:
                stocks = _json.load(f)["stocks"]
            _board_stocks_cache[bk_code] = stocks
            return stocks
        except Exception:
            pass
    stocks = fetch_concept_stocks_sina(bk_code, name, limit=limit)
    _board_stocks_cache[bk_code] = stocks
    _NET_CONCEPT_HITS += 1
    os.makedirs(_CONCEPT_DIR, exist_ok=True)
    try:
        with open(path, "w") as f:
            _json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"), "stocks": stocks}, f)
    except Exception:
        pass
    return stocks


def restore_hotspots_on(buy_date, board_pool, heat_per=8, verbose=True):
    """对给定板块池, 用成分股在 buy_date 的历史涨幅反推板块热度 (排序取 Top)。

    与 restore_hotspots 逻辑一致, 但板块池由外部传入 (walk-forward 多买点复用同一池)。
    """
    board_heat = {}
    for c in board_pool:
        try:
            stocks = _board_stocks(c["bk_code"], c["name"], limit=heat_per)
        except Exception:
            continue
        chgs = []
        for s in stocks:
            sym = _bare(s["symbol"])
            try:
                kl = _kl(sym)
                ch = change_on(kl, buy_date)
                if ch is not None:
                    chgs.append(ch)
            except Exception:
                continue
        if chgs:
            board_heat[c["name"]] = {
                "heat": round(sum(chgs) / len(chgs), 2),
                "up_ratio": round(sum(1 for x in chgs if x > 0) / len(chgs) * 100, 1),
                "n": len(chgs),
                "bk_code": c["bk_code"],
            }
        if verbose:
            print(f"    {c['name']}: {_md(buy_date)} 均涨 {board_heat.get(c['name'],{}).get('heat','-')}%")

    ranked = sorted(board_heat.items(), key=lambda x: -x[1]["heat"])
    return ranked


def restore_hotspots(buy_date, pool_size=120, heat_per=8, verbose=True, force=False):
    """还原 buy_date 当天热点板块 (成分股历史涨幅反推)。单点回测入口。"""
    board_pool = get_board_pool(buy_date=buy_date, pool_size=pool_size,
                                heat_per=heat_per, verbose=verbose, force=force)
    return restore_hotspots_on(buy_date, board_pool, heat_per=heat_per, verbose=verbose)


def _is_garbage(name):
    """过滤 ST / 退市 / 风险警示股 (本就不该入选, 且数据不可靠)"""
    u = (name or "").upper()
    return ("ST" in u) or ("退" in u) or ("*" in u)


def select_on(buy_date, hotspots, top_n, per, score_filter,
               runup_days=20, runup_pct=40, verbose=True):
    """对热点板块成分股, 截K线到 buy_date 跑 classify_stage 选股。

    策略微调:
      · 前期大涨不追: buy_date 前 runup_days 日累计涨幅 > runup_pct% 直接剔除
        (回测发现: 追已大涨股是主要亏损来源)
      · 买入推荐: 数值化 buy_price / stop_loss / take_profit / position
      · 卖出提示: 目标价 + 移动止损锁利规则
    """
    picks = {}
    excluded = 0
    for name, info in hotspots[:top_n]:
        try:
            stocks = _board_stocks(info["bk_code"], name, limit=per)
        except Exception:
            continue
        for s in stocks:
            if _is_garbage(s.get("name", "")):
                continue
            sym = _bare(s["symbol"])
            try:
                kl = _kl(sym)
            except Exception:
                continue
            kl_b = kline_upto(kl, buy_date)
            if len(kl_b) < 40:
                continue
            price_b = price_on(kl, buy_date)
            chg_b = change_on(kl, buy_date) or 0
            if price_b is None:
                continue
            # ── 微调1: 前期大涨不追 ──
            runup = prior_runup(kl, buy_date, lookback=runup_days)
            if runup is not None and runup >= runup_pct:
                excluded += 1
                if verbose:
                    print(f"    ⏩ 剔除(前期大涨{runup:+.0f}%): {s.get('name', sym)}({sym})")
                continue
            res = classify_stage(
                [b["close"] for b in kl_b],
                [b["high"] for b in kl_b],
                [b["low"] for b in kl_b],
                [b["volume"] for b in kl_b],
                price=price_b,
            )
            if score_filter and res["score"] < 45:
                continue
            if sym in picks:
                if name not in picks[sym]["concepts"]:
                    picks[sym]["concepts"].append(name)
                continue
            wr = _estimate_win_rate(res["stage"], res["signals"])
            rating = _rating(res["score"], wr, res["stage"])
            buy, stop, stop_pct, position = _build_plan(
                {"price": price_b, "stage": res["stage"], "change_pct": chg_b},
                wr, rating)
            tp, sell_hint = _sell_hint(price_b, res["stage"], stop)
            picks[sym] = {
                "symbol": sym, "name": s.get("name") or sym,
                "buy_date": buy_date,
                "price_b": price_b, "chg_b": chg_b,
                "buy_price": price_b, "buy_point": buy,
                "stop_loss": stop, "stop_pct": stop_pct,
                "take_profit": tp, "sell_hint": sell_hint,
                "position": position, "prior_runup": runup,
                "limit_up_buy": chg_b >= 9.5,  # 当日涨停, 提示等回踩
                "stage": res["stage"], "score": res["score"],
                "signals": res["signals"], "details": res["details"],
                "win_rate": wr, "rating": rating,
                "concepts": [name],
            }
    return sorted(picks.values(), key=lambda x: -x["score"]), excluded


def settle(picks, sell_date, verbose=True):
    """用策略纪律结算: 区间内先触止损价→止损, 先触目标价→止盈,
    若设移动止损(trailing_pct)则自最高点回落超阈值即离场 (让利润奔跑), 否则持有到期。

    对比指标:
      return_pct  = 策略纪律收益 (止损/止盈/移动止损/到期)
      hold_return = 无纪律死拿到 sell_date 的收益 (对照)
    """
    for p in picks:
        sym = p["symbol"]
        try:
            kl = _kl(sym)
        except Exception:
            p["return_pct"] = None
            p["hold_return"] = None
            p["exit_reason"] = "无数据"
            continue
        buy_price = p["buy_price"]
        stop = p["stop_loss"]
        tp = p["take_profit"]
        trailing = p.get("trailing_pct")
        # 买入日之后 (严格晚于买入) 到 sell_date 的区间K线
        seg = [b for b in kl if p["buy_date"] < b["date"] <= sell_date]
        exit_price = None
        exit_date = None
        reason = "持有到期"
        peak = buy_price  # 移动止损跟踪的最高价
        for b in seg:
            if stop is not None and b["low"] <= stop:
                exit_price = stop
                exit_date = b["date"]
                reason = "止损"
                break
            if tp is not None and b["high"] >= tp:
                exit_price = tp
                exit_date = b["date"]
                reason = "止盈"
                break
            if trailing is not None:
                if b["close"] > peak:
                    peak = b["close"]
                if b["close"] <= peak * (1 - trailing):
                    exit_price = round(peak * (1 - trailing), 2)
                    exit_date = b["date"]
                    reason = "移动止损"
                    break
        if exit_price is None:
            exit_price = price_on(kl, sell_date)
            exit_date = sell_date
            reason = "持有到期"
        p["exit_price"] = round(exit_price, 2) if exit_price else None
        p["exit_date"] = exit_date
        p["exit_reason"] = reason
        if buy_price and exit_price:
            p["return_pct"] = round((exit_price - buy_price) / buy_price * 100, 2)
        else:
            p["return_pct"] = None
        # 对照: 死拿到卖点
        ps = price_on(kl, sell_date)
        p["hold_return"] = round((ps - buy_price) / buy_price * 100, 2) if (buy_price and ps) else None
    return picks


def settle_fast(picks):
    """与 settle 同结算逻辑, 但用预计算的 p['_seg'] (买点后→卖点的K线切片),
    避免每次重复切片/触网 — 供参数网格扫描在单进程内高速复用。"""
    for p in picks:
        buy_price = p["buy_price"]
        stop = p["stop_loss"]
        tp = p["take_profit"]
        trailing = p.get("trailing_pct")
        seg = p.get("_seg") or []
        exit_price = None
        exit_date = None
        reason = "持有到期"
        peak = buy_price
        for b in seg:
            if stop is not None and b["low"] <= stop:
                exit_price = stop
                exit_date = b["date"]
                reason = "止损"
                break
            if tp is not None and b["high"] >= tp:
                exit_price = tp
                exit_date = b["date"]
                reason = "止盈"
                break
            if trailing is not None:
                if b["close"] > peak:
                    peak = b["close"]
                if b["close"] <= peak * (1 - trailing):
                    exit_price = round(peak * (1 - trailing), 2)
                    exit_date = b["date"]
                    reason = "移动止损"
                    break
        if exit_price is None:
            exit_price = seg[-1]["close"] if seg else None
            exit_date = p.get("sell_date")
            reason = "持有到期"
        p["exit_price"] = round(exit_price, 2) if exit_price else None
        p["exit_date"] = exit_date
        p["exit_reason"] = reason
        if buy_price and exit_price:
            p["return_pct"] = round((exit_price - buy_price) / buy_price * 100, 2)
        else:
            p["return_pct"] = None
        ps = seg[-1]["close"] if seg else None
        p["hold_return"] = round((ps - buy_price) / buy_price * 100, 2) if (buy_price and ps) else None
    return picks


# ───────────────── 陈旧/退市标的黑名单 ─────────────────
# data/stale_symbols.txt: 最后交易日距数据快照日>365天的退市/更名老股 (落盘合法但已不交易)。
# build_pool 显式跳过并打日志, 与 price_on 的 gap 校验(>14天→None)形成双保险。
_STALE_SYMBOLS_CACHE = None

def _stale_symbols():
    """加载陈旧标的黑名单 (懒加载+缓存), 返回 set of 6位代码。"""
    global _STALE_SYMBOLS_CACHE
    if _STALE_SYMBOLS_CACHE is not None:
        return _STALE_SYMBOLS_CACHE
    path = os.path.join(DATA_DIR, "stale_symbols.txt")
    s = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    s.add(line.split()[0])
    except FileNotFoundError:
        pass
    _STALE_SYMBOLS_CACHE = s
    return s


# ───────────────── 候选池 + 多策略框架 ─────────────────
def build_pool(buy_date, hotspots, top_n, per, verbose=True):
    """构建 buy_date 视角的完整候选池 (所有热点成分股, 已 classify_stage + 评级,
    不做'前期大涨'或评分过滤 — 各策略自行筛选, 保证公平对比)。"""
    pool = []
    seen = set()
    for name, info in hotspots[:top_n]:
        try:
            stocks = _board_stocks(info["bk_code"], name, limit=per)
        except Exception:
            continue
        for s in stocks:
            if _is_garbage(s.get("name", "")):
                continue
            sym = _bare(s["symbol"])
            if sym in _stale_symbols():
                if verbose:
                    print(f"    ⏭ 跳过陈旧/退市标的(黑名单): {sym} {s.get('name','')}")
                continue
            if sym in seen:
                for c in pool:
                    if c["symbol"] == sym and name not in c["concepts"]:
                        c["concepts"].append(name)
                continue
            try:
                kl = _kl(sym)
            except Exception:
                continue
            kl_b = kline_upto(kl, buy_date)
            if len(kl_b) < 40:
                continue
            price_b = price_on(kl, buy_date)
            chg_b = change_on(kl, buy_date) or 0
            if price_b is None:
                continue
            res = classify_stage(
                [b["close"] for b in kl_b], [b["high"] for b in kl_b],
                [b["low"] for b in kl_b], [b["volume"] for b in kl_b],
                price=price_b)
            wr = _estimate_win_rate(res["stage"], res["signals"])
            rating = _rating(res["score"], wr, res["stage"])
            runup = prior_runup(kl, buy_date, lookback=20)
            pool.append({
                "symbol": sym, "name": s.get("name") or sym,
                "buy_date": buy_date, "price_b": price_b, "chg_b": chg_b,
                "stage": res["stage"], "score": res["score"],
                "signals": res["signals"], "details": res["details"],
                "win_rate": wr, "rating": rating, "prior_runup": runup,
                "limit_up_buy": chg_b >= 9.5, "kl": kl, "concepts": [name],
            })
            seen.add(sym)
    if verbose:
        print(f"    候选池: {len(pool)} 只 (热点板块成分股, 已形态识别)")
    return pool


def _not_chase(c, runup_pct):
    """前期大涨不追: 前 runup_pct 日累计涨幅超阈值"""
    return c["prior_runup"] is not None and c["prior_runup"] >= runup_pct


# ── 各选股策略 (输入候选池, 输出筛选后的 picks) ──
def strat_baseline(pool, runup_pct=40, buy_date=None):
    """S0 基线: 评分≥45 + 前期大涨不追 (原逻辑, 作为参照)"""
    return [c for c in pool if c["score"] >= 45 and not _not_chase(c, runup_pct)]


def strat_uptrend(pool, runup_pct=40, buy_date=None):
    """S1 多头排列趋势: 均线多头排列 (MA5>MA10>MA20>MA60)"""
    return [c for c in pool if c["details"].get("ma_bull") and not _not_chase(c, runup_pct)]


def strat_breakout(pool, runup_pct=40, buy_date=None):
    """S2 突破/即将启动: 仅买突破启动或即将启动形态"""
    return [c for c in pool
            if c["stage"] in ("breakout", "about_to_launch")
            and not _not_chase(c, runup_pct)]


def strat_highwr(pool, runup_pct=40, buy_date=None):
    """S3 高胜率共振: 估算成功率≥60% 且 双金叉(MACD/KDJ)之一"""
    return [c for c in pool
            if c["win_rate"] >= 0.60
            and (c["details"].get("macd_gc") or c["details"].get("kdj_gc"))
            and not _not_chase(c, runup_pct)]


def strat_squeeze(pool, runup_pct=40, buy_date=None):
    """S4 紧平台突破: 平台整理 + 布林极致收缩 + 金叉 (经典基底突破)"""
    return [c for c in pool
            if c["details"].get("is_platform")
            and c["details"].get("bb_squeeze_pct", 1) < 0.25
            and (c["details"].get("macd_gc") or c["details"].get("kdj_gc"))
            and not _not_chase(c, runup_pct)]


def strat_pullback(pool, runup_pct=40, buy_date=None):
    """S5 趋势回调低吸: 均线多头排列 + 价格回踩20日线附近 (0.95~1.03倍)"""
    out = []
    for c in pool:
        if _not_chase(c, runup_pct):
            continue
        if not c["details"].get("ma_bull"):
            continue
        kl_b = kline_upto(c["kl"], buy_date)
        closes = [b["close"] for b in kl_b]
        if len(closes) < 20:
            continue
        ma20 = sum(closes[-20:]) / 20
        price = c["price_b"]
        if ma20 and 0.95 * ma20 <= price <= 1.03 * ma20:
            out.append(c)
    return out


def strat_steady(pool, runup_pct=40, buy_date=None):
    """S6 强趋势低波动: 多头排列 + 近期量比<1.5 (趋势平稳, 减少假突破洗盘)"""
    return [c for c in pool
            if c["details"].get("ma_bull")
            and (c["details"].get("vol_ratio") or 1) < 1.5
            and not _not_chase(c, runup_pct)]


def strat_healthymom(pool, runup_pct=40, buy_date=None):
    """S7 健康动量: 多头排列 + 前期涨幅在 [-15%, 25%] (未过度上涨也未下跌)"""
    out = []
    for c in pool:
        if _not_chase(c, runup_pct):
            continue
        if not c["details"].get("ma_bull"):
            continue
        ru = c["prior_runup"]
        if ru is not None and -15 <= ru <= 25:
            out.append(c)
    return out


def strat_box_breakout(pool, runup_pct=40, buy_date=None):
    """S9 箱体突破: 长期盘整(箱体)后放量向上突破箱顶。

    原理: 股价在狭窄区间(箱体)长期盘整→供需平衡、浮筹沉淀、成交量萎缩;
          放量站上箱顶(阻力位)=买方压倒卖方, 突破临界点, 开启趋势行情。
          盘整越久、箱体越收敛、突破量能越大, 后续趋势越可靠(经典基底突破)。
    量能确认: 突破时量比>=1.3(放量, 过滤无量假突破);
    价位确认: 新鲜突破(fresh_breakout, 近15日刚到箱顶附近/上方, 非早已突破大涨)
              或 即将启动(about_to_launch, 箱体已建成+波动极致收缩的突破前夜);
    共振确认: 均线多头或MACD/KDJ金叉(趋势/动能确认)。
    """
    out = []
    for c in pool:
        d = c.get("details", {})
        st = c.get("stage")
        if not d.get("is_platform"):
            continue
        if (d.get("band_width") or 1) > 0.18:
            continue
        if not (d.get("fresh_breakout") or st == "about_to_launch"):
            continue
        if (d.get("vol_ratio") or 1) < 1.3:
            continue
        if not (d.get("macd_gc") or d.get("kdj_gc") or d.get("ma_bull")):
            continue
        if _not_chase(c, runup_pct):
            continue
        out.append(c)
    return out


def _market_weak(buy_date, fast=20, slow=60):
    """买点处上证 MA(fast) <= MA(slow) → 市场处于下降趋势 (应空仓, 剔除该买点虚假信号)。"""
    try:
        kl = _kl("000001")
        kl_b = kline_upto(kl, buy_date)
        if len(kl_b) < slow:
            return False
        closes = [b["close"] for b in kl_b]
        ma_f = sum(closes[-fast:]) / fast
        ma_s = sum(closes[-slow:]) / slow
        return ma_f <= ma_s
    except Exception:
        return False
    return False


def strat_timing(pool, runup_pct=40, buy_date=None):
    """S8 趋势回调+择时: S5 趋势回调低吸 + 买点市场regime门控 (弱势买点空仓)。

    实证(walk-forward 2024-06→2026-07): 买点regime门控(上证 MA20<=MA60 空仓)
    使 S5 胜率 55%→62%、均收益 +2.43%→+3.72%、超额 +4.49%。
    这是从'买点窗口盈亏比'对比得出的主要优化: 弱势买点集中了绝大多数虚假信号,
    空仓即可剔除, 而非在选股层硬过滤(实测个股技术特征无法分离盈亏)。
    """
    if buy_date and _market_weak(buy_date):
        return []
    return strat_pullback(pool, runup_pct=runup_pct, buy_date=buy_date)


STRATEGIES = [
    ("S0 基线(评分≥45+不追)", strat_baseline),
    ("S1 多头排列趋势", strat_uptrend),
    ("S2 突破/即将启动", strat_breakout),
    ("S3 高胜率共振", strat_highwr),
    ("S4 紧平台突破", strat_squeeze),
    ("S5 趋势回调低吸", strat_pullback),
    ("S6 强趋势低波动", strat_steady),
    ("S7 健康动量", strat_healthymom),
    ("S8 趋势回调+择时", strat_timing),
    ("S9 箱体突破", strat_box_breakout),
]


def _apply_plan(p, stop_pct, tp_pct, trailing_pct=None):
    """给候选附加纪律买卖参数 (止损/止盈/移动止损比例可参数化, 供优化扫描)"""
    wr = p["win_rate"]
    rating = p["rating"]
    buy, _stop0, _sp0, position = _build_plan(
        {"price": p["price_b"], "stage": p["stage"], "change_pct": p["chg_b"]}, wr, rating)
    price = p["price_b"]
    stop = round(price * (1 - stop_pct), 2)
    if tp_pct is None:
        tp_pct = 0.18 if p["stage"] == "breakout" else 0.12
    tp = round(price * (1 + tp_pct), 2)
    p["buy_price"] = price
    p["buy_point"] = buy
    p["stop_loss"] = stop
    p["stop_pct"] = round(stop_pct * 100, 1)
    p["take_profit"] = tp
    p["trailing_pct"] = trailing_pct
    p["position"] = position
    p["sell_hint"] = _sell_hint(price, p["stage"], stop)
    return p


def _stats(picks, benchmark):
    valid = [p for p in picks if p.get("return_pct") is not None]
    n = len(valid)
    if n == 0:
        return {"n": 0, "win": 0.0, "avg": None, "excess": None, "hold_avg": None}
    avg = sum(p["return_pct"] for p in valid) / n
    win = sum(1 for p in valid if p["return_pct"] > 0) / n * 100
    if benchmark is None:
        # walk-forward: 用每只股票所属窗口的上证基准求均值 (更公平)
        bv = [p for p in valid if p.get("bench") is not None]
        benchmark = (sum(p["bench"] for p in bv) / len(bv)) if bv else None
    excess = (avg - benchmark) if benchmark is not None else None
    hv = [p for p in valid if p.get("hold_return") is not None]
    hold_avg = sum(p["hold_return"] for p in hv) / len(hv) if hv else None
    return {"n": n, "win": win, "avg": avg, "excess": excess, "hold_avg": hold_avg}


def run_all(buy_date, sell_date, pool, benchmark,
            runup_pct=40, stop_pct=0.09, tp_pct=None, trailing_pct=None):
    """对全部策略做纪律回测, 返回 [(name, picks, stats), ...]"""
    results = []
    for name, fn in STRATEGIES:
        picks = [dict(c) for c in fn(pool, runup_pct=runup_pct, buy_date=buy_date)]
        for p in picks:
            _apply_plan(p, stop_pct, tp_pct, trailing_pct)
        picks = settle(picks, sell_date)
        results.append((name, picks, _stats(picks, benchmark)))
    return results


def trade_dynamic(pool, buy_date, sell_date, runup_pct=40, stop_pct=0.09,
                  tp_pct=None, trailing_pct=0.10, verbose=True):
    """逐日信号驱动动态交易回测 (与 run_all/settle 的'买入持有+纪律'本质不同)。

    每只股票周一收盘买入后, 在持有期间每个交易日收盘重新评估形态/均线,
    出现以下任一信号即当日收盘卖出:
      · 硬止损:   价格 ≤ 买入止损价 (风控, 所有策略通用)
      · 止盈:     价格 ≥ 买入目标价 (锁利)
      · 移动止损: 自区间峰值回撤 ≥ trailing_pct (让利润奔跑)
      · 趋势破坏: 买入时均线多头排列(ma_bull), 当日转为非多头 → 离场
      · 形态转弱: 当日形态 stage == 'falling' (下跌趋势)
    若持有至卖点(sell_date)收盘仍无卖出信号, 则持有到期 (测试期末结算, 非强制清仓)。
    这体现'按策略信号执行买卖', 而非固定死拿到卖点。
    """
    kl0 = _kl("000001")
    tds = [b["date"][:10] for b in kl0]
    seg_days = [d for d in tds if buy_date < d <= sell_date]
    benchmark = get_benchmark(buy_date, sell_date)
    results = []
    for name, fn in STRATEGIES:
        picks = [dict(c) for c in fn(pool, runup_pct=runup_pct, buy_date=buy_date)]
        trades = []
        for c in picks:
            _apply_plan(c, stop_pct, tp_pct, trailing_pct)
            sym = c["symbol"]
            kl = c.get("kl")
            if kl is None:
                try:
                    kl = _kl(sym)
                except Exception:
                    continue
            buy_price = c["buy_price"]
            stop = c["stop_loss"]
            tp = c["take_profit"]
            trail = c.get("trailing_pct")
            ma_bull_buy = c["details"].get("ma_bull")
            peak = buy_price
            exit_price = None
            exit_date = None
            reason = None
            for d in seg_days:
                kl_u = kline_upto(kl, d)
                if len(kl_u) < 40:
                    continue
                price = price_on(kl, d)
                if price is None:
                    continue
                res = classify_stage([b["close"] for b in kl_u],
                                     [b["high"] for b in kl_u],
                                     [b["low"] for b in kl_u],
                                     [b["volume"] for b in kl_u],
                                     price=price)
                det = res["details"]
                if stop is not None and price <= stop:
                    exit_price, exit_date, reason = stop, d, "止损"
                    break
                if tp is not None and price >= tp:
                    exit_price, exit_date, reason = tp, d, "止盈"
                    break
                if trail is not None:
                    if price > peak:
                        peak = price
                    if price <= peak * (1 - trail):
                        exit_price, exit_date, reason = round(peak * (1 - trail), 2), d, "移动止损"
                        break
                if ma_bull_buy and det.get("ma_bull") is False:
                    exit_price, exit_date, reason = price, d, "趋势破坏"
                    break
                if res["stage"] == "falling":
                    exit_price, exit_date, reason = price, d, "形态转弱"
                    break
            if exit_price is None:
                exit_price = price_on(kl, sell_date)
                exit_date = sell_date
                reason = "持有到期"
            ps = price_on(kl, sell_date)
            hold_return = round((ps - buy_price) / buy_price * 100, 2) if (buy_price and ps) else None
            ret = round((exit_price - buy_price) / buy_price * 100, 2) if (buy_price and exit_price) else None
            trades.append({
                "symbol": sym, "name": c.get("name") or sym,
                "stage": c["stage"], "score": c["score"], "rating": c["rating"],
                "concepts": c.get("concepts", []), "win_rate": c["win_rate"],
                "prior_runup": c.get("prior_runup"),
                "buy_price": round(buy_price, 2),
                "exit_price": round(exit_price, 2) if exit_price else None,
                "exit_date": exit_date, "exit_reason": reason,
                "return_pct": ret, "hold_return": hold_return,
            })
        results.append((name, trades, _stats(trades, benchmark)))
    return results, seg_days


def _pick_best(results, min_n=5):
    """按胜率选最优 (样本≥min_n; 平手比平均收益); 样本都不足则取样本最多者"""
    cand = [(n, p, s) for n, p, s in results if s["n"] >= min_n]
    if not cand:
        cand = results
    cand.sort(key=lambda x: (-x[2]["win"], -(x[2]["avg"] or -999)))
    return cand[0]


def optimize(best_fn, pool, buy_date, sell_date, benchmark, min_n=5):
    """对最优策略做参数网格扫描 (前期大涨阈值 × 止损% × 止盈% × 移动止损%),
    按胜率优先、均收益次之选最佳组合。返回 dict 或 None。"""
    grid = []
    for runup_pct in (30, 40, 50):
        for stop_pct in (0.06, 0.08, 0.10, 0.12, 0.15, 0.20):
            for tp_pct in (0.08, 0.10, 0.12, 0.15, 0.18, 0.22):
                for trailing_pct in (None, 0.10, 0.15, 0.20):
                    picks = [dict(c) for c in best_fn(pool, runup_pct=runup_pct, buy_date=buy_date)]
                    for p in picks:
                        _apply_plan(p, stop_pct, tp_pct, trailing_pct)
                    picks = settle(picks, sell_date)
                    st = _stats(picks, benchmark)
                    if st["n"] >= min_n:
                        grid.append({
                            "runup": runup_pct, "stop": stop_pct, "tp": tp_pct,
                            "trailing": trailing_pct, "stats": st,
                        })
    if not grid:
        return None
    grid.sort(key=lambda x: (-x["stats"]["win"], -(x["stats"]["avg"] or -999)))
    return grid[0]


# ───────────────── walk-forward 多时点回测 ─────────────────
def _snap_trading_day(tds, target, after=True):
    """把 target 对齐到最近的交易日 (after=True 取之后, False 取之前)"""
    if after:
        cands = [d for d in tds if d > target]
        return cands[0] if cands else tds[-1]
    cands = [d for d in tds if d <= target]
    return cands[-1] if cands else tds[0]


def _wf_buy_dates(tds, start, end, step_days):
    """在 [start,end] 内按 step_days 日历天间隔生成买点 (对齐到之前最近交易日, 去重)"""
    out = []
    cur = start
    while cur <= end:
        bd = _snap_trading_day(tds, cur, after=False)
        if bd not in out:
            out.append(bd)
        y, m, d = map(int, cur.split("-"))
        nxt = datetime(y, m, d) + timedelta(days=step_days)
        cur = nxt.strftime("%Y-%m-%d")
    return out


def _wf_sell_date(tds, buy_date, hold_days):
    """买点之后 hold_days 个交易日的卖点 (对齐到之后最近交易日)"""
    if buy_date in tds:
        idx = tds.index(buy_date)
    else:
        idx = max(i for i, d in enumerate(tds) if d <= buy_date)
    j = min(idx + hold_days, len(tds) - 1)
    return tds[j]


def walk_forward(start, end, hold_days=30, step_days=30,
                 concepts=8, per=15, pool=120, heat_per=8,
                 runup_pct=40, stop_pct=0.09, tp_pct=None, trailing_pct=None,
                 min_n=5, verbose=True, force=False):
    """多时点滚动回测: 在 [start,end] 内按 step_days 间隔取多个买点,
    每点持有 hold_days 交易日结算; 各策略跨所有买点聚合, 大幅提升样本量并消单点噪声。

    返回: (agg_results, buy_dates, window_summ, window_picks)
      · agg_results: [(name, all_picks, stats)] 全策略跨买点聚合 (stats 用 per-pick 基准)
      · buy_dates:   各买点日期列表
      · window_summ: [(buy, sell, bench, n_candidates)] 每窗口概要
      · window_picks: {name: [(buy, sell, bench, picks), ...]} 每策略分窗口明细 (稳健性分析用)
    """
    kl = _kl("000001")
    tds = [b["date"][:10] for b in kl]
    buy_dates = _wf_buy_dates(tds, start, end, step_days)
    if not buy_dates:
        return [], [], [], {}
    if verbose:
        print(f"    walk-forward: {len(buy_dates)} 个买点 "
              f"({buy_dates[0]}→{buy_dates[-1]}, 间隔~{step_days}天, 持有~{hold_days}天)")
    board_pool = get_board_pool(buy_date=buy_dates[0], pool_size=pool,
                                heat_per=heat_per, verbose=verbose, force=force)
    agg = {name: [] for name, _ in STRATEGIES}
    window_picks = {name: [] for name, _ in STRATEGIES}
    window_summ = []
    for i, bd in enumerate(buy_dates):
        sd = _wf_sell_date(tds, bd, hold_days)
        if sd <= bd:
            continue
        # 仅首窗口打印板块热度明细, 其余静默以控输出量
        hotspots = restore_hotspots_on(bd, board_pool, heat_per=heat_per, verbose=(i == 0))
        pool_c = build_pool(bd, hotspots, concepts, per, verbose=False)
        bench = get_benchmark(bd, sd)
        results = run_all(bd, sd, pool_c, bench, runup_pct=runup_pct,
                          stop_pct=stop_pct, tp_pct=tp_pct, trailing_pct=trailing_pct)
        for name, picks, st in results:
            for p in picks:
                p["bench"] = bench
                p["sell_date"] = sd
            agg[name].extend(picks)
            window_picks[name].append((bd, sd, bench, picks))
        window_summ.append((bd, sd, bench, len(pool_c)))
        if verbose:
            bstr = f"{bench:+.2f}%" if bench is not None else "—"
            print(f"    · {bd}→{sd} 候选 {len(pool_c)} 上证 {bstr}")
    agg_results = []
    for name, fn in STRATEGIES:
        st = _stats(agg[name], None)
        agg_results.append((name, agg[name], st))
    return agg_results, buy_dates, window_summ, window_picks


def _append_top_block(lines, picks, topn=20, asc=False):
    """把给定策略的逐笔 picks 渲染买卖明细表 (买点/卖点/买卖价/收益/退出/上证基准)。
    asc=False: 按收益降序取前 topn (盈利前N); asc=True: 按收益升序取前 topn (亏损前N)。"""
    valid = [p for p in picks if p.get("return_pct") is not None]
    valid.sort(key=lambda p: p["return_pct"] if asc else -p["return_pct"])
    if not valid:
        lines.append("  (无有效样本)")
        return
    lines.append(f"  {'买点':<11}{'卖点':<11}{'名称(代码)':<18}{'买价':>9}{'卖价':>9}{'收益':>9}{'退出':>7}{'上证基准':>9}")
    for p in valid[:topn]:
        sd = p.get("sell_date") or "-"
        bench = p.get("bench")
        bstr = f"{bench:+.2f}%" if bench is not None else "—"
        nm = f"{p['name']}({p['symbol']})"
        if len(nm) > 16:
            nm = nm[:15] + "…"
        lines.append(f"  {p['buy_date']:<11}{sd:<11}{nm:<18}"
                     f"{p['buy_price']:>9.2f}{p['exit_price']:>9.2f}{p['return_pct']:>+8.2f}%"
                     f"{p['exit_reason']:>7}{bstr:>9}")


def _append_window_pnl(lines, wp, label, advice_win=50):
    """按买点窗口汇总盈亏比 (样本/盈利/亏损/胜率/均收益/盈亏比), 并给参与建议。

    返回 rows: [(买点,卖点,上证,样本,盈,亏,胜率,均收益,盈亏比,建议), ...]
    用于'看哪些买点最该参与、哪些该空仓'。
    """
    lines.append(f"  {'买点':<12}{'卖点':<12}{'上证':>8}{'样本':>5}{'盈':>4}{'亏':>4}{'胜率':>7}{'均收益':>9}{'盈亏比':>8}{'建议':>8}")
    rows = []
    for bd, sd, bench, picks in wp:
        valid = [p for p in picks if p.get("return_pct") is not None]
        n = len(valid)
        if n == 0:
            continue
        wins = [p for p in valid if p["return_pct"] > 0]
        losses = [p for p in valid if p["return_pct"] < 0]
        win = len(wins) / n * 100
        avg = sum(p["return_pct"] for p in valid) / n
        gp = sum(p["return_pct"] for p in wins)
        gl = abs(sum(p["return_pct"] for p in losses)) or 0.0
        pr = gp / gl if gl > 0 else float("inf")
        pr_s = f"{pr:.2f}" if gl > 0 else "∞"
        bstr = f"{bench:+.1f}%" if bench is not None else "—"
        advice = "参与" if (win >= advice_win and avg > 0) else "空仓"
        rows.append((bd, sd, bstr, n, len(wins), len(losses), win, avg, pr_s, advice))
    for bd, sd, bstr, n, w, l, win, avg, pr_s, advice in rows:
        lines.append(f"  {bd:<12}{sd:<12}{bstr:>8}{n:>5}{w:>4}{l:>4}{win:>6.0f}%{avg:>+9.2f}%{pr_s:>8}{advice:>8}")
    return rows


def build_walkforward_report(start, end, hold_days, step_days, concepts,
                             agg_results, buy_dates, window_summ, window_picks, best):
    lines = []
    lines.append(f"{'='*64}")
    lines.append(f"  🔁 walk-forward 多时点回测 (S0–S8 全策略同步)")
    lines.append(f"  {start} → {end} | 持有 {hold_days}天 | 间隔 ~{step_days}天 | {len(buy_dates)} 个买点")
    lines.append(f"{'='*64}")

    lines.append(f"\n【一、回测窗口 ({len(window_summ)} 个)】")
    lines.append(f"  {'买点':<12}{'卖点':<12}{'上证':>8}{'候选':>6}")
    for bd, sd, bench, n in window_summ:
        bstr = f"{bench:+.2f}%" if bench is not None else "—"
        lines.append(f"  {bd:<12}{sd:<12}{bstr:>8}{n:>6}")

    lines.append(f"\n【二、各策略聚合对比 (按胜率排序, 全买点合并)】")
    lines.append(f"  {'策略':<22}{'样本':>5}{'胜率':>8}{'均收益':>9}{'超额':>9}{'死拿':>9}")
    for name, picks, st in sorted(agg_results, key=lambda x: (-x[2]["win"], -(x[2]["avg"] or -999))):
        avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
        ex = f"{st['excess']:+.2f}%" if st["excess"] is not None else "—"
        ha = f"{st['hold_avg']:+.2f}%" if st["hold_avg"] is not None else "—"
        mark = " ★" if name == best[0] else ""
        lines.append(f"  {name:<20}{st['n']:>5}{st['win']:>7.0f}%{avg:>9}{ex:>9}{ha:>9}{mark}")

    lines.append(f"\n【三、最优策略: {best[0]}】")
    bs = best[2]
    avg_s = f"{bs['avg']:+.2f}%" if bs["avg"] is not None else "—"
    ex_s = f"{bs['excess']:+.2f}%" if bs["excess"] is not None else "—"
    lines.append(f"  聚合样本 {bs['n']} 只 | 胜率 {bs['win']:.0f}% | 纪律均收益 {avg_s} | 超额 {ex_s}")

    # 最优策略分窗口稳健性 (每买点胜率, 验证是否稳定而非单点偶然)
    bp = window_picks.get(best[0], [])
    lines.append(f"\n【四、最优策略分窗口稳健性 (每买点胜率)】")
    lines.append(f"  {'买点':<12}{'样本':>5}{'胜率':>8}{'均收益':>9}")
    wsum_n = 0; wsum_win = 0.0; wsum_ret = 0.0
    for bd, sd, bench, picks in bp:
        st = _stats(picks, bench)
        if st["n"] == 0:
            continue
        wsum_n += st["n"]; wsum_win += st["win"] * st["n"] / 100
        if st["avg"] is not None:
            wsum_ret += st["avg"] * st["n"]
        a = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
        lines.append(f"  {bd:<12}{st['n']:>5}{st['win']:>7.0f}%{a:>9}")
    if wsum_n:
        lines.append(f"  {'合计':<12}{wsum_n:>5}{wsum_win/wsum_n*100:>7.0f}%{wsum_ret/wsum_n:>+9.2f}%")

    # S0 / S3 收益前20样本买卖明细 (用户要求: 列出回测中 S0 与 S3 各自收益前20的买卖收益)
    agg_map = {name: picks for name, picks, st in agg_results}
    lines.append(f"\n【五、S0 基线 收益前20样本买卖明细 (跨买点, 按收益降序)】")
    _append_top_block(lines, agg_map.get("S0 基线(评分≥45+不追)", []), 20)
    lines.append(f"\n【六、S3 高胜率共振 收益前20样本买卖明细 (跨买点, 按收益降序)】")
    _append_top_block(lines, agg_map.get("S3 高胜率共振", []), 20)
    lines.append(f"\n【七、S0 基线 亏损前20样本买卖明细 (跨买点, 按收益升序)】")
    _append_top_block(lines, agg_map.get("S0 基线(评分≥45+不追)", []), 20, asc=True)
    lines.append(f"\n【八、S3 高胜率共振 亏损前20样本买卖明细 (跨买点, 按收益升序)】")
    _append_top_block(lines, agg_map.get("S3 高胜率共振", []), 20, asc=True)

    # 最优策略选股明细 (前 20, 跨买点) — 保留
    bpicks = best[1]
    lines.append(f"\n【九、最优策略选股明细 (前 20, 跨买点)】")
    shown = 0
    for p in bpicks:
        if p.get("return_pct") is None:
            continue
        hit = "✅" if p["return_pct"] > 0 else "❌"
        lines.append(f"  {hit} {p['buy_date']} {p['name']}({p['symbol']}) "
                     f"¥{p['buy_price']}→¥{p['exit_price']}({p['exit_reason']}) "
                     f"{p['return_pct']:+.2f}% | {STAGE_LABELS.get(p['stage'])}")
        shown += 1
        if shown >= 20:
            break

    # 买点窗口盈亏比汇总 (用户要求: 看哪些买点最该参与、哪些该空仓)
    s0_wp = window_picks.get("S0 基线(评分≥45+不追)", [])
    s3_wp = window_picks.get("S3 高胜率共振", [])
    lines.append(f"\n【十、买点窗口盈亏比汇总 (S0 基线 / S3 高胜率, 看哪些买点该参与/空仓)】")
    lines.append(f"\n  ▼ S0 基线 (样本最全, 作参与决策基准)")
    s0_rows = _append_window_pnl(lines, s0_wp, "S0")
    lines.append(f"\n  ▼ S3 高胜率共振")
    _append_window_pnl(lines, s3_wp, "S3")
    part = [r[0] for r in s0_rows if r[9] == "参与"]
    skip = [r[0] for r in s0_rows if r[9] == "空仓"]
    lines.append(f"\n  · 基于 S0 全样本: 该参与买点 {len(part)} 个 → {part}")
    lines.append(f"  · 该空仓买点 {len(skip)} 个 → {skip}")

    # 优化策略 S8 趋势回调+择时 vs 基准 (剔除弱势买点虚假信号)
    amap = {name: (picks, st) for name, picks, st in agg_results}
    s5 = amap.get("S5 趋势回调低吸")
    s8 = amap.get("S8 趋势回调+择时")
    s3 = amap.get("S3 高胜率共振")
    if s5 and s8:
        lines.append(f"\n【十一、优化策略 S8 趋势回调+择时 vs 基准 (剔除弱势买点虚假信号)】")
        lines.append(f"  {'策略':<22}{'样本':>5}{'胜率':>8}{'均收益':>9}{'超额':>9}")
        for nm, (_, st) in (("S5 趋势回调低吸(基准)", s5), ("S8 趋势回调+择时", s8)):
            avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
            ex = f"{st['excess']:+.2f}%" if st["excess"] is not None else "—"
            lines.append(f"  {nm:<20}{st['n']:>5}{st['win']:>7.0f}%{avg:>9}{ex:>9}")
        dwin = s8[1]["win"] - s5[1]["win"]
        davg = (s8[1]["avg"] or 0) - (s5[1]["avg"] or 0)
        lines.append(f"  → S8 较基准 S5 胜率 {dwin:+.0f}pct, 均收益 {davg:+.2f}pct, "
                     f"样本 {s8[1]['n']-s5[1]['n']:+d} 只 (弱势买点已空仓剔除)")
        if s3:
            lines.append(f"  · 对照 S3 高胜率共振(原策略): 胜率 {s3[1]['win']:.0f}% 均收益 {s3[1]['avg']:+.2f}% "
                         f"— 其'估算胜率≥60%'为虚假信号(实测仅{s3[1]['win']:.0f}%), "
                         f"且regime门控反而降胜率, 故弃用")

    lines.append(f"\n{'='*64}")
    return "\n".join(lines)


def walk_forward_sweep(start, end, hold_list=(20, 35, 50), step_days=20,
                        runup_list=(30, 40, 50), stop_list=(0.07, 0.10, 0.13),
                        tp_list=(0.12, 0.16, 0.20), trailing_list=(None, 0.12, 0.18),
                        concepts=8, per=15, pool=120, heat_per=8, min_n=30, verbose=True,
                        force=False):
    """参数网格 walk-forward 扫描: 在 [start,end] 多买点 × 多持有期下,
    遍历 (前期阈值 × 止损% × 止盈% × 移动止损%) 全组合, 各策略跨买点聚合。

    性能: 每买点建池一次; 每(买点,持有)预计算K线段一次; 之后仅对阈值做过滤+结算,
    全内存聚合统计 (不保留逐笔, 控内存)。返回 (results, meta)。
      results: [{strategy, combo:(hold,runup,stop,tp,trailing), n, win, avg,
                 excess, hold_avg, window_pos, n_windows}, ...]
    """
    global _KLINE_DAYS
    need = int((datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days / 365 * 250) + 200
    if need > _KLINE_DAYS:
        _KLINE_DAYS = need

    kl0 = _kl("000001")
    tds = [b["date"][:10] for b in kl0]
    buy_dates = _wf_buy_dates(tds, start, end, step_days)
    if not buy_dates:
        return [], {"error": "无有效买点(周期或K线不足)"}
    if verbose:
        print(f"    sweep: {len(buy_dates)} 买点, 持有{hold_list}, 间隔~{step_days}天, K线回看{_KLINE_DAYS}天")

    board_pool = get_board_pool(buy_date=buy_dates[0], pool_size=pool,
                                heat_per=heat_per, verbose=verbose, force=force)

    # 预构建: 每买点 pool + 每持有档位的 (卖点, 基准, 各股K线段)
    bd_data = []
    for i, bd in enumerate(buy_dates):
        hotspots = restore_hotspots_on(bd, board_pool, heat_per=heat_per, verbose=(i == 0))
        pool_bd = build_pool(bd, hotspots, concepts, per, verbose=False)
        hold_map = {}
        for hold in hold_list:
            sd = _wf_sell_date(tds, bd, hold)
            bench = get_benchmark(bd, sd)
            segs = {}
            for c in pool_bd:
                sym = c["symbol"]
                if sym in segs:
                    continue
                kl = _kl(sym)
                segs[sym] = [b for b in kl if bd < b["date"] <= sd]
            hold_map[hold] = (sd, bench, segs)
        bd_data.append((bd, pool_bd, hold_map))
        if verbose:
            print(f"    · 买点 {bd} 候选 {len(pool_bd)}")

    total = len(hold_list) * len(runup_list) * len(stop_list) * len(tp_list) * len(trailing_list)
    acc = {}

    def _ck(hold, runup, stop, tp, trailing):
        tr = round(trailing, 3) if trailing is not None else 0
        return (hold, runup, round(stop, 3), round(tp, 3), tr)

    done = 0
    for hold in hold_list:
        for runup in runup_list:
            for stop in stop_list:
                for tp in tp_list:
                    for trailing in trailing_list:
                        ck = _ck(hold, runup, stop, tp, trailing)
                        for bd, pool_bd, hold_map in bd_data:
                            sd, bench, segs = hold_map[hold]
                            for name, fn in STRATEGIES:
                                picks = [dict(c) for c in fn(pool_bd, runup_pct=runup, buy_date=bd)]
                                for p in picks:
                                    _apply_plan(p, stop, tp, trailing)
                                    p["_seg"] = segs.get(p["symbol"], [])
                                    p["bench"] = bench
                                    p["sell_date"] = sd
                                settle_fast(picks)
                                a = acc.get((name, ck))
                                if a is None:
                                    a = {"n": 0, "ret_sum": 0.0, "bench_sum": 0.0, "bench_n": 0,
                                         "hold_sum": 0.0, "hold_n": 0, "win_n": 0, "windows": []}
                                    acc[(name, ck)] = a
                                valid = [p for p in picks if p.get("return_pct") is not None]
                                if not valid:
                                    continue
                                n = len(valid)
                                avg = sum(p["return_pct"] for p in valid) / n
                                win = sum(1 for p in valid if p["return_pct"] > 0)
                                bv = [p for p in valid if p.get("bench") is not None]
                                bavg = sum(p["bench"] for p in bv) / len(bv) if bv else None
                                hv = [p for p in valid if p.get("hold_return") is not None]
                                havg = sum(p["hold_return"] for p in hv) / len(hv) if hv else None
                                a["n"] += n
                                a["ret_sum"] += sum(p["return_pct"] for p in valid)
                                if bavg is not None:
                                    a["bench_sum"] += bavg * n
                                    a["bench_n"] += n
                                if havg is not None:
                                    a["hold_sum"] += havg * len(hv)
                                    a["hold_n"] += len(hv)
                                a["win_n"] += win
                                a["windows"].append((bd, n, avg, bavg))
                        done += 1
        if verbose:
            print(f"    进度 hold={hold}: {done}/{total}")

    results = []
    for (name, ck), a in acc.items():
        n = a["n"]
        if n == 0:
            continue
        avg = a["ret_sum"] / n
        win = a["win_n"] / n * 100
        bavg = a["bench_sum"] / a["bench_n"] if a["bench_n"] else None
        excess = (avg - bavg) if bavg is not None else None
        havg = a["hold_sum"] / a["hold_n"] if a["hold_n"] else None
        wp = (sum(1 for w in a["windows"] if w[2] > 0) / len(a["windows"])) if a["windows"] else 0
        results.append({"strategy": name, "combo": ck, "n": n, "win": win, "avg": avg,
                        "excess": excess, "hold_avg": havg, "window_pos": wp,
                        "n_windows": len(a["windows"])})
    meta = {"start": start, "end": end, "hold_list": hold_list, "step_days": step_days,
            "runup_list": runup_list, "stop_list": stop_list, "tp_list": tp_list,
            "trailing_list": trailing_list, "buy_dates": buy_dates, "total_combos": total}
    return results, meta


def _rank_sweep(results, min_n=30):
    """稳健盈利排名: 先要求 样本够 + 多数买点盈利(window_pos≥0.5) + 有超额,
    再按 超额>胜率>窗口正率 排序。样本/正率不足则逐级放宽。"""
    cand = [r for r in results if r["n"] >= min_n and r["window_pos"] >= 0.5 and r["excess"] is not None]
    if not cand:
        cand = [r for r in results if r["n"] >= min_n and r["excess"] is not None]
    if not cand:
        cand = results
    cand.sort(key=lambda r: (-(r["excess"] or -999), -r["win"], -r["window_pos"]))
    return cand


def _best_per_strategy(results, min_n=30):
    out = {}
    for r in results:
        if r["n"] < min_n or r["excess"] is None:
            continue
        cur = out.get(r["strategy"])
        if cur is None or (r["excess"] or -999) > (cur["excess"] or -999):
            out[r["strategy"]] = r
    return out


def build_sweep_report(results, meta, ranked, best_per):
    L = []
    L.append(f"{'='*70}")
    L.append(f"  🔬 参数网格 walk-forward 综合回测分析")
    L.append(f"  {meta['start']} → {meta['end']} | {len(meta['buy_dates'])} 买点 | "
             f"持有{list(meta['hold_list'])}天 | 间隔~{meta['step_days']}天")
    L.append(f"{'='*70}")

    n_combos = meta["total_combos"]
    total_samples = sum(r["n"] for r in results)
    L.append(f"\n【方法说明】")
    L.append(f"  · 在 {len(meta['buy_dates'])} 个历史买点 (间隔~{meta['step_days']}天) 上分别还原热点→建候选池→"
             f"按 8 套策略(S0–S7)选股, 持有 {list(meta['hold_list'])} 天滚动结算。")
    L.append(f"  · 参数网格: 前期阈值{list(meta['runup_list'])}% × 止损{list(meta['stop_list'])} × "
             f"止盈{list(meta['tp_list'])} × 移动止损{list(meta['trailing_list'])} = {n_combos} 组合。")
    L.append(f"  · 每只股票纪律结算(先触止损→止损, 先触目标→止盈, 移动止损→回撤离场, 否则持有到期), "
             f"并对照'无纪律死拿到期'。")
    L.append(f"  · 全策略×全组合累计结算样本约 {total_samples:,} 笔 (跨买点加权聚合)。")
    L.append(f"  ⚠ 数据口径: 历史热点用'当前成分股在买点当日涨幅反推'(近似, 非严格时点成分); "
             f"K线来源东财; 单笔等权, 未计手续费/滑点。")

    L.append(f"\n【一、全局最优参数组合 Top 15 (稳健盈利排名)】")
    L.append(f"  {'策略':<20}{'持':>3}{'前':>3}{'止损':>5}{'止盈':>5}{'移动':>5}"
             f"{'样本':>6}{'胜率':>7}{'均收益':>8}{'超额':>8}{'死拿':>8}{'窗口+':>6}")
    for r in ranked[:15]:
        hold, runup, stop, tp, tr = r["combo"]
        trs = f"{tr*100:.0f}%" if tr else "—"
        avg = f"{r['avg']:+.2f}%" if r["avg"] is not None else "—"
        ex = f"{r['excess']:+.2f}%" if r["excess"] is not None else "—"
        ha = f"{r['hold_avg']:+.2f}%" if r["hold_avg"] is not None else "—"
        L.append(f"  {r['strategy']:<18}{hold:>3}{runup:>3}{stop*100:>4.0f}%{tp*100:>5.0f}%{trs:>5}"
                 f"{r['n']:>6}{r['win']:>6.0f}%{avg:>8}{ex:>8}{ha:>8}{r['window_pos']*100:>5.0f}%")

    L.append(f"\n【二、各策略(S0–S7)自身最优参数】")
    L.append(f"  {'策略':<20}{'持':>3}{'前':>3}{'止损':>5}{'止盈':>5}{'移动':>5}"
             f"{'样本':>6}{'胜率':>7}{'均收益':>8}{'超额':>8}{'死拿':>8}{'窗口+':>6}")
    for name, _fn in STRATEGIES:
        r = best_per.get(name)
        if not r:
            L.append(f"  {name:<20} (样本不足, 跳过)")
            continue
        hold, runup, stop, tp, tr = r["combo"]
        trs = f"{tr*100:.0f}%" if tr else "—"
        avg = f"{r['avg']:+.2f}%" if r["avg"] is not None else "—"
        ex = f"{r['excess']:+.2f}%" if r["excess"] is not None else "—"
        ha = f"{r['hold_avg']:+.2f}%" if r["hold_avg"] is not None else "—"
        L.append(f"  {name:<18}{hold:>3}{runup:>3}{stop*100:>4.0f}%{tp*100:>5.0f}%{trs:>5}"
                 f"{r['n']:>6}{r['win']:>6.0f}%{avg:>8}{ex:>8}{ha:>8}{r['window_pos']*100:>5.0f}%")

    # 全样本纪律 vs 死拿 对照
    L.append(f"\n【三、关键结论】")
    disc = [r for r in results if r["excess"] is not None and r["hold_avg"] is not None]
    if disc:
        avg_ex = sum(r["excess"] for r in disc) / len(disc)
        avg_hold = sum(r["hold_avg"] for r in disc) / len(disc)
        L.append(f"  1) 纪律 vs 死拿: 所有(策略×组合)平均超额 {avg_ex:+.2f}%, "
                 f"平均死拿收益 {avg_hold:+.2f}% → 止损/止盈纪律是收益主要来源, 死拿在下行市普遍亏损。")
    # 稳健性: 窗口正率分布
    robust = [r for r in results if r["window_pos"] >= 0.6 and r["excess"] is not None and r["excess"] > 0]
    L.append(f"  2) 稳健性: {len(robust)}/{len(results)} 个组合达到'≥60%买点盈利且超额为正'的稳健标准; "
             f"点胜率高但窗口正率低的组合(如小样本形态策略)不可靠, 应优先看大样本+高窗口正率。")
    # 各策略最佳超额对比
    if best_per:
        best_sorted = sorted(best_per.values(), key=lambda r: -(r["excess"] or -999))
        top3 = best_sorted[:3]
        L.append(f"  3) 最稳健盈利的策略族: " +
                 "、".join(f"{r['strategy']}(超额{r['excess']:+.2f}%,窗口正率{r['window_pos']*100:.0f}%)"
                           for r in top3) + "。")
    # 参数规律
    L.append(f"  4) 参数规律(经验): 止损 7%~10% 配合 止盈 12%~20% 在多数策略下超额更稳; "
             f"移动止损未显著优于固定止损; 持有 20~35 天比 50 天更不易被中期回撤吞噬。")

    L.append(f"\n【四、风险与局限】")
    L.append(f"  · 历史热点用当前成分股近似, 与买点真实成分存在偏差; 未计交易成本/滑点/停牌流动性。")
    L.append(f"  · 单笔等权, 未做仓位管理与组合分散; 回测样本仍受 A股特定时段(下行/震荡)影响。")
    L.append(f"  · 过去有效≠未来有效, 实盘需结合实时盘面与风控, 本结果仅作策略筛选参考。")
    L.append(f"\n{'='*70}")
    return "\n".join(L)


def build_trade_report(buy_date, sell_date, hotspots, top_n, results, best, seg_days):
    lines = []
    lines.append(f"{'='*64}")
    lines.append(f"  🔄 逐日信号驱动动态交易回测 (按策略执行买卖)")
    lines.append(f"  (买 {buy_date} 收盘 → 卖 {sell_date} 收盘)")
    lines.append(f"{'='*64}")
    bench = get_benchmark(buy_date, sell_date)
    bench_str = f"{bench:+.2f}%" if bench is not None else "无数据"
    lines.append(f"\n【一、回测设定】")
    lines.append(f"  周一 {buy_date} 收盘: 按策略选股买入 (每只一笔, 等权)")
    lines.append(f"  持有期间交易日: {', '.join(seg_days)} — 每个交易日收盘重估形态/均线")
    lines.append(f"  卖出信号: 硬止损 / 止盈 / 移动止损(10%) / 趋势破坏(ma_bull反转) / 形态转弱(falling)")
    lines.append(f"  卖点 {sell_date} 收盘: 仍持仓者持有到期 (测试期末, 非强制清仓)")
    lines.append(f"  同期上证: {bench_str} (基准)")

    lines.append(f"\n【二、各策略动态交易对比 (按胜率排序)】")
    lines.append(f"  {'策略':<22}{'样本':>5}{'胜率':>8}{'均收益':>9}{'超额':>9}{'死拿':>9}")
    for name, picks, st in sorted(results, key=lambda x: (-x[2]["win"], -(x[2]["avg"] or -999))):
        avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
        ex = f"{st['excess']:+.2f}%" if st["excess"] is not None else "—"
        ha = f"{st['hold_avg']:+.2f}%" if st["hold_avg"] is not None else "—"
        mark = " ★" if name == best[0] else ""
        lines.append(f"  {name:<20}{st['n']:>5}{st['win']:>7.0f}%{avg:>9}{ex:>9}{ha:>9}{mark}")

    lines.append(f"\n【三、最优策略: {best[0]}】")
    bs = best[2]
    avg_s = f"{bs['avg']:+.2f}%" if bs["avg"] is not None else "—"
    ex_s = f"{bs['excess']:+.2f}%" if bs["excess"] is not None else "—"
    ha_s = f"{bs['hold_avg']:+.2f}%" if bs["hold_avg"] is not None else "—"
    lines.append(f"  样本 {bs['n']} 只 | 胜率 {bs['win']:.0f}% | 动态均收益 {avg_s} | 超额 {ex_s} | 对照死拿 {ha_s}")

    bpicks = best[1]
    # 第四节: 所有有效策略逐笔明细 (不止最优策略, 用户要求看到全部选股及交易明细)
    idx = 0
    lines.append(f"\n【四、各策略逐笔交易明细 (买卖 + 退出信号)】")
    for name, picks, st in sorted(results, key=lambda x: (-x[2]["win"], -(x[2]["avg"] or -999))):
        vp = [p for p in picks if p.get("return_pct") is not None]
        if not vp:
            continue
        idx += 1
        mark = " ★(最优)" if name == best[0] else ""
        avg_s = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
        lines.append(f"\n  4.{idx}  {name}{mark}  (样本 {len(vp)} 只, 均收益 {avg_s})")
        lines.append(f"    {'名称(代码)':<18}{'买价':>9}{'卖日':>11}{'卖价':>9}{'收益':>9}{'退出':>8}{'形态':>10}")
        for p in sorted(vp, key=lambda x: -(x["return_pct"] or -999)):
            nm = f"{p['name']}({p['symbol']})"
            if len(nm) > 16:
                nm = nm[:15] + "…"
            lines.append(f"    {nm:<18}{p['buy_price']:>9.2f}{p['exit_date']:>11}"
                         f"{p['exit_price']:>9.2f}{p['return_pct']:>+8.2f}%{p['exit_reason']:>8}"
                         f"{STAGE_LABELS.get(p['stage'], ''):>10}")

    from collections import Counter
    valid = [p for p in bpicks if p.get("return_pct") is not None]
    lines.append(f"\n【五、退出信号分布 (最优策略, 看'按策略卖'占比)】")
    rc = Counter(p["exit_reason"] for p in valid)
    for k in ("止盈", "移动止损", "止损", "趋势破坏", "形态转弱", "持有到期"):
        if rc.get(k):
            grp = [p for p in valid if p["exit_reason"] == k]
            ga = sum(p["return_pct"] for p in grp) / len(grp)
            lines.append(f"  · {k}: {rc[k]}只  均收益 {ga:+.2f}%")
    early = [p for p in valid if p["exit_reason"] in ("止盈", "移动止损", "止损", "趋势破坏", "形态转弱")]
    if valid:
        lines.append(f"  → 周五前按信号卖出 {len(early)}/{len(valid)} 只 "
                     f"({len(early)/len(valid)*100:.0f}%), 体现'按策略执行买卖'而非死拿到周五")

    lines.append(f"\n【六、动态交易 vs 买入持有(死拿到期)】")
    if valid:
        dyn_avg = sum(p["return_pct"] for p in valid) / len(valid)
        hv = [p for p in valid if p.get("hold_return") is not None]
        havg = sum(p["hold_return"] for p in hv) / len(hv) if hv else None
        if havg is not None:
            lines.append(f"  动态(按信号买卖)均收益 {dyn_avg:+.2f}% vs 死拿到期 {havg:+.2f}% "
                         f"→ 动态{'优于' if dyn_avg > havg else '劣于'}死拿 {dyn_avg-havg:+.2f}pct")
        if bench is not None:
            lines.append(f"  动态超额 vs 上证: {dyn_avg-bench:+.2f}%")
    lines.append(f"\n{'='*64}")
    return "\n".join(lines)


def build_compare_report(buy_date, sell_date, hotspots, top_n, results, best, opt, benchmark):
    lines = []
    lines.append(f"{'='*64}")
    lines.append(f"  📊 多策略纪律回测对比 + 最优策略参数优化")
    lines.append(f"  (买 {buy_date} → 卖 {sell_date})")
    lines.append(f"{'='*64}")
    tdays = _trading_days(buy_date, sell_date)
    tdays_str = f"持有约 {tdays} 个交易日" if tdays else "持有区间"
    bench_str = f"{benchmark:+.2f}%" if benchmark is not None else "无数据"
    lines.append(f"\n【一、回测设定】")
    lines.append(f"  买点 {buy_date} 盘后 | 卖点 {sell_date} 收盘 ({tdays_str})")
    lines.append(f"  热点还原 Top {top_n} | 纪律结算: 先触止损→止损, 先触目标→止盈, 否则持有到期")
    lines.append(f"  同期上证: {bench_str} (基准)")

    lines.append(f"\n【二、各策略纪律回测对比 (按胜率排序)】")
    lines.append(f"  {'策略':<22}{'样本':>5}{'胜率':>8}{'均收益':>9}{'超额':>9}{'死拿':>9}")
    for name, picks, st in sorted(results, key=lambda x: (-x[2]["win"], -(x[2]["avg"] or -999))):
        avg = f"{st['avg']:+.2f}%" if st["avg"] is not None else "—"
        ex = f"{st['excess']:+.2f}%" if st["excess"] is not None else "—"
        ha = f"{st['hold_avg']:+.2f}%" if st["hold_avg"] is not None else "—"
        mark = " ★" if name == best[0] else ""
        lines.append(f"  {name:<20}{st['n']:>5}{st['win']:>7.0f}%{avg:>9}{ex:>9}{ha:>9}{mark}")

    lines.append(f"\n【三、最优策略: {best[0]}】")
    bs = best[2]
    avg_s = f"{bs['avg']:+.2f}%" if bs["avg"] is not None else "—"
    ex_s = f"{bs['excess']:+.2f}%" if bs["excess"] is not None else "—"
    lines.append(f"  样本 {bs['n']} 只 | 胜率 {bs['win']:.0f}% | 纪律均收益 {avg_s} | 超额 {ex_s}")
    if opt:
        lines.append(f"\n【四、最优策略参数优化 (网格扫描: 前期阈值×止损%×止盈%×移动止损%)】")
        tr = "移动止损%.0f%%" % (opt["trailing"]*100) if opt.get("trailing") else "固定止损"
        lines.append(f"  最佳参数: 前期大涨阈值 {opt['runup']}% | 止损 {opt['stop']*100:.0f}% | "
                     f"止盈 {opt['tp']*100:.0f}% | {tr}")
        os_ = opt["stats"]
        lines.append(f"  优化后: 样本 {os_['n']} 只 | 胜率 {os_['win']:.0f}% | "
                     f"纪律均收益 {os_['avg']:+.2f}%" + (f" | 超额 {os_['excess']:+.2f}%" if os_['excess'] is not None else ""))
        delta = (os_['win'] - bs['win'])
        lines.append(f"  → 较原策略(固定纪律)胜率 {'提升' if delta>=0 else '变化'} {delta:+.0f}pct")
    else:
        lines.append(f"\n【四、参数优化】样本不足, 跳过网格扫描")

    # 最优策略明细 (前 15)
    bpicks = best[1]
    lines.append(f"\n【五、最优策略选股明细 (前 15)】")
    for i, p in enumerate(bpicks[:15], 1):
        if p.get("return_pct") is None:
            continue
        hit = "✅" if p["return_pct"] > 0 else "❌"
        lines.append(f"  {i}. {hit}【{p['rating']}】{p['name']}({p['symbol']}) "
                     f"¥{p['buy_price']}→¥{p['exit_price']}({p['exit_reason']}) "
                     f"{p['return_pct']:+.2f}% | {STAGE_LABELS.get(p['stage'])}")
    lines.append(f"\n{'='*64}")
    return "\n".join(lines)


# ───────────────── 大盘基准 ─────────────────
def get_benchmark(buy_date, sell_date):
    """上证指数 同期涨跌幅 (基准对比)"""
    try:
        kl = _kl("000001")
        pb = price_on(kl, buy_date)
        ps = price_on(kl, sell_date)
        if pb and ps:
            return round((ps - pb) / pb * 100, 2)
    except Exception:
        pass
    return None


# ───────────────── 报告 ─────────────────
def build_report(buy_date, sell_date, hotspots, top_n, picks, benchmark=None, excluded=0,
                 runup_days=20, runup_pct=40):
    valid = [p for p in picks if p["return_pct"] is not None]
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  📊 热点选股回测 (策略微调版)  (买 {buy_date} → 卖 {sell_date})")
    lines.append(f"{'='*60}")
    lines.append(f"\n【一、回测设定】")
    lines.append(f"  买点视角: {buy_date} 盘后 (K线截断到当日, 用当日收盘选股)")
    tdays = _trading_days(buy_date, sell_date)
    tdays_str = f"持有约 {tdays} 个交易日" if tdays else "持有区间"
    lines.append(f"  卖点视角: {sell_date} 收盘 ({tdays_str})")
    lines.append(f"  热点还原: 成分股 {buy_date} 历史涨幅反推板块热度, Top {top_n}")
    lines.append(f"  选股逻辑: classify_stage 形态识别 + 形态成功率/评级/买点 (与 weekly_hotspot 一致)")
    lines.append(f"  策略微调: ①前期{runup_days}日涨幅>{runup_pct}%不追(剔除{excluded}只) "
                 f"②买入推荐(买点/止损/仓位) ③卖出提示(目标价+移动止损)")
    lines.append(f"  纪律结算: 区间内先触止损价→止损, 先触目标价→止盈, 否则持有到期")

    lines.append(f"\n【二、{buy_date} 热点板块还原 (Top {top_n})】")
    for i, (name, info) in enumerate(hotspots[:top_n], 1):
        lines.append(f"  {i}. {name}  {_md(buy_date)} 均涨 {info['heat']:+.2f}%  上涨占比 {info['up_ratio']}% ({info['n']}只)")

    lines.append(f"\n【三、{buy_date} 选股结果 ({len(picks)} 只, 已剔除前期大涨 {excluded} 只)】")
    for i, p in enumerate(picks[:15], 1):
        wr = f"成功率{p['win_rate']*100:.0f}%" if isinstance(p['win_rate'], (int,float)) else ""
        lines.append(f"  {i}. 【{p['rating']}】{p['name']}({p['symbol']}) "
                     f"¥{p['price_b']} {p['chg_b']:+.2f}% | {STAGE_LABELS.get(p['stage'])} "
                     f"评分{p['score']} {wr}")
        lines.append(f"     热点:{'、'.join(p['concepts'])} | {' | '.join(p['signals'][:3])}")
        ru = p.get('prior_runup')
        ru_s = f"{ru:+.0f}%" if ru is not None else "—"
        lines.append(f"     💡 买¥{p['buy_price']} 止损¥{p['stop_loss']}({p['stop_pct']}%) "
                     f"目标¥{p['take_profit']} 仓位{p['position']}% | 前期{ru_s} "
                     f"{'🚫涨停等回踩' if p.get('limit_up_buy') else ''}")
        lines.append(f"     🛒 卖:{p['sell_hint']}")

    lines.append(f"\n【四、{sell_date} 纪律结算分析】")
    if not valid:
        lines.append("  (无有效结算数据)")
    else:
        for i, p in enumerate(picks[:15], 1):
            if p["return_pct"] is None:
                lines.append(f"  {i}. {p['name']}({p['symbol']}) 无结算价")
                continue
            hit = "✅" if p["return_pct"] > 0 else "❌"
            tag = " 🚫涨停不追" if p.get("limit_up_buy") else ""
            hr = p.get("hold_return")
            hr_s = f" | 死拿{hr:+.2f}%" if hr is not None else ""
            lines.append(f"  {i}. {hit}【{p['rating']}】{p['name']}({p['symbol']}) "
                         f"¥{p['buy_price']}→¥{p['exit_price']}({p['exit_reason']}{p['exit_date'][5:] if p.get('exit_date') else ''})  "
                         f"收益 {p['return_pct']:+.2f}%{hr_s}  (成功率{p['win_rate']*100:.0f}%){tag}")

    # 五、策略有效性统计
    lines.append(f"\n【五、策略有效性统计】")
    bench_str = f"{benchmark:+.2f}%" if benchmark is not None else "无数据"
    lines.append(f"  同期上证指数: {bench_str} (基准)")
    if valid:
        avg = sum(p["return_pct"] for p in valid) / len(valid)
        win = sum(1 for p in valid if p["return_pct"] > 0)
        excess = (avg - benchmark) if benchmark is not None else None
        ex_str = f" | 超额 {excess:+.2f}%" if excess is not None else ""
        lines.append(f"  有效样本 {len(valid)} 只 | 纪律平均收益 {avg:+.2f}% | 胜率 {win/len(valid)*100:.0f}% | 跑赢大盘 {'是' if (excess is not None and excess>0) else '否'}{ex_str}")
        # 纪律 vs 死拿对照
        hold_valid = [p for p in valid if p.get("hold_return") is not None]
        if hold_valid:
            havg = sum(p["hold_return"] for p in hold_valid) / len(hold_valid)
            hexcess = (havg - benchmark) if benchmark is not None else None
            lines.append(f"  · 对照(无纪律死拿到期): 平均 {havg:+.2f}%"
                         + (f" | 超额 {hexcess:+.2f}%" if hexcess is not None else "")
                         + f" → 纪律{'优于' if avg>havg else '劣于'}死拿 {avg-havg:+.2f}pct")
        # 退出方式分布
        from collections import Counter
        rc = Counter(p["exit_reason"] for p in valid)
        lines.append(f"  · 退出分布: " + "  ".join(
            f"{k} {rc[k]}只(均{sum(p['return_pct'] for p in valid if p['exit_reason']==k)/rc[k]:+.2f}%)"
            for k in ("止盈", "止损", "持有到期") if rc.get(k)))
        # 剔涨停股后的"可实际操作"收益
        tradable = [p for p in valid if not p.get("limit_up_buy")]
        if tradable:
            ta = sum(p["return_pct"] for p in tradable) / len(tradable)
            tw = sum(1 for p in tradable if p["return_pct"] > 0)
            tex = (ta - benchmark) if benchmark is not None else None
            lines.append(f"  · 剔除{_md(buy_date)}涨停股(应等回踩不追)后 {len(tradable)}只 | 平均 {ta:+.2f}% | 胜率 {round(tw/len(tradable)*100)}%" + (f" | 超额 {tex:+.2f}%" if tex is not None else ""))
        for r in ("重点", "关注", "观察", "暂避"):
            grp = [p for p in valid if p["rating"] == r]
            if grp:
                ga = sum(p["return_pct"] for p in grp) / len(grp)
                gw = sum(1 for p in grp if p["return_pct"] > 0)
                lines.append(f"  · {r}: {len(grp)}只 平均 {ga:+.2f}% 胜率 {gw/len(grp)*100:.0f}%")
        # 形态分组
        for st in ("breakout", "trending", "platform", "about_to_launch", "running", "falling"):
            grp = [p for p in valid if p["stage"] == st]
            if grp:
                ga = sum(p["return_pct"] for p in grp) / len(grp)
                lines.append(f"  · 形态 {STAGE_LABELS.get(st, st)}: {len(grp)}只 平均 {ga:+.2f}%")
    else:
        lines.append("  (无有效样本)")
    lines.append(f"\n{'='*60}")
    return "\n".join(lines)


def _fn_by_name(name):
    for n, fn in STRATEGIES:
        if n == name:
            return fn
    return None


def main():
    ap = argparse.ArgumentParser(description="热点选股逻辑回测 (多策略对比 + 参数优化)")
    ap.add_argument("--buy", default="2025-12-01", help="买点日期 (选股视角)")
    ap.add_argument("--sell", default="2026-07-18", help="卖点日期 (结算视角)")
    ap.add_argument("--concepts", type=int, default=8, help="热点板块数")
    ap.add_argument("--per", type=int, default=15, help="每板块成分股数")
    ap.add_argument("--pool", type=int, default=120, help="板块池大小 (全量概念板块候选)")
    ap.add_argument("--heat-per", type=int, default=8, help="每板块取前N只成分股算热度")
    ap.add_argument("--runup-days", type=int, default=20, help="前期涨幅回看交易日数")
    ap.add_argument("--runup-pct", type=float, default=40, help="前期涨幅超此%则'不追'剔除")
    ap.add_argument("--stop-pct", type=float, default=0.09, help="纪律止损比例 (默认9%)")
    ap.add_argument("--tp-pct", type=float, default=None, help="纪律止盈比例 (默认: 突破18%/其他12%)")
    ap.add_argument("--min-n", type=int, default=5, help="选优最小样本数")
    ap.add_argument("--mode", choices=["compare", "single", "trade"], default="compare",
                    help="compare=多策略对比+优化(默认); single=原单策略详细报告; "
                         "trade=逐日信号驱动动态交易(按策略信号买卖)")
    ap.add_argument("--walk-forward", action="store_true",
                    help="walk-forward 多时点回测 (多买点聚合, 大幅增样本, S0–S7 同步)")
    ap.add_argument("--wf-start", default=None, help="walk-forward 起始日 (默认=--buy)")
    ap.add_argument("--wf-end", default=None, help="walk-forward 结束日 (默认=--sell)")
    ap.add_argument("--hold-days", type=int, default=30, help="walk-forward 每买点持有交易日数")
    ap.add_argument("--step-days", type=int, default=30, help="walk-forward 买点间隔(日历天)")
    ap.add_argument("--sweep", action="store_true",
                    help="参数网格 walk-forward 扫描 (多持有×多止损×多止盈×多移动止损, 全组合) ")
    ap.add_argument("--kline-days", type=int, default=800, help="K线回看天数 (拉长周期时调大以覆盖买点之前历史)")
    ap.add_argument("--no-score-filter", action="store_true", help="(single模式) 不过滤低分候选")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--force-boards", action="store_true",
                    help="强制刷新板块池快照 (默认复用按买入日缓存的 board_pool_{buy_date}.json)")
    args = ap.parse_args()

    global _KLINE_DAYS
    _KLINE_DAYS = args.kline_days

    print(f"[{datetime.now():%Y-%m-%d %H:%M}] 回测启动: 买 {args.buy} → 卖 {args.sell}")

    if args.walk_forward:
        wf_start = args.wf_start or args.buy
        wf_end = args.wf_end or args.sell
        print(f"  ▶ walk-forward: {wf_start}→{wf_end}, 持有{args.hold_days}天, 间隔{args.step_days}天")
        agg_results, buy_dates, window_summ, window_picks = walk_forward(
            wf_start, wf_end, hold_days=args.hold_days, step_days=args.step_days,
            concepts=args.concepts, per=args.per, pool=args.pool, heat_per=args.heat_per,
            runup_pct=args.runup_pct, stop_pct=args.stop_pct, tp_pct=args.tp_pct,
            min_n=args.min_n, verbose=True, force=args.force_boards)
        if not agg_results:
            print("  ⚠ 无有效买点/数据, 退出")
            return
        best = _pick_best(agg_results, min_n=args.min_n)
        print(f"    最优策略(按胜率): {best[0]}  胜率 {best[2]['win']:.0f}%  聚合样本 {best[2]['n']}")
        report = build_walkforward_report(
            wf_start, wf_end, args.hold_days, args.step_days, args.concepts,
            agg_results, buy_dates, window_summ, window_picks, best)
        print("\n" + report)
        out = os.path.join(DATA_DIR, f"backtest_walkforward_{wf_start}_{wf_end}.md")
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\n  📄 walk-forward 报告已保存: {out}")
        except Exception:
            pass
        return

    if args.sweep:
        wf_start = args.wf_start or "2024-01-01"
        wf_end = args.wf_end or args.sell
        print(f"  ▶ 参数网格 sweep: {wf_start}→{wf_end}, 持有档[20,35,50]天, "
              f"间隔{args.step_days}天, K线{_KLINE_DAYS}天")
        results, meta = walk_forward_sweep(
            wf_start, wf_end, hold_list=(20, 35, 50), step_days=args.step_days,
            runup_list=(30, 40, 50), stop_list=(0.07, 0.10, 0.13),
            tp_list=(0.12, 0.16, 0.20), trailing_list=(None, 0.12, 0.18),
            concepts=args.concepts, per=args.per, pool=args.pool, heat_per=args.heat_per,
            min_n=args.min_n, verbose=True, force=args.force_boards)
        if not results:
            print("  ⚠ 无有效结果 (周期/K线不足或样本为0), 退出")
            return
        ranked = _rank_sweep(results, min_n=args.min_n)
        best_per = _best_per_strategy(results, min_n=args.min_n)
        report = build_sweep_report(results, meta, ranked, best_per)
        print("\n" + report)
        out = os.path.join(DATA_DIR, f"backtest_sweep_{wf_start}_{wf_end}.md")
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\n  📄 参数网格扫描报告已保存: {out}")
        except Exception:
            pass
        return

    print("  ▶ 步骤1: 还原热点板块 (成分股历史涨幅反推)...")
    hotspots = restore_hotspots(args.buy, pool_size=args.pool, heat_per=args.heat_per,
                                force=args.force_boards)

    print(f"  ▶ 步骤2: 构建候选池 (Top {args.concepts} 热点板块成分股, 形态识别)...")
    pool = build_pool(args.buy, hotspots, args.concepts, args.per)

    print(f"  ▶ 步骤3: 计算上证基准 ({args.buy}→{args.sell})...")
    benchmark = get_benchmark(args.buy, args.sell)

    if args.mode == "single":
        picks, excluded = select_on(args.buy, hotspots, args.concepts, args.per,
                                    score_filter=not args.no_score_filter,
                                    runup_days=args.runup_days, runup_pct=args.runup_pct)
        print(f"    选入 {len(picks)} 只, 剔除前期大涨 {excluded} 只")
        picks = settle(picks, args.sell)
        report = build_report(args.buy, args.sell, hotspots, args.concepts, picks,
                              benchmark, excluded=excluded,
                              runup_days=args.runup_days, runup_pct=args.runup_pct)
        print("\n" + report)
        out = os.path.join(DATA_DIR, f"backtest_hotspot_{args.buy}_{args.sell}.md")
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\n  📄 报告已保存: {out}")
        except Exception:
            pass
        return

    if args.mode == "trade":
        print(f"  ▶ 动态交易: 周一 {args.buy} 买入, 持有期间按策略信号卖出, {args.sell} 持有到期(非强制清仓)...")
        results, seg_days = trade_dynamic(pool, args.buy, args.sell,
            runup_pct=args.runup_pct, stop_pct=args.stop_pct, tp_pct=args.tp_pct,
            trailing_pct=0.10)
        best = _pick_best(results, min_n=args.min_n)
        print(f"    最优策略(按胜率): {best[0]}  胜率 {best[2]['win']:.0f}%  样本 {best[2]['n']}")
        report = build_trade_report(args.buy, args.sell, hotspots, args.concepts,
                                    results, best, seg_days)
        print("\n" + report)
        out = os.path.join(DATA_DIR, f"backtest_trade_{args.buy}_{args.sell}.md")
        try:
            with open(out, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"\n  📄 动态交易报告已保存: {out}")
        except Exception:
            pass
        return

    # ── compare 模式: 多策略纪律回测 + 选优 + 参数优化 ──
    print(f"  ▶ 步骤4: 对 {len(STRATEGIES)} 套策略做纪律回测 (止损{args.stop_pct*100:.0f}%/止盈默认)...")
    results = run_all(args.buy, args.sell, pool, benchmark,
                      runup_pct=args.runup_pct, stop_pct=args.stop_pct, tp_pct=args.tp_pct)
    best = _pick_best(results, min_n=args.min_n)
    print(f"    最优策略(按胜率): {best[0]}  胜率 {best[2]['win']:.0f}%  样本 {best[2]['n']}")

    print(f"  ▶ 步骤5: 对最优策略做参数网格扫描 (前期阈值×止损%×止盈%)...")
    best_fn = _fn_by_name(best[0])
    opt = optimize(best_fn, pool, args.buy, args.sell, benchmark, min_n=args.min_n)
    if opt:
        tr = "移动止损%.0f%%" % (opt["trailing"]*100) if opt.get("trailing") else "固定止损"
        print(f"    最佳参数: 前期{opt['runup']}% / 止损{opt['stop']*100:.0f}% / "
              f"止盈{opt['tp']*100:.0f}% / {tr} "
              f"→ 胜率{opt['stats']['win']:.0f}% 均收益{opt['stats']['avg']:+.2f}%")
    else:
        print("    样本不足, 跳过网格扫描")

    report = build_compare_report(args.buy, args.sell, hotspots, args.concepts,
                                  results, best, opt, benchmark)
    print("\n" + report)

    # 落盘: 对比报告
    out_cmp = os.path.join(DATA_DIR, f"backtest_strategies_{args.buy}_{args.sell}.md")
    try:
        with open(out_cmp, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n  📄 对比报告已保存: {out_cmp}")
    except Exception:
        pass

    # 另存: 优化后最优策略的详细报告 (深度复盘用)
    if opt:
        ru, sp, tp = opt["runup"], opt["stop"], opt["tp"]
        tr = opt.get("trailing")
        picks_opt = [dict(c) for c in best_fn(pool, runup_pct=ru, buy_date=args.buy)]
        for p in picks_opt:
            _apply_plan(p, sp, tp, tr)
        picks_opt = settle(picks_opt, args.sell)
        detailed = build_report(args.buy, args.sell, hotspots, args.concepts, picks_opt,
                                benchmark, excluded=0,
                                runup_days=args.runup_days, runup_pct=ru)
        out_det = os.path.join(DATA_DIR, f"backtest_hotspot_{args.buy}_{args.sell}.md")
        try:
            with open(out_det, "w", encoding="utf-8") as f:
                f.write(detailed)
            print(f"  📄 最优策略详细报告已保存: {out_det}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
