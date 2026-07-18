# -*- coding: utf-8 -*-
"""突破扫描计划 — 热点版块 → 成分股 → 形态识别 → 排序

复用既有积木:
  - collectors.concept.concept_rank_sina / fetch_concept_stocks_sina  (热点版块 + 成分股)
  - collectors.quote.kline / realtime                                  (K线 + 实时价)
  - analysis.breakout.classify_stage                                    (形态识别/状态分类)

流程:
  1. 取热点版块榜单 (默认 Top N, 资金/涨幅驱动)
  2. 逐版块拉成分股
  3. 对每只成分股拉 250 日 K线 + 实时价, 跑 classify_stage
  4. 按评分降序, 标注所属热点版块 (板块共振提权在 score 中已含量价信号)

输出: {date, concepts, count, candidates:[{symbol,name,price,change_pct,
        concept,concept_pct,stage,score,signals,details}, ...]}
"""
import os
import sys
import json
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from collectors.concept import concept_rank_sina, fetch_concept_stocks_sina, merge_duplicate_concepts
from collectors.quote import kline, realtime
from analysis.breakout import classify_stage, STAGE_LABELS

# ── K线磁盘缓存 (避免重复触网) ──
# 设计: 按 symbol 永久落盘全量K线到 data/klines/kl_<symbol>.json,
#       记录 fetched_at(抓取时间) 与 last_date(数据最新交易日)。
# 历史买点回测: 缓存的 last_date 必 ≥ 买点日期(全量含买点前所有历史),
#       故历史买点永远命中永久缓存、零触网; 仅"今天/最近"有增量刷新。
# 不再用 days 做缓存键(避免 250/800 两套缓存互不共享), 读取时再按 days 截断。
KL_DIR = os.path.join(BASE_DIR, "data", "klines")
# ── K线校验阈值 ──
MAX_STALE_DAYS = 365   # 缓存最新交易日距今超此值(如坏缓存停在2011)→重抓; 正常短期刷新不触发, 保证回测零触网
MIN_BARS = 60          # 有效K线最少根数
GAP_WARN_DAYS = 10     # 相邻交易日间隔超此值→记为缺口(警告, 不致命)
_KL_FETCH_DAYS = 6000  # 新浪单页最大 datalen, 覆盖上市至今全历史(约6000根日线)


def _bare(symbol: str) -> str:
    """成分股 symbol 常带 sh/sz/bj 前缀, 而 kline/realtime 内部会再加前缀,
    需先剥掉, 否则变成 szsh600236 之类无效代码。
    注意: 落盘缓存文件名用裸 6 位代码 (kl_920106.json), 故这里必须把 bj 也剥掉,
    否则 bj920106 找不到 kl_920106.json → 触网重抓。"""
    return symbol[2:] if symbol[:2] in ("sh", "sz", "bj") else symbol


def _kl_path(symbol: str) -> str:
    return os.path.join(KL_DIR, f"kl_{symbol}.json")


def _kl_sanitize(kl):
    """修复新浪零填充异常日, 返回干净K线。

    - open=high=low=volume=0 但 close>0: 新浪对停牌/异常日的零填充, 用收盘价补全为平盘。
    - close<=0: 完全无效日, 丢弃(避免污染回测)。
    - high<low: 交换修正。
    """
    out = []
    for b in kl:
        try:
            o = float(b.get("open", 0)); h = float(b.get("high", 0))
            l = float(b.get("low", 0)); c = float(b.get("close", 0))
            v = float(b.get("volume", 0))
        except Exception:
            continue
        if c <= 0:
            continue  # 完全无效日, 丢弃
        if o == 0 and h == 0 and l == 0 and v == 0:
            o = h = l = c  # 零填充异常→平盘
        elif o == 0:
            o = c
        if h < l:
            h, l = max(h, l), min(h, l)
        out.append({"date": b.get("date", ""), "open": o, "high": h,
                    "low": l, "close": c, "volume": v})
    return out


