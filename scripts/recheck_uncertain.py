"""重新检测待确认站点（filter_status=uncertain）

对待确认集合里每个站用当前代理服务重跑平台检测：
- 确认 Shopify → 转正（filter_status=unfiltered，进主流程）
- 确认非 Shopify → 标记 not_shopify（从待确认移除）
- 仍无法确认 → 保留 uncertain（记录 rechecked_at，可再重试）

用法:
    python scripts/recheck_uncertain.py --category media
    python scripts/recheck_uncertain.py --category toys_games --limit 100 --workers 4
"""
import argparse
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from qmds.modules.data_scraper.engine import DataScraperModule


def main():
    parser = argparse.ArgumentParser(description="重新检测待确认站点")
    parser.add_argument("--category", required=True, help="类目集合名，如 media、toys_games")
    parser.add_argument("--limit", type=int, default=0, help="最多处理条数（0=全部）")
    parser.add_argument("--workers", type=int, default=4, help="并发数（默认 4，与代理服务容量匹配）")
    args = parser.parse_args()

    module = DataScraperModule()
    try:
        result = module.recheck_uncertain(
            category=args.category,
            limit=args.limit,
            workers=args.workers,
        )
        print(f"\n重检完成: 总数 {result['total']} | "
              f"Shopify {result['shopify']} | 非 Shopify {result['not_shopify']} | "
              f"仍待确认 {result['still_uncertain']} | 异常 {result['errors']}")
    finally:
        module.shutdown()


if __name__ == "__main__":
    main()
