"""数据爬取模块入口脚本"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from qmds.utils.logger import setup_logger
from qmds.utils.http_client import HttpClient
from qmds.modules.data_scraper import DataScraperModule


def main():
    parser = argparse.ArgumentParser(description="QMDS 数据爬取工具")
    parser.add_argument("action", choices=["discover", "detect", "extract", "pipeline"], help="爬取动作")
    parser.add_argument("--query", "-q", help="搜索关键词")
    parser.add_argument("--url", "-u", help="目标 URL")
    parser.add_argument("--domain", "-d", help="目标域名")
    parser.add_argument("--pages", "-p", type=int, default=3, help="最大页数")
    parser.add_argument("--output", "-o", help="输出 JSON 文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")

    args = parser.parse_args()
    setup_logger(level="DEBUG" if args.verbose else "INFO")

    http = HttpClient()
    module = DataScraperModule(http_client=http)

    try:
        if args.action == "discover":
            result = module.discover_stores(args.query, args.pages)
            print(f"发现 {result.total_found} 个店铺")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result.data, f, ensure_ascii=False, indent=2)
                print(f"已保存到 {args.output}")

        elif args.action == "detect":
            url = args.url or args.query
            if not url:
                parser.error("detect 需要 --url 或 --query")
            result = module.detect_platform(url)
            print(f"平台: {result.platform.value}, 置信度: {result.confidence}")
            if result.product_count:
                print(f"商品数: {result.product_count}")

        elif args.action == "extract":
            domain = args.domain or args.query
            if not domain:
                parser.error("extract 需要 --domain 或 --query")
            result = module.extract_products(domain, args.pages)
            print(f"提取 {result.total_scraped} 个商品")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result.data, f, ensure_ascii=False, indent=2)

        elif args.action == "pipeline":
            if not args.query:
                parser.error("pipeline 需要 --query")
            result = module.run_pipeline(args.query, args.pages)
            print(f"流水线完成: 发现 {result.total_found} -> 提取 {result.total_scraped} 个商品")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(result.data, f, ensure_ascii=False, indent=2)

    except KeyboardInterrupt:
        print("\n用户中断")
        sys.exit(1)
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
