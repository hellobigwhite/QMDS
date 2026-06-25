import re
import asyncio
from datetime import datetime
import aiohttp
from WebSites import BB_Network_API

# 并发量控制（最大同时进行多少个请求）
CONCURRENCY = 20
# 每次请求超时时间（秒）
REQUEST_TIMEOUT = 20  
# 重试次数
MAX_RETRIES = 3
# 重试间隔（秒）
RETRY_DELAY = 1

async def test_access(session: aiohttp.ClientSession, domain: str):
    """
    使用 aiohttp 异步测试单个域名访问性，带重试机制，
    并同步调用 BB_Network_API 提交结果。
    """
    url = domain if domain.startswith(('http://', 'https://')) else f'https://{domain}'
    status = 2
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with session.get(url, timeout=REQUEST_TIMEOUT) as resp:
                status = 1 if resp.status == 200 else 2
                print(f"✅ {url} (尝试 {attempt}) → 状态码 {resp.status}")
                break
        except Exception as e:
            print(f"❌ {url} (尝试 {attempt}) → 失败：{e}")
            if attempt < MAX_RETRIES:
                await asyncio.sleep(RETRY_DELAY)
            else:
                status = 2

    # 提交测试结果
    domain_clean = re.sub(r'^https?://', '', url)
    BB_Network_API.api_call_with_retry(
        BB_Network_API.update_access_test,
        domain_name=domain_clean,
        access_time=datetime.now().isoformat(),
        access_status=status
    )

async def bound_test(semaphore: asyncio.Semaphore, session: aiohttp.ClientSession, domain: str):
    async with semaphore:
        await test_access(session, domain)

async def run_async(domains: list[str]):
    connector = aiohttp.TCPConnector(limit=CONCURRENCY)
    timeout = aiohttp.ClientTimeout(total=None)
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        tasks = [bound_test(semaphore, session, d) for d in domains]
        await asyncio.gather(*tasks)


def main():
    # 读取 TXT 文件逻辑保持不变，但去掉时间间隔
    try:
        file_path = input("请输入TXT文件路径（每行一个域名）：").strip(" '\"")
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        domains = [line.strip() for line in lines if line.strip()]
    except Exception as e:
        print(f"❌ 读取文件时出错：{e}")
        return

    print(f"共 {len(domains)} 个域名，开始异步测试…（并发 {CONCURRENCY}，超时 {REQUEST_TIMEOUT}s，重试 {MAX_RETRIES} 次）")
    asyncio.run(run_async(domains))
    print("全部测试完成。")

if __name__ == '__main__':
    main()
