"""
域名处理统一方案
"""


def run(domain: str) -> str:
    # 去除前后空格
    domain = domain.strip()
    # 去除协议头（如 http:// 或 https://）
    if domain.startswith("http://"):
        domain = domain[len("http://"):]
    elif domain.startswith("https://"):
        domain = domain[len("https://"):]
    # 去除末尾的斜杠
    domain = domain.rstrip('/')
    # 如果以 'www.' 开头，则去除
    if domain.startswith('www.'):
        domain = domain[4:]
    return domain


# 调用示例
if __name__ == "__main__":
    test_domains = [
        "   www.example.com/  ",
        "www.testsite.org",
        "example.net/",
        "  example.com  ",
        "  xxx.example.com  ",
        "  https://example.com  ",
        "http://www.another-example.org/"
    ]
    
    for d in test_domains:
        print(run(d))