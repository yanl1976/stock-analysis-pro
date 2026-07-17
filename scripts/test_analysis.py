#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""系统全功能测试 + HTML 报告生成 + 企微推送

依次运行所有股票分析命令，记录 通过/降级/失败 状态、耗时、输出摘要，
生成自包含 HTML 测试报告，并通过企业微信智能机器人以「文件消息」推送。

用法:
    python scripts/test_analysis.py            # 测试 + 报告 + 推送
    python scripts/test_analysis.py --no-push  # 仅测试 + 报告, 不推送
"""
import os
import sys
import io
import re
import json
import time
import html
import subprocess
import argparse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
REPORT_DIR = os.path.join(BASE_DIR, "cache")

# 强制 stdout/stderr UTF-8, 避免 Windows gbk 控制台无法编码 ▶/✅ 等字符
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PY = sys.executable

# 每个测试用例: id / 名称 / 分类 / 命令参数 / 超时 / 是否校验 HTML / 通过标记
TESTS = [
    {
        "id": "analyze_brief",
        "name": "个股深度分析（直连轻量）",
        "cat": "个股分析",
        "args": ["analyze", "600519", "--no-browser", "--brief"],
        "timeout": 120,
        "html": False,
        "pass_marker": "pe",
    },
    {
        "id": "analyze_html",
        "name": "个股分析 + HTML 报告渲染",
        "cat": "个股分析",
        "args": ["analyze", "600519", "--no-browser", "--html"],
        "timeout": 120,
        "html": True,
        "pass_marker": None,
    },
    {
        "id": "market",
        "name": "大盘概览（宏观/指数）",
        "cat": "市场",
        "args": ["market", "--html"],
        "timeout": 150,
        "html": True,
        "pass_marker": None,
    },
    {
        "id": "concept_list",
        "name": "概念板块扫描（榜单 list）",
        "cat": "概念",
        "args": ["concept", "--stage", "list", "--top", "5"],
        "timeout": 120,
        "html": False,
        "pass_marker": None,
    },
    {
        "id": "review",
        "name": "每日复盘",
        "cat": "复盘",
        "args": ["review", "--html"],
        "timeout": 150,
        "html": True,
        "pass_marker": None,
    },
    {
        "id": "options",
        "name": "ETF 期权扫描",
        "cat": "期权",
        "args": ["options", "--top", "5"],
        "timeout": 180,
        "html": False,
        "pass_marker": None,
    },
    {
        "id": "analyze_all",
        "name": "批量分析自选股（analyze-all）",
        "cat": "批量",
        "args": ["analyze-all"],
        "timeout": 200,
        "html": False,
        "pass_marker": None,
    },
    {
        "id": "portfolio",
        "name": "持仓管理（portfolio）",
        "cat": "管理",
        "args": ["portfolio"],
        "timeout": 30,
        "html": False,
        "pass_marker": None,
    },
    {
        "id": "watchlist",
        "name": "自选股列表（list）",
        "cat": "管理",
        "args": ["list"],
        "timeout": 30,
        "html": False,
        "pass_marker": None,
    },
]

# 降级（运行成功但数据缺失/接口不可用）关键词
# 注意: 不含 "跳过" — 个股报告里 "融资融券: 接口不稳定, 暂跳过" 是已知限制的正常措辞, 非测试失败
WARN_KEYWORDS = [
    "不可用", "无数据", "为空", "Cookie", "RemoteDisconnected", "降级",
    "失败", "超时", "无输出", "未配置", "缺失", "无结果",
    "暂无", "无法获取", "接口异常", "无响应",
]


def run_case(case):
    """运行单个测试用例, 返回结果 dict。"""
    args = case["args"]
    cmd = [PY, "core/cli.py"] + args
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    started = time.time()
    rec = {
        "id": case["id"],
        "name": case["name"],
        "cat": case["cat"],
        "args": " ".join(args),
        "timeout": case["timeout"],
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=BASE_DIR,
            env=env,
            capture_output=True,
            timeout=case["timeout"],
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        elapsed = time.time() - started
        out = (proc.stdout or "")
        err = (proc.stderr or "")
        combined = out + "\n" + err
        rec["returncode"] = proc.returncode
        rec["elapsed"] = round(elapsed, 1)
        rec["stdout_tail"] = out.strip()[-1200:]
        rec["stderr_tail"] = err.strip()[-800:]

        # HTML 校验
        rec["html_path"] = ""
        if case["html"]:
            m = re.search(r"([A-Za-z]:\\[^\n]*?\.html|/[^\n]*?\.html)", out)
            if m and os.path.exists(m.group(1)):
                rec["html_path"] = m.group(1)

        # 状态判定
        if proc.returncode != 0 or "Traceback" in combined or "Error:" in combined:
            rec["status"] = "FAIL"
            rec["reason"] = "进程异常/抛错" if proc.returncode != 0 else "输出含异常堆栈"
        elif case["html"] and not rec["html_path"]:
            rec["status"] = "FAIL"
            rec["reason"] = "未生成 HTML 报告文件"
        elif case["pass_marker"] and case["pass_marker"] not in out:
            rec["status"] = "FAIL"
            rec["reason"] = f"缺少通过标记 '{case['pass_marker']}'"
        else:
            # 检查降级关键词
            hit = [k for k in WARN_KEYWORDS if k in combined]
            if hit:
                rec["status"] = "WARN"
                rec["reason"] = "运行成功但存在降级/数据缺失: " + "、".join(hit[:4])
            else:
                rec["status"] = "PASS"
                rec["reason"] = "正常"
    except subprocess.TimeoutExpired:
        rec["returncode"] = -1
        rec["elapsed"] = round(time.time() - started, 1)
        rec["stdout_tail"] = ""
        rec["stderr_tail"] = f"超时({case['timeout']}s)被杀"
        rec["html_path"] = ""
        rec["status"] = "FAIL"
        rec["reason"] = f"超时 {case['timeout']}s"
    except Exception as e:
        rec["returncode"] = -2
        rec["elapsed"] = round(time.time() - started, 1)
        rec["stdout_tail"] = ""
        rec["stderr_tail"] = str(e)
        rec["html_path"] = ""
        rec["status"] = "FAIL"
        rec["reason"] = f"执行异常: {e}"
    return rec


def build_report(results, env_info, total_elapsed):
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        counts[r["status"]] += 1

    badge = {
        "PASS": '<span class="b b-pass">通过</span>',
        "WARN": '<span class="b b-warn">降级</span>',
        "FAIL": '<span class="b b-fail">失败</span>',
    }

    rows = []
    for r in results:
        args_html = html.escape(r["args"])
        reason = html.escape(r.get("reason", ""))
        out_snip = html.escape((r.get("stdout_tail") or "").strip())
        err_snip = html.escape((r.get("stderr_tail") or "").strip())
        detail = out_snip
        if err_snip:
            detail += f"\n\n[stderr]\n{err_snip}"
        html_link = ""
        if r.get("html_path"):
            html_link = f'<br><span class="fp">报告: {html.escape(r["html_path"])}</span>'
        rows.append(f"""
        <tr>
          <td>{html.escape(r['cat'])}</td>
          <td><b>{html.escape(r['name'])}</b><br><code>{args_html}</code>{html_link}</td>
          <td>{badge[r['status']]}</td>
          <td>{r['elapsed']}s</td>
          <td>{html.escape(reason)}</td>
          <td><details><summary>输出摘要</summary><pre>{detail}</pre></details></td>
        </tr>""")

    summary_color = "#2e7d32" if counts["FAIL"] == 0 else ("#ed6c02" if counts["WARN"] and counts["FAIL"] == 0 else "#c62828")

    env_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
        for k, v in env_info.items()
    )

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    passed_pct = round(100 * counts["PASS"] / len(results)) if results else 0

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>股票分析系统 · 全功能测试报告</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; background:#0f1419; color:#e6edf3; margin:0; padding:24px; }}
  .wrap {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color:#8b949e; font-size: 13px; margin-bottom: 18px; }}
  .cards {{ display:flex; gap:12px; flex-wrap:wrap; margin-bottom: 20px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:14px 18px; min-width:120px; }}
  .card .n {{ font-size: 26px; font-weight:700; }}
  .card .l {{ font-size:12px; color:#8b949e; }}
  .c-pass {{ color:#3fb950; }} .c-warn {{ color:#d29922; }} .c-fail {{ color:#f85149; }}
  table {{ width:100%; border-collapse:collapse; background:#161b22; border-radius:10px; overflow:hidden; }}
  th, td {{ text-align:left; padding:10px 12px; border-bottom:1px solid #21262d; vertical-align:top; font-size:13px; }}
  th {{ background:#21262d; color:#8b949e; font-weight:600; }}
  code {{ background:#0d1117; padding:2px 6px; border-radius:4px; color:#a5d6ff; font-size:12px; }}
  .b {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:12px; font-weight:600; }}
  .b-pass {{ background:rgba(63,185,80,.15); color:#3fb950; }}
  .b-warn {{ background:rgba(210,153,34,.15); color:#d29922; }}
  .b-fail {{ background:rgba(248,81,73,.15); color:#f85149; }}
  pre {{ white-space:pre-wrap; word-break:break-all; background:#0d1117; padding:10px; border-radius:6px; max-height:240px; overflow:auto; font-size:12px; color:#c9d1d9; }}
  details summary {{ cursor:pointer; color:#58a6ff; font-size:12px; }}
  .fp {{ color:#8b949e; font-size:11px; word-break:break-all; }}
  .env {{ margin-top:18px; }}
  .env table td:first-child {{ color:#8b949e; width:200px; }}
</style></head>
<body><div class="wrap">
  <h1>📊 股票分析系统 · 全功能测试报告</h1>
  <div class="sub">生成时间: {now} &nbsp;|&nbsp; 总耗时: {total_elapsed:.1f}s &nbsp;|&nbsp; 通过率: {passed_pct}%</div>
  <div class="cards">
    <div class="card"><div class="n c-pass">{counts['PASS']}</div><div class="l">通过</div></div>
    <div class="card"><div class="n c-warn">{counts['WARN']}</div><div class="l">降级/数据缺失</div></div>
    <div class="card"><div class="n c-fail">{counts['FAIL']}</div><div class="l">失败</div></div>
    <div class="card"><div class="n" style="color:{summary_color}">{len(results)}</div><div class="l">用例总数</div></div>
  </div>
  <table>
    <thead><tr><th>分类</th><th>功能 / 命令</th><th>状态</th><th>耗时</th><th>判定说明</th><th>输出</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <div class="env">
    <h3 style="color:#8b949e;font-size:14px;">运行环境</h3>
    <table><tbody>{env_rows}</tbody></table>
  </div>
  <p class="sub" style="margin-top:18px;">说明: 降级(WARN)=命令成功执行但部分数据源不可用(如东财接口封锁/akshare 代理未配置), 属外部依赖问题而非代码缺陷。失败(FAIL)=进程异常、抛错或超时。</p>
</div></body></html>"""


