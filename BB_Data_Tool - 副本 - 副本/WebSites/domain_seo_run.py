import re
import time

try:
    import tkinter as tk
except Exception:
    tk = None

from DrissionPage import Chromium
from DrissionPage import ChromiumOptions
from DrissionPage.common import Settings

from WebSites import BB_Network_API


co = ChromiumOptions().auto_port()
browser = Chromium(co)
Settings.set_singleton_tab_obj(False)


class GoogleSearcher:
    def __init__(self):
        self.current_tab = None
        self.root = None
        self.gui_available = False
        self._init_prompt_ui()

    def _init_prompt_ui(self):
        """Initialize optional GUI prompt support."""
        if tk is None:
            return

        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.gui_available = True
        except Exception as exc:
            self.root = None
            print(f"提示：图形弹窗不可用，将改为控制台确认。原因：{exc}")

    def _find_result_count(self):
        """Try to read the Google result count from the page."""
        result_stats = self.current_tab.ele("@id=result-stats", timeout=10)
        if not result_stats:
            return None

        match = re.search(r"(\d{1,3}(?:,\d{3})*)", result_stats.text)
        return int(match.group(1).replace(",", "")) if match else None

    def _handle_captcha(self):
        """Pause for manual verification when Google blocks the request."""
        if self.gui_available and self.root is not None:
            try:
                popup = tk.Toplevel(self.root)
                popup.title("需要人工确认")

                msg = tk.Label(
                    popup,
                    text=(
                        "请完成以下操作：\n"
                        "1. 处理验证码（如果有）\n"
                        "2. 确认页面已正常显示搜索结果\n"
                        "3. 点击继续按钮"
                    ),
                )
                msg.pack(padx=20, pady=10)

                btn = tk.Button(popup, text="继续", command=popup.destroy)
                btn.pack(pady=(0, 10))

                popup.attributes("-topmost", True)
                popup.grab_set()
                popup.focus_force()
                popup.wait_window()
                self.current_tab.wait(5)
                return
            except Exception as exc:
                self.gui_available = False
                print(f"提示：图形弹窗不可用，将改为控制台确认。原因：{exc}")

        print("请在浏览器中手动完成以下操作：")
        print("1. 处理验证码（如果有）")
        print("2. 确认页面已正常显示搜索结果")
        input("完成后按回车继续...")
        self.current_tab.wait(5)

    def _submit_result(self, count, domain_name):
        """Submit the query result to the API."""
        print(f"域名 {domain_name} 的收录数量为: {count}")

        try:
            result = BB_Network_API.api_add_google_count(str(domain_name), round(count))
        except Exception as exc:
            print(f"警告：提交域名 {domain_name} 的 SEO 收录信息失败: {exc}")
            return False

        if result:
            print("添加域名 SEO 收录信息成功:", result)
            return True

        print(f"警告：域名 {domain_name} 的 SEO 收录信息未返回成功结果。")
        return False

    def search(self, domain_name):
        """Execute one Google site query."""
        self.current_tab = browser.new_tab()
        try:
            query = f"site:{domain_name}"
            url = f"https://www.google.com/search?q={query}"
            self.current_tab.get(url, retry=2, interval=5, timeout=30)

            result = self._find_result_count()
            if result is not None:
                return self._submit_result(result, domain_name)

            print("未找到有效结果，进入人工确认流程。")
            self._handle_captcha()

            result = self._find_result_count()
            if result is not None:
                return self._submit_result(result, domain_name)

            print(f"未获取到结果，域名: {domain_name}")
            return False
        finally:
            if self.current_tab:
                self.current_tab.close()
                self.current_tab = None

    def close(self):
        """Release tkinter resources when available."""
        if self.root is not None:
            try:
                self.root.destroy()
            except Exception:
                pass
            finally:
                self.root = None


def process_txt(file_path, interval):
    """Process domains from a text file with a configurable interval."""
    searcher = GoogleSearcher()
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            domains = [line.strip() for line in file if line.strip()]

        total = len(domains)
        success_count = 0
        failed_count = 0

        for idx, domain in enumerate(domains, 1):
            print(f"\n=== 正在处理第 {idx} 个域名: {domain} ===")
            try:
                if searcher.search(domain):
                    success_count += 1
                else:
                    failed_count += 1
            except Exception as exc:
                failed_count += 1
                print(f"警告：处理域名 {domain} 时发生错误，已跳过: {exc}")
            time.sleep(interval)

        print(f"\n处理完成：共 {total} 个域名，成功 {success_count} 个，失败 {failed_count} 个。")
    except FileNotFoundError:
        print(f"错误：文件不存在: {file_path}")
    except Exception as exc:
        print(f"错误：读取文件时出错 - {exc}")
    finally:
        searcher.close()


if __name__ == "__main__":
    try:
        print("注意：由于网络限制，以下链接可能无法正常访问：")
        print("如果遇到问题，请检查链接合法性或网络连接。\n")

        interval_input = input("请输入每个域名查询的时间间隔（秒，回车默认0.1）：").strip()
        try:
            interval = float(interval_input) if interval_input else 0.1
        except ValueError:
            print("输入无效，使用默认间隔 0.1 秒。")
            interval = 0.1

        file_path = input("请输入TXT文件路径（每行一个域名）：").strip(" '\"")
        process_txt(file_path, interval)
    finally:
        browser.quit()
        print("\n所有操作已完成，浏览器已关闭")
