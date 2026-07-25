#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""概念热度追踪引擎 — 从历史榜单快照中提取热点生命周期信号.

核心问题: 热点分三类——一日游、持续发酵、波段上涨。如何跟踪？如何分辨？
本引擎通过跨日榜单对比，为每个概念打上「热度标签」，让选股从"今天扫出来什么"
升级为"这个热点持续多久了，处于什么生命周期阶段"。

热度标签 (hot_type):
  🆕 新兴    = 连续 1-2 天上榜且排名急升 (新热点刚起)
  🔥 持续发酵 = 连续 3+ 天上榜且排名/涨幅稳定或上升 (真热点)
  📈 波段上涨 = 非连续但有多次上榜记录 (周期性反复)
  ⚠️ 衰减    = 连续 3+ 天但排名/涨幅递减 (热点在退)
  📉 退潮    = 之前连续上榜但今天消失 (热点已过)
  💤 一日游  = 仅上榜 1 天且今天已不在榜 (昙花一现)

用法:
  python plans/concept_tracker.py           # 打印热度报告
  python plans/concept_tracker.py --json     # 结构化输出
  python plans/concept_tracker.py --save     # 写入 data/concepts/concept_heat.json
"""
import os
import sys
import json
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

CONCEPTS_DIR = os.path.join(BASE_DIR, "data", "concepts")
HEAT_FILE = os.path.join(CONCEPTS_DIR, "concept_heat.json")

# ── 热度分类 ──
HOT_TYPES = {
    "新兴": "🆕",       # 1-2 天连续, 排名上升
    "持续发酵": "🔥",   # 3+ 天连续, 稳定/上升
    "波段上涨": "📈",   # 非连续但反复出现
    "衰减": "⚠️",       # 连续但排名/涨幅递减
    "退潮": "📉",       # 之前热门, 今天消失
    "一日游": "💤",     # 仅 1 天出现就消失
    "首次出现": "🌟",   # 今天首次上榜, 无历史
}


def _load_snapshot(filepath):
    """加载单个 board_pool 快照文件."""
    try:
        with open(filepath, encoding="utf-8") as f:
            data = json.load(f)
        pool = data.get("pool", [])
        # 按 change_pct 排序 (榜内排名, 越大越靠前)
        pool_sorted = sorted(pool, key=lambda x: x.get("change_pct", 0), reverse=True)
        return pool_sorted
    except Exception:
        return []


def _extract_date_from_filename(fname):
    """从 board_pool_YYYY-MM-DD.json 提取日期."""
    # 格式: board_pool_2026-07-13.json 或 board_pool.json(=today)
    if fname == "board_pool.json":
        return None  # 当天
    parts = fname.replace("board_pool_", "").replace(".json", "")
    try:
        return datetime.strptime(parts, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def load_all_snapshots():
    """加载所有历史概念榜单快照, 返回 {date: [{name, bk_code, change_pct, rank}]}."""
    snapshots = {}
    if not os.path.exists(CONCEPTS_DIR):
        return snapshots

    for fname in os.listdir(CONCEPTS_DIR):
        if not fname.startswith("board_pool") or not fname.endswith(".json"):
            continue
        date_str = _extract_date_from_filename(fname)
        pool = _load_snapshot(os.path.join(CONCEPTS_DIR, fname))
        if not pool:
            continue
        # 为每条记录添加排名序号
        for i, item in enumerate(pool):
            item["rank"] = i + 1
        if date_str:
            snapshots[date_str] = pool
        else:
            # board_pool.json = 当天, 用今天日期
            today = datetime.now().strftime("%Y-%m-%d")
            # 如果 fetched_at 字段有值, 用它
            try:
                with open(os.path.join(CONCEPTS_DIR, fname), encoding="utf-8") as f:
                    meta = json.load(f)
                fa = meta.get("fetched_at", "")
                if fa:
                    today = fa[:10]
            except Exception:
                pass
            snapshots[today] = pool

    # 按日期排序
    return dict(sorted(snapshots.items()))


def classify_concept_heat(name, history_dates, snapshots):
    """为单个概念计算热度指标.

    Args:
        name: 概念名称
        history_dates: 所有快照日期列表(升序)
        snapshots: {date: [{name, rank, change_pct}]}

    Returns:
        dict with consecutive_days, total_appearances, hot_type, etc.
    """
    # 找出该概念在哪些天出现
    appearances = []  # [(date, rank, change_pct)]
    for d in history_dates:
        pool = snapshots.get(d, [])
        for item in pool:
            if item.get("name") == name:
                appearances.append((d, item.get("rank", 999), item.get("change_pct", 0)))
                break

    if not appearances:
        return {
            "name": name,
            "hot_type": "首次出现",
            "consecutive_days": 0,
            "total_appearances": 0,
            "cumulative_pct": 0,
            "avg_pct": 0,
            "pct_trend": "unknown",
            "rank_trend": "unknown",
            "peak_rank": None,
            "best_rank": None,
            "pct_list": [],
            "rank_list": [],
            "first_seen": None,
            "last_seen": None,
        }

    # 连续出现天数 (从最近一次出现往回数, 中间断了就停止)
    today_str = history_dates[-1] if history_dates else ""
    consecutive = 0
    for i in range(len(appearances) - 1, -1, -1):
        d, _, _ = appearances[i]
        if i == len(appearances) - 1:
            consecutive = 1
            continue
        # 检查前一个出现日是否紧邻 (允许间隔≤3天, 跨周末)
        prev_d = appearances[i][0]
        next_d = appearances[i + 1][0]
        try:
            prev_dt = datetime.strptime(prev_d, "%Y-%m-%d")
            next_dt = datetime.strptime(next_d, "%Y-%m-%d")
            gap = (next_dt - prev_dt).days
            if gap <= 3:  # 跨周末(周五→周一=3天)仍算连续
                consecutive += 1
            else:
                break
        except ValueError:
            break

    # 近期排名/涨幅列表 (连续段内的)
    recent = appearances[-consecutive:] if consecutive > 0 else appearances[-1:]
    pct_list = [p for _, _, p in recent]
    rank_list = [r for _, r, _ in recent]

    # 涨幅趋势: 后半段均值 vs 前半段均值
    pct_trend = "stable"
    if len(pct_list) >= 4:
        mid = len(pct_list) // 2
        early_avg = sum(pct_list[:mid]) / mid if mid > 0 else 0
        late_avg = sum(pct_list[mid:]) / (len(pct_list) - mid) if len(pct_list) - mid > 0 else 0
        if late_avg > early_avg * 1.3:
            pct_trend = "accelerating"
        elif late_avg < early_avg * 0.7:
            pct_trend = "declining"

    # 排名趋势: 后半段最佳排名 vs 前半段最佳排名
    rank_trend = "stable"
    if len(rank_list) >= 4:
        mid = len(rank_list) // 2
        early_best = min(rank_list[:mid])
        late_best = min(rank_list[mid:])
        if late_best < early_best - 3:
            rank_trend = "rising"  # 排名数字越小越好
        elif late_best > early_best + 3:
            rank_trend = "falling"

    cumulative_pct = sum(pct_list)
    avg_pct = sum(pct_list) / len(pct_list) if pct_list else 0
    best_rank = min(rank_list) if rank_list else None
    peak_rank = best_rank
    first_seen = appearances[0][0]
    last_seen = appearances[-1][0]

    # 是否在当天榜单中
    in_today = any(d == today_str for d, _, _ in appearances)

    # ── 热度分类 ──
    total_app = len(appearances)
    hot_type = "首次出现"

    if not in_today:
        # 今天不在榜: 退潮 or 一日游
        if consecutive >= 3:
            hot_type = "退潮"
        elif total_app == 1:
            hot_type = "一日游"
        else:
            hot_type = "退潮"
    elif consecutive <= 2 and total_app <= 2:
        # 连续 1-2 天, 新兴
        if rank_trend == "rising" or pct_trend == "accelerating":
            hot_type = "新兴"
        else:
            hot_type = "新兴"  # 1-2 天都算新兴
    elif consecutive >= 3:
        # 连续 3+ 天: 持续发酵 or 衰减
        if pct_trend == "declining" and rank_trend == "falling":
            hot_type = "衰减"
        elif pct_trend == "accelerating" or rank_trend == "rising":
            hot_type = "持续发酵"
        else:
            hot_type = "持续发酵"  # 连续3+天默认持续
    elif total_app >= 3 and consecutive < 3:
        # 非连续但多次出现: 波段上涨
        hot_type = "波段上涨"

    return {
        "name": name,
        "hot_type": hot_type,
        "consecutive_days": consecutive,
        "total_appearances": total_app,
        "cumulative_pct": round(cumulative_pct, 2),
        "avg_pct": round(avg_pct, 2),
        "pct_trend": pct_trend,
        "rank_trend": rank_trend,
        "peak_rank": peak_rank,
        "best_rank": best_rank,
        "pct_list": [round(p, 2) for p in pct_list],
        "rank_list": rank_list,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "in_today": in_today,
    }


def compute_heat_report():
    """计算所有概念的热度报告."""
    snapshots = load_all_snapshots()
    if not snapshots:
        return {"date": _today(), "concepts": [], "summary": {}}

    dates = list(snapshots.keys())
    today_str = dates[-1]
    today_pool = snapshots[today_str]

    # 收集所有概念名 (今天+历史)
    all_names = set()
    for d, pool in snapshots.items():
        for item in pool:
            all_names.add(item.get("name"))

    # 为每个概念计算热度
    concepts = []
    for name in sorted(all_names):
        heat = classify_concept_heat(name, dates, snapshots)
        # 附加今天的数据
        for item in today_pool:
            if item.get("name") == name:
                heat["today_pct"] = item.get("change_pct", 0)
                heat["today_rank"] = item.get("rank", 0)
                heat["bk_code"] = item.get("bk_code", "")
                heat["leader"] = item.get("leader", "")
                heat["leader_pct"] = item.get("leader_pct", 0)
                break
        concepts.append(heat)

    # 按热度优先级排序: 持续发酵 > 新兴 > 波段上涨 > 衰减 > 退潮 > 一日游 > 首次出现
    type_order = {"持续发酵": 0, "新兴": 1, "波段上涨": 2, "衰减": 3, "退潮": 4, "一日游": 5, "首次出现": 6}
    concepts.sort(key=lambda x: (type_order.get(x["hot_type"], 9), -x.get("consecutive_days", 0), -x.get("avg_pct", 0)))

    # 统计摘要
    summary = defaultdict(int)
    for c in concepts:
        summary[c["hot_type"]] += 1

    return {
        "date": today_str,
        "snapshot_count": len(dates),
        "concepts": concepts,
        "summary": dict(summary),
    }


def get_concept_heat_map():
    """返回 {概念名: hot_type} 映射, 供 stock_pool / decision_engine 使用."""
    report = compute_heat_report()
    return {c["name"]: c["hot_type"] for c in report["concepts"]}


def get_stock_hot_type(concepts_list):
    """根据股票所属概念, 返回最优先的热度标签.

    优先级: 持续发酵 > 新兴 > 波段上涨 > 衰减 > 退潮 > 一日游 > 首次出现
    取概念中优先级最高的标签。
    """
    if not concepts_list:
        return "无概念"
    if isinstance(concepts_list, str):
        concepts_list = [concepts_list]

    heat_map = get_concept_heat_map()
    type_priority = {"持续发酵": 0, "新兴": 1, "波段上涨": 2, "衰减": 3, "退潮": 4, "一日游": 5, "首次出现": 6}

    best = "无概念"
    best_pri = 99
    for c in concepts_list:
        ht = heat_map.get(c, "首次出现")
        pri = type_priority.get(ht, 99)
        if pri < best_pri:
            best = ht
            best_pri = pri
    return best


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def format_report(report):
    """格式化热度报告为可读文本."""
    lines = []
    lines.append(f"📊 概念热度追踪报告 ({report['date']})")
    lines.append(f"   历史快照: {report['snapshot_count']} 天")
    lines.append("")

    summary = report.get("summary", {})
    parts = []
    for ht, cnt in sorted(summary.items(), key=lambda x: HOT_TYPES.get(x[0], "")):
        emoji = HOT_TYPES.get(ht, "?")
        parts.append(f"{emoji}{ht}:{cnt}")
    lines.append(f"   热度分布: {' | '.join(parts)}")
    lines.append("─" * 60)

    # 按热度分组展示
    for ht in ["持续发酵", "新兴", "波段上涨", "衰减", "退潮", "一日游", "首次出现"]:
        group = [c for c in report["concepts"] if c["hot_type"] == ht]
        if not group:
            continue
        emoji = HOT_TYPES.get(ht, "?")
        lines.append(f"\n{emoji} {ht} ({len(group)}个)")
        for c in group:
            days = c["consecutive_days"]
            cum = c["cumulative_pct"]
            avg = c["avg_pct"]
            pct_t = c["pct_trend"]
            rank_t = c["rank_trend"]
            best_r = c["best_rank"]
            first = c.get("first_seen", "")
            lines.append(
                f"  {c['name']}  连{days}天  累涨{cum:+.1f}%  "
                f"日均{avg:+.1f}%  排名#{best_r or '-'}  "
                f"涨幅{pct_t}/排名{rank_t}  首见{first}"
            )

    return "\n".join(lines)


def save_heat_report(report):
    """保存热度报告到 data/concepts/concept_heat.json."""
    os.makedirs(CONCEPTS_DIR, exist_ok=True)
    with open(HEAT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  热度报告已保存 → {HEAT_FILE}")


def save_daily_snapshot():
    """保存今天 board_pool.json 为历史快照 (供未来跨日对比).
    在每次概念扫描完成后调用。
    """
    src = os.path.join(CONCEPTS_DIR, "board_pool.json")
    if not os.path.exists(src):
        return False
    today = _today()
    dst = os.path.join(CONCEPTS_DIR, f"board_pool_{today}.json")
    if os.path.exists(dst):
        return True  # 已存在
    try:
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
        # 补充 fetched_at (如果缺失)
        if not data.get("fetched_at"):
            data["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  概念榜单快照已保存 → {dst}")
        return True
    except Exception as e:
        print(f"  保存快照失败: {e}")
        return False


if __name__ == "__main__":
    # Windows gkb 编码兼容
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description="概念热度追踪引擎")
    parser.add_argument("--json", action="store_true", help="JSON结构化输出")
    parser.add_argument("--save", action="store_true", help="保存到 concept_heat.json")
    parser.add_argument("--snapshot", action="store_true", help="保存今天榜单快照")
    args = parser.parse_args()

    if args.snapshot:
        save_daily_snapshot()

    report = compute_heat_report()

    if args.save:
        save_heat_report(report)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
