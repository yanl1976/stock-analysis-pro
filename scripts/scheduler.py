#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一定时执行计划 (唯一调度器)

集中定义所有定时任务: 操作内容 / 执行时间 / 间隔 / 是否仅交易日。
这是整个项目的「唯一」调度可信源 —— Windows 任务计划里只注册一个常驻任务
(install_scheduler.ps1), 由本脚本在后台常驻, 各任务的触发时刻全部由下方 TASKS 表控制。

=====================================================================
一、安装 (只需做一次)
=====================================================================
调度器本身是跨平台的常驻进程(python scripts/scheduler.py --daemon), 它会每 20 秒
巡检、在 TASKS 表定义的时刻触发任务。因此各平台只需解决"如何把它作为后台服务常驻
启动 + 崩溃自启"即可, 真正的计划全部写在 scheduler.py 的 TASKS 表里, 不在平台脚本中
分散定义, 便于统一维护。

---------------------------------------------------------------------
1.1 Windows —— 用 install_scheduler.ps1 (推荐)
---------------------------------------------------------------------
向 Windows「任务计划程序」注册一个常驻任务, 用户登录时自动启动
`python scripts/scheduler.py --daemon`, 并配置崩溃后 1 分钟内自动重启(最多 3 次)。

  # 以管理员身份打开 PowerShell, 在项目根目录执行:
  .\\install_scheduler.ps1 install      # 注册并立即启动常驻调度
  .\\install_scheduler.ps1 uninstall    # 卸载(停止并删除计划任务)
  .\\install_scheduler.ps1 status       # 查看计划任务运行状态

脚本会自动把项目根目录写入任务的工作目录, 因此各任务里的相对路径
(如 scripts/xxx.py) 都能正确解析。

---------------------------------------------------------------------
1.2 Ubuntu / Linux —— 用 systemd (推荐)
---------------------------------------------------------------------
创建一个 systemd 用户级(或系统级)服务, 开机自启 + 崩溃自动重启。
项目已附带模板 scripts/stock-scheduler.service, 把其中的 __USER__ / __PROJECT_DIR__
替换为实际值即可; 也可直接复制下面内容写入 /etc/systemd/system/stock-scheduler.service
(系统级, 需 sudo; 若用用户级改为 ~/.config/systemd/user/ 并去掉 [Service] 里的 User):

  [Unit]
  Description=Stock Analysis 统一定时调度器
  After=network-online.target
  Wants=network-online.target

  [Service]
  Type=simple
  User=<你的用户名>                       # 用户级服务可删掉此行
  WorkingDirectory=/opt/stock-analysis-pro   # 改成项目实际根目录
  ExecStart=/usr/bin/python3 /opt/stock-analysis-pro/scripts/scheduler.py --daemon
  Restart=on-failure
  RestartSec=10
  # 可选: 限制资源, 避免单个任务拖垮整机
  # MemoryMax=2G

  [Install]
  WantedBy=multi-user.target            # 用户级改为 default.target

然后执行:

  sudo systemctl daemon-reload
  sudo systemctl enable stock-scheduler      # 开机自启(只需一次)
  sudo systemctl start  stock-scheduler      # 立即启动
  sudo systemctl status stock-scheduler      # 查看状态
  sudo systemctl restart stock-scheduler     # 改了 TASKS 表后重启生效
  journalctl -u stock-scheduler -f           # 跟随查看日志(也可用 data/scheduler.log)

注意: ExecStart 里请写绝对路径的 python3 与 scheduler.py; 若项目依赖在虚拟环境,
把 ExecStart 改为该 venv 的 python, 例如
  ExecStart=/opt/stock-analysis-pro/venv/bin/python /opt/stock-analysis-pro/scripts/scheduler.py --daemon

---------------------------------------------------------------------
1.3 Ubuntu / Linux —— 用 crontab (最简, 无崩溃自启)
---------------------------------------------------------------------
若不想用 systemd, 可用 cron 的 @reboot 在开机时拉起 (进程崩溃不会自动重启):

  crontab -e
  # 加入一行(注意用绝对路径, 且确保 python3 在 PATH):
  @reboot /usr/bin/python3 /opt/stock-analysis-pro/scripts/scheduler.py --daemon >> /opt/stock-analysis-pro/data/scheduler.cron.log 2>&1

