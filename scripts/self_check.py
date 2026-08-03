#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每日自检 + 自我进化 (StockAnalysis Self-Check & Self-Evolution)

由 Windows 任务计划 StockSelfCheck 在「交易日 12:00」触发(不依赖调度 daemon 生死)。

自检四件事(对应需求):
  1. 推送任务逻辑 / 环境是否有 BUG   —— 校验 TASKS 命令键/脚本存在、扫描调度日志异常、
                                        标记已知风险(update_klines max-stale 已收紧为2)。
  2. K线是否联网更新                —— 扫描股票池 active 标的 K 线最后交易日, 统计滞后;
                                        发现滞后超阈值 → 联网强制补刷(自进化修复)。
  3. 热点板块及成分股是否联网跟踪    —— 实时调 concept_rank_sina + fetch_concept_stocks_sina
                                        验证联网正常并报告当前 Top 热点。
  4. 每日股票池分析                  —— 加载 stock_pool, 重算形态/评分, 复算 S3 高胜率共振
                                        门控(胜率≥60% & 金叉), 输出评级分布与入选数。

成功/失败总结 → 写入 data/self_check_history.json(跨日"记忆"), 安全范围内自动修复,
并在数据/self_check_config.json 中自适应微调阈值(自进化)。最后企微推送摘要+HTML报告。

