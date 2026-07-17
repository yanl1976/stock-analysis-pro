---
name: stock-analysis-pro
version: 3.0.0
description: A股全维度分析工具 — 个股分析、概念板块扫描、突破扫描、宏观市场概览、每日复盘、ETF期权扫描
category: research
---

# stock-analysis-pro

A股全维度分析 + ETF 期权分析工具，覆盖个股深度分析、概念板块扫描、突破扫描、宏观市场概览、每日复盘、ETF 期权扫描六大核心功能。

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
# ── 每日复盘 ──
python core/cli.py review              # 文本输出
python core/cli.py review --html       # HTML 报告（推荐，微信/浏览器查看）

# ── ETF 期权扫描 ──
python core/cli.py options             # 全合约扫描（卖方+买方机会）
python core/cli.py options --top 10    # 取前10个
python core/cli.py options --symbol 510050  # 指定标的（默认自动检测）

# ── 持仓管理 ──
python core/cli.py portfolio           # 查看持仓（默认list）
python core/cli.py portfolio --action list   # 查看持仓
python core/cli.py portfolio --action add --code 603893 --name 瑞芯微
python core/cli.py portfolio --action rm --code 603893
python core/cli.py portfolio --action update --code 603893 --cost 120 --shares 100

# ── 个股分析 ──
python core/cli.py analyze 600519          # 全维度分析 (文本)
python core/cli.py analyze 600519 --html   # HTML 报告
python core/cli.py analyze 600519 --json   # JSON 输出
python core/cli.py analyze 600519 --brief  # 简要模式

# ── 概念板块扫描 ──
python core/cli.py concept                 # 资金流入 Top10
python core/cli.py concept --html          # HTML 报告

# ── 突破扫描 (概念技术突破增强版) ──
python core/cli.py breakthrough                      # 热点版块 Top5 × 每版块15只，全扫
python core/cli.py breakthrough --concepts 3 --per 40  # 3个版块 × 每版块40只(更深挖平台股)
python core/cli.py breakthrough --sector 风电          # 只扫指定版块
python core/cli.py breakthrough --stage-filter about_to_launch  # 仅保留"即将启动"态
python core/cli.py breakthrough --json                 # JSON 输出
python core/cli.py breakthrough --html                 # HTML 报告

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
| 东财 行情页 | 涨跌家数+涨跌停 | Playwright | ✅ |
| akshare THS | 财务/分红/预测 | 需代理 | ✅ |
| akshare 涨停池 | 涨跌停统计 | 需代理 | ✅ |
| akshare 期权 | IV/HV/Greeks | 需代理 | ✅ |
| 新浪 期权 | 全合约列表 | 直连 | ✅ |

---

## 分析维度

### 每日复盘 (review)

每日收盘后自动生成综合报告，包含：

1. **指数概览** — 上证/深证/创业板/科创50 实时行情
2. **涨跌统计** — 涨跌家数、涨停跌停数、赚钱效应分析
3. **概念资金流** — 资金净流入 Top10 概念板块
4. **持仓跟踪** — 当日持仓盈亏、涨跌、风险提示
5. **自选股监控** — 自选股列表及表现
6. **宏观指标** — 北向资金、市场情绪、关键事件

输出格式：
- 文本模式：终端查看，紧凑格式
- HTML 模式：可视化报告，暗色主题，支持微信/浏览器查看

数据来源：
- 指数行情：腾讯 qt.gtimg.cn
- 涨跌家数：Playwright 访问东财行情页
- 涨跌停数据：Playwright 拦截东财页面
- 概念资金流：东财 push2 API (需 Cookie)
- 持仓数据：`data/portfolio.json`

### ETF 期权扫描 (options)

全市场 ETF 期权合约扫描，寻找高胜率交易机会：

**卖方机会分析**（卖出期权赚时间价值）：
- IV > HV 的虚值期权（时间价值高估）
- 按时间价值/天排序，找出衰减最快的合约
- 风险提示：Delta 暴露、Gamma 风险、跳空风险

**买方机会分析**（买入期权赌方向）：
- IV < HV 的平值/浅虚值期权（波动率低估）
- 近期事件驱动（财报、政策、技术突破）
- 按性价比排序（预期收益/权利金）

**风险指标**：
- Delta：方向暴露
- Gamma：Delta 变化速度
- Theta：时间价值衰减/天
- Vega：波动率敏感度

**过滤规则**：
- 剩余天数 > 7 天（避免末日轮）
- 成交量 > 100 手（流动性）
- 持仓量 > 500 手（市场关注度）

数据来源：
- 全合约列表：新浪期权频道
- IV/HV/Greeks：akshare 期权数据
- 标的行情：腾讯 qt.gtimg.cn

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

### 突破扫描 (breakthrough) — 概念技术突破增强版

在热点版块（概念扫描结果）内对成分股做**形态识别 + 量价确认 + 趋势状态分类**，是 concept 报告中"技术突破/刚启动"模块的独立增强版。

**编排**：`concept_rank_sina` 热点版块 → `fetch_concept_stocks_sina` 成分股 → 逐股 `kline`(250日)+`realtime` → `classify_stage()` 七态分类 → 按评分降序。

**七态分类**（`analysis/breakout.py:classify_stage`）：
| 状态 | 含义 | 触发信号 |
|------|------|----------|
| `platform` 平台整理 | 波动收敛、箱体未破 | 布林带宽<15% + 持续≥20日 |
| `about_to_launch` 即将启动 | 平台末端、变盘前夜 | 平台 + BB挤压极致 + VCP收缩 + MACD/KDJ金叉 |
| `breakout` 突破 | 放量越过平台上沿 | 创N日新高 + 量能>20日均量×1.5 |
| `running` 已运行 | 已主升、慎追高 | 远离MA、RSI超买 |
| `falling` 下跌 / `trending` 趋势中 / `unknown` 未知 | 其他 | — |

**评分维度**(0-100)：形态分(平台干净度/收敛极致度) + 突破分(距上沿/量能) + 趋势分(MA多头/MACD/KDJ) + 板块分(版块资金排名) + 资金分(量比/换手)。

**与 concept 的关系**：concept 报告内 `breakout_stocks`/`just_started` 原本是轻量启发式（连涨天数+距月低涨幅），现已**去重**——直接调用 `classify_stage()`，全库仅一套形态识别逻辑。breakthrough 命令则是对外独立的完整扫描入口。

**参数**：`--concepts N`(版块数,默认5) / `--per N`(每版块股数,默认15) / `--sector 名称`(单版块) / `--stage-filter`(状态过滤) / `--json` / `--html`。

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
