import requests
import urllib3


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class MainCategoryUploader:
    def __init__(self, domain=None):
        self.session = requests.Session()
        self.domain = None
        self.base_url = None
        self.category_search_url = None
        self.main_category_set_url = None
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            )
        }
        if domain:
            self.set_domain(domain)

    def set_domain(self, domain):
        self.domain = domain
        self.base_url = f"https://www.{domain}"
        self.category_search_url = f"{self.base_url}/cf-updata/category/categorySearch.php"
        self.main_category_set_url = f"{self.base_url}/cf-updata/category/mainCategorySet.php"
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/91.0.4472.124 Safari/537.36"
            ),
            "Referer": f"https://www.{domain}/cf-updata/category/main_category.php",
        }

    def _request(self, method, url, **kwargs):
        return self.session.request(method, url, timeout=30, verify=False, **kwargs)

    def _extract_rows(self, payload):
        if isinstance(payload, dict):
            return payload.get("data", [])
        if isinstance(payload, list):
            return payload
        return []

    def _fetch_all_categories(self, keyword=""):
        if not self.category_search_url:
            raise RuntimeError("域名未设置，无法获取分类")

        rows = []
        page = 1
        limit = 200
        max_pages = 500
        last_page_ids = None

        while True:
            if page > max_pages:
                break
            payload = {"page": page, "limit": limit}
            if keyword:
                payload["category_name"] = keyword
            response = self._request(
                "POST",
                self.category_search_url,
                data=payload,
                headers=self.headers,
            )
            response.raise_for_status()

            try:
                result = response.json()
            except ValueError as exc:
                raise RuntimeError(f"分类接口返回不是 JSON: {response.text[:200]}") from exc

            current_rows = self._extract_rows(result)
            if not current_rows:
                break

            current_page_ids = tuple(
                str(row.get("term_id") or row.get("id") or "").strip() for row in current_rows
            )
            if current_page_ids and current_page_ids == last_page_ids:
                break
            last_page_ids = current_page_ids

            rows.extend(current_rows)

            total = None
            if isinstance(result, dict):
                try:
                    total = int(result.get("count", 0))
                except Exception:
                    total = None

            if total is not None and total > 0 and len(rows) >= total:
                break
            page += 1

        unique = {}
        for row in rows:
            term_id = str(row.get("term_id") or row.get("id") or "").strip()
            if not term_id:
                continue
            unique[term_id] = {
                "term_id": term_id,
                "parent_id": str(row.get("parent_id") or "0").strip(),
                "term_name": (row.get("term_name") or row.get("name") or "").strip(),
                "slug": (row.get("slug") or "").strip(),
            }
        return list(unique.values())

    def _normalize_category_path(self, category_name):
        text = (category_name or "").strip()
        if not text:
            return ""
        if "主打分类为：" in text:
            text = text.split("主打分类为：", 1)[1].strip()
        return "|||".join(part.strip() for part in text.split("|||") if part.strip())

    def _build_full_paths(self, rows):
        by_id = {row["term_id"]: row for row in rows}
        cache = {}

        def build_path(term_id):
            if term_id in cache:
                return cache[term_id]
            row = by_id.get(term_id)
            if not row:
                return ""
            name = row.get("term_name", "").strip()
            parent_id = row.get("parent_id", "0")
            if not parent_id or parent_id == "0" or parent_id not in by_id:
                cache[term_id] = name
                return name
            parent_path = build_path(parent_id)
            full_path = f"{parent_path}|||{name}" if parent_path else name
            cache[term_id] = full_path
            return full_path

        result = []
        for row in rows:
            item = dict(row)
            item["full_path"] = build_path(row["term_id"])
            result.append(item)
        return result

    def _norm(self, value):
        return (value or "").strip().lower()

    def _format_candidates(self, rows, limit=20):
        return [
            (
                f"{row.get('full_path') or row.get('term_name') or ''} "
                f"[ID={row.get('term_id')}, parent_id={row.get('parent_id') or '0'}, slug={row.get('slug') or ''}]"
            )
            for row in rows[:limit]
        ]

    def _pick_first_top_level(self, rows):
        for row in rows:
            if str(row.get("parent_id") or "0").strip() == "0":
                return row
        return None

    def get_category_id(self, category_name, progress_callback=None):
        target = self._normalize_category_path(category_name)
        if not target:
            raise RuntimeError("主分类为空")

        def log(message):
            if progress_callback:
                progress_callback(message)
            else:
                print(message)

        search_keyword = target.split("|||")[-1].strip()
        rows = []
        searched_rows = []
        if search_keyword:
            log(f"正在通过搜索框查找分类: {search_keyword}")
            searched_rows = self._build_full_paths(self._fetch_all_categories(search_keyword))
            rows = searched_rows
            log(f"搜索结果数: {len(rows)}")

        if not rows:
            log("搜索结果为空，正在拉取全部分类列表...")
            rows = self._build_full_paths(self._fetch_all_categories())
        else:
            log("搜索已有结果，先在搜索结果中匹配；如未命中再补拉全部分类。")
        if not rows:
            raise RuntimeError("未获取到任何分类")

        log(f"分类总数: {len(rows)}")
        target_norm = self._norm(target)

        full_path_matches = [row for row in rows if self._norm(row.get("full_path")) == target_norm]
        if len(full_path_matches) == 1:
            match = full_path_matches[0]
            log(f"精确匹配完整路径: {match['full_path']} -> ID={match['term_id']}")
            return str(match["term_id"])
        if len(full_path_matches) > 1 and searched_rows:
            log("搜索结果里出现重名/同路径分类，正在补拉全部分类以还原真实父级路径...")
            rows = self._build_full_paths(self._fetch_all_categories())
            log(f"补拉后的分类总数: {len(rows)}")
            full_path_matches = [row for row in rows if self._norm(row.get("full_path")) == target_norm]
        if len(full_path_matches) > 1:
            raise RuntimeError("完整路径命中多个分类：\n" + "\n".join(self._format_candidates(full_path_matches)))

        leaf_matches = [row for row in rows if self._norm(row.get("term_name")) == target_norm]
        if len(leaf_matches) == 1:
            match = leaf_matches[0]
            log(f"精确匹配分类名: {match['full_path']} -> ID={match['term_id']}")
            return str(match["term_id"])
        if len(leaf_matches) > 1 and searched_rows:
            log("搜索结果里命中了多个同名分类，正在补拉全部分类以输出完整路径...")
            rows = self._build_full_paths(self._fetch_all_categories())
            log(f"补拉后的分类总数: {len(rows)}")
            leaf_matches = [row for row in rows if self._norm(row.get("term_name")) == target_norm]
        if len(leaf_matches) > 1:
            if "|||" not in target:
                top_level_match = self._pick_first_top_level(leaf_matches)
                if top_level_match:
                    log(
                        "检测到多个同名分类，按规则优先选择无父级的第一个分类: "
                        f"{top_level_match['full_path']} -> ID={top_level_match['term_id']}"
                    )
                    return str(top_level_match["term_id"])
            raise RuntimeError(
                "找到多个同名分类，请填写完整路径 Parent|||Child，不再自动回退：\n"
                + "\n".join(self._format_candidates(leaf_matches))
            )

        compact_target = target_norm.replace(" ", "")
        compact_matches = [
            row
            for row in rows
            if self._norm(row.get("term_name")).replace(" ", "") == compact_target
        ]
        if len(compact_matches) == 1:
            match = compact_matches[0]
            log(f"忽略空格匹配分类名: {match['full_path']} -> ID={match['term_id']}")
            return str(match["term_id"])
        if len(compact_matches) > 1 and searched_rows:
            log("搜索结果里忽略空格后仍有多个同名分类，正在补拉全部分类以输出完整路径...")
            rows = self._build_full_paths(self._fetch_all_categories())
            log(f"补拉后的分类总数: {len(rows)}")
            compact_matches = [
                row
                for row in rows
                if self._norm(row.get("term_name")).replace(" ", "") == compact_target
            ]
        if len(compact_matches) > 1:
            if "|||" not in target:
                top_level_match = self._pick_first_top_level(compact_matches)
                if top_level_match:
                    log(
                        "检测到多个同名分类，按规则优先选择无父级的第一个分类: "
                        f"{top_level_match['full_path']} -> ID={top_level_match['term_id']}"
                    )
                    return str(top_level_match["term_id"])
            raise RuntimeError(
                "忽略空格后命中多个分类，请填写完整路径，不再自动回退：\n"
                + "\n".join(self._format_candidates(compact_matches))
            )

        if search_keyword and len(rows) < 1000:
            log("搜索结果里未命中，正在补拉全部分类做二次匹配...")
            rows = self._build_full_paths(self._fetch_all_categories())
            log(f"补拉后的分类总数: {len(rows)}")

            full_path_matches = [row for row in rows if self._norm(row.get("full_path")) == target_norm]
            if len(full_path_matches) == 1:
                match = full_path_matches[0]
                log(f"在全量分类中匹配完整路径: {match['full_path']} -> ID={match['term_id']}")
                return str(match["term_id"])
            if len(full_path_matches) > 1:
                raise RuntimeError("完整路径命中多个分类：\n" + "\n".join(self._format_candidates(full_path_matches)))

            leaf_matches = [row for row in rows if self._norm(row.get("term_name")) == target_norm]
            if len(leaf_matches) == 1:
                match = leaf_matches[0]
                log(f"在全量分类中匹配分类名: {match['full_path']} -> ID={match['term_id']}")
                return str(match["term_id"])
            if len(leaf_matches) > 1:
                raise RuntimeError(
                    "找到多个同名分类，请填写完整路径 Parent|||Child，不再自动回退：\n"
                    + "\n".join(self._format_candidates(leaf_matches))
                )

        fuzzy_matches = [
            row
            for row in rows
            if target_norm in self._norm(row.get("full_path"))
            or target_norm in self._norm(row.get("term_name"))
            or target_norm.replace("|||", "-") in self._norm(row.get("slug"))
        ]
        if fuzzy_matches:
            raise RuntimeError(
                "未找到精确分类，下面是匹配到的候选项。请直接使用其中的完整路径，不再自动回退：\n"
                + "\n".join(self._format_candidates(fuzzy_matches, limit=30))
            )

        raise RuntimeError(
            "未找到匹配分类，并且不会再自动使用回退分类。\n前 30 个分类如下：\n"
            + "\n".join(self._format_candidates(rows, limit=30))
        )

    def set_main_category(self, category_id):
        if not self.main_category_set_url:
            print("错误: 域名未设置，无法设置主分类")
            return False

        try:
            print(f"正在设置主分类，ID={category_id}")
            response = self._request(
                "POST",
                self.main_category_set_url,
                data={"term_id": category_id},
                headers=self.headers,
            )
            response.raise_for_status()

            try:
                result = response.json()
            except ValueError:
                return response.status_code == 200

            if result.get("error"):
                print(f"设置失败: {result.get('msg')}")
                return False

            print(f"设置成功: {result.get('msg')}")
            return True
        except Exception as exc:
            print(f"设为主分类失败: {exc}")
            return False

    def upload_main_category(self, domain, main_category, progress_callback):
        self.set_domain(domain)
        progress_callback(f"设置目标站点: {domain}")

        normalized_main_category = self._normalize_category_path(main_category)
        progress_callback(f"目标主分类: {normalized_main_category}")
        progress_callback("正在获取分类ID...")

        try:
            category_id = self.get_category_id(normalized_main_category, progress_callback)
        except Exception as exc:
            progress_callback(str(exc))
            return 0

        progress_callback(f"找到分类ID: {category_id}")
        progress_callback("正在设为主分类...")
        success = self.set_main_category(category_id)
        if success:
            progress_callback("主分类设置成功")
            return 1

        progress_callback("主分类设置失败")
        return 0
