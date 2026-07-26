# -*- coding: utf-8 -*-
"""统一指令注册表 —— 调度器(scripts/scheduler.py)与微信 bot(notify/wecom_bot.py)共用

一处定义「每个指令到底跑什么」(入口脚本 + 参数 + html/no_browser 提示),
两边均从这里取 argv, 杜绝两套命令字符串各写各、日久漂移。

字段说明:
  args        : 命令参数(不含可执行文件 python)。cli 类以 "core/cli.py" 开头,
                脚本类以 "plans/xxx.py" 或 "scripts/xxx.py" 开头。
  html        : 该指令是否默认产出 HTML 报告(统一追加 --html)
  no_browser  : 是否默认跳过 Playwright 浏览器采集(统一追加 --no-browser)
  desc        : 一句话说明(用于 --list / 帮助)
  scheduler    : True=可被定时调度引用; False=仅 bot 交互专用(不进 TASKS)
"""
COMMANDS = {
    # ============ 调度器 / bot 共用(定时任务)+ 交互 ============
    "update_klines": {
        "args": ["scripts/fetch_all_klines.py", "--max-stale", "7", "--workers", "6"],
        "html": False, "no_browser": False,
        "desc": "增量刷新全市场 A 股日线 K 线落盘",
    },
    "macro": {
        "args": ["plans/macro_report.py"],
        "html": False, "no_browser": False,
        "desc": "盘前宏观研判(国际+国内+涨停池)",
    },
    "cookie": {
        "args": ["scripts/get_em_cookie.py"],
        "html": False, "no_browser": False,
        "desc": "刷新东财匿名会话 Cookie(概念板块依赖)",
    },
    "intraday_watch": {
        "args": ["plans/intraday_watch.py", "--threshold", "3"],
        "html": False, "no_browser": False,
        "desc": "盘中自选异动监控",
    },
    "intraday_breakout": {
        "args": ["core/cli.py", "breakthrough", "--concepts", "5", "--per", "15"],
        "html": False, "no_browser": True,
        "desc": "盘中热点突破扫描(轻量 5 板块)",
    },
    "s14": {
        "args": ["plans/intraday_select.py", "--top", "15"],
        "html": False, "no_browser": False,
        "desc": "三角形突破选股(S14 同源 detect_triangle)",
    },
    "concept_list": {
        "args": ["core/cli.py", "concept", "--stage", "list", "--top", "10"],
        "html": True, "no_browser": False,
        "desc": "概念板块扫描(涨幅榜)",
    },
    "concept_track": {
        "args": ["plans/concept_tracker.py", "--save"],
        "html": False, "no_browser": False,
        "desc": "概念热度追踪(跨日对比)",
    },
    "post_breakout": {
        "args": ["core/cli.py", "breakthrough", "--concepts", "10", "--per", "15", "--to-pool"],
        "html": False, "no_browser": True,
        "desc": "收盘热点突破选股(10 板块, 入策略池)",
    },
    "pool_refresh": {
        "args": ["plans/stock_pool.py", "--refresh", "--expire"],
        "html": False, "no_browser": False,
        "desc": "股票池刷新(重算关卡+清理过期)",
    },
    "analyze_all": {
        "args": ["core/cli.py", "analyze-all"],
        "html": True, "no_browser": True,
        "desc": "自选批量分析(遍历 watch)",
    },
    "review": {
        "args": ["core/cli.py", "review"],
        "html": True, "no_browser": True,
        "desc": "每日复盘",
    },
    "decision": {
        "args": ["core/cli.py", "decision"],
        "html": True, "no_browser": True,
        "desc": "每日决策简报",
    },
    "weekly_hotspot": {
        "args": ["plans/weekly_hotspot.py"],
        "html": True, "no_browser": False,
        "desc": "每周热点选股流水线",
    },
    "daily_hotspot": {
        "args": ["plans/weekly_hotspot.py", "--daily"],
        "html": True, "no_browser": False,
        "desc": "每日热点选股(蒸馏精选, 出今日可买清单, 带买卖点)",
    },

    # ============ bot 交互专用(不进定时调度) ============
    "analyze": {
        "args": ["core/cli.py", "analyze"],
        "html": True, "no_browser": True,
        "desc": "个股 6 维分析",
    },
    "concept": {
        "args": ["core/cli.py", "concept", "--stage", "list", "--top", "10"],
        "html": True, "no_browser": False,
        "desc": "概念板块资金榜",
    },
    "breakthrough": {
        "args": ["core/cli.py", "breakthrough"],
        "html": True, "no_browser": True,
        "desc": "突破形态选股",
    },
    "options": {
        "args": ["core/cli.py", "options"],
        "html": True, "no_browser": True,
        "desc": "ETF 期权机会扫描",
    },
    "market": {
        "args": ["core/cli.py", "market"],
        "html": True, "no_browser": True,
        "desc": "大盘概览",
    },
    "add": {
        "args": ["core/cli.py", "add"],
        "html": False, "no_browser": True,
        "desc": "加自选",
    },
    "list": {
        "args": ["core/cli.py", "list"],
        "html": False, "no_browser": True,
        "desc": "自选列表(stock_pool watch)",
    },
    "clear": {
        "args": ["core/cli.py", "clear"],
        "html": False, "no_browser": True,
        "desc": "清空自选",
    },
    "portfolio": {
        "args": ["core/cli.py", "portfolio"],
        "html": False, "no_browser": True,
        "desc": "持仓盈亏(真实持仓)",
    },
}


def expand_args(key: str, extra=None):
    """返回命令参数(不含可执行文件 python), 并按 hint 补 --html / --no-browser。

    供调度器拼出子进程 argv(["python"] + expand_args(key)),
    以及供 bot 拼出 run_cli/run_script 的参数。
    """
    c = COMMANDS[key]
    a = list(c["args"])
    if c.get("html"):
        a.append("--html")
    if c.get("no_browser"):
        a.append("--no-browser")
    if extra:
        a += list(extra)
    return a


def is_cli(key: str) -> bool:
    """True=入口是 core/cli.py(走 run_cli); False=独立脚本(走 run_script)。"""
    return COMMANDS[key]["args"][0] == "core/cli.py"