退出码: 0=正常(含"无可修复项"也算正常); 非交易日提前退出返回 0。
"""
import os
import sys
import json
import time
import datetime
import tempfile
import subprocess

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(BASE_DIR, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "self_check_history.json")
CONFIG_PATH = os.path.join(DATA_DIR, "self_check_config.json")
KL_DIR = os.path.join(DATA_DIR, "klines")
SCHEDULER_LOG = os.path.join(DATA_DIR, "scheduler.log")

# ---------------------------------------------------------------------------
# 交易日判断(内联, 避免依赖 daemon 进程; 与 scheduler.py 保持一致)
# ---------------------------------------------------------------------------
HOLIDAYS = {
    "2026-01-01", "2026-01-02",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23", "2026-02-24",
    "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21", "2026-06-22",
    "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07",
    "2026-10-08",
    "2026-12-25",
}


def is_trading_day(d: datetime.date) -> bool:
    if d.weekday() >= 5:
        return False
    if d.strftime("%Y-%m-%d") in HOLIDAYS:
        return False
    return True


# ---------------------------------------------------------------------------
# 配置(自进化写入)
# ---------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "kline_refresh_max_age_days": 2,   # 池K线滞后超过该自然日 → 午间强制补刷
    "highwr_min_winrate": 0.60,        # S3 门控胜率阈值(仅用于自检查报告对比, 不改策略源码)
    "highwr_min_winrate_warn": 0.55,   # 池中存在 >=该胜率 且金叉 但 0 入选 → 提示降阈
    "history_keep_days": 60,
    "version": 1,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            cfg.update(json.load(open(CONFIG_PATH, encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_config(cfg: dict):
    try:
        json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 报告收集
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.sections = []          # [(title, ok, lines, metrics)]
        self.issues = []            # 发现的问题(文本)
        self.fixes = []             # 已应用的修复(文本)
        self.evolutions = []        # 自我进化动作/建议(文本)
        self.card_lines = []        # 企微摘要卡片行

    def add(self, title, ok, lines, metrics=None):
        self.sections.append((title, ok, lines, metrics or {}))
        tag = "✅" if ok else "⚠️"
        self.card_lines.append(f"{tag} {title}")
        for ln in lines[:4]:
            self.card_lines.append(f"   {ln}")

    def issue(self, text):
        self.issues.append(text)
        self.card_lines.append(f"  🔴 问题: {text[:90]}")

    def fix(self, text):
        self.fixes.append(text)
        self.card_lines.append(f"  🟢 修复: {text[:90]}")

    def evolve(self, text):
        self.evolutions.append(text)
        self.card_lines.append(f"  🧬 进化: {text[:90]}")


def _today() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1) 环境 / 推送逻辑 BUG 检查
# ---------------------------------------------------------------------------
def check_push_logic(rep: Report):
    lines = []
    ok = True
    # 1.1 依赖
    deps = []
    for mod in ("akshare", "requests", "yaml", "jinja2"):
        try:
            __import__(mod)
            deps.append(f"{mod}:OK")
        except Exception as e:
            deps.append(f"{mod}:缺失({e})")
            ok = False
    lines.append("依赖: " + "  ".join(deps))

    # 1.2 企微推送
    try:
        from notify.wecom_bot import is_enabled
        wecom = is_enabled()
        lines.append("企微推送: " + ("已启用" if wecom else "未启用(报告将只落盘不推送)"))
        if not wecom:
            ok = False
    except Exception as e:
        lines.append(f"企微推送: 不可用 ({e})")
        ok = False

    # 1.3 TASKS 命令键 / 脚本存在性
    try:
        from core.commands import COMMANDS
        expected = ["update_klines", "macro", "daily_hotspot", "intraday_watch", "s14",
                    "pool_refresh", "review", "decision", "weekly_hotspot", "cookie",
                    "concept_list", "concept_track"]
        missing = [k for k in expected if k not in COMMANDS]
        if missing:
            lines.append(f"⚠️ 调度命令键缺失: {missing}")
            ok = False
        else:
            lines.append("调度命令键: 全部存在(12项)")

        # 脚本文件存在性
        bad_files = []
        for k, c in COMMANDS.items():
            a = c.get("args", [])
            if a and a[0].endswith(".py"):
                p = os.path.join(BASE_DIR, a[0])
                if not os.path.exists(p):
                    bad_files.append(a[0])
        if bad_files:
            lines.append(f"⚠️ 命令指向脚本不存在: {bad_files}")
            ok = False
        else:
            lines.append("命令脚本文件: 全部存在")
    except Exception as e:
        lines.append(f"命令注册表检查失败: {e}")
        ok = False

    # 1.4 扫描调度日志异常(最近 300 行, 按时间+类型分类, 区分"当前真错误"与"已知已缓解")
    err_hits = []
    if os.path.exists(SCHEDULER_LOG):
        try:
            with open(SCHEDULER_LOG, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-300:]
            cutoff = datetime.datetime.now() - datetime.timedelta(days=2)
            for ln in tail:
                s = ln.strip()
                # 仅匹配真正的错误标记, 避免"失败/超时"等正常信息行误判
                if not any(k in s for k in ("✗", "Traceback", "Exception", "errcode=", "Error:")):
                    continue
                # 解析行首时间 [YYYY-MM-DD HH:MM:SS]
                recent = True
                try:
                    ts = datetime.datetime.strptime(s[1:20], "%Y-%m-%d %H:%M:%S")
                    recent = ts >= cutoff
                except Exception:
                    pass
                if not recent:
                    continue
                # 类型分类
                if "ChunkedEncodingError" in s or "Connection broken" in s or "ConnectionError" in s:
                    kind = "mitigated"   # akshare 网络断流 → 已用本地缓存+continue_on_error缓解
                elif "93006" in s:
                    kind = "mitigated"   # 旧 chatid 错误 → 已修复
                else:
                    kind = "real"
                err_hits.append((kind, s[-140:]))
        except Exception:
            pass
    real_errs = [h for k, h in err_hits if k == "real"]
    mit_errs = [h for k, h in err_hits if k == "mitigated"]
    if real_errs:
        lines.append(f"调度日志(近2天)命中当前错误 {len(real_errs)} 处:")
        for h in real_errs[-5:]:
            lines.append("   " + h)
        ok = False
        rep.issue(f"调度日志近期存在未缓解错误 {len(real_errs)} 处(见①明细)")
    else:
        lines.append("调度日志(近2天): 无未缓解错误")
    if mit_errs:
        lines.append(f"近期已知已缓解异常 {len(mit_errs)} 处(如 akshare 网络断流/旧chatid, 已有缓存+continue_on_error兜底)")

    # 1.5 已知风险: update_klines max-stale 已收紧
    try:
        from core.commands import COMMANDS as C2
        args = C2["update_klines"]["args"]
        idx = args.index("--max-stale")
        ms = int(args[idx + 1])
        if ms <= 2:
            lines.append(f"K线刷新阈值 max-stale={ms}(已收紧, 旧值7 → 修复旧数据问题)")
        else:
            lines.append(f"⚠️ K线刷新阈值仍为 max-stale={ms}(建议≤2)")
            ok = False
    except Exception:
        pass

    rep.add("① 推送任务逻辑/环境检查", ok, lines)
    if not ok:
        rep.issue("推送链路或环境存在异常(见①明细)")


# ---------------------------------------------------------------------------
# 2) K线联网更新检查 + 自进化补刷
# ---------------------------------------------------------------------------
def _pool_active_symbols():
    """返回股票池中需要保持 K线新鲜的 active 标的(排除已退出/停牌/次新)。"""
    try:
        from plans.stock_pool import load_pool
        pool = load_pool()
        syms = []
        for e in pool.get("entries", []):
            if e.get("exited"):
                continue
            syms.append(str(e["symbol"]))
        return syms
    except Exception:
        return []


def check_klines(rep: Report, cfg: dict):
    from plans.breakout_scan import _kl_load
    threshold = cfg.get("kline_refresh_max_age_days", 2)
    lines = []
    today = datetime.datetime.now()

    # 全市场目录整体新鲜度(最新 mtime)
    if os.path.isdir(KL_DIR):
        files = [f for f in os.listdir(KL_DIR) if f.startswith("kl_") and f.endswith(".json")]
        if files:
            newest = max(os.path.getmtime(os.path.join(KL_DIR, f)) for f in files)
            age_h = (today.timestamp() - newest) / 3600
            lines.append(f"K线目录: {len(files)} 文件, 最新写入 {age_h:.1f} 小时前")
        else:
            lines.append("K线目录: 空")
    else:
        lines.append("K线目录: 不存在")
        rep.add("② K线联网更新检查", False, lines)
        rep.issue("K线目录缺失")
        return

    # 池内标的滞后统计
    syms = _pool_active_symbols()
    if not syms:
        lines.append("股票池: 无 active 标的, 跳过滞后检查")
        rep.add("② K线联网更新检查", True, lines)
        return

    stale = []
    ages = []
    for s in syms:
        try:
            kl, last_date, sa, short = _kl_load(s)
        except Exception:
            kl, last_date, sa, short = None, None, False, False
        if not kl or sa or short:
            continue  # 退市/停牌/次新不参与
        if not last_date:
            stale.append((s, 9999))
            continue
        try:
            ld = datetime.datetime.strptime(last_date, "%Y-%m-%d")
            age = (today - ld).days
        except Exception:
            age = 9999
        ages.append(age)
        if age > threshold:
            stale.append((s, age))

    if ages:
        lines.append(f"池 active 标的 {len(ages)} 只, K线滞后中位 {sorted(ages)[len(ages)//2]} 天, "
                     f"最大 {max(ages)} 天, 超阈值(>{threshold}天) {len(stale)} 只")
    else:
        lines.append("池 active 标的 K线: 无可统计项")

    ok = True
    if stale:
        ok = False
        rep.issue(f"{len(stale)} 只池标的 K线滞后超 {threshold} 天(旧数据风险)")
        lines.append("滞后标的(前10): " + ", ".join(f"{s}({a}d)" for s, a in stale[:10]))
        # —— 自进化修复: 联网强制补刷 ——
        try:
            codes = [s for s, _ in stale]
            tf = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            tf.write("\n".join(codes))
            tf.close()
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.run(
                [sys.executable, "scripts/fetch_all_klines.py",
                 "--symbols-file", tf.name, "--force", "--max-stale", "0", "--workers", "6"],
                cwd=BASE_DIR, capture_output=True, text=True, timeout=1800,
                env=env, encoding="utf-8", errors="replace")
            out = (proc.stdout or "") + (proc.stderr or "")
            # 解析 "新抓N"
            import re
            m = re.search(r"新抓(\d+)", out)
            fetched = m.group(1) if m else "?"
            # 复验
            still = 0
            for s in codes:
                try:
                    _, last_date2, sa2, short2 = _kl_load(s)
                except Exception:
                    last_date2, sa2, short2 = None, False, False
                if last_date2:
                    try:
                        age2 = (today - datetime.datetime.strptime(last_date2, "%Y-%m-%d")).days
                    except Exception:
                        age2 = 9999
                    if age2 > threshold and not sa2 and not short2:
                        still += 1
                else:
                    still += 1
            lines.append(f"自进化修复: 联网补刷 {len(codes)} 只 → 新抓 {fetched} 只, 复验仍滞后 {still} 只")
            rep.fix(f"联网强制补刷 {len(codes)} 只滞后K线(原最大滞后 {max(a for _,a in stale)} 天)")
            if still > 0:
                lines.append(f"⚠️ 仍有 {still} 只补刷后判定滞后(可能停牌/数据缺失)")
        except Exception as e:
            lines.append(f"自进化补刷失败: {e}")
            rep.issue(f"K线补刷异常: {e}")
    else:
        lines.append(f"✅ 池标的 K线均 <= {threshold} 天, 新鲜度达标(联网更新有效)")

    rep.add("② K线联网更新检查", ok, lines)


# ---------------------------------------------------------------------------
# 3) 热点板块及成分股联网跟踪检查
# ---------------------------------------------------------------------------
def check_hotspots(rep: Report):
    lines = []
    ok = True
    try:
        from collectors.concept import concept_rank_sina, fetch_concept_stocks_sina
        t0 = time.time()
        concepts = concept_rank_sina(limit=12)
        dt = time.time() - t0
        if not concepts:
            lines.append("⚠️ 新浪概念榜单返回空(联网失败/接口变更)")
            ok = False
            rep.issue("热点板块联网获取失败")
        else:
            lines.append(f"热点板块联网正常: 取回 {len(concepts)} 个(耗时 {dt:.2f}s)")
            top = concepts[:5]
            lines.append("当前 Top5 热点: " + "  ".join(
                f"{c['name']}({c.get('change_pct',0):+.2f}%)" for c in top))
            # 成分股跟踪校验: Top3 各取成分股
            track_ok = 0
            for c in concepts[:3]:
                try:
                    stocks = fetch_concept_stocks_sina(c["code"], c["name"], limit=20)
                    if stocks:
                        track_ok += 1
                        lines.append(f"  ▸ {c['name']}: 成分股 {len(stocks)} 只, "
                                     f"领涨 {stocks[0].get('name','')}({stocks[0].get('change_pct',0):+.2f}%)")
                except Exception as e:
                    lines.append(f"  ▸ {c['name']}: 成分股获取失败 {e}")
            if track_ok < 3:
                ok = False
                rep.issue("部分热点板块成分股跟踪失败")
            else:
                lines.append(f"成分股跟踪: Top3 全部可联网拉取({track_ok}/3) ✅")
    except Exception as e:
        lines.append(f"⚠️ 热点检查异常: {e}")
        ok = False
        rep.issue(f"热点检查异常: {e}")

    # 概念缓存是否存在且当日
    concept_dir = os.path.join(DATA_DIR, "concepts")
    if os.path.isdir(concept_dir):
        fs = [f for f in os.listdir(concept_dir) if f.endswith(".json")]
        if fs:
            newest = max(os.path.getmtime(os.path.join(concept_dir, f)) for f in fs)
            age_h = (datetime.datetime.now().timestamp() - newest) / 3600
            lines.append(f"概念缓存目录: {len(fs)} 文件, 最新 {age_h:.1f} 小时前")
        else:
            lines.append("概念缓存目录: 空(盘后 concept_track 将生成)")
    else:
        lines.append("概念缓存目录: 不存在")

    rep.add("③ 热点板块/成分股跟踪检查", ok, lines)


# ---------------------------------------------------------------------------
# 4) 每日股票池分析 + S3 高胜率共振门控复算
# ---------------------------------------------------------------------------
def analyze_pool(rep: Report, cfg: dict):
    from plans.stock_pool import load_pool
    from plans.breakout_scan import _kline_cached
    from analysis.breakout import classify_stage

    pool = load_pool()
    entries = pool.get("entries", [])
    lines = []
    if not entries:
        lines.append("股票池为空, 无标的可分析/监控(盘后播报未运行或蒸馏未产出)")
        rep.issue("股票池为空: 无标的可监控(检查盘后播报/周热点是否正常运行)")
        rep.add("④ 每日股票池分析", True, lines)
        return

    n_watch = sum(1 for e in entries if e.get("watch"))
    n_exited = sum(1 for e in entries if e.get("exited"))
    n_active = len(entries) - n_exited
    lines.append(f"股票池: 共 {len(entries)} 只 (自选 {n_watch} / 已退出 {n_exited} / active {n_active})")

    # 评级分布(用池中已存字段, 避免全部重算)
    rating_order = {"推荐": 0, "关注": 1, "观察": 2, "暂避": 3}
    dist = {}
    for e in entries:
        if e.get("exited"):
            continue
        r = e.get("rating") or "未知"
        dist[r] = dist.get(r, 0) + 1
    lines.append("评级分布: " + "  ".join(f"{k}={v}" for k, v in sorted(
        dist.items(), key=lambda x: rating_order.get(x[0], 9))))

    # 复算 S3 高胜率共振门控(用最新K线): 胜率>=60% 且 (MACD或KDJ金叉)
    min_wr = cfg.get("highwr_min_winrate", 0.60)
    warn_wr = cfg.get("highwr_min_winrate_warn", 0.55)
    try:
        from plans.weekly_hotspot import _estimate_win_rate
    except Exception:
        _estimate_win_rate = None

    passed, near, checked = [], [], 0
    for e in entries:
        if e.get("exited"):
            continue
        sym = str(e["symbol"])
        try:
            kl = _kline_cached(sym)
            if not kl or len(kl) < 40:
                continue
            closes = [float(b["close"]) for b in kl]
            highs = [float(b["high"]) for b in kl]
            lows = [float(b["low"]) for b in kl]
            vols = [float(b["volume"]) for b in kl]
            res = classify_stage(closes, highs, lows, vols, closes[-1])
            det = res.get("details", {})
            signals = res.get("signals", [])
            stage = res.get("stage", "unknown")
            gc = bool(det.get("macd_gc") or det.get("kdj_gc"))
            wr = _estimate_win_rate(stage, signals) if _estimate_win_rate else 0
            checked += 1
            if wr >= min_wr and gc:
                passed.append((sym, e.get("name", sym), round(wr, 2), stage))
            elif wr >= warn_wr and gc:
                near.append((sym, e.get("name", sym), round(wr, 2), stage))
        except Exception:
            continue

    lines.append(f"S3高胜率共振复算(基于最新K线): 检查 {checked} 只, "
                 f"胜率≥{min_wr:.0%}且金叉 入选 {len(passed)} 只, "
                 f"≥{warn_wr:.0%}且金叉(临界) {len(near)} 只")
    if passed:
        lines.append("  入选: " + "  ".join(f"{s}({n},{wr},{st})" for s, n, wr, st in passed[:15]))
    if near:
        lines.append("  临界(建议阈值降至55%即入选): " + "  ".join(
            f"{s}({n},{wr})" for s, n, wr, st in near[:10]))

    ok = True
    if len(passed) == 0 and len(near) > 0:
        rep.evolve(f"池中存在 {len(near)} 只 胜率≥55%且金叉 但被60%阈值卡掉 → "
                   f"建议将 S3 胜率阈值由 0.60 降至 0.55 (需人工确认, 不改源码)")
    if len(passed) == 0 and checked > 0:
        lines.append("⚠️ 当前 S3 入选为 0, 可能因 K线滞后或市场无共振; 详见历史趋势")
        rep.issue("S3 高胜率共振当日 0 入选")

    rep.add("④ 每日股票池分析", ok, lines,
            {"pool_total": len(entries), "active": n_active, "s3_passed": len(passed),
             "s3_near": len(near), "checked": checked})


# ---------------------------------------------------------------------------
# 自我进化: 刷新股票池数值关卡(对齐最新K线) + 配置自适应
# ---------------------------------------------------------------------------
def self_evolve(rep: Report, cfg: dict, history: list):
    lines = []
    # 5.1 刷新股票池关卡(对齐午间最新K线) —— 安全, 等同每日收盘refresh
    try:
        from plans.stock_pool import refresh_stock_pool
        n = refresh_stock_pool(verbose=False)
        lines.append(f"股票池数值关卡已用最新K线重算: 更新 {n} 只(对齐午间行情)")
        if n > 0:
            rep.fix(f"refresh_stock_pool 重算 {n} 只关卡(对齐最新K线)")
    except Exception as e:
        lines.append(f"股票池刷新失败: {e}")
        rep.issue(f"股票池refresh异常: {e}")

    # 5.2 配置自适应: 若近期 K线补刷频繁, 进一步收紧阈值(自进化)
    recent = [h for h in history[-7:] if h.get("kline_stale_fixed", 0) > 0]
    if len(recent) >= 3 and cfg.get("kline_refresh_max_age_days", 2) > 1:
        cfg["kline_refresh_max_age_days"] = max(1, cfg["kline_refresh_max_age_days"] - 1)
        lines.append(f"自进化: 近7天多次补刷 → K线滞后阈值收紧至 {cfg['kline_refresh_max_age_days']} 天")
        rep.evolve(f"K线滞后阈值自适应收紧至 {cfg['kline_refresh_max_age_days']} 天")
    else:
        lines.append(f"自进化: K线滞后阈值维持 {cfg.get('kline_refresh_max_age_days')} 天(无需收紧)")

    rep.add("⑤ 自我进化(自动修复+自适应)", True, lines)


# ---------------------------------------------------------------------------
# 历史记录
# ---------------------------------------------------------------------------
def load_history() -> list:
    if os.path.exists(HISTORY_PATH):
        try:
            return json.load(open(HISTORY_PATH, encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(history: list, cfg: dict):
    try:
        keep = cfg.get("history_keep_days", 60)
        history = history[-keep:]
        json.dump(history, open(HISTORY_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 汇总 + 推送
# ---------------------------------------------------------------------------
def build_card(rep: Report) -> str:
    today = _today()
    head = (f"🩺 每日自检与自我进化报告\n"
            f"📅 {today} (交易日)\n"
            f"发现问题 {len(rep.issues)} 项 / 已修复 {len(rep.fixes)} 项 / 进化动作 {len(rep.evolutions)} 项\n\n")
    body = "\n".join(rep.card_lines)
    return head + body


def push_report(card_text: str, html_path: str):
    try:
        from notify.wecom_bot import is_enabled, push_markdown_via_bot, send_file_via_bot
    except Exception:
        print("[SELF-CHECK] 企微模块不可用, 仅落盘报告")
        return
    if not is_enabled():
        print("[SELF-CHECK] 企微未启用, 仅落盘报告")
        return
    try:
        push_markdown_via_bot(card_text)
    except Exception as e:
        print(f"[SELF-CHECK] 摘要推送失败: {e}")
    if html_path and os.path.exists(html_path):
        try:
            send_file_via_bot(html_path)
        except Exception as e:
            print(f"[SELF-CHECK] HTML 推送失败: {e}")


def render_html(rep: Report) -> str:
    try:
        from core.html_renderer import render
        import re as _re
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = _re.sub(r"\W+", "_", "self_check")[:40]
        body = []
        for title, ok, lines, metrics in rep.sections:
            body.append(f"### {'✅' if ok else '⚠️'} {title}")
            for ln in lines:
                body.append(ln)
            body.append("")
        if rep.issues:
            body.append("### 🔴 发现问题")
            for x in rep.issues:
                body.append(" - " + x)
            body.append("")
        if rep.fixes:
            body.append("### 🟢 已修复")
            for x in rep.fixes:
                body.append(" - " + x)
            body.append("")
        if rep.evolutions:
            body.append("### 🧬 自我进化")
            for x in rep.evolutions:
                body.append(" - " + x)
            body.append("")
        data = {
            "title": "每日自检与自我进化",
            "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": build_card(rep),
            "body": "\n".join(body),
        }
        os.makedirs(os.path.join(DATA_DIR, "notify_html"), exist_ok=True)
        return render(data, "task_report", output_dir=os.path.join(DATA_DIR, "notify_html"),
                      filename=f"self_check_{ts}.html")
    except Exception as e:
        print(f"[SELF-CHECK] HTML 生成失败: {e}")
        return None


def main():
    today = datetime.date.today()
    if not is_trading_day(today):
        print(f"[{_today()}] 非交易日, 跳过自检")
        return

    print(f"[{_today()} 12:00] 开始每日自检与自我进化 ...", flush=True)
    cfg = load_config()
    history = load_history()
    rep = Report()

    check_push_logic(rep)
    check_klines(rep, cfg)
    check_hotspots(rep)
    analyze_pool(rep, cfg)
    self_evolve(rep, cfg, history)

    # 统计本次补刷数量(从 fixes 文本粗略计数) → 写入历史
    fixed_kline = sum(1 for f in rep.fixes if "滞后K线" in f)

    # 汇总打印
    print("\n" + "=" * 70)
    print(build_card(rep))
    print("=" * 70)

    # HTML + 推送
    html_path = render_html(rep)
    if html_path:
        print(f"HTML_REPORT:{html_path}")
    push_report(build_card(rep), html_path)

    # 写历史
    summary = {
        "date": _today(),
        "issues": len(rep.issues),
        "fixes": len(rep.fixes),
        "evolutions": len(rep.evolutions),
        "kline_stale_fixed": fixed_kline,
        "sections": [
            {"title": t, "ok": ok, "metrics": m} for t, ok, _, m in rep.sections
        ],
    }
    history.append(summary)
    save_history(history, cfg)
    save_config(cfg)

    print(f"[{_today()}] 自检完成: 问题 {len(rep.issues)} / 修复 {len(rep.fixes)} / 进化 {len(rep.evolutions)}", flush=True)


if __name__ == "__main__":
    main()