def _kl_load(symbol: str):
    """读磁盘缓存, 返回 (kl_list, last_date, stale_accepted, short_history) 或 (None, None, False, False)。

    stale_accepted: 该股票数据源天然缺失(退市/停牌), 重抓仍停在旧日期, 已标记接受。
    short_history: 上市不足 MIN_BARS 天(次新股), 数据有效但过短, 已标记避免重复抓取。
    """
    path = _kl_path(symbol)
    if not os.path.exists(path):
        return None, None, False, False
    try:
        with open(path, "r", encoding="utf-8") as f:
            obj = json.load(f)
        kl = obj.get("kl")
        if not kl:
            return None, None, False, False
        last_date = kl[-1]["date"][:10] if kl[-1].get("date") else None
        return kl, last_date, bool(obj.get("stale_accepted", False)), bool(obj.get("short_history", False))
    except Exception:
        return None, None, False, False


def _kl_save(symbol: str, kl, stale_accepted: bool = False, short_history: bool = False):
    os.makedirs(KL_DIR, exist_ok=True)
    path = _kl_path(symbol)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"symbol": symbol, "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "last_date": kl[-1]["date"][:10] if kl else None,
                       "bars": len(kl), "stale_accepted": stale_accepted,
                       "short_history": short_history, "kl": kl}, f)
    except Exception:
        pass


def _kl_validate(kl, max_stale: int = MAX_STALE_DAYS, today=None):
    """校验K线完整有效, 返回 (ok:bool, info:str)。

    - 结构性: 字段非空/OHLC有效(high>=low, 价格>0)/最小长度/日期可解析
    - 新鲜度: 最新交易日距今天 ≤ max_stale
    """
    if not kl:
        return False, "empty"
    if len(kl) < MIN_BARS:
        return False, f"bars={len(kl)}<{MIN_BARS}"
    today = today or datetime.now()
    last = kl[-1].get("date", "")[:10]
    try:
        ld = datetime.strptime(last, "%Y-%m-%d")
    except Exception:
        return False, f"bad last_date={last}"
    stale = (today - ld).days
    if stale > max_stale:
        return False, f"stale {last} ({stale}d>{max_stale})"
    for b in kl:
        if not b.get("date"):
            return False, "empty date"
        try:
            o, h, l, c, v = (float(b["open"]), float(b["high"]),
                             float(b["low"]), float(b["close"]), float(b["volume"]))
        except Exception:
            return False, f"bad field @{b.get('date')}"
        if min(o, h, l, c) <= 0:
            return False, f"nonpositive @{b.get('date')}"
        if h < l - 1e-6:
            return False, f"high<low @{b.get('date')}"
        if v < 0:
            return False, f"neg vol @{b.get('date')}"
    gaps = 0
    for i in range(1, len(kl)):
        try:
            d0 = datetime.strptime(kl[i - 1]["date"][:10], "%Y-%m-%d")
            d1 = datetime.strptime(kl[i]["date"][:10], "%Y-%m-%d")
        except Exception:
            return False, "date parse error"
        if (d1 - d0).days > GAP_WARN_DAYS:
            gaps += 1
    return True, f"ok bars={len(kl)} gaps={gaps}"


def _kl_struct_ok(kl):
    """仅结构性校验(忽略新鲜度), 用于判断数据源天然缺失(退市/停牌股)。"""
    return _kl_validate(kl, max_stale=10 ** 9)[0]


def _kl_fields_ok(kl):
    """仅校验每个 bar 字段有效(OHLC>0 / high>=low / vol>=0 / 日期可解析), 忽略长度与新鲜度。

    用于判断"数据本身是否可用"(含次新股短序列), 与 _kl_struct_ok(含 MIN_BARS 长度门槛)区分。
    """
    if not kl:
        return False
    for b in kl:
        if not b.get("date"):
            return False
        try:
            o, h, l, c, v = (float(b["open"]), float(b["high"]),
                             float(b["low"]), float(b["close"]), float(b["volume"]))
        except Exception:
            return False
        if min(o, h, l, c) <= 0:
            return False
        if h < l - 1e-6:
            return False
        if v < 0:
            return False
    return True


def _fetch_full_kline(symbol: str, max_retry: int = 3):
    """新浪日线(不复权, 与回测口径一致)拉全历史(上市至今)。

    新浪 getKLineData 支持 datalen 上限约6000根(≈24年), 单次即覆盖全历史。
    返回升序 [{date,open,high,low,close,volume}]。
    """
    from collectors.quote import kline
    for attempt in range(max_retry):
        try:
            kl = kline(symbol, days=_KL_FETCH_DAYS)
            if kl:
                return _kl_sanitize(kl)
        except Exception:
            time.sleep(0.5 * (attempt + 1))
    return []


