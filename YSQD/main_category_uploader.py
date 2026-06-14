import requests
import json

class MainCategoryUploader:
    def __init__(self, domain=None):
        self.session = requests.Session()
        self.domain = domain
        if domain:
            self.base_url = f"https://www.{domain}"
            self.category_search_url = f"{self.base_url}/cf-updata/category/categorySearch.php"
            self.main_category_set_url = f"{self.base_url}/cf-updata/category/mainCategorySet.php"
            self.headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
                'Referer': f'https://www.{domain}/cf-updata/category/main_category.php'
            }
        else:
            # 默认值，防止初始化时不设置URL
            self.base_url = None
            self.category_search_url = None
            self.main_category_set_url = None
            self.headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
            }
    
    def set_domain(self, domain):
        """设置当前处理的域名"""
        self.domain = domain
        self.base_url = f"https://www.{domain}"
        self.category_search_url = f"{self.base_url}/cf-updata/category/categorySearch.php"
        self.main_category_set_url = f"{self.base_url}/cf-updata/category/mainCategorySet.php"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': f'https://www.{domain}/cf-updata/category/main_category.php'
        }
    
    def get_category_id(self, category_name):
        """
        获取分类ID
        """
        if not self.category_search_url:
            print(f"错误: 域名未设置，无法获取分类")
            return None
        
        try:
            print(f"正在请求分类列表...")
            
            # 模拟layui.table的请求，获取分类数据
            payload = {
                'page': 1,
                'limit': 100  # 获取足够多的分类
            }
            
            response = self.session.post(
                self.category_search_url, 
                data=payload, 
                headers=self.headers
            )
            response.raise_for_status()
            
            print(f"分类列表请求状态码: {response.status_code}")
            
            # 解析返回的JSON数据
            try:
                result = response.json()
            except:
                # 如果不是JSON，尝试直接解析文本
                print(f"返回内容不是JSON: {response.text[:200]}")
                return None
            
            print(f"分类列表数据: {json.dumps(result, ensure_ascii=False)[:500]}")
            
            # 查找匹配的分类
            if 'data' in result:
                categories = result['data']
            elif isinstance(result, list):
                categories = result
            else:
                print(f"无法解析分类数据结构")
                return None
            
            # 先收集所有分类名称用于调试
            all_category_names = []
            for category in categories:
                # 查找分类名称字段（可能是term_name或name）
                name = category.get('term_name') or category.get('name') or ''
                if name:
                    all_category_names.append(name)
            
            print(f"所有可用分类: {', '.join(all_category_names)}")
            
            # 尝试多种匹配方式
            for category in categories:
                # 查找分类名称字段（可能是term_name或name）
                name = category.get('term_name') or category.get('name') or ''
                # 查找分类ID字段（可能是term_id或id）
                cid = category.get('term_id') or category.get('id')
                
                if name and cid:
                    print(f"找到分类: ID={cid}, 名称={name}")
                    
                    # 方式1: 精确匹配
                    if name.strip() == category_name.strip():
                        print(f"找到匹配的分类（精确匹配）: {category_name}, ID={cid}")
                        return str(cid)
                    
                    # 方式2: 大小写不敏感匹配
                    if name.strip().lower() == category_name.strip().lower():
                        print(f"找到匹配的分类（大小写不敏感）: {category_name} -> {name}, ID={cid}")
                        return str(cid)
                    
                    # 方式3: 去掉空格后匹配
                    if name.replace(' ', '').lower() == category_name.replace(' ', '').lower():
                        print(f"找到匹配的分类（去掉空格）: {category_name} -> {name}, ID={cid}")
                        return str(cid)
                    
                    # 方式4: 部分匹配（目标名称包含分类名称，或分类名称包含目标名称）
                    target_lower = category_name.strip().lower()
                    name_lower = name.strip().lower()
                    if target_lower in name_lower or name_lower in target_lower:
                        print(f"找到匹配的分类（部分匹配）: {category_name} -> {name}, ID={cid}")
                        return str(cid)
            
            # 如果没找到，使用回退机制：选择第一个分类
            print(f"未找到分类: {category_name}")
            if categories and len(categories) > 0:
                # 选择第一个分类作为回退
                fallback_category = categories[0]
                fallback_name = fallback_category.get('term_name') or fallback_category.get('name') or ''
                fallback_cid = fallback_category.get('term_id') or fallback_category.get('id')
                if fallback_name and fallback_cid:
                    print(f"⚠️ 使用回退分类: {fallback_name}, ID={fallback_cid}")
                    return str(fallback_cid)
            
            print(f"未找到可用分类")
            return None
            
        except Exception as e:
            print(f"获取分类ID失败: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def set_main_category(self, category_id):
        """
        设为主分类
        """
        if not self.main_category_set_url:
            print(f"错误: 域名未设置，无法设置主分类")
            return False
        
        try:
            print(f"正在设置主分类，ID={category_id}")
            
            # 模拟AJAX请求，设为主分类
            payload = {
                'term_id': category_id
            }
            
            response = self.session.post(
                self.main_category_set_url, 
                data=payload, 
                headers=self.headers
            )
            response.raise_for_status()
            
            print(f"设为主分类请求状态码: {response.status_code}")
            print(f"设为主分类返回内容: {response.text}")
            
            # 解析返回结果
            try:
                result = response.json()
                if result.get('error'):
                    print(f"设置失败: {result.get('msg')}")
                    return False
                else:
                    print(f"设置成功: {result.get('msg')}")
                    return True
            except:
                # 如果不是JSON，根据状态码判断
                if response.status_code == 200:
                    print("设置成功（非JSON响应）")
                    return True
                else:
                    print("设置失败")
                    return False
            
        except Exception as e:
            print(f"设为主分类失败: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def upload_main_category(self, domain, main_category, progress_callback):
        """
        上传主分类
        """
        # 设置当前处理的域名
        self.set_domain(domain)
        progress_callback(f"设置目标站点: {domain}")
        
        # 处理主分类，只取最后一级分类名称
        if "|||" in main_category:
            main_category = main_category.split("|||")[-1].strip()
            progress_callback(f"处理后的主分类: {main_category}")
        
        progress_callback(f"开始设置主分类: {main_category}")
        
        # 步骤1: 获取分类ID
        progress_callback("正在获取分类ID...")
        category_id = self.get_category_id(main_category)
        if not category_id:
            progress_callback(f"未找到分类: {main_category}")
            return 0
        
        progress_callback(f"找到分类ID: {category_id}")
        
        # 步骤2: 设为主分类
        progress_callback("正在设为主分类...")
        success = self.set_main_category(category_id)
        if success:
            progress_callback("主分类设置成功")
            return 1
        else:
            progress_callback("主分类设置失败")
            return 0
