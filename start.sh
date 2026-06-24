#!/usr/bin/env bash
# Render部署启动脚本 - 自动安装Node.js
echo "检查Node.js..."
if ! command -v node &> /dev/null; then
    echo "正在安装Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - 2>/dev/null
    apt-get install -y nodejs 2>/dev/null || {
        # 如果apt-get失败，用二进制方式安装
        echo "使用二进制方式安装..."
        curl -sL https://nodejs.org/dist/v20.11.0/node-v20.11.0-linux-x64.tar.xz -o /tmp/node.tar.xz
        tar -xf /tmp/node.tar.xz -C /tmp/
        export PATH="/tmp/node-v20.11.0-linux-x64/bin:$PATH"
        echo "export PATH=/tmp/node-v20.11.0-linux-x64/bin:\$PATH" >> ~/.bashrc
    fi
fi
echo "Node.js版本: $(node -v)"
echo "启动量化分析服务..."
gunicorn data_proxy:app --bind 0.0.0.0:$PORT --timeout 180 --workers 1 --keep-alive 5
