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

from collectors.concept import concept_rank_sina, fetch_concept_stocks_sina
from collectors.quote import kline, realtime
from analysis.breakout import classify_stage, STAGE_LABELS

# ── K线磁盘缓存 (避免重复触网) ──
CACHE_DIR = os.path.join(BASE_DIR, "cache")
CACHE_TTL = 86400  # 1 天


def _bare(symbol: str) -> str:
    """成分股 symbol 常带 sh/sz 前缀, 而 kline/realtime 内部会再加前缀,
    需先剥掉, 否则变成 szsh600236 之类无效代码。"""
    return symbol[2:] if symbol[:2] in ("sh", "sz") else symbol


def _kline_cached(symbol: str, days: int = 250):
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = f"kl_{symbol}_{days}"
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < CACHE_TTL:
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    kl = kline(symbol, days=days)
    try:
        with open(path, "w") as f:
            json.dump(kl, f)
    except Exception:
        pass
    return kl


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
                     "change_pct": c.get("change_pct", 0)} for c in raw][:top_concepts]

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
        from collectors.quote import kline as _k, realtime as _r
        from analysis.breakout import classify_stage as _cs, STAGE_LABELS as _L
        out = []
        for sym in args.symbols:
            kl = _k(sym, days=250)
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
