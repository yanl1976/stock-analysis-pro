# stock-analysis-pro 容器镜像
# 基础镜像: Python 3.12 slim (Debian bookworm)
FROM python:3.12-slim

# 避免交互式提示 / 设中文环境
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# 系统依赖: playwright 需要 chromium 运行库
# 使用清华 Debian 镜像源加速 (目标机访问 deb.debian.org 极慢)
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    sed -i 's|security.debian.org|mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    apt-get update && apt-get install -y --no-install-recommends \
    curl wget gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libpango-1.0-0 libcairo2 libasound2 fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/stock-analysis-pro

# 先装 Python 依赖 (利用层缓存) — 清华 PyPI 源加速
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install --no-cache-dir flask -i https://pypi.tuna.tsinghua.edu.cn/simple

# 安装 Playwright chromium (概念板块抓取用) — 清华 Playwright 源加速
ENV PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright
RUN pip install --no-cache-dir playwright -i https://pypi.tuna.tsinghua.edu.cn/simple \
    && playwright install chromium

# 拷贝项目源码 (不含 .dockerignore 排除项)
COPY . .

# 创建 data 目录占位 (实际数据由卷挂载, 但防止路径缺失)
RUN mkdir -p data/klines data/concepts data/reports data/notify_html cache \
    && mkdir -p config

# 默认启动 web 服务 (compose 可 override command)
EXPOSE 8500
CMD ["python", "web/app.py"]