def collect_env():
    info = {}
    info["Python"] = sys.version.split()[0]
    info["工作目录"] = BASE_DIR
    info["操作系统"] = sys.platform
    info["HTTPS_PROXY"] = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or "(未设置)"
    info["东财 Cookie"] = "已配置" if _em_cookie_set() else "未配置"
    return info


def _em_cookie_set():
    cfg = os.path.join(BASE_DIR, "config", "config.yaml")
    if not os.path.exists(cfg):
        return False
    txt = open(cfg, encoding="utf-8").read()
    m = re.search(r"cookie:\s*(\S+)", txt)
    return bool(m and m.group(1) and m.group(1) != '""' and "qgqp" in m.group(1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-push", action="store_true", help="仅测试+报告, 不推送企微")
    args = ap.parse_args()

    print("=" * 60)
    print("股票分析系统全功能测试")
    print("=" * 60)

    env_info = collect_env()
    results = []
    t0 = time.time()
    for case in TESTS:
        # analyze-all 依赖非空自选股列表, 测试前临时注入并运行后恢复原状
        wl_backup = None
        if case["id"] == "analyze_all":
            wl_path = os.path.join(BASE_DIR, "data", "watchlist.json")
            wl_backup = (wl_path, open(wl_path, "r", encoding="utf-8").read() if os.path.exists(wl_path) else None)
            try:
                with open(wl_path, "w", encoding="utf-8") as f:
                    json.dump(["600519"], f)
            except Exception:
                wl_backup = None

        print(f"▶ 测试 [{case['name']}] ...", flush=True)
        rec = run_case(case)
        mark = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}[rec["status"]]
        print(f"  {mark} {rec['status']} ({rec['elapsed']}s) {rec.get('reason','')}", flush=True)
        results.append(rec)

        # 恢复自选股列表
        if wl_backup is not None:
            wl_path, content = wl_backup
            try:
                if content is None:
                    if os.path.exists(wl_path):
                        os.remove(wl_path)
                else:
                    with open(wl_path, "w", encoding="utf-8") as f:
                        f.write(content)
            except Exception:
                pass
    total = time.time() - t0

    report = build_report(results, env_info, total)
    os.makedirs(REPORT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(REPORT_DIR, f"test_report_{ts}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n📄 测试报告已生成: {report_path}")

    if args.no_push:
        print("[跳过] 按 --no-push 不推送企微")
        return

    try:
        from notify.wecom_bot import send_file_via_bot
        if send_file_via_bot(report_path):
            print("📤 已通过企业微信智能机器人(aibot)推送测试报告(HTML文件)")
        else:
            print("[AIBOT] 文件推送未成功, 详见上方错误", file=sys.stderr)
    except Exception as e:
        print(f"[AIBOT] 推送失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
