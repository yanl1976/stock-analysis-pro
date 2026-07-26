#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""仪表盘 API — 所有数据端点 (JSON)."""
import os
import sys
import json
import glob
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from flask import Blueprint, jsonify, request, render_template

dashboard_bp = Blueprint("dashboard", __name__)


def _json_ok(data):
    return jsonify({"ok": True, "data": data, "ts": datetime.now().isoformat()})


# ── 1. 仪表盘主页面 (整合: 概念榜 + 成分股 + 操作建议) ──
@dashboard_bp.route("/")
def index():
    return render_template("dashboard.html")


# ── 2. 大盘状态 & 概况 ──
@dashboard_bp.route("/api/v1/dashboard/status")
def api_status():
    try:
        from plans.weekly_hotspot import market_regime
        regime, rdiff = market_regime()
    except Exception:
        regime, rdiff = "未知", None

    pool = _load_pool()
    entries = pool.get("entries", [])
    active = [e for e in entries if not e.get("exited")]
    held = [e for e in active if e.get("entered")]

    return _json_ok({
        "regime": regime,
        "regime_diff": round(rdiff, 2) if isinstance(rdiff, (int, float)) else None,
        "pool_total": len(entries),
        "pool_active": len(active),
        "held": len(held),
    })


# ── 3. 决策简报 (买入/卖出/持仓/评级变化/核心池) ──
@dashboard_bp.route("/api/v1/dashboard/decision")
def api_decision():
    try:
        from plans.decision_engine import build_brief
        brief = build_brief(use_quote=True)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return _json_ok(_brief_to_api(brief))


def _brief_to_api(brief):
    """把 decision_engine 的内部结构转为前端友好的扁平结构。"""
    return {
        "date": brief["date"],
        "regime": brief["regime"],
        "regime_diff": round(brief.get("regime_diff"), 2) if isinstance(brief.get("regime_diff"), (int, float)) else None,
        "stats": {
            "pool_total": brief["pool_total"],
            "pool_active": brief["pool_active"],
            "held": brief["held"],
        },
        "buy": [_simplify_entry(b) for b in brief.get("buy", [])],
        "sell": [_simplify_entry(s) for s in brief.get("sell", [])],
        "hold": [_simplify_entry(h) for h in brief.get("hold", [])],
        "rating_change": brief.get("rating_change", []),
        "core": [_simplify_entry(c) for c in brief.get("core", [])],
    }


def _simplify_entry(e):
    """去掉 None, 把 float 四舍五入。"""
    out = {}
    for k, v in e.items():
        if v is None:
            continue
        if isinstance(v, float):
            out[k] = round(v, 2)
        else:
            out[k] = v
    return out


# ── 4. 股票池 (支持筛选) ──
@dashboard_bp.route("/api/v1/dashboard/pool")
def api_pool():
    rating = request.args.get("rating")       # 关注/观察/暂避/推荐
    min_score = request.args.get("min_score", type=int)
    search = request.args.get("search", "")   # 名称/代码模糊搜索
    limit = request.args.get("limit", 50, type=int)
    sort_by = request.args.get("sort", "score")  # score / rating / stage / date

    pool = _load_pool()
    entries = [e for e in pool.get("entries", []) if not e.get("exited")]

    if rating:
        entries = [e for e in entries if e.get("rating") == rating]
    if min_score is not None:
        entries = [e for e in entries if (e.get("score") or 0) >= min_score]
    if search:
        s = search.lower()
        entries = [e for e in entries
                   if s in (e.get("name") or "").lower() or s in (e.get("symbol") or "")]

    # 排序
    if sort_by == "rating":
        order = {"暂避": 0, "观察": 1, "关注": 2, "推荐": 3}
        entries.sort(key=lambda e: order.get(e.get("rating"), 0), reverse=True)
    elif sort_by == "stage":
        entries.sort(key=lambda e: e.get("stage") or "")
    elif sort_by == "date":
        entries.sort(key=lambda e: e.get("added") or "", reverse=True)
    else:  # score
        entries.sort(key=lambda e: e.get("score") or 0, reverse=True)

    items = [_simplify_entry(e) for e in entries[:limit]]
    # 补充概念
    for item, raw in zip(items, entries[:limit]):
        cons = raw.get("concepts") or []
        if isinstance(cons, str):
            cons = [cons]
        item["concept"] = "、".join([c for c in cons if c][:2])

    return _json_ok({
        "total": len(entries),
        "shown": len(items),
        "items": items,
    })


