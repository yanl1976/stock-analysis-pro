#!/usr/bin/env bash
# 目标机初始化: 装 Docker + 配国内镜像源 + 建 /opt 部署目录
# 用法(需 sudo):  sudo bash init_host.sh
set -euo pipefail

echo "==> [1/4] 安装 docker.io + docker-compose-v2"
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2

echo "==> [2/4] 配置 Docker 国内镜像加速器 (DaoCloud)"
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "registry-mirrors": [
    "https://docker.m.daocloud.io"
  ]
}
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now docker
sudo systemctl restart docker

echo "==> [3/4] 将当前用户加入 docker 组 (免 sudo, 需重登录生效)"
sudo usermod -aG docker "$USER"

echo "==> [4/4] 创建 /opt 部署根目录"
sudo mkdir -p /opt/stock-analysis-pro
sudo chown -R "$USER:$USER" /opt/stock-analysis-pro

echo "==> 验证 hello-world (可能因镜像首次拉取稍慢)"
sudo docker run --rm hello-world || echo "hello-world 拉取失败, 请检查网络/镜像源"
echo "DONE. 注意: docker 组生效需重新登录(或 newgrp docker)。"
