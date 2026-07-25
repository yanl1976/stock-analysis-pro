#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盘中 S14 三角形突破选股 (供 scheduler.py 盘中任务调用)

每 15 分钟(调度器在 09:30-11:30 / 13:00-15:00 窗口内重复触发)执行:
  1. 候选宇宙: 复用 S14 回测同源 detect_triangle, 对全市场落盘 K 线扫描对称三角形盘整
     (上升中继型). 该集合仅取决于历史 K 线(截至昨日收盘), 故按交易日缓存到
     data/s14_candidates.json, 每天只重算一次(避免 5500+ 文件反复扫描).
  2. 盘中触发: 用腾讯实时行情, 对候选逐个判断"是否已突破上轨 + 放量(量比≥1.5)".
     突破即买点触发(对应 S14 回测的 breakout 入场); 临近上轨(<2%) 标注"即将突破".
  3. 风控关卡: 每只给出 S14 同源的 止损(下轨−1%) / 目标(测量目标位) 数值, 供盘中参考.
  4. 输出 markdown 报告 → scheduler 捕获 stdout 末段推企微.

退出纪律(与回测 S14 一致, 仅提示, 不自动执行):
  · 破 MA20 仅在 MA20 斜率转负时清仓(单日回踩不砍)
  · 保本止损: 浮盈≥5% 后硬止损上移至成本价上方≈+1%

复用 (保证与回测同源, 单一数据源):
  - plans.backtest_strategy.detect_triangle / build_full_pool  (三角检测, 与回测完全一致)
  - plans.breakout_scan._kline_cached                          (零触网读盘)
  - collectors.quote.batch_quotes_tencent / market_indices      (实时价 / 大盘)

用法:
  python plans/intraday_select.py [--top 15] [--rebuild] [--json] [--to-pool] [--symbols 600519 000001]
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 强制 stdout/stderr 使用 UTF-8，避免 Windows 控制台默认 gbk 无法编码 ¥/▲ 等字符崩溃
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DATA_DIR = os.path.join(BASE_DIR, "data")
CAND_PATH = os.path.join(DATA_DIR, "s14_candidates.json")        # 候选宇宙缓存(按日)
REPORTED_PATH = os.path.join(DATA_DIR, "s14_reported.json")     # 已推送触发(按日去重)


# ───────────────── 候选宇宙 (全市场三角形突破, 按日缓存) ─────────────────
def _last_kline_date():
    """取全市场 K 线的最新交易日(截至昨日收盘), 用作三角形检测的 buy_date。

    三角形检测基于历史 K 线, 而盘中运行时今天尚无收盘价, 若把 buy_date 传成
    今天, backtest_strategy.price_on 会返回 None 导致全市场被跳过(0 候选)。
    用样本文件(贵州茅台, 必存在且数据完整)的最后一根日期代表全市场最新交易日。
    """
    sample = os.path.join(DATA_DIR, "klines", "kl_600519.json")
    try:
        d = json.load(open(sample, encoding="utf-8"))
        kl = d.get("kl") or []
        if kl:
            return kl[-1]["date"][:10]
    except Exception:
        pass
    return datetime.now().strftime("%Y-%m-%d")


def build_candidates(verbose=True):
    """扫全市场落盘 K 线, 返回满足 S14 对称三角形盘整的候选列表。

    复用 backtest_strategy.build_full_pool (其内部用 detect_triangle, 与回测同源)。
    仅保留必要字段(不含完整 K 线)写入缓存。buy_date 用 K 线最新交易日(非今天,
    否则 price_on 返回 None 全市场被跳过)。
    """
    from plans.backtest_strategy import build_full_pool
    buy_date = _last_kline_date()
    pool = build_full_pool(buy_date, verbose=verbose)
    cands = []
    for c in pool:
        tri = c.get("triangle")
        if not tri:
            continue
        kl = c.get("kl") or []
        last_date = kl[-1]["date"][:10] if kl else None
        cands.append({
            "symbol": c["symbol"],
            "name": c.get("name") or c["symbol"],
            "triangle": tri,
            "price_b": c.get("price_b"),
            "last_date": last_date,
        })
    return cands


