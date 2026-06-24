# 量化分析 v9.0 — Docker部署
# 数据流: 腾讯API(实时行情) + SEC EDGAR XBRL(美股财报) + westock(备用)
# 用法: docker build -t quant-tool . && docker run -p 5001:5001 quant-tool

FROM python:3.11-slim

# 安装Node.js（westock备用数据源，npm包缓存化减少启动时间）
RUN apt-get update && apt-get install -y curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    npm cache clean --force && \
    npm install -g westock-data-clawhub@1.0.4 && \
    rm -rf /var/lib/apt/lists/* && \
    node -v && npm -v

WORKDIR /app

# Python依赖（精简：SEC EDGAR仅需requests，yfinance/westock为可选）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 项目文件 — 必须包含quant_tool/子模块 + JSON数据文件
COPY data_proxy.py quant.html portfolio.json industry_chains.json ./
COPY quant_tool/ ./quant_tool/

# SEC EDGAR缓存目录
RUN mkdir -p quant_tool/.sec_cache

# 端口
EXPOSE 5001

# 启动（单worker避免westock并发冲突）
CMD gunicorn data_proxy:app --bind 0.0.0.0:$PORT --timeout 180 --workers 1 --keep-alive 5
