import sys, warnings, os
warnings.filterwarnings('ignore')
project_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_dir)
os.chdir(project_dir)

from web_console import create_task, run_domain_crawl_task

options = {
    "domains": ["colourpop.com"],
    "category": "TestCrawl",
    "max_workers": "1",
    "max_retry_per_site": "2",
    "min_price": "0",
    "reuse_requeue_before_crawl": False,
    "reuse_per_category_limit": "200",
}

task = create_task("测试", "domain_crawl", [])
print("=== 开始爬取 ===")
run_domain_crawl_task(task, options)
print("\n=== 结果 ===")
print("成功:", task.get("success_count"))
print("失败:", task.get("failed_count"))
print("\n=== 日志 ===")
for log in task.get("logs", []):
    print(log)