启动当前会话:
  nohup /usr/bin/python3 /opt/stock-analysis-pro/scripts/scheduler.py --daemon >> /opt/stock-analysis-pro/data/scheduler.cron.log 2>&1 &

=====================================================================
二、使用方法 (命令行)
=====================================================================
  python scripts/scheduler.py --list            # 列出计划表(操作/时间/间隔/交易日)
  python scripts/scheduler.py --dry-run         # 打印今天将执行的任务(不真正执行)
  python scripts/scheduler.py --run-once <任务名>  # 立即执行某个任务(调试)
  python scripts/scheduler.py --check           # 检查环境/依赖
  python scripts/scheduler.py --daemon          # 常驻循环(默认, 注册后自动运行)

常用示例:
  python scripts/scheduler.py --list            # 查看全部 9 个任务的时刻与说明
  python scripts/scheduler.py --run-once 宏观分析   # 手动跑一次宏观分析(不等待定时)
  python scripts/scheduler.py --check           # 确认 python/依赖/企微推送/落盘目录就绪

=====================================================================
三、计划表 (9 个任务)
=====================================================================
  任务名              时间             间隔         仅交易日  通知   说明
  ──────────────────────────────────────────────────────────────────────────
  东财Cookie刷新      07:30            每周一       否        否     刷新东财匿名会话 Cookie(概念板块依赖)
  落盘数据更新        08:00            每个交易日   是        是     增量刷新全市场 A 股日线 K 线落盘
  宏观分析            08:30            每个交易日   是        是     国际+国内+涨停池 → 综合研判(盘前定调)
  盘中自选异动监控    09:30-11:30/13:00-15:00  盘中每15分  是     是     盘中扫描自选股+大盘, 异动推企微
  盘中热点选股(突破扫描) 09:30-11:30/13:00-15:00  盘中每15分  是     是     盘中跑突破扫描, 实时价筛选突破/即将启动个股推企微
  概念板块扫描        15:45            每个交易日   是        是     收盘后拉概念板块涨幅榜单
  热点选股(突破扫描)  16:00            每个交易日   是        是     热点板块成分股形态识别选股
  自选分析            16:30            每个交易日   是        是     遍历 watchlist.json 多维评分
  每日复盘            17:00            每个交易日   是        是     全市场复盘报告
  周热点回测          18:00            每周五       是        否     周度热点板块回测
  数据质量巡检        23:00            每天         否        否     扫描落盘 K 线(stale/short 统计)

=====================================================================
四、注意事项
=====================================================================
  - 节假日: HOLIDAYS 集合为 2026 年粗略法定节假日, 如与实际安排不符请自行维护
            (仅影响"仅交易日"任务的跳过, 不影响周末判断)。
  - 网络依赖: 落盘更新 / Cookie / 宏观 / 概念 / 复盘 均触网; 宏观分析依赖
            config/config.yaml 的 proxy.https (akshare 接口需代理)。
  - 自选分析: 依赖 data/watchlist.json 有内容, 空则跳过。
  - 企微通知: 复用 notify.wecom_bot 底层, 把任务输出末 4000 字符推送到企微会话;
            未配置机器人时静默跳过(不重跑分析)。可用 --check 确认是否启用。
  - 日志: 运行日志写入 data/scheduler.log; 每日执行状态写入 data/scheduler_state.json
            (保留最近 7 天, 用于避免重复执行)。

=====================================================================
五、任务类型
=====================================================================
  - type="shell" (默认): cmd 为子进程参数列表, 如 ["python","scripts/xxx.py",...]
  - type="python": func 为 scheduler 进程内可调用的函数名(用于内联轻量任务)
