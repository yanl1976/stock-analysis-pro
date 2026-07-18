# -*- coding: utf-8 -*-
"""全市场 A 股 K 线批量拉取 — 永久落盘 + 断点续传 + 完整性校验。

数据源: 腾讯日线(不复权, 与回测口径一致), 分页拉全历史(上市至今)。
股票池: akshare 全市场 A 股代码(约5500+)。
特性:
  - 断点续传: 已落盘且校验通过(默认7天内)则跳过
  - 完整性校验: 字段有效/最小长度/连续性/新鲜度
  - 并发抓取 + 限速, 失败重试
  - 进度 + 汇总统计

用法:
  python scripts/fetch_all_klines.py                 # 全量(跳过已有效)
  python scripts/fetch_all_klines.py --force         # 全量重抓(含已有效)
  python scripts/fetch_all_klines.py --limit 50      # 小范围测试
  python scripts/fetch_all_klines.py --workers 8 --max-stale 7
"""
import os
import sys
import time
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from plans.breakout_scan import (
    _fetch_full_kline, _kl_validate, _kl_struct_ok, _kl_fields_ok, _kl_save, _kl_path,
    _kl_load, _kline_net_hits, MAX_STALE_DAYS, MIN_BARS,
)

try:
    import akshare as ak
except Exception as e:
    print("akshare 不可用:", e)
    sys.exit(1)

# 低于此根数视为"不够全"(如旧缓存仅900根), 即使新鲜也重抓升级为全历史
MIN_FULL_BARS = 2000


def get_all_symbols():
    """返回 [(code6, name), ...] 全市场 A 股"""
    df = ak.stock_info_a_code_name()
    code_col = "code" if "code" in df.columns else ("symbol" if "symbol" in df.columns else None)
    name_col = "name" if "name" in df.columns else None
    if code_col is None:
        raise RuntimeError(f"无法识别代码列, columns={list(df.columns)}")
    out = []
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        if not code.isdigit() or len(code) != 6:
            continue
        name = str(row[name_col]) if name_col else ""
        out.append((code, name))
    return out


def process(symbol, name, max_stale, force):
    """返回 (status, info)。status ∈ {skip, fetched, failed}"""
    if not force:
        kl, _, sa, short = _kl_load(symbol)
        if kl:
            if sa and _kl_struct_ok(kl):
                return "skip", f"stale_accepted bars={len(kl)}"
            if short and _kl_fields_ok(kl):
                return "skip", f"short_history bars={len(kl)}"
            # 已全历史(根数足够)且新鲜 → 跳过; 旧缓存(仅900根)自动升级
            if len(kl) >= MIN_FULL_BARS and _kl_validate(kl, max_stale=max_stale)[0]:
                return "skip", f"ok bars={len(kl)}"
    try:
        kl = _fetch_full_kline(symbol)
    except Exception as e:
        return "failed", f"fetch err: {e}"
    if not kl:
        return "failed", "empty"
    if not _kl_fields_ok(kl):
        return "failed", "fields bad"
    fresh_ok, info = _kl_validate(kl, max_stale=max_stale)
    # 次新股: 数据有效但上市不足 MIN_BARS 天, 标记 short_history 落盘, 避免反复重抓
    if len(kl) < MIN_BARS:
        _kl_save(symbol, kl, short_history=True)
        return "fetched", f"short_history bars={len(kl)}"
    # 数据源天然缺失(退市/停牌股): 标记 stale_accepted 避免反复重抓
    old_last = _kl_load(symbol)[1]
    new_last = kl[-1]["date"][:10]
    try:
        new_stale = (datetime.now() - datetime.strptime(new_last, "%Y-%m-%d")).days
    except Exception:
        new_stale = 0
    accepted = (old_last is None and new_stale > MAX_STALE_DAYS) or \
               (old_last is not None and old_last == new_last and new_stale > MAX_STALE_DAYS)
    _kl_save(symbol, kl, stale_accepted=accepted)
    return "fetched", (info if fresh_ok else f"stale_accepted bars={len(kl)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略已有缓存, 全量重抓")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 只(测试)")
    ap.add_argument("--offset", type=int, default=0, help="从列表第 N 只开始(分批续传)")
    ap.add_argument("--workers", type=int, default=6, help="并发数")
    ap.add_argument("--max-stale", type=int, default=7, help="缓存新鲜度阈值(天), 超过则重抓")
    ap.add_argument("--symbols-file", default="", help="仅抓取指定代码文件(每行一个6位代码, 用于补齐缺失)")
    args = ap.parse_args()

    if args.symbols_file:
        with open(args.symbols_file, "r", encoding="utf-8") as fh:
            codes = [ln.strip() for ln in fh if ln.strip() and len(ln.strip()) == 6 and ln.strip().isdigit()]
        symbols = [(c, "") for c in codes]
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 从文件读取 {len(symbols)} 只代码")
    else:
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 获取全市场 A 股列表...")
        symbols = get_all_symbols()
        if args.offset:
            symbols = symbols[args.offset:]
        if args.limit:
            symbols = symbols[:args.limit]
    print(f"  共 {len(symbols)} 只 (offset={args.offset}, workers={args.workers}, max_stale={args.max_stale}, force={args.force})")

    stats = {"skip": 0, "fetched": 0, "failed": 0}
    failed_list = []
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process, s, n, args.max_stale, args.force): (s, n)
                for s, n in symbols}
        done = 0
        for fut in as_completed(futs):
            s, n = futs[fut]
            done += 1
            try:
                status, info = fut.result()
            except Exception as e:
                status, info = "failed", str(e)
            stats[status] = stats.get(status, 0) + 1
            if status == "failed":
                failed_list.append((s, info))
            if done % 200 == 0 or status == "failed":
                print(f"  [{done}/{len(symbols)}] {s} {status} {info}")

    # 失败项单线程重试一次
    if failed_list:
        print(f"重试 {len(failed_list)} 只失败项(单线程)...")
        still_fail = []
        for s, _ in failed_list:
            try:
                kl = _fetch_full_kline(s)
                if kl and _kl_fields_ok(kl):
                    if len(kl) < MIN_BARS:
                        _kl_save(s, kl, short_history=True)
                    else:
                        _kl_save(s, kl)
                    stats["fetched"] += 1
                    stats["failed"] -= 1
                else:
                    still_fail.append(s)
            except Exception:
                still_fail.append(s)
        failed_list = [(s, "retry failed") for s in still_fail]

    print(f"\n完成: 跳过{stats.get('skip',0)} 新抓{stats.get('fetched',0)} 失败{stats.get('failed',0)} "
          f"耗时{round(time.time()-t0, 1)}s 触网{_kline_net_hits()}次")
    if failed_list:
        print("失败样例:", failed_list[:20])


if __name__ == "__main__":
    main()
