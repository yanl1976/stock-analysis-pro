---
name: chinese-stock-analysis
version: 1.1.0
description: A股全维度分析工具 — 个股分析、概念板块扫描、宏观市场概览
category: research
---

# chinese-stock-analysis (stock-analysis-pro)

A股全维度分析工具，覆盖个股深度分析、概念板块扫描、宏观市场概览三大核心功能。

---

## 首次安装

```bash
# 1. 克隆到 ~/.hermes/skills/ 下
git clone https://github.com/bobby1129/stock-analysis-pro.git ~/.hermes/skills/stock-analysis-pro
cd ~/.hermes/skills/stock-analysis-pro

# 2. 安装依赖
pip install -r requirements.txt

# 3. 安装 Playwright 浏览器 (用于东财 F10/股吧/研报采集)
playwright install chromium

# 4. 配置
cp config/config.example.yaml config/config.yaml
# 编辑 config.yaml，填入东财 Cookie (见下方说明)
# 如有代理需求，配置 proxy.https 或设置 HTTPS_PROXY 环境变量
```

**Cookie 获取步骤**：
1. 浏览器打开 https://quote.eastmoney.com/bk/
2. F12 → Network → 刷新页面
3. 找到 `push2.eastmoney.com` 请求
4. 复制 Request Headers 中的 Cookie 值

> ⚠️ Cookie 有效期 1-7 天，过期后概念板块功能降级为离线缓存模式。

---

## 验证安装

```bash
# 1. 验证基本功能（不需要Cookie）
python core/cli.py analyze 600519 --brief
# 预期输出：贵州茅台 实时价格 + PE/PB/市值

# 2. 验证概念板块（需要有效Cookie）
python core/cli.py concept
# 预期输出：Top10概念列表 + 成分股分析
# 如果提示Cookie过期，按提示重新获取

# 3. 验证HTML报告
python core/cli.py analyze 600519 --html
# 预期输出：cache/stock_600519.html 生成
```

如果第1步就失败，检查：
- `playwright install chromium` 是否执行成功
- `pip install -r requirements.txt` 是否完整
- `config/config.yaml` 是否存在（从 `config.example.yaml` 复制）

---

## 命令参考

所有命令在项目根目录下执行：

```bash
# ── 个股分析 ──
python core/cli.py analyze 600519          # 全维度分析 (文本)
python core/cli.py analyze 600519 --html   # HTML 报告
python core/cli.py analyze 600519 --json   # JSON 输出
python core/cli.py analyze 600519 --brief  # 简要模式

# ── 概念板块扫描 ──
python core/cli.py concept                 # 资金流入 Top10
python core/cli.py concept --html          # HTML 报告

# ── 宏观市场概览 ──
python core/cli.py market                  # 文本输出
python core/cli.py market --html           # HTML 报告

# ── 自选股 ──
python core/cli.py add 600519              # 加入自选
python core/cli.py rm 600519               # 移除
python core/cli.py list                    # 查看列表
python core/cli.py analyze-all             # 批量分析自选股
```

---

## 架构 (三层流水线)

```
collectors/          analysis/           plans/
┌─────────────┐    ┌──────────────┐    ┌──────────────┐
│  数据采集层  │ ──▶│   分析引擎层  │ ──▶│  输出规划层   │
│  (多源采集)  │    │  (评分/聚合)  │    │ (格式化呈现)  │
└─────────────┘    └──────────────┘    └──────────────┘
```

### 数据源

| 数据源 | 用途 | 方式 | 状态 |
|--------|------|------|------|
| 腾讯 qt.gtimg.cn | 实时行情(价格/PE/PB/市值) | 直连 | ✅ |
| 新浪 money.finance | K线数据(250日) | 直连 | ✅ |
| 东财 emweb | 公司F10 (Playwright) | Playwright拦截 | ✅ |
| 东财 guba | 股吧热帖 (Playwright) | Playwright拦截 | ✅ |
| 东财 search-api | 概念新闻 | 直连 | ✅ |
| 东财 push2 | 概念资金流 | Cookie+JSONP | ✅ |
| akshare THS | 财务/分红/预测 | 需代理 | ✅ |
| akshare 涨停池 | 涨跌停统计 | 需代理 | ✅ |

---

## 分析维度

### 个股分析 (analyze)

6大维度全方位，综合评分 -100 ~ +100：

| 维度 | 关键数据 |
|------|----------|
| 公司概况 | F10资料、主营、行业、实控人 |
| 技术面 | MA/MACD/KDJ/RSI/BOLL、量价、支撑压力 |
| 基本面 | ROE/毛利率/负债率/增速/分红/一致预期 |
| 估值 | PE/PB/市值/换手率 |
| 资金面 | 成交额统计、北向持股 |
| 舆情 | 股吧情绪、新闻、分析师评级 |

评级: 强看多(≥60) / 看多(≥30) / 偏多(≥10) / 中性(≥-10) / 偏空(≥-30) / 看空(≥-60) / 强看空(<-60)

### 概念板块扫描 (concept)

1. 东财 push2 按资金流入排序概念
2. 过滤非行业概念(风格/市值/地域类)
3. 成分股涨幅分布 → 趋势定性(启动/主升浪/走弱)
4. 概念评分(赚钱效应/介入时机/资金强度/板块宽度)
5. 新闻归因 → 驱动逻辑

### 宏观市场概览 (market)

| 维度 | 关键数据 |
|------|----------|
| 国际环境 | 美债10Y/美联储利率/黄金/白银/原油 |
| 国内经济 | CPI/PMI/M2/LPR → 经济周期+流动性定性 |
| 事件驱动 | 涨停数/连板/热门方向/昨日涨停今日表现 |
| 综合研判 | 多维加权 → 操作建议 |

---

## 输出格式

| 格式 | 参数 | 场景 |
|------|------|------|
| 文本 | (默认) | 终端 |
| HTML | `--html` | 微信/浏览器 (暗色主题) |
| JSON | `--json` | 程序对接 |
| 简要 | `--brief` | 快速概览 |

HTML 报告输出到 `cache/` 目录，可直接发送给用户查看。

---

## ⚠️ 常见陷阱

1. **Playwright 必须安装 Chromium**: `playwright install chromium`，否则 F10/股吧/研报采集失败
2. **东财 Cookie 会过期**: 概念板块返回空或使用离线缓存时，重新获取 Cookie 并更新 `config.yaml`
3. **东财搜索 API 返回 JSONP**: 需要剥离 jQuery 回调包装再解析
4. **腾讯行情 PB 在 d[46]**: 不是 d[52] (52是涨停价)
5. **push2his 个股主力资金流不可用**: 服务器 IP 被封，用成交额+北向替代