"""
import os
import sys
import json
import time
import argparse
import subprocess
import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_PATH = os.path.join(DATA_DIR, "scheduler.log")
STATE_PATH = os.path.join(DATA_DIR, "scheduler_state.json")

# ---------------------------------------------------------------------------
# 交易日判断: 周一~周五 且 不在法定节假日集合
# (2026 年主要节假日, 如与实际安排不符可自行维护; 仅影响"仅交易日"任务的跳过)
# ---------------------------------------------------------------------------
HOLIDAYS = {
    "2026-01-01", "2026-01-02",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23", "2026-02-24",
    "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21", "2026-06-22",
    "2026-09-25", "2026-09-26", "2026-09-27", "2026-09-28",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
    "2026-12-25",
}


def is_trading_day(d: datetime.date) -> bool:
    if d.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    if d.strftime("%Y-%m-%d") in HOLIDAYS:
        return False
    return True


# ---------------------------------------------------------------------------
# 任务表 —— 唯一的定时执行计划
#   name            : 任务名(唯一, 用于 --run-once / 状态记录)
#   cmd / func      : 执行内容
#   time            : 触发时刻 "HH:MM"
#   interval        : 间隔描述(仅用于展示, 实际为每日定点; 周期类用 weekday 控制)
#   weekday         : 执行的星期集合(Mon=0..Sun=6), None=每天
#   trading_day_only: True=仅交易日执行(跳过周末/节假日)
#   timeout         : 超时秒数
#   notify          : 是否把执行结果摘要推送到企微(未配置机器人则静默跳过)
#   enabled         : 是否启用
# ---------------------------------------------------------------------------
TASKS = [
    {
        "name": "东财Cookie刷新",
        "cmd": ["python", "scripts/get_em_cookie.py"],
        "time": "08:30",
        "interval": "每周一",
        "weekday": [0],
        "trading_day_only": False,
        "timeout": 300,
        "notify": False,
        "enabled": True,
        "desc": "刷新东财匿名会话 Cookie, 概念板块分析依赖(会过期)",
    },
    {
        "name": "落盘数据更新",
        "cmd": ["python", "scripts/fetch_all_klines.py", "--max-stale", "7", "--workers", "6"],
        "time": "08:00",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 3600,
        "notify": True,
        "enabled": True,
        "desc": "增量刷新全市场 A 股日线 K 线落盘(跳过 7 天内已有效的)",
    },
    {
        "name": "宏观分析",
        "cmd": ["python", "plans/macro_report.py"],
        "time": "08:30",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 600,
        "notify": True,
        "enabled": True,
        "desc": "国际宏观+国内经济+涨停池情绪 → 综合研判(盘前定调)",
    },
    {
        "name": "概念板块扫描",
        "cmd": ["python", "core/cli.py", "concept", "--stage", "list", "--top", "10"],
        "time": "15:45",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 900,
        "notify": True,
        "enabled": True,
        "desc": "收盘后拉概念板块涨幅榜单(快, 纯 requests)",
    },
    {
        "name": "热点选股(突破扫描)",
        "cmd": ["python", "core/cli.py", "breakthrough", "--concepts", "10", "--per", "15"],
        "time": "16:00",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 1800,
        "notify": True,
        "enabled": True,
        "desc": "热点板块成分股形态识别(突破/即将启动), 选股池产出",
    },
    {
        "name": "自选分析",
        "cmd": ["python", "core/cli.py", "analyze-all", "--no-browser"],
        "time": "16:30",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 1800,
        "notify": True,
        "enabled": True,
        "desc": "遍历 data/watchlist.json 全部自选股, 多维评分(技术/基本/资金/舆情)",
    },
    {
        "name": "每日复盘",
        "cmd": ["python", "core/cli.py", "review"],
        "time": "17:00",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 900,
        "notify": True,
        "enabled": True,
        "desc": "全市场复盘报告(指数/板块/情绪/涨跌家数)",
    },
    {
        "name": "周热点回测",
        "cmd": ["python", "plans/weekly_hotspot.py"],
        "time": "18:00",
        "interval": "每周五",
        "weekday": [4],
        "trading_day_only": True,
        "timeout": 1800,
        "notify": False,
        "enabled": True,
        "desc": "周度热点板块回测, 校验选股策略有效性",
    },
    {
        "name": "数据质量巡检",
        "type": "python",
        "func": "check_data_quality",
        "time": "23:00",
        "interval": "每天",
        "weekday": None,
        "trading_day_only": False,
        "timeout": 300,
        "notify": False,
        "enabled": True,
        "desc": "扫描落盘 K 线, 统计 stale_accepted(退市/停牌)/short_history(次新), 输出清单",
    },
    {
        "name": "盘中自选异动监控",
        "cmd": ["python", "plans/intraday_watch.py", "--threshold", "3"],
        "time": "09:30",
        "interval": "盘中每15分",
        "repeat_minutes": 15,
        "window": [["09:30", "11:30"], ["13:00", "15:00"]],
        "weekday": None,
        "trading_day_only": True,
        "timeout": 120,
        "notify": True,
        "enabled": True,
        "desc": "盘中每15分扫描自选股+大盘, 异动(涨停/跌停/创日内新高/大幅涨跌/高换手)推企微",
    },
    {
        "name": "盘中热点选股(突破扫描)",
        "cmd": ["python", "core/cli.py", "breakthrough", "--concepts", "5", "--per", "15"],
        "time": "09:30",
        "interval": "盘中每15分",
        "repeat_minutes": 15,
        "window": [["09:30", "11:30"], ["13:00", "15:00"]],
        "weekday": None,
        "trading_day_only": True,
        "timeout": 600,
        "notify": True,
        "enabled": True,
        "desc": "盘中每15分跑突破扫描(热点板块→成分股→形态识别), 盘中实时价筛选突破/即将启动个股推企微",
    },
]


# ---------------------------------------------------------------------------
# 日志 / 状态
# ---------------------------------------------------------------------------
def log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        try:
            return json.load(open(STATE_PATH, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"状态保存失败: {e}")


# ---------------------------------------------------------------------------
# 企微通知(可选, 未配置机器人则静默跳过)
# ---------------------------------------------------------------------------
def notify(title: str, body: str):
    try:
        import asyncio
        from notify.wecom_bot import build_client, load_chat_id, _split_by_bytes, is_enabled
    except Exception:
        return
    if not is_enabled():
        return
    chat_id = load_chat_id()
    if not chat_id:
        return

    async def _push():
        client = build_client()
        await client.connect()
        await asyncio.sleep(2)
        full = f"## 📅 {title}\n" + (body or "(无输出)")
        for ch in _split_by_bytes(full):
            await client.send_message(chat_id, {"msgtype": "markdown", "markdown": {"content": ch}})
        client.disconnect()

    try:
        asyncio.run(_push())
    except Exception as e:
        log(f"[NOTIFY] 推送失败({title}): {e}")


# ---------------------------------------------------------------------------
# 内联 python 任务
# ---------------------------------------------------------------------------
def check_data_quality():
    """扫描 data/klines, 统计 stale_accepted / short_history, 输出清单。"""
    kl_dir = os.path.join(DATA_DIR, "klines")
    stale, short, ok = [], [], 0
    if os.path.isdir(kl_dir):
        for fn in os.listdir(kl_dir):
            if not (fn.startswith("kl_") and fn.endswith(".json")):
                continue
            try:
                d = json.load(open(os.path.join(kl_dir, fn), encoding="utf-8"))
            except Exception:
                continue
            if d.get("stale_accepted"):
                stale.append(fn[3:-5])
            elif d.get("short_history"):
                short.append(fn[3:-5])
            else:
                ok += 1
    lines = [f"K线数据质量巡检: 有效={ok}  长期无更新(退市/停牌)={len(stale)}  次新股(上市<60日)={len(short)}"]
    if stale:
        lines.append("  退市/停牌(长期无更新): " + ", ".join(stale[:30]))
    if short:
        lines.append("  次新股(待满足60日解除标记): " + ", ".join(short[:30]))
    out = "\n".join(lines)
    print(out)
    return out


PYTHON_TASKS = {"check_data_quality": check_data_quality}


# ---------------------------------------------------------------------------
# 任务执行
# ---------------------------------------------------------------------------
def run_task(t: dict):
    log(f"▶ 开始: {t['name']}  ({t.get('desc','')})")
    try:
        if t.get("type") == "python":
            func = PYTHON_TASKS.get(t["func"])
            if not func:
                raise RuntimeError(f"未知内联任务函数: {t.get('func')}")
            out = func() or ""
            rc = 0
        else:
            proc = subprocess.run(
                t["cmd"], cwd=BASE_DIR, capture_output=True, text=True,
                timeout=t.get("timeout", 3600), env=os.environ,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            rc = proc.returncode
        tail = out.strip()[-4000:] if isinstance(out, str) else str(out)[-4000:]
        log(f"✓ 完成: {t['name']} (rc={rc})")
        if tail:
            for ln in tail.splitlines()[-15:]:
                log(f"    └ {ln}")
        if t.get("notify"):
            notify(t["name"], tail)
        return rc
    except subprocess.TimeoutExpired:
        log(f"✗ 超时: {t['name']} (>{t.get('timeout',3600)}s)")
        if t.get("notify"):
            notify(t["name"], f"⚠️ 执行超时(>{t.get('timeout',3600)}s)")
        return -1
    except Exception as e:
        log(f"✗ 异常: {t['name']}: {e}")
        if t.get("notify"):
            notify(t["name"], f"⚠️ 执行异常: {e}")
        return -1


def in_window(now: datetime.datetime, windows) -> bool:
    """判断 now 是否落在任一时间窗内。windows=[["09:30","11:30"],["13:00","15:00"]]。"""
    if not windows:
        return True
    t = now.time()
    for w in windows:
        s = datetime.time(*map(int, w[0].split(":")))
        e = datetime.time(*map(int, w[1].split(":")))
        if s <= t <= e:
            return True
    return False


def due_tasks(now: datetime.datetime, last: datetime.datetime, state: dict):
    """返回 (待执行任务列表, 今日日期key)。

    两类任务:
      - 定点任务: 时刻落在 (last, now] 区间即触发一次(按日去重)。
      - 盘中重复任务(repeat_minutes 字段): 在 window 时间窗内、距上次执行
        已满 repeat_minutes 分钟即再次触发(用 state["_last_run"] 去重)。
    """
    today = now.date()
    result = []
    for t in TASKS:
        if not t.get("enabled", True):
            continue
        # 盘中重复任务
        if t.get("repeat_minutes"):
            if not in_window(now, t.get("window")):
                continue
            wd = t.get("weekday")
            if wd is not None and now.weekday() not in wd:
                continue
            if t.get("trading_day_only") and not is_trading_day(now.date()):
                continue
            last_run = state.get("_last_run", {}).get(t["name"])
            if last_run:
                last_dt = datetime.datetime.strptime(last_run, "%Y-%m-%d %H:%M:%S")
                if (now - last_dt).total_seconds() < t["repeat_minutes"] * 60:
                    continue
            result.append(t)
            continue
        # 定点任务(原有逻辑)
        hh, mm = map(int, t["time"].split(":"))
        task_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if not (last < task_dt <= now):
            continue
        wd = t.get("weekday")
        if wd is not None and task_dt.weekday() not in wd:
            continue
        if t.get("trading_day_only") and not is_trading_day(task_dt.date()):
            continue
        result.append(t)
    return result, today.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------
def cmd_list():
    print(f"{'任务名':<18}{'时间':<16}{'间隔':<12}{'仅交易日':<8}{'通知':<6}  说明")
    print("-" * 110)
    for t in TASKS:
        if not t.get("enabled", True):
            continue
        wd = t.get("weekday")
        interval = t.get("interval", "每天")
        if wd is not None and not t.get("repeat_minutes"):
            names = ["一", "二", "三", "四", "五", "六", "日"]
            interval = "周" + "/".join(names[i] for i in wd)
        # 时间列: 盘中重复任务显示时间窗, 否则显示定点时刻
        if t.get("repeat_minutes"):
            wins = t.get("window") or []
            time_col = "/".join(f"{w[0]}-{w[1]}" for w in wins) or t["time"]
        else:
            time_col = t["time"]
        print(f"{t['name']:<16}{time_col:<18}{interval:<12}"
              f"{('是' if t.get('trading_day_only') else '否'):<8}"
              f"{('是' if t.get('notify') else '否'):<6}  {t.get('desc','')}")


def cmd_dry_run():
    now = datetime.datetime.now()
    # 模拟从今日 00:00 到当前时刻之间应触发的任务
    last = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tasks, day_key = due_tasks(now, last)
    print(f"今天 ({day_key}) 至当前时刻将执行的任务:")
    if not tasks:
        print("  (无)")
    for t in tasks:
        print(f"  {t['time']}  {t['name']}  — {t.get('desc','')}")


def cmd_run_once(name: str):
    t = next((x for x in TASKS if x["name"] == name), None)
    if not t:
        print(f"未找到任务: {name}", file=sys.stderr)
        print("可用任务: " + ", ".join(x["name"] for x in TASKS), file=sys.stderr)
        sys.exit(1)
    rc = run_task(t)
    sys.exit(0 if rc == 0 else 1)


def cmd_check():
    print("环境检查:")
    py = subprocess.run(["python", "--version"], capture_output=True, text=True)
    print(f"  python: {py.stdout.strip() or py.stderr.strip()}")
    for mod in ("akshare", "requests", "yaml", "jinja2"):
        try:
            __import__(mod)
            print(f"  依赖 {mod}: OK")
        except Exception as e:
            print(f"  依赖 {mod}: 缺失 ({e})")
    # 企微机器人
    try:
        from notify.wecom_bot import is_enabled
        print(f"  企微推送: {'已启用' if is_enabled() else '未启用(通知将静默跳过)'}")
    except Exception as e:
        print(f"  企微推送: 不可用 ({e})")
    print(f"  落盘K线目录: {os.path.join(DATA_DIR,'klines')} "
          f"({'存在' if os.path.isdir(os.path.join(DATA_DIR,'klines')) else '缺失'})")


def daemon():
    log("调度器启动 (daemon) — 共 %d 个任务" % len([t for t in TASKS if t.get('enabled', True)]))
    last = datetime.datetime.now() - datetime.timedelta(seconds=1)
    while True:
        try:
            now = datetime.datetime.now()
            state = load_state()
            tasks, day_key = due_tasks(now, last, state)
            if tasks:
                log(f"到点任务 {len(tasks)} 个: {', '.join(t['name'] for t in tasks)}")
                for t in tasks:
                    rc = run_task(t)
                    if rc == 0:
                        if t.get("repeat_minutes"):
                            # 盘中重复任务: 记录上次执行时刻
                            state.setdefault("_last_run", {})[t["name"]] = now.strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            # 定点任务: 按日去重
                            state.setdefault(day_key, [])
                            if t["name"] not in state[day_key]:
                                state[day_key].append(t["name"])
                # 仅保留最近 7 天状态(含 _last_run 中的过期键)
                for k in list(state.keys()):
                    if k == "_last_run":
                        continue
                    if (now.date() - datetime.datetime.strptime(k, "%Y-%m-%d").date()).days > 7:
                        state.pop(k, None)
                save_state(state)
            last = now
            time.sleep(20)
        except KeyboardInterrupt:
            log("调度器收到中断, 退出")
            break
        except Exception as e:
            log(f"主循环异常: {e}")
            time.sleep(20)


def main():
    ap = argparse.ArgumentParser(description="统一定时执行计划 (唯一调度器)")
    ap.add_argument("--list", action="store_true", help="列出计划表")
    ap.add_argument("--dry-run", action="store_true", help="打印今天将执行的任务")
    ap.add_argument("--run-once", metavar="任务名", help="立即执行某任务(调试)")
    ap.add_argument("--check", action="store_true", help="检查环境/依赖")
    ap.add_argument("--daemon", action="store_true", help="常驻循环(默认)")
    args = ap.parse_args()

    if args.list:
        cmd_list()
    elif args.dry_run:
        cmd_dry_run()
    elif args.run_once:
        cmd_run_once(args.run_once)
    elif args.check:
        cmd_check()
    else:
        daemon()


if __name__ == "__main__":
    main()