# ── 5. 调度器状态 ──
@dashboard_bp.route("/api/v1/dashboard/scheduler")
def api_scheduler():
    state_file = os.path.join(BASE_DIR, "data", "scheduler_state.json")
    log_file = os.path.join(BASE_DIR, "data", "scheduler.log")

    tasks = []
    if os.path.exists(state_file):
        try:
            with open(state_file, encoding="utf-8") as f:
                state = json.load(f)
            for name, info in (state.get("tasks") or {}).items():
                tasks.append({
                    "name": name,
                    "last_run": info.get("last_run"),
                    "last_result": info.get("last_result"),
                    "next_run": info.get("next_run"),
                    "enabled": info.get("enabled", True),
                })
        except Exception:
            pass

    log_tail = []
    if os.path.exists(log_file):
        try:
            with open(log_file, encoding="utf-8") as f:
                lines = f.readlines()
            log_tail = [l.strip() for l in lines[-30:]]
        except Exception:
            pass

    return _json_ok({"tasks": tasks, "log_tail": log_tail})


# ── 6. 报告列表 ──
@dashboard_bp.route("/api/v1/dashboard/reports")
def api_reports():
    reports_dir = os.path.join(BASE_DIR, "data", "reports")
    notify_dir = os.path.join(BASE_DIR, "data", "notify_html")

    items = []
    for src_dir, label in [(reports_dir, "reports"), (notify_dir, "notify")]:
        if not os.path.isdir(src_dir):
            continue
        for fn in sorted(os.listdir(src_dir), reverse=True):
            fp = os.path.join(src_dir, fn)
            if not os.path.isfile(fp):
                continue
            ext = os.path.splitext(fn)[1].lower()
            if ext not in (".md", ".html"):
                continue
            stat = os.stat(fp)
            items.append({
                "name": fn,
                "label": label,
                "size_kb": round(stat.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            })

    return _json_ok({"reports": items[:50]})


# ── 7. 选股流水线 (概念榜 → 成分股 → 突破扫描 → 池内概念分布) ──
@dashboard_bp.route("/api/v1/dashboard/pipeline")
def api_pipeline():
    result = {"concept_rank": [], "board_pool": None, "pool_by_concept": [],
              "pool_stage_dist": {}, "pool_rating_dist": {}}

    # 7a. 最新概念榜单 (从 board_pool.json)
    board_file = os.path.join(BASE_DIR, "data", "concepts", "board_pool.json")
    if os.path.exists(board_file):
        try:
            with open(board_file, encoding="utf-8") as f:
                bp = json.load(f)
            result["board_pool"] = {
                "fetched_at": bp.get("fetched_at"),
                "count": len(bp.get("pool", [])),
                "items": bp.get("pool", [])[:30],  # top 30 概念
            }
        except Exception:
            pass

    # 7b. 池内按概念分布 (哪些概念产出了最多股票)
    pool = _load_pool()
    entries = [e for e in pool.get("entries", []) if not e.get("exited")]
    concept_counter = {}
    for e in entries:
        cons = e.get("concepts") or []
        if isinstance(cons, str):
            cons = [cons]
        for c in cons:
            if c:
                concept_counter[c] = concept_counter.get(c, 0) + 1
    result["pool_by_concept"] = sorted(
        [{"name": k, "count": v} for k, v in concept_counter.items()],
        key=lambda x: -x["count"])[:20]

    # 7c. 池内形态 & 评级分布
    from collections import Counter
    result["pool_stage_dist"] = dict(Counter(
        e.get("stage") for e in entries if e.get("stage")).most_common())
    result["pool_rating_dist"] = dict(Counter(
        e.get("rating") for e in entries if e.get("rating")).most_common())

    # 7d. 每个概念对应的突破候选 (从池中取每概念评分最高的 5 只)
    by_concept = {}
    for e in entries:
        cons = e.get("concepts") or []
        if isinstance(cons, str):
            cons = [cons]
        for c in cons:
            if not c:
                continue
            by_concept.setdefault(c, []).append({
                "symbol": e.get("symbol"),
                "name": e.get("name"),
                "stage": e.get("stage"),
                "score": e.get("score"),
                "rating": e.get("rating"),
                "buy_level": e.get("buy_level"),
                "stop_level": e.get("stop_level"),
                "tp_level": e.get("tp_level"),
                "hot_type": e.get("hot_type", ""),
                "signals": e.get("signals", []),
                "trend": e.get("trend"),
                "resistance": e.get("resistance"),
                "support": e.get("support"),
                "buy_point": e.get("buy_point"),
                "sell_hint": e.get("sell_hint"),
                "position": e.get("position"),
                "concepts": cons,
                "entered": bool(e.get("entered")),
                "entered_price": e.get("entered_price"),
                "entered_date": e.get("entered_date"),
                "price_entry": e.get("price_entry"),
            })
    # 每概念取 Top5 按评分
    result["concept_stocks"] = {}
    for c, stocks in sorted(by_concept.items(), key=lambda x: -len(x[1]))[:12]:
        stocks.sort(key=lambda x: x.get("score") or 0, reverse=True)
        result["concept_stocks"][c] = stocks[:5]

    # 7e. 概念热度追踪报告
    heat_file = os.path.join(BASE_DIR, "data", "concepts", "concept_heat.json")
    if os.path.exists(heat_file):
        try:
            with open(heat_file, encoding="utf-8") as f:
                heat = json.load(f)
            # 只返回 today 榜单中的概念 (带 hot_type)
            result["concept_heat"] = {
                "date": heat.get("date"),
                "summary": heat.get("summary"),
                "concepts": [
                    {
                        "name": c["name"],
                        "hot_type": c["hot_type"],
                        "consecutive_days": c["consecutive_days"],
                        "cumulative_pct": c["cumulative_pct"],
                        "avg_pct": c["avg_pct"],
                        "pct_trend": c["pct_trend"],
                        "rank_trend": c["rank_trend"],
                        "best_rank": c["best_rank"],
                        "today_pct": c.get("today_pct"),
                        "today_rank": c.get("today_rank"),
                    }
                    for c in heat.get("concepts", [])
                    if c.get("in_today") or c.get("hot_type") in ("持续发酵", "新兴", "衰减", "退潮")
                ][:30],
            }
        except Exception:
            result["concept_heat"] = None
    else:
        result["concept_heat"] = None

    # 7f. 池内 hot_type 分布
    result["pool_hot_type_dist"] = dict(Counter(
        e.get("hot_type") for e in entries if e.get("hot_type")).most_common())

    # 7g. 自选股(watch=True)完整数据
    held_entries = [e for e in entries if e.get("watch")]
    result["watchlist"] = [{
        "symbol": e.get("symbol"),
        "watch": bool(e.get("watch")),
        "name": e.get("name"),
        "stage": e.get("stage"),
        "score": e.get("score"),
        "rating": e.get("rating"),
        "hot_type": e.get("hot_type", ""),
        "signals": e.get("signals", []),
        "trend": e.get("trend"),
        "resistance": e.get("resistance"),
        "support": e.get("support"),
        "buy_point": e.get("buy_point"),
        "sell_hint": e.get("sell_hint"),
        "position": e.get("position"),
        "buy_level": e.get("buy_level"),
        "stop_level": e.get("stop_level"),
        "tp_level": e.get("tp_level"),
        "entered_price": e.get("entered_price"),
        "entered_date": e.get("entered_date"),
        "price_entry": e.get("price_entry"),
        "highest_since_entry": e.get("highest_since_entry"),
        "concepts": e.get("concepts", []),
    } for e in held_entries]

    return _json_ok(result)


def _load_pool():
    pool_file = os.path.join(BASE_DIR, "data", "stock_pool.json")
    if os.path.exists(pool_file):
        with open(pool_file, encoding="utf-8") as f:
            return json.load(f)
    return {"entries": []}
