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
  python scripts/scheduler.py --list            # 查看全部任务(窗口/时间/间隔/交易日)
  python scripts/scheduler.py --run-once 盘前播报   # 手动跑一次盘前播报(不等待定时)
  python scripts/scheduler.py --check           # 确认 python/依赖/企微推送/落盘目录就绪

=====================================================================
三、计划表 (7 个任务 = 4 推送窗口 + 3 后台, 已深度整合精简)
=====================================================================
  推送窗口    任务名        时间      间隔       仅交易日  通知   说明
  ──────────────────────────────────────────────────────────────────────────
  -          东财Cookie刷新  08:30    每周一     否        否     刷新东财匿名会话 Cookie(概念板块依赖)
  -          数据质量巡检    23:00    每天       否        否     扫描落盘 K 线(stale/short 统计)
  -          周热点回测      周五18:00 每周五     是        否     周度热点板块回测(无推送)
  盘前        盘前播报        08:30    每个交易日 是        是     落盘更新 + 宏观研判
  早盘        早盘播报        11:30    每个交易日 是        是     自选异动 + 热点突破(轻量快照)
  午盘        午盘播报        14:30    每个交易日 是        是     自选异动 + S14三角形突破复核(只扫池)
  盘后        盘后播报        17:15    每个交易日 是        是     概念扫描→热度追踪→突破选股→池刷新→自选分析→复盘→决策
  注: 4 个推送窗口各自把多任务合并为一条流水线(命令键见 core.commands),
       输出汇总后统一推送一次企微, 每天仅 4 条定时推送。

=====================================================================
四、注意事项
=====================================================================
  - 节假日: HOLIDAYS 集合为 2026 年粗略法定节假日, 如与实际安排不符请自行维护
            (仅影响"仅交易日"任务的跳过, 不影响周末判断)。
  - 网络依赖: 落盘更新 / Cookie / 宏观 / 概念 / 复盘 均触网; 宏观分析依赖
            config/config.yaml 的 proxy.https (akshare 接口需代理)。
  - 自选分析: 依赖 stock_pool 中 watch=True 有内容, 空则跳过。
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
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 统一指令注册表: 定时任务与微信 bot 共用一份命令定义, 避免漂移
# (必须在 sys.path 注入项目根之后导入, 否则 scripts/ 目录下找不到 core 包)
from core.commands import COMMANDS, expand_args  # noqa: E402

