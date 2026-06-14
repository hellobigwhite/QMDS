import json
import re
import time
import urllib3

import requests
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ExtraDataUploader:
    def __init__(self, password):
        self._password = password
        self._session = self._build_session()

    def _build_session(self):
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/133.0.0.0 Safari/537.36"
                )
            }
        )
        return session

    def _reset_session(self):
        try:
            self._session.close()
        except Exception:
            pass
        self._session = self._build_session()

    def _request(self, method, url, **kwargs):
        for _ in range(3):
            try:
                resp = self._session.request(method, url, timeout=60, verify=False, **kwargs)
                if resp is not None and resp.status_code in (200, 201, 302):
                    return resp
            except requests.exceptions.RequestException:
                pass
        return None

    def _login(self, site):
        login_url = f"https://www.{site}/bbwllogin/"
        user = site.replace(".com", "").strip()
        data = {
            "log": f"Ad{user}min",
            "pwd": self._password,
            "wp-submit": "Log In",
            "redirect_to": f"https://www.{site}/wp-admin/",
            "testcookie": "1",
        }
        resp = self._request("POST", login_url, data=data, allow_redirects=True)
        if resp is None:
            raise RuntimeError("WP login failed")
        if any("wordpress_logged_in" in c.name for c in self._session.cookies):
            return True
        admin_check = self._request("GET", f"https://www.{site}/wp-admin/")
        if admin_check is not None and admin_check.status_code == 200:
            return True
        raise RuntimeError("WP login failed")

    def _get_update_img_url(self, site):
        wp_url = f"https://www.{site}/wp-admin/options-general.php"
        resp = self._request("GET", wp_url)
        if not resp:
            return "/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB"
        soup = BeautifulSoup(resp.text, "html.parser")
        update_img = soup.find("a", string="Update Img", class_="ab-item")
        if update_img and update_img.get("href"):
            return update_img.get("href")
        return "/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB"

    def _parse_json_response(self, text):
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        match = re.search(r"(\{.*\})", raw, re.S)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                return None
        return None

    def _has_warning(self, text):
        raw = str(text or "")
        return "Warning" in raw or "Fatal error" in raw

    def _parse_progress(self, message):
        pattern = r"成功[:：]?(\d+)失败:(\d+)-重复(\d+)-名牌(\d+)已上传-?(\d+)执行时间"
        match = re.search(pattern, str(message or ""))
        if not match:
            return None
        return {
            "success": int(match.group(1)),
            "failure": int(match.group(2)),
            "repeat": int(match.group(3)),
            "brand": int(match.group(4)),
            "cs": int(match.group(5)),
        }

    def _process_images(self, base, update_img, headers, progress_callback=None):
        img_success_count = 0
        img_failure_count = 0
        retrytime = 0

        while True:
            dimg_url = f"{base}{update_img.replace('/plxztp.php?', '/dimg.php?')}"
            try:
                resp = self._session.get(dimg_url, headers=headers, timeout=120, verify=False)
            except requests.exceptions.RequestException as exc:
                retrytime += 1
                if progress_callback:
                    progress_callback(f"图片处理请求异常，继续重试({retrytime}): {exc}")
                time.sleep(2)
                continue

            if resp.status_code != 200 or self._has_warning(resp.text):
                retrytime += 1
                if progress_callback:
                    progress_callback(f"图片处理失败，继续重试({retrytime})")
                time.sleep(2)
                continue

            payload = self._parse_json_response(resp.text)
            if payload is None:
                retrytime += 1
                if progress_callback:
                    progress_callback(f"图片处理返回无法解析，继续重试({retrytime})")
                time.sleep(2)
                continue

            retrytime = 0
            message = str(payload.get("msg", ""))
            if "成功-0失败-0" in message:
                if progress_callback:
                    progress_callback(
                        f"图片处理完成 成功数量{img_success_count} 失败数量{img_failure_count}"
                    )
                return

            match = re.search(r"成功-(\d+)失败-(\d+)", message)
            if match:
                img_success_count += int(match.group(1))
                img_failure_count += int(match.group(2))

            if progress_callback:
                progress_callback(f"图片处理: {message}")

            time.sleep(1)

    def upload_extra_data(self, site, idcode, start_cs="0", progress_callback=None, errortime=0):
        del errortime

        cs = str(start_cs)
        idcode = str(idcode).strip().strip(",").replace(",", "%2C")
        success_count = 0
        failure_count = 0
        repeat_count = 0
        brand_count = 0
        outer_retry = 0

        while True:
            try:
                if progress_callback:
                    progress_callback(f"开始处理站点 {site}")
                self._reset_session()
                self._login(site)
                if progress_callback:
                    progress_callback("登录成功")

                update_img = self._get_update_img_url(site)
                base = f"https://www.{site}"
                headers = {"X-Requested-With": "XMLHttpRequest"}
                retrytime = 0

                if progress_callback:
                    progress_callback(f"开始上传补充数据，断点: {cs}")

                while True:
                    upload_url = (
                        f"{base}{update_img.replace('/plxztp.php?', '/dan_duopsot.php?')}"
                        f"&lv={idcode}&cs={cs}"
                    )
                    try:
                        resp = self._session.get(upload_url, headers=headers, timeout=120, verify=False)
                    except requests.exceptions.RequestException as exc:
                        retrytime += 1
                        if progress_callback:
                            progress_callback(f"上传请求异常，继续重试({retrytime}): {exc}")
                        time.sleep(2)
                        continue

                    if resp.status_code != 200 or self._has_warning(resp.text):
                        retrytime += 1
                        if progress_callback:
                            progress_callback(f"上传请求失败，继续重试({retrytime})")
                        time.sleep(2)
                        continue

                    payload = self._parse_json_response(resp.text)
                    if payload is None:
                        retrytime += 1
                        if progress_callback:
                            progress_callback(f"返回无法解析，继续重试({retrytime})")
                        time.sleep(2)
                        continue

                    retrytime = 0
                    message = str(payload.get("msg", ""))
                    if progress_callback:
                        progress_callback(message)

                    progress = self._parse_progress(message)
                    if progress:
                        success_count += progress["success"]
                        failure_count += progress["failure"]
                        repeat_count += progress["repeat"]
                        brand_count += progress["brand"]
                        if progress["cs"] > int(cs):
                            cs = str(progress["cs"])
                            if progress_callback:
                                progress_callback(f"更新断点: {cs}")

                    if "完成" in message:
                        if progress_callback:
                            progress_callback(
                                f"完成 已上传{cs} 成功{success_count} 失败{failure_count} 重复{repeat_count} 名牌{brand_count}"
                            )
                            progress_callback("开始批量处理图片")
                        self._process_images(base, update_img, headers, progress_callback)
                        return {
                            "upload_success": success_count,
                            "repeat_count": repeat_count,
                            "failure_count": failure_count,
                            "completed": True,
                        }

                    next_cs = str(payload.get("code") or "").strip()
                    if next_cs:
                        if next_cs != cs:
                            cs = next_cs
                            if progress_callback:
                                progress_callback(f"更新断点: {cs}")
                    else:
                        if progress_callback:
                            progress_callback("未返回 code，等待后继续重试当前断点")
                        time.sleep(5)
                        continue

                    time.sleep(1)
            except Exception as exc:
                outer_retry += 1
                if progress_callback:
                    progress_callback(f"上传异常，第{outer_retry}次重试，保留断点 {cs}: {exc}")
                time.sleep(10)
