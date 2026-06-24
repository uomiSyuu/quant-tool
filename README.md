# 量化分析工具 v7.0 — 部署说明

## 一、Docker部署（推荐）

```bash
cd deploy
docker build -t quant-tool .
docker run -d -p 5001:5001 -e ACCESS_KEY=quant888 --name quant-tool quant-tool
```

访问：http://你的服务器IP:5001/quant.html?key=quant888

## 二、直接Python运行

```bash
cd deploy
pip install -r requirements.txt
pip install gunicorn

# 安装Node.js + westock数据源（支持A股/港股）
npm install -g westock-data-clawhub

# 启动
ACCESS_KEY=quant888 gunicorn data_proxy:app --bind 0.0.0.0:5001 --timeout 120 --workers 2
```

## 三、环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| ACCESS_KEY | (空=无密码) | 访问密码，URL加 ?key=xxx |
| PORT | 5001 | 端口 |
| WESTOCK_CMD | 自动检测 | 数据源命令 |

## 四、股票代码格式

- 美股：MSFT, NVDA, TSLA
- A股：600519(茅台), 000063(中兴), 300750(宁德)
- 港股：00700(腾讯), 09988(阿里)

## 五、文件清单

```
deploy/
├── data_proxy.py      # 后端服务（双数据源自动切换）
├── quant.html          # 前端页面 v7.0
├── requirements.txt    # Python依赖
├── Dockerfile          # Docker构建文件
└── README.md           # 本文件
```
