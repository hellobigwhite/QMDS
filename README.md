# QMDS - 模块化数据管理系统

模块化可扩展的数据管理与爬取系统。

## 项目结构

```
QMDS/
├── src/qmds/             # 主源码
│   ├── config/           # 全局配置
│   ├── core/             # 抽象基类与异常
│   ├── utils/            # 共享工具（HTTP、代理、日志、重试）
│   ├── db/               # 数据库客户端
│   └── modules/          # 业务模块（可插拔）
│       └── data_scraper/ # 数据爬取模块
├── tests/                # 测试
├── scripts/              # 入口脚本
├── YSQD/                 # 参考项目
└── requirements.txt
```

## 快速开始

```bash
pip install -r requirements.txt

# 发现店铺
python scripts/run_scraper.py discover -q "inurl:collections/all" -p 3

# 检测平台
python scripts/run_scraper.py detect -u "https://example.com"

# 提取商品
python scripts/run_scraper.py extract -d "example.com" -p 5

# 完整流水线
python scripts/run_scraper.py pipeline -q "inurl:collections/all" -o result.json
```

## 模块

| 模块 | 状态 | 说明 |
|------|------|------|
| data_scraper | ✅ | Shopify 店铺发现、平台检测、商品提取 |

## 开发

```bash
pip install -e .
pytest tests/
```