# 强制 stdout/stderr 使用 UTF-8, 避免 Windows 控制台默认 gbk 无法编码 ▶/🔥 等字符
# (log() 与前台 --run-once 直连 PowerShell 控制台时会触发 UnicodeEncodeError 崩溃)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_PATH = os.path.join(DATA_DIR, "scheduler.log")
STATE_PATH = os.path.join(DATA_DIR, "scheduler_state.json")
NOTIFY_HTML_DIR = os.path.join(DATA_DIR, "notify_html")

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
# ===========================================================================
# 任务表 —— 唯一的定时执行计划
#   设计原则(2026-07-25 深度整合): 用户只要求 4 个定时推送窗口
#     · 盘前 (08:30)  · 早盘 (11:30)  · 午盘 (15:00)  · 盘后 (17:15)
#   每个窗口把原本分散的多任务合并为一条流水线(commands=命令键列表,
#   由 core.commands 统一展开), 输出汇总后只推一条企微消息。
#   另有 3 个后台任务(无推送): Cookie刷新 / 数据质量巡检 / 周热点回测。
#
#   name            : 任务名(唯一)
#   commands        : 统一指令键列表(见 core.commands.COMMANDS), 顺序执行、汇总推送一次
#   time            : 触发时刻 "HH:MM"
#   interval        : 间隔描述(仅展示)
#   window          : 推送窗口标签(盘前/早盘/午盘/盘后), 仅展示
#   weekday         : 执行的星期集合(Mon=0..Sun=6), None=每天
#   trading_day_only: True=仅交易日执行
#   timeout         : 整个流水线超时秒数
#   notify          : 是否推送企微
#   enabled         : 是否启用
# ===========================================================================
TASKS = [
    # ---------- 后台任务(无推送) ----------
    {
        "name": "东财Cookie刷新",
        "commands": ["cookie"],
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
        # 重蒸馏: walk-forward 回看最近13个月, 按大盘三状态拆策略胜率, 产出参数建议
        # (只建议不改代码; 报告落盘 data/reports/redistill_*.md + data/redistill_suggestions.json,
        #  脚本自带 --push 推摘要卡片, 故本任务 notify=False 避免双推)
        "name": "周末重蒸馏",
        "commands": ["redistill", "--step-days", "20", "--push"],
        "time": "10:00",
        "interval": "每周六",
        "weekday": [5],
        "trading_day_only": False,
        "timeout": 3600,
        "notify": False,
        "enabled": True,
        "desc": "walk-forward 重蒸馏参数建议(机器建议人工拍板, 摘要自行推送)",
    },
    {
        "name": "周热点回测",
        "commands": ["weekly_hotspot"],
        "time": "18:00",
        "interval": "每周五",
        "weekday": [4],
        "trading_day_only": True,
        "timeout": 1800,
        "notify": False,
        "enabled": True,
        "desc": "周度热点板块回测, 校验选股策略有效性(无推送)",
    },

    # ---------- 四大推送窗口 ----------
    {
        # 盘前: 落盘更新 + 宏观研判, 盘前定调
        "name": "盘前播报",
        "window": "盘前",
        "commands": ["update_klines", "macro"],
        "time": "08:30",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 3600,
        "notify": True,
        "continue_on_error": True,
        "enabled": True,
        "desc": "盘前一条龙: 落盘K线更新 + 宏观研判(国际+国内+涨停池), 综合定调",
    },
    {
        # 早盘: 蒸馏精选(今日可买清单) → 盘中异动监控 → S14 三角形过滤(只扫池子, 不裸扫)
        "name": "早盘播报",
        "window": "早盘",
        "commands": ["daily_hotspot", "intraday_watch", "s14"],
        "time": "10:00",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 1800,
        "notify": True,
        "enabled": True,
        "desc": "早盘: 热点蒸馏精选(可买清单) + 策略池异动监控 + S14三角形过滤蒸馏池",
    },
    {
        # 午盘: 盘中跟踪策略池信号 + S14 三角形突破触发复核(只扫池子)
        "name": "午盘播报",
        "window": "午盘",
        "commands": ["intraday_watch", "s14"],
        "time": "14:30",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 600,
        "notify": True,
        "enabled": True,
        "desc": "午盘: 策略池信号跟踪(买卖点触发) + S14三角形盘中突破复核, 推一次",
    },
    {
        # 盘后: 收盘跟踪策略池信号(买卖点触发) + 池刷新 + 复盘 + 决策, 不再裸扫候选
        "name": "盘后播报",
        "window": "盘后",
        "commands": [
            "intraday_watch",      # 收盘跟踪策略池信号(买卖点触发)
            "pool_refresh",        # 股票池刷新(重算关卡+清理过期)
            "review",              # 每日复盘
            "decision",            # 每日决策简报
        ],
        "time": "17:15",
        "interval": "每个交易日",
        "weekday": None,
        "trading_day_only": True,
        "timeout": 4200,
        "notify": True,
        "enabled": True,
        "desc": "盘后: 策略池跟踪(收盘触发买卖点) + 池刷新 + 复盘 + 决策, 汇总推一次",
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
# intraday_watch 输出的分组分隔符: 据此把"策略池"与"自选"拆成两条企微消息
SPLIT_SENTINEL = "<<<SPLIT>>>"


def _find_html_report(body: str):
    """若任务输出自带 HTML 报告(HTML_REPORT:<path> 约定), 直接复用, 不重复生成。"""
    for line in (body or "").splitlines():
        if line.startswith("HTML_REPORT:"):
            p = line.split("HTML_REPORT:", 1)[1].strip()
            if p and os.path.exists(p):
                return p
    return None


_NOISE_RE = re.compile(
    r"(\d+%\|)"          # tqdm 进度条: " 58%|█████▊  | 11/19 ..."
    r"|(\bit/s\]?)"      # tqdm 速率尾巴
    r"|(^HTML_REPORT:)"  # 内部标记行
    r"|(^\[20\d\d-\d\d-\d\dT.*\[AiBotSDK\])"  # SDK 日志
)


def _clean_lines(body: str):
    """去掉进度条/内部标记等噪声行, 返回干净文本行列表。"""
    out = []
    for l in (body or "").splitlines():
        if _NOISE_RE.search(l):
            continue
        out.append(l)
    return out


def _build_card(title: str, body: str) -> str:
    """构造企微摘要卡片(markdown, 简短): 优先用脚本自带卡片标记, 否则取前若干行预览。"""
    # 1) 脚本显式卡片标记 <<<WECHAT_CARD_START/END>>>
    if "<<<WECHAT_CARD_START>>>" in body and "<<<WECHAT_CARD_END>>>" in body:
        seg = body.split("<<<WECHAT_CARD_START>>>", 1)[1]
        inner = seg.split("<<<WECHAT_CARD_END>>>", 1)[0].strip()
        if inner:
            return f"## 📅 {title}\n\n{inner}"
    # 2) 去除进度条/标记等噪声行, 取前 15 个非空行作为预览
    clean = "\n".join(_clean_lines(body)).strip()
    if not clean:
        return f"## 📅 {title}\n\n✅ 已完成，完整明细见附件 HTML"
    preview = "\n".join([l for l in clean.splitlines() if l.strip()][:15])
    return (f"## 📅 {title}\n\n"
            f"{preview}\n\n"
            f"📎 完整明细见附件 HTML 文件")


def _render_html_report(title: str, body: str):
    """把纯文本任务输出渲染成 HTML 文件(含完整详细内容), 返回路径。

    统一走项目 HTML 报告体系(core.html_renderer + templates/base.html 暗色卡片主题),
    使用通用文本模板 task_report.html, 而非另起炉灶手搓样式。
    """
    import datetime as _dt
    try:
        os.makedirs(NOTIFY_HTML_DIR, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"\W+", "_", title)[:40] or "task"

        # 摘要: 复用脚本自带卡片标记内容(若有)
        summary = ""
        if "<<<WECHAT_CARD_START>>>" in body and "<<<WECHAT_CARD_END>>>" in body:
            seg = body.split("<<<WECHAT_CARD_START>>>", 1)[1]
            summary = seg.split("<<<WECHAT_CARD_END>>>", 1)[0].strip()

        # 完整明细: 去掉内部标记行/进度条噪声/分隔符, 保留纯净文本
        clean = "\n".join(_clean_lines(body))
        clean = clean.replace("<<<WECHAT_CARD_START>>>", "").replace("<<<WECHAT_CARD_END>>>", "")
        clean = clean.replace(SPLIT_SENTINEL, "\n").strip() or "(无输出)"

        from core.html_renderer import render
        data = {
            "title": title,
            "time": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "summary": summary,
            "body": clean,
        }
        return render(
            data, "task_report",
            output_dir=NOTIFY_HTML_DIR,
            filename=f"{safe}_{ts}.html",
        )
    except Exception as e:
        log(f"[NOTIFY] HTML 生成失败({title}): {e}")
        return None


def notify(title: str, body: str):
    """企微通知: 发【摘要卡片】+ 附【HTML 文件(完整详细内容)】, 不再堆文字。"""
    try:
        import asyncio
        from notify.wecom_bot import build_client, load_chat_id, is_enabled, send_file_on_client, resolve_chat_id
    except Exception:
        return
    if not is_enabled():
        return
    chat_id = resolve_chat_id()
    if not chat_id:
        log(f"[NOTIFY] 跳过(无有效 chat_id): {title} —— 请在接收推送的会话里 @机器人 一次以记录会话, 或在 .env 配置 WECOM_PUSH_CHAT_ID")
        return

    # HTML: 优先复用脚本自带报告, 否则由完整输出渲染
    html_path = _find_html_report(body) or _render_html_report(title, body)

    # 分群: 若含分隔符, 拆成两段分别推卡片(策略池 / 自选)
    if SPLIT_SENTINEL in body:
        parts = body.split(SPLIT_SENTINEL, 1)
        groups = [
            ("【策略池】" + title, parts[0]),
            ("【自选】" + title, parts[1]),
        ]
    else:
        groups = [(title, body)]

    async def _push():
        client = build_client(push_mode=True)
        # 等待认证真正完成(避免未认证就发); 10s 兜底
        _authed = asyncio.Event()
        try:
            client.on("authenticated", lambda: _authed.set())
        except Exception:
            pass
        await client.connect()
        try:
            await asyncio.wait_for(_authed.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
        for g_title, g_body in groups:
            card = _build_card(g_title, g_body)
            await client.send_message(chat_id, {"msgtype": "markdown", "markdown": {"content": card}})
        if html_path and os.path.exists(html_path):
            await send_file_on_client(client, html_path, chat_id)
        # 关键: 发送后保持连接片刻, 等 aibot 服务端异步转发到企微完成再断开,
        # 否则立刻 disconnect 会打断转发(与 push_markdown_via_bot 同一根因)
        await asyncio.sleep(4)
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
def _run_sub(cmd_argv, timeout):
    """运行单条子命令, 返回 (combined_text, rc)。超时抛 subprocess.TimeoutExpired。"""
    # 强制子进程 UTF-8, 避免 Windows 控制台默认 gbk 导致打印中文/emoji 时
    # UnicodeEncodeError 崩溃(如 宏观分析 的 '🌐'), 进而使 notify 拿到错误内容
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        cmd_argv, cwd=BASE_DIR, capture_output=True, text=True,
        timeout=timeout, env=env, encoding="utf-8", errors="replace",
    )
    return (proc.stdout or "") + (proc.stderr or ""), proc.returncode


def resolve_task_cmds(t: dict):
    """把任务里的命令键展开为子进程 argv 列表。

    优先级: commands(统一指令键, 推荐) > cmds(裸 argv, 兼容) > cmd(单条, 兼容)。
    """
    cmds = []
    for k in (t.get("commands") or []):
        cmds.append(["python"] + expand_args(k))
    if t.get("cmds"):
        cmds += t["cmds"]
    if t.get("cmd"):
        cmds.append(t["cmd"])
    return cmds


def run_task(t: dict):
    log(f"▶ 开始: {t['name']}  ({t.get('desc','')})")
    try:
        out_parts = []  # 按子步骤分段, 供 #NO_PUSH# 逐段过滤(合并窗口下不能一票否决)
        if t.get("type") == "python":
            func = PYTHON_TASKS.get(t["func"])
            if not func:
                raise RuntimeError(f"未知内联任务函数: {t.get('func')}")
            out = func() or ""
            out_parts.append(out)
            rc = 0
        else:
            # 深度整合: commands 为统一指令键列表(见 core.commands), 顺序执行、
            # 输出汇总、只推一次企微, 从根上减少消息轰炸
            cmds = resolve_task_cmds(t)
            timeout = t.get("timeout", 3600)
            out, rc = "", 0
            for i, c in enumerate(cmds):
                log(f"  ├ 子步骤 {i+1}/{len(cmds)}: {' '.join(c)}")
                try:
                    text, r = _run_sub(c, timeout)
                except subprocess.TimeoutExpired:
                    msg = f"⚠️ 子步骤超时(>{timeout}s): {' '.join(c)}"
                    log(f"✗ {msg}")
                    out += msg + "\n"
                    out_parts.append(msg)
                    rc = -1
                    if not t.get("continue_on_error", False):
                        break
                    continue
                out += text
                out_parts.append(text)
                rc = rc or r
                if r != 0 and not t.get("continue_on_error", False):
                    log(f"  └ 子步骤返回非0(rc={r}), 后续步骤跳过(如需继续置 continue_on_error)")
                    break
        tail = out.strip()[-4000:] if isinstance(out, str) else str(out)[-4000:]
        log(f"✓ 完成: {t['name']} (rc={rc})")
        if tail:
            for ln in tail.splitlines()[-15:]:
                log(f"    └ {ln}")
        if t.get("notify"):
            # 子任务可输出 `#NO_PUSH#` sentinel 表示"无合适入选/无内容可推"。
            # 合并窗口(多子步骤)下按【子步骤】过滤: 仅剔除含 sentinel 的那一段,
            # 其余段照常推送; 全部段都无内容才整体跳过(避免 S14 空报告吞掉整条午盘)。
            pushable = [p for p in out_parts if p.strip() and "#NO_PUSH#" not in p]
            if not pushable:
                log(f"  ⊘ 跳过推送: {t['name']} (全部子步骤无内容/含 #NO_PUSH#)")
            else:
                if len(pushable) < len(out_parts):
                    log(f"  ⊘ 已剔除 {len(out_parts)-len(pushable)} 个无内容子步骤, 推送其余 {len(pushable)} 段")
                notify(t["name"], "\n".join(pushable))
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
    push_windows = []
    for t in TASKS:
        if t.get("enabled", True) and t.get("notify"):
            w = t.get("window")
            if w and w not in push_windows:
                push_windows.append(w)
    print(f"推送窗口({len(push_windows)}): " + " / ".join(push_windows))
    print("-" * 120)
    print(f"{'窗口':<6}{'任务名':<14}{'时间':<8}{'间隔':<12}{'仅交易日':<8}{'通知':<6}  说明")
    print("-" * 120)
    for t in TASKS:
        if not t.get("enabled", True):
            continue
        wd = t.get("weekday")
        interval = t.get("interval", "每天")
        if wd is not None:
            names = ["一", "二", "三", "四", "五", "六", "日"]
            interval = "周" + "/".join(names[i] for i in wd)
        time_col = t["time"]
        print(f"{(t.get('window') or '-'):<6}{t['name']:<14}{time_col:<8}{interval:<12}"
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