# 网络请求计数器 (供回测/批量打印"触网次数", 验证缓存复用)
_NET_KLINE_HITS = 0


def _kline_net_hits():
    return _NET_KLINE_HITS


def _kline_cached(symbol: str, days: int = 250, force_refresh: bool = False):
    """取K线: 优先磁盘永久缓存(校验通过即复用), 过期/缺失则新浪全历史重抓。

    回测场景: 历史K线永不变, 正常缓存校验通过→零触网(仅极旧坏缓存如停在2011才重抓)。
    数据源天然缺失(退市/停牌股, 重抓仍停在旧日期): 标记 stale_accepted, 避免无限重抓。
    实盘场景: 传 force_refresh=True 强制重抓最新。
    返回: 按 days 截断末尾 days 根(与原接口返回结构一致)。
    """
    global _NET_KLINE_HITS
    kl, last_date, sa, short_hist = _kl_load(symbol)
    if not force_refresh and kl:
        if sa and _kl_struct_ok(kl):
            return kl[-days:] if days and days < len(kl) else kl
        if short_hist and _kl_fields_ok(kl):
            return kl[-days:] if days and days < len(kl) else kl
        if _kl_validate(kl)[0]:
            return kl[-days:] if days and days < len(kl) else kl
    _NET_KLINE_HITS += 1
    old_last = last_date
    try:
        kl = _fetch_full_kline(symbol)
    except Exception:
        kl = []
    if kl:
        fresh_ok, _ = _kl_validate(kl)
        struct_ok = _kl_struct_ok(kl)
        if struct_ok:
            new_last = kl[-1]["date"][:10]
            try:
                new_stale = (datetime.now() - datetime.strptime(new_last, "%Y-%m-%d")).days
            except Exception:
                new_stale = 0
            if old_last is None:
                accepted = new_stale > MAX_STALE_DAYS      # 首次即很旧→数据源旧股
            else:
                accepted = (old_last == new_last) and (new_stale > MAX_STALE_DAYS)  # 重抓无变化且很旧
            short_history = len(kl) < MIN_BARS
            _kl_save(symbol, kl, stale_accepted=accepted, short_history=short_history)
            return kl[-days:] if days and days < len(kl) else kl
    return []


def run(top_concepts: int = 5, top_per_concept: int = 15,
        sector: str = None, stage_filter: str = None,
        verbose: bool = True, use_cache: bool = True) -> dict:
    """执行突破扫描

    Args:
        top_concepts: 扫描的热点版块数量
        top_per_concept: 每个版块取前 N 只成分股
        sector: 指定单一版块名称 (优先于 top_concepts)
        stage_filter: 仅保留某状态 (about_to_launch/breakout/...)
        verbose: 打印进度
        use_cache: 启用 K线缓存
    """
    date_str = datetime.now().strftime("%Y-%m-%d")

    # ── 1. 热点版块 ──
    if sector:
        raw = concept_rank_sina(limit=top_concepts * 3)
        hit = next((c for c in raw if sector in c["name"]), None)
        if not hit:
            return {"error": f"未找到版块: {sector}", "date": date_str}
        concepts = [{"name": hit["name"], "bk_code": hit["code"],
                     "change_pct": hit.get("change_pct", 0)}]
    else:
        raw = concept_rank_sina(limit=top_concepts * 3)
        concepts = [{"name": c["name"], "bk_code": c["code"],
                     "change_pct": c.get("change_pct", 0)} for c in raw]

    concepts = merge_duplicate_concepts(concepts)[:top_concepts]
    if verbose:
        names = "、".join(c["name"] for c in concepts)
        print(f"[{date_str}] 突破扫描启动 → 热点版块: {names}")

    candidates = []
    for c in concepts:
        if verbose:
            print(f"  ▶ 版块 {c['name']} 拉取成分股...")
        try:
            stocks = fetch_concept_stocks_sina(
                c["bk_code"], name=c["name"], limit=top_per_concept)
        except Exception as e:
            if verbose:
                print(f"    ⚠️ 成分股获取失败: {e}")
            continue

        for s in stocks:
            sym = _bare(s["symbol"])
            try:
                kl = _kline_cached(sym) if use_cache else kline(sym, days=250)
                if len(kl) < 40:
                    continue
                try:
                    rt = realtime(sym)
                    price = rt["price"]
                    chg = rt["change_pct"]
                    name = rt["name"]
                except Exception:
                    price = kl[-1]["close"]
                    chg = 0
                    name = s.get("name", sym)
            except Exception as e:
                if verbose:
                    print(f"    ⚠️ {sym} K线获取失败: {e}")
                continue

            res = classify_stage(
                [b["close"] for b in kl],
                [b["high"] for b in kl],
                [b["low"] for b in kl],
                [b["volume"] for b in kl],
                price=price,
            )
            res.update({
                "symbol": sym, "name": name, "price": price,
                "change_pct": chg, "concept": c["name"],
                "concept_pct": c["change_pct"],
            })
            if stage_filter and res["stage"] != stage_filter:
                continue
            candidates.append(res)
            if verbose:
                print(f"    {name}({sym}) {STAGE_LABELS.get(res['stage'])} "
                      f"评分{res['score']} {res['signals'] and '|'.join(res['signals'][:3])}")
        time.sleep(0.3)

    candidates.sort(key=lambda x: -x["score"])
    if verbose:
        print(f"  ✓ 扫描完成, 候选 {len(candidates)} 只, 按评分降序。")
    return {"date": date_str, "concepts": concepts,
            "count": len(candidates), "candidates": candidates}


