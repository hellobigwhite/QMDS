import re
import time
import requests
from pathlib import Path
from typing import Optional
from qmds.utils.logger import get_logger

log = get_logger("seo_checker")


class SEOChecker:
    """网站收录检查工具 - 使用ScraperAPI查询Google收录"""
    
    def __init__(self, proxy=None, api_key=None):
        self._last_request_time = 0
        self._min_interval = 1.0
        self._api_keys = self._load_api_keys()
        self._current_key_idx = 0
        
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        })
    
    def _load_api_keys(self) -> list:
        """从scraperapi_keys.txt加载API Key"""
        keys = []
        try:
            keys_file = Path(__file__).parent.parent.parent.parent / "scraperapi_keys.txt"
            if keys_file.exists():
                for line in keys_file.read_text(encoding="utf-8").strip().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        keys.append(line)
        except Exception as e:
            log.error(f"加载ScraperAPI Key失败: {e}")
        log.info(f"加载了 {len(keys)} 个ScraperAPI Key")
        return keys
    
    def _get_next_key(self) -> Optional[str]:
        """获取下一个API Key"""
        if not self._api_keys:
            return None
        key = self._api_keys[self._current_key_idx]
        self._current_key_idx = (self._current_key_idx + 1) % len(self._api_keys)
        return key
    
    def _wait_interval(self):
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()
    
    def check_google_index(self, domain: str) -> dict:
        result = {
            "success": False,
            "domain": domain,
            "count": None,
            "method": None,
            "error": None
        }
        
        domain = domain.strip().lower()
        domain = domain.replace("http://", "").replace("https://", "")
        domain = domain.rstrip("/")
        result["domain"] = domain
        
        log.info(f"开始查询 {domain} 的Google收录数量...")
        
        if not self._api_keys:
            result["error"] = "没有可用的ScraperAPI Key，请在scraperapi_keys.txt中配置"
            log.error(result["error"])
            return result
        
        # 使用ScraperAPI查询
        count = self._query_scraperapi(domain)
        if count is not None:
            result["success"] = True
            result["count"] = count
            result["method"] = "scraperapi"
            log.info(f"域名 {domain} Google收录数量: {count}")
            return result
        
        result["error"] = "Google收录查询失败，请检查ScraperAPI Key额度"
        log.warning(f"域名 {domain} 查询失败: {result['error']}")
        return result
    
    def _query_scraperapi(self, domain: str) -> Optional[int]:
        """使用ScraperAPI查询Google收录"""
        self._wait_interval()
        
        api_key = self._get_next_key()
        if not api_key:
            log.error("没有可用的ScraperAPI Key")
            return None
        
        try:
            query = f"site:{domain}"
            params = {
                "api_key": api_key,
                "url": f"https://www.google.com/search?q={query}&num=10&hl=en",
                "render": "true",
            }
            
            log.debug(f"使用ScraperAPI查询: {domain}")
            response = self.session.get(
                "https://api.scraperapi.com/",
                params=params,
                timeout=60
            )
            
            if response.status_code == 200:
                html = response.text
                return self._parse_google_result(html)
            elif response.status_code == 403:
                log.warning("ScraperAPI Key额度已用完")
            else:
                log.warning(f"ScraperAPI返回状态码: {response.status_code}")
                
        except Exception as e:
            log.error(f"ScraperAPI查询失败: {e}")
        
        return None
    
    def _parse_google_result(self, html: str) -> Optional[int]:
        """解析Google搜索结果中的收录数量"""
        patterns = [
            r'About\s+([\d,]+)\s+results',
            r'约\s+([\d,]+)\s+条结果',
            r'([\d,]+)\s+results',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                count_str = match.group(1).replace(",", "")
                try:
                    count = int(count_str)
                    if count > 0:
                        return count
                except ValueError:
                    continue
        
        match = re.search(r'id="result-stats"[^>]*>(.*?)</div>', html, re.IGNORECASE | re.DOTALL)
        if match:
            stats_text = re.sub(r'<[^>]+>', '', match.group(1))
            numbers = re.findall(r'([\d,]+)', stats_text)
            if numbers:
                try:
                    count = int(numbers[0].replace(",", ""))
                    if count > 0:
                        return count
                except ValueError:
                    pass
        
        return None
    
    def batch_check(self, domains: list, interval: float = 1.0, callback=None) -> dict:
        results = {
            "total": len(domains),
            "success": 0,
            "failed": 0,
            "results": []
        }
        
        self._min_interval = interval
        
        for idx, domain in enumerate(domains, 1):
            result = self.check_google_index(domain)
            results["results"].append(result)
            
            if result["success"]:
                results["success"] += 1
            else:
                results["failed"] += 1
            
            if callback:
                callback(idx, len(domains), domain, result)
        
        return results
    
    def close(self):
        self.session.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
