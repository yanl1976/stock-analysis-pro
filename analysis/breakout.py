# -*- coding: utf-8 -*-
"""突破形态识别 — 纯函数, 可单测

对单只股票的历史 K 线, 识别其当前所处的技术状态:
  - platform         : 平台整理中 (箱体震荡, 波动收敛但未突破)
  - about_to_launch  : 即将启动 (平台已建成 + 波动极致收缩 + 量能/指标转强)
  - breakout         : 已经突破启动 (放量站上平台上沿 + 多头排列)
  - running          : 已主升/远离平台 (谨慎追高)
  - falling          : 下跌趋势
  - trending         : 非平台的一般趋势
  - unknown          : 数据不足

所有函数均为纯函数, 仅依赖传入的序列, 不触网, 便于单测与复用。
"""
from typing import List, Optional, Dict


# ───────────────────────── 指标基元 ─────────────────────────

def sma(values: List[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema(values: List[float], n: int) -> List[float]:
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def macd(closes: List[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Dict:
    if len(closes) < slow + signal:
        return {"dif": None, "dea": None, "hist": None,
                "golden_cross": False, "dead_cross": False}
    e_fast = ema(closes, fast)
    e_slow = ema(closes, slow)
    offset = len(e_fast) - len(e_slow)
    dif = [e_fast[i + offset] - e_slow[i] for i in range(len(e_slow))]
    dea = ema(dif, signal)
    dif_v, dea_v = dif[-1], dea[-1]
    hist = 2 * (dif_v - dea_v)
    gc = len(dif) >= 2 and dif[-2] <= dea[-2] and dif_v > dea_v
    dc = len(dif) >= 2 and dif[-2] >= dea[-2] and dif_v < dea_v
    return {"dif": dif_v, "dea": dea_v, "hist": hist,
            "golden_cross": gc, "dead_cross": dc}


def kdj(highs: List[float], lows: List[float], closes: List[float],
        n: int = 9, m1: int = 3, m2: int = 3) -> Dict:
    """返回最近两根 K/D, 用于判断金叉/死叉"""
    if len(closes) < n + 1:
        return {"k": 50, "d": 50, "j": 50, "golden_cross": False}
    rsvs = []
    for i in range(n - 1, len(closes)):
        hn = max(highs[i - n + 1:i + 1])
        ln = min(lows[i - n + 1:i + 1])
        rsvs.append(50 if hn == ln else (closes[i] - ln) / (hn - ln) * 100)
    k, d = 50.0, 50.0
    ks, ds = [], []
    for r in rsvs:
        k = (m1 - 1) / m1 * k + 1 / m1 * r
        d = (m2 - 1) / m2 * d + 1 / m2 * k
        ks.append(k)
        ds.append(d)
    k0, d0 = ks[-1], ds[-1]
    j0 = 3 * k0 - 2 * d0
    gc = ks[-2] <= ds[-2] and k0 > d0
    return {"k": k0, "d": d0, "j": j0, "golden_cross": gc}


def boll(closes: List[float], n: int = 20, k: float = 2):
    if len(closes) < n:
        return (None, None, None)
    recent = closes[-n:]
    mid = sum(recent) / n
    std = (sum((x - mid) ** 2 for x in recent) / n) ** 0.5
    return (mid + k * std, mid, mid - k * std)


def bb_width(closes: List[float], n: int = 20, k: float = 2) -> Optional[float]:
    u, m, l = boll(closes, n, k)
    if not m:
        return None
    return (u - l) / m


def rolling_bb_width(closes: List[float], n: int = 20, k: float = 2,
                     lookback: int = 120) -> List[float]:
    """返回最近 lookback 日内, 每日布林带宽序列 (用于求当前带宽分位)"""
    widths = []
    start = max(n, len(closes) - lookback)
    for i in range(start, len(closes)):
        w = bb_width(closes[:i + 1], n, k)
        if w is not None:
            widths.append(w)
    return widths


def vol_ratio(volumes: List[float], short: int = 5, long: int = 20) -> float:
    if len(volumes) < long or sum(volumes[-long:]) == 0:
        return 1.0
    return (sum(volumes[-short:]) / short) / (sum(volumes[-long:]) / long)


def atr(highs: List[float], lows: List[float], closes: List[float],
        n: int = 14) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs[-n:]) / n


# ───────────────────────── 形态识别 ─────────────────────────

def detect_platform(highs: List[float], lows: List[float], closes: List[float],
                    window: int = 60, band_thresh: float = 0.15) -> Dict:
    """检测最近 window 日是否处于平台 (箱体) 整理"""
    if len(closes) < window:
        return {"is_platform": False, "band_width": None,
                "support": None, "resistance": None, "mid": None}
    h = highs[-window:]
    l = lows[-window:]
    c = closes[-window:]
    hi, lo = max(h), min(l)
    midp = sum(c) / len(c)
    band_width = (hi - lo) / midp if midp else 0
    return {"is_platform": band_width < band_thresh,
            "band_width": band_width,
            "support": lo, "resistance": hi, "mid": midp}


def vcp_contractions(highs: List[float], lows: List[float], volumes: List[float],
                     window: int = 60, segments: int = 3) -> int:
    """VCP (Volatility Contraction Pattern): 统计波动+量能逐级收缩的次数

    将最近 window 日均分为 segments 段, 从老到新检查每段是否
    振幅收窄且量能不放大 — 连续收缩轮次越多, 平台越成熟、突破越可期。
    """
    if len(highs) < window:
        return 0
    seg_len = window // segments
    rngs, vols = [], []
    for i in range(segments):
        start = len(highs) - (i + 1) * seg_len
        end = len(highs) - i * seg_len if i > 0 else len(highs)
        hh = max(highs[start:end])
        ll = min(lows[start:end])
        rngs.append(hh - ll)
        vols.append(sum(volumes[start:end]) / max(1, end - start))
    # 转为 老→新 顺序
    rngs.reverse()
    vols.reverse()
    cnt = 0
    for i in range(1, len(rngs)):
        if rngs[i] < rngs[i - 1] and vols[i] <= vols[i - 1] * 1.15:
            cnt += 1
    return cnt


# ───────────────────────── 状态分类 ─────────────────────────

_STAGE_BASE = {
    "about_to_launch": 60,
    "breakout": 75,
    "running": 55,
    "platform": 30,
    "trending": 40,
    "falling": 10,
    "unknown": 0,
}


def _score(stage: str, d: Dict, price: float, ma60: Optional[float]) -> int:
    base = _STAGE_BASE.get(stage, 0)
    add = 0
    if d.get("bb_squeeze_pct", 1) < 0.2:
        add += 10
    if d.get("vcp", 0) >= 2:
        add += 8
    if d.get("macd_gc"):
        add += 6
    if d.get("kdj_gc"):
        add += 4
    if d.get("ma_bull"):
        add += 6
    vr = d.get("vol_ratio", 1) or 1
    if vr > 1.5:
        add += 6
    elif vr > 1.0:
        add += 3
    # 已主升且过度远离均线 → 追高风险扣分
    if stage == "running" and ma60 and price > ma60 * 1.4:
        add -= 10
    return max(0, min(100, base + add))


def classify_stage(closes: List[float], highs: List[float], lows: List[float],
                   volumes: List[float], price: Optional[float] = None,
                   window: int = 60) -> Dict:
    """核心: 输入 OHLCV 序列, 输出状态分类 + 评分 + 信号 + 明细

    Args:
        closes/highs/lows/volumes: 时间升序的浮点序列
        price: 最新价 (默认取 closes[-1]); 用于突破阈值判定
        window: 平台检测回看窗口 (默认 60 日)
    """
    if price is None:
        price = closes[-1] if closes else 0
    out = {"stage": "unknown", "score": 0, "signals": [], "details": {}}
    if len(closes) < 40:
        return out

    plat = detect_platform(highs, lows, closes, window)
    widths = rolling_bb_width(closes, 20, 2, lookback=120)
    cur_w = bb_width(closes)
    squeeze = 0.5
    if widths and cur_w is not None:
        below = sum(1 for w in widths if w <= cur_w)
        squeeze = below / len(widths)  # 0=最窄(挤压), 1=最宽

    m = macd(closes)
    kd = kdj(highs, lows, closes)
    vr = vol_ratio(volumes)
    vcp = vcp_contractions(highs, lows, volumes)

    ma5, ma10, ma20, ma60 = (sma(closes, p) for p in (5, 10, 20, 60))
    bull = all(x is not None for x in (ma5, ma10, ma20, ma60)) and ma5 > ma10 > ma20 > ma60

    resistance = plat["resistance"]
    support = plat["support"]
    above_resistance = resistance is not None and price >= resistance
    near_resistance = resistance is not None and price >= resistance * 0.97

    d = {
        "band_width": round(plat["band_width"], 4) if plat["band_width"] else None,
        "is_platform": plat["is_platform"],
        "bb_squeeze_pct": round(squeeze, 3),
        "vol_ratio": round(vr, 2),
        "vcp": vcp,
        "macd_hist": round(m["hist"], 3) if m["hist"] is not None else None,
        "macd_gc": m["golden_cross"],
        "kdj_k": round(kd["k"], 1),
        "kdj_d": round(kd["d"], 1),
        "kdj_gc": kd["golden_cross"],
        "ma_bull": bull,
        "resistance": round(resistance, 2) if resistance else None,
        "support": round(support, 2) if support else None,
        "pct_to_resistance": round((price / resistance - 1) * 100, 2) if resistance else None,
    }

    signals = []
    if plat["is_platform"]:
        signals.append("平台整理")
    if squeeze < 0.25:
        signals.append("布林带极致收缩")
    if vcp >= 2:
        signals.append(f"VCP波动收缩×{vcp}")
    if m["golden_cross"]:
        signals.append("MACD金叉")
    if kd["golden_cross"]:
        signals.append("KDJ金叉")
    if vr > 1.5:
        signals.append(f"放量(量比{vr:.2f})")
    if bull:
        signals.append("均线多头排列")
    if above_resistance:
        signals.append("突破平台上沿")

    # ── 状态判定 ──
    hist_pos = m["hist"] is not None and m["hist"] > 0 and m["dif"] > m["dea"]
    if above_resistance and (vr > 1.3 or bull or m["golden_cross"]):
        stage = "breakout"
        if ma60 and price > ma60 * 1.25:
            stage = "running"
    elif (plat["is_platform"] and squeeze < 0.3 and vr > 0.85
          and (m["golden_cross"] or kd["golden_cross"] or hist_pos)):
        stage = "about_to_launch"
    elif plat["is_platform"]:
        stage = "platform"
    else:
        if ma60 and price < ma60 and (m["hist"] is not None and m["hist"] < 0):
            stage = "falling"
        else:
            stage = "trending"

    score = _score(stage, d, price, ma60)
    out.update({"stage": stage, "score": score, "signals": signals, "details": d})
    return out


STAGE_LABELS = {
    "platform": "🟦 平台整理",
    "about_to_launch": "🟢 即将启动",
    "breakout": "🚀 突破启动",
    "running": "🔥 已主升(慎追)",
    "falling": "📉 下跌",
    "trending": "📈 趋势中",
    "unknown": "❓ 未知",
}


if __name__ == "__main__":
    # 快速验证: python analysis/breakout.py 600519 000001 ...
    import sys
    import os
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from collectors.quote import kline, realtime
    for sym in sys.argv[1:]:
        try:
            kl = kline(sym, days=250)
            rt = realtime(sym)
            res = classify_stage(
                [b["close"] for b in kl], [b["high"] for b in kl],
                [b["low"] for b in kl], [b["volume"] for b in kl],
                price=rt["price"],
            )
            print(f"\n{rt['name']}({sym}) ¥{rt['price']} {rt['change_pct']:+.2f}%")
            print(f"  状态: {STAGE_LABELS.get(res['stage'])}  评分: {res['score']}")
            print(f"  信号: {' | '.join(res['signals']) or '—'}")
            print(f"  明细: {res['details']}")
        except Exception as e:
            print(f"\n{sym} 失败: {e}")