def format_report(data: dict) -> str:
    if "error" in data:
        return f"❌ 错误: {data['error']}"

    lines = [f"\n{'='*50}",
             f"  🎯 突破扫描 ({data['date']})  候选 {data['count']} 只",
             f"{'='*50}"]

    if not data["candidates"]:
        lines.append("\n⚠️ 未筛选出候选 (放宽 --stage-filter 或增大 --per)")
        lines.append("\n报告生成完毕")
        return "\n".join(lines)

    for i, c in enumerate(data["candidates"], 1):
        d = c.get("details", {})
        lines.append(f"\n{i}. {c['name']}({c['symbol']}) "
                     f"¥{c['price']} {c['change_pct']:+.2f}%")
        lines.append(f"   {STAGE_LABELS.get(c['stage'])}  评分 {c['score']}")
        lines.append(f"   所属热点: {c['concept']} ({c['concept_pct']:+.2f}%)")
        if c["signals"]:
            lines.append(f"   信号: {' | '.join(c['signals'])}")
        lines.append(f"   平台带宽={d.get('band_width')} 布林收缩分位={d.get('bb_squeeze_pct')} "
                     f"VCP={d.get('vcp')} 量比={d.get('vol_ratio')} "
                     f"距压力{d.get('pct_to_resistance')}%")

    lines.append(f"\n{'='*50}")
    lines.append("报告生成完毕")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    import sys as _sys
    for _s in (_sys.stdout, _sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="热点版块突破扫描")
    ap.add_argument("symbols", nargs="*", help="直接指定股票代码 (跳过版块)")
    ap.add_argument("--concepts", type=int, default=5, help="热点版块数量")
    ap.add_argument("--per", type=int, default=15, help="每版块成分股数")
    ap.add_argument("--sector", help="指定单一版块名称")
    ap.add_argument("--stage-filter", help="仅保留某状态")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.symbols:
        # 直接对指定股票做形态识别 (复用 classify_stage)
        from collectors.quote import realtime as _r
        from analysis.breakout import classify_stage as _cs, STAGE_LABELS as _L
        out = []
        for sym in args.symbols:
            kl = _kline_cached(sym, days=250)  # 优先本地落盘缓存, 零触网
            rt = _r(sym)
            r = _cs([b["close"] for b in kl], [b["high"] for b in kl],
                    [b["low"] for b in kl], [b["volume"] for b in kl], price=rt["price"])
            r.update({"symbol": sym, "name": rt["name"], "price": rt["price"],
                      "change_pct": rt["change_pct"], "concept": "-", "concept_pct": 0})
            out.append(r)
        data = {"date": datetime.now().strftime("%Y-%m-%d"), "concepts": [],
                "count": len(out), "candidates": out}
    else:
        data = run(top_concepts=args.concepts, top_per_concept=args.per,
                   sector=args.sector, stage_filter=args.stage_filter)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(format_report(data))
