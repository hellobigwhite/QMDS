"""QMDS 命令行入口"""

import argparse
import json
import sys
from pathlib import Path

from qmds.utils.logger import setup_logger
from qmds.utils.http_client import HttpClient
from qmds.utils.proxy_manager import ProxyManager
from qmds.config import settings
from qmds.modules.data_scraper import DataScraperModule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qmds",
        description="QMDS - 模块化数据管理系统",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")

    sub = parser.add_subparsers(dest="command", required=True)

    # discover
    p_discover = sub.add_parser("discover", help="发现 Shopify 店铺")
    p_discover.add_argument("--query", "-q", default="inurl:collections/all", help="搜索关键词")
    p_discover.add_argument("--pages", "-p", type=int, default=0, help="搜索页数（0 表示遍历全部页面）")
    p_discover.add_argument("--output", "-o", help="输出 JSON 文件")

    # detect
    p_detect = sub.add_parser("detect", help="检测电商平台")
    p_detect.add_argument("--url", "-u", required=True, help="目标 URL")

    # extract
    p_extract = sub.add_parser("extract", help="提取 Shopify 商品")
    p_extract.add_argument("--domain", "-d", required=True, help="目标域名")
    p_extract.add_argument("--pages", "-p", type=int, default=5, help="爬取页数")
    p_extract.add_argument("--output", "-o", help="输出 JSON 文件")

    # pipeline
    p_pipe = sub.add_parser("pipeline", help="完整流水线：发现→检测→提取→清洗")
    p_pipe.add_argument("--query", "-q", default="inurl:collections/all", help="搜索关键词")
    p_pipe.add_argument("--pages", "-p", type=int, default=0, help="最大页数（0 表示遍历全部页面）")
    p_pipe.add_argument("--output", "-o", required=True, help="输出 JSON 文件")

    # web
    p_web = sub.add_parser("web", help="启动 Web 控制台")
    p_web.add_argument("--port", type=int, default=5001, help="监听端口")
    p_web.add_argument("--host", default="0.0.0.0", help="监听地址")
    p_web.add_argument("--dev", action="store_true", help="开发模式 (Flask内置服务器)")

    # list-modules
    sub.add_parser("modules", help="列出所有可用模块")

    return parser


def list_modules():
    """扫描并列出所有已注册模块"""
    print("已安装模块:")
    print(f"  {'模块':<20} {'状态':<8} {'说明'}")
    print(f"  {'-'*20} {'-'*8} {'-'*30}")
    print(f"  {'data_scraper':<20} {'OK':<8} {'数据爬取：店铺发现、平台检测、商品提取'}")
    print("  后续模块可通过 src/qmds/modules/ 目录添加")
    print()
    print("使用方法:")
    print("  qmds discover    - 发现 Shopify 店铺")
    print("  qmds detect      - 检测电商平台")
    print("  qmds extract     - 提取商品数据")
    print("  qmds pipeline    - 完整流水线")
    print("  qmds web         - 启动 Web 控制台")
    print("  qmds modules     - 列出模块")
    print("  qmds --help      - 查看帮助")


def main():
    parser = build_parser()
    args = parser.parse_args()

    setup_logger(level="DEBUG" if args.verbose else "INFO")

    if args.command == "modules":
        list_modules()
        return

    if args.command == "web":
        from qmds.modules.web import WebModule
        web = WebModule(host=args.host, port=args.port, debug=args.dev)
        if args.dev:
            web.run_dev()
        else:
            web.run()
        return

    pm = ProxyManager.from_settings() if settings.load_proxies() else None
    http = HttpClient(proxy_manager=pm)
    module = DataScraperModule(http_client=http)

    try:
        if args.command == "discover":
            result = module.discover_stores(args.query, args.pages)
            print(f"发现 {result.total_found} 个店铺")
            if result.data:
                for item in result.data[:10]:
                    print(f"  {item['url']}")
                if len(result.data) > 10:
                    print(f"  ... 及其他 {len(result.data) - 10} 个")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result.data, f, ensure_ascii=False, indent=2)

        elif args.command == "detect":
            result = module.detect_platform(args.url)
            print(f"URL:    {args.url}")
            print(f"平台:   {result.platform.value}")
            print(f"置信度: {result.confidence}")
            if result.product_count:
                print(f"商品数: {result.product_count}")
            if result.store_name:
                print(f"店名:   {result.store_name}")

        elif args.command == "extract":
            result = module.extract_products(args.domain, args.pages)
            print(f"域名:   {args.domain}")
            print(f"成功:   {result.total_scraped} 个商品")
            print(f"失败:   {len(result.errors)} 个错误")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result.data, f, ensure_ascii=False, indent=2)

        elif args.command == "pipeline":
            result = module.run_pipeline(args.query, args.pages)
            print(f"流水线完成")
            print(f" 发现店铺: {result.total_found}")
            print(f" 提取商品: {result.total_scraped}")
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(result.data, f, ensure_ascii=False, indent=2)
            print(f" 结果保存: {args.output}")

    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