def load_candidates(verbose=True):
    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(CAND_PATH):
        try:
            obj = json.load(open(CAND_PATH, encoding="utf-8"))
            # 注意: 空列表 [] 也是合法缓存(当日确实无候选), 用 `is not None` 而非
            # 真值判断, 否则空候选会被当成"未缓存"而每次都重建全市场(扫 5500+ 文件约 3 分钟)
            if obj.get("date") == today and obj.get("candidates") is not None:
                if verbose:
                    print(f"  ♻️ 复用今日候选缓存 ({len(obj['candidates'])} 只)")
                return obj["candidates"]
        except Exception:
            pass
    cands = build_candidates(verbose=verbose)
    try:
        json.dump({"date": today, "candidates": cands},
                  open(CAND_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass
    if verbose:
        print(f"  🔃 重建候选宇宙 {len(cands)} 只 (全市场对称三角形突破)")
    return cands


# ───────────────── 已推送触发(按日去重, 避免企微重复轰炸) ─────────────────
def load_reported():
    today = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(REPORTED_PATH):
        try:
            obj = json.load(open(REPORTED_PATH, encoding="utf-8"))
            if obj.get("date") == today:
                return set(obj.get("symbols", []))
        except Exception:
            pass
    return set()


def save_reported(syms):
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        json.dump({"date": today, "symbols": list(syms)},
                  open(REPORTED_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


# ───────────────── 工具 ─────────────────
def _weekday_count(d0_str, now):
    """d0_str(检测基准日, 含) 到 now 之间的交易日数(最小1), 用于推算今日上轨位置。"""
    try:
        a = datetime.strptime(d0_str, "%Y-%m-%d").date()
    except Exception:
        a = now.date() - timedelta(days=1)
    b = now.date()
    cnt = 0
    cur = a + timedelta(days=1)
    while cur <= b:
        if cur.weekday() < 5:
            cnt += 1
        cur += timedelta(days=1)
    return max(1, cnt)


def _session_minutes(now):
    """当前距开盘的"交易分钟数"(用于把当日累计量投影成全天量, 与 avg_vol20 可比)。"""
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 30:
        return 1
    if hm <= 11 * 60 + 30:
        return hm - (9 * 60 + 30)
    if hm < 13 * 60:
        return 120
    if hm <= 15 * 60:
        return 120 + (hm - 13 * 60)
    return 240


def check_candidate(cand, q, now):
    """对单只候选用实时行情判突破。返回结果 dict 或 None(无行情)。"""
    tri = cand["triangle"]
    price = q.get("price")
    vol = q.get("volume") or 0          # 当日累计成交量(手)
    if price is None or price <= 0:
        return None
    k = _weekday_count(cand.get("last_date"), now)
    upper_now = tri["upper_at_buy"] + tri["upper_slope"] * k
    lower_now = tri["lower_at_buy"] + tri["lower_slope"] * k
    avg_vol20 = tri.get("avg_vol20") or 0
    # 当日量投影到全天(早盘量小, 用投影量比更合理)
    mins = _session_minutes(now)
    proj_vol = vol * (240.0 / mins) if mins else vol
    vol_ratio = (proj_vol / avg_vol20) if avg_vol20 else 0
    gap_pct = (price / upper_now - 1) * 100 if upper_now else 0
    triggered = price > upper_now and proj_vol >= avg_vol20 * 1.5
    near = (not triggered) and (gap_pct > -2.0)   # 距上轨<2% 视为即将突破
    # S14 同源风控关卡
    stop = round(lower_now * 0.99, 2)
    h0 = tri.get("height0")
    if h0 and price:
        tp = min(max(price + h0, price * 1.10), price * 1.20)
    else:
        tp = round(price * 1.18, 2)
    tp = round(tp, 2)
    return {
        "symbol": cand["symbol"], "name": cand.get("name") or q.get("name") or cand["symbol"],
        "price": round(price, 2), "gap_pct": round(gap_pct, 2),
        "vol_ratio": round(vol_ratio, 2), "upper_now": round(upper_now, 2),
        "triggered": triggered, "near": near,
        "stop": stop, "tp": tp,
    }


# ───────────────── 主流程 ─────────────────
def run(top=15, rebuild=False, to_pool=False, symbols=None, verbose=True):
    now = datetime.now()
    now_str = now.strftime("%H:%M")

    # 大盘环境
    lines = []
    try:
        from collectors.quote import market_indices
        idx = market_indices()
        if idx:
            parts = []
            for name, info in idx.items():
                arrow = "▲" if info["change_pct"] >= 0 else "▼"
                parts.append(f"{name} {info['price']:.2f} {arrow}{abs(info['change_pct']):.2f}%")
            lines.append("📊【大盘 " + now_str + "】 " + "  |  ".join(parts))
        else:
            lines.append("📊【大盘 " + now_str + "】 (指数获取失败)")
    except Exception:
        lines.append("📊【大盘 " + now_str + "】 (指数获取失败)")

    # ── 大盘状态门控: 空头 → 空仓观望, 不出股/不推/不加池 ──
    try:
        from plans.weekly_hotspot import market_regime
        _regime, _rdiff = market_regime()
    except Exception:
        _regime, _rdiff = "未知", None
    if _regime == "空头":
        _rdiff_s = f"{_rdiff:+.2f}%" if isinstance(_rdiff, (int, float)) else "—"
        lines.append(f"\n🛑 大盘空头(MA20/MA60 {_rdiff_s}) → 空仓观望, 不出股")
        lines.append("   按 weekly_hotspot 路由: 空头无可靠策略, 规避买点窗口虚假信号")
        print("\n".join(lines))
        print("#NO_PUSH#")
        return {"date": now.strftime("%Y-%m-%d"), "count": 0, "triggered": [], "near": []}

    # 候选宇宙
    if rebuild and os.path.exists(CAND_PATH):
        try:
            os.remove(CAND_PATH)
        except Exception:
            pass
    cands = load_candidates(verbose=verbose)

    if symbols:
        # 直接指定股票: 用 detect_triangle 重新检测(若不在候选宇宙)
        from plans.backtest_strategy import detect_triangle
        from plans.breakout_scan import _kline_cached, _bare
        direct = []
        for s in symbols:
            sym = _bare(s)
            try:
                kl = _kline_cached(sym)
            except Exception:
                kl = None
            if not kl or len(kl) < 80:
                continue
            tri = detect_triangle(kl)
            if not tri:
                continue
            direct.append({"symbol": sym, "name": sym, "triangle": tri,
                           "price_b": kl[-1]["close"], "last_date": kl[-1]["date"][:10]})
        cands = direct
        if verbose:
            print(f"  🎯 直接检测 {len(symbols)} 只 → 命中三角形 {len(cands)} 只")

    if not cands:
        lines.append("\n😌 当前无三角形突破候选 (全市场未检出对称三角形盘整)")
        print("\n".join(lines))
        # 无候选 = 没有合适入选, 打印 sentinel 让 scheduler 跳过企微推送
        print("#NO_PUSH#")
        return {"date": now.strftime("%Y-%m-%d"), "count": 0, "triggered": [], "near": []}

    # 实时行情(批量一次请求)
    from collectors.quote import batch_quotes_tencent
    syms = [c["symbol"] for c in cands]
    q = batch_quotes_tencent(syms) if syms else {}
    if not q and syms:
        lines.append("\n（行情获取失败, 无数据）")
        print("\n".join(lines))
        # 无行情数据 = 无可推送内容, 跳过推送
        print("#NO_PUSH#")
        return {"date": now.strftime("%Y-%m-%d"), "count": 0, "triggered": [], "near": []}

    results = []
    for c in cands:
        info = q.get(c["symbol"])
        if not info:
            continue
        r = check_candidate(c, info, now)
        if r:
            results.append(r)

    triggered = [r for r in results if r["triggered"]]
    near = [r for r in results if (not r["triggered"]) and r["near"]]
    triggered.sort(key=lambda x: -x["vol_ratio"])
    near.sort(key=lambda x: x["gap_pct"], reverse=True)

    # 按日去重推送: 仅"新触发"进企微明细, 已报的汇总计数
    reported = load_reported()
    new_trig = [r for r in triggered if r["symbol"] not in reported]
    old_trig = [r for r in triggered if r["symbol"] in reported]
    save_reported(reported | {r["symbol"] for r in triggered})

    # ── 无合适入选: 既无新触发/持续突破, 也无即将突破 → 跳过推送 ──
    if not new_trig and not old_trig and not near:
        lines.append("\n😌 当前盘中无突破触发 / 即将突破候选 (未达入选条件)")
        print("\n".join(lines))
        # 打印 sentinel 让 scheduler 跳过企微推送(避免无意义的空报告轰炸)
        print("#NO_PUSH#")
        return {"date": now.strftime("%Y-%m-%d"), "count": len(cands),
                "triggered": [], "near": []}

    # ── 报告 ──
    lines.append(f"\n🎯 S14 三角形突破选股 (盘中 {now_str})  候选宇宙 {len(cands)} 只")
    if new_trig:
        lines.append(f"\n🔥 突破触发 (买点·新) {len(new_trig)} 只:")
        for r in new_trig[:top]:
            lines.append(
                f"   • {r['name']}({r['symbol']}) 现价{r['price']} "
                f"突破上轨{r['gap_pct']:+.1f}% 量比{r['vol_ratio']} "
                f"| 止损{r['stop']} 目标{r['tp']}")
        if len(new_trig) > top:
            lines.append(f"   • …另 {len(new_trig) - top} 只")
    else:
        lines.append("\n😌 突破触发(新): 无")
    if old_trig:
        lines.append(f"\n↺ 持续突破(已报) {len(old_trig)} 只: "
                     + ", ".join(f"{r['name']}({r['symbol']})" for r in old_trig[:top]))
    if near:
        lines.append(f"\n👀 即将突破 (距上轨<2%) {len(near)} 只:")
        for r in near[:top]:
            lines.append(
                f"   • {r['name']}({r['symbol']}) 现价{r['price']} "
                f"距上轨{r['gap_pct']:+.1f}% 量比{r['vol_ratio']}")
        if len(near) > top:
            lines.append(f"   • …另 {len(near) - top} 只")
    lines.append("\n📌 退出纪律: 破MA20仅斜率转负才清仓; 浮盈≥5%后止损上移至成本+1%(保本)")
    lines.append("报告生成完毕")

    print("\n".join(lines))

    # 可选: 加入策略股票池(供 intraday_watch 监控止损/止盈)
    if to_pool and new_trig:
        try:
            from plans.stock_pool import add_entries
            entries = [{
                "symbol": r["symbol"], "name": r["name"],
                "concepts": ["S14三角形突破"], "reason": "S14盘中突破触发",
            } for r in new_trig]
            # 用 S14 同源关卡覆盖 buy/stop/tp
            added = add_entries(entries, reason_default="S14盘中突破触发")
            # add_entries 内部重算关卡(基于 classify_stage, 非三角形), 这里直接补写三角形关卡
            _patch_pool_levels(new_trig)
            print(f"[POOL] 已加入股票池 {len(entries)} 只 (新增 {added})")
        except Exception as e:
            print(f"[POOL] 加入失败: {e}")

    return {"date": now.strftime("%Y-%m-%d"), "count": len(cands),
            "triggered": triggered, "near": near}


def _patch_pool_levels(trig_list):
    """把 S14 同源的 买点(上轨)/止损(下轨−1%)/目标(测量) 写回股票池对应条目。"""
    try:
        from plans.stock_pool import load_pool, save_pool
    except Exception:
        return
    pool = load_pool()
    by_sym = {e["symbol"]: e for e in pool.get("entries", [])}
    for r in trig_list:
        e = by_sym.get(r["symbol"])
        if not e:
            continue
        e["buy_level"] = r["upper_now"]
        e["stop_level"] = r["stop"]
        e["tp_level"] = r["tp"]
        e["buy_point"] = f"突破上轨{r['upper_now']}"
        e["reason_tag"] = "S14盘中突破触发"
        e["last_refresh"] = datetime.now().strftime("%Y-%m-%d")
    save_pool(pool)


def main():
    ap = argparse.ArgumentParser(description="盘中 S14 三角形突破选股")
    ap.add_argument("--top", type=int, default=15, help="最多展示条数, 默认 15")
    ap.add_argument("--rebuild", action="store_true", help="强制重建候选宇宙(忽略当日缓存)")
    ap.add_argument("--to-pool", action="store_true",
                    help="把新触发突破的标的加入策略股票池(供 intraday_watch 监控)")
    ap.add_argument("--symbols", nargs="*", help="直接指定股票代码(跳过全市场扫描)")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    res = run(top=args.top, rebuild=args.rebuild, to_pool=args.to_pool,
              symbols=args.symbols, verbose=not args.json)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
