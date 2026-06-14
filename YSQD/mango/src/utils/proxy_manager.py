import json
import random

import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

class ProxyManager:
    def __init__(self, proxy_file=None):
        if proxy_file is None:
            proxy_file = os.path.join(BASE_DIR, 'config', 'proxies.json')
        self.proxy_file = proxy_file
        self.proxies = self.load_proxies()
        self.valid_proxies = self.parse_proxies()
    
    def load_proxies(self):
        try:
            with open(self.proxy_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading proxies: {e}")
            return []
    
    def parse_proxies(self):
        parsed = []
        for proxy_str in self.proxies:
            try:
                if 'http://' in proxy_str:
                    proxy_str = proxy_str.replace('http://', '')
                parts = proxy_str.split('@')
                auth = parts[0]
                ip_port = parts[1]
                user, pw = auth.split(':')
                ip, port = ip_port.split(':')
                proxy_url = f"http://{user}:{pw}@{ip}:{port}"
                proxy_dict = {
                    "http": proxy_url,
                    "https": proxy_url
                }
                parsed.append((proxy_dict, f"{ip}:{port}"))
            except Exception:
                continue
        return parsed
    
    def get_random_proxy(self):
        if self.valid_proxies:
            return random.choice(self.valid_proxies)
        return None, None
    
    def get_proxies(self):
        return self.proxies
    
    def get_valid_proxies(self):
        return self.valid_proxies

# 单例模式
global_proxy_manager = None

def get_proxy_manager():
    global global_proxy_manager
    if global_proxy_manager is None:
        global_proxy_manager = ProxyManager()
    return global_proxy_manager

def get_random_proxy():
    manager = get_proxy_manager()
    return manager.get_random_proxy()

def get_proxies():
    manager = get_proxy_manager()
    return manager.get_proxies()

def get_valid_proxies():
    manager = get_proxy_manager()
    return manager.get_valid_proxies()