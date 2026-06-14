import time
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


class WpPluginButtonClicker:
    def __init__(self, password, headless=True):
        self._password = password
        self._headless = headless
        self._script = self._load_panel_script()

    def _load_panel_script(self):
        script_path = (
            Path(__file__).resolve().parent
            / "参考代码"
            / "站点配置"
            / "插件一键配置.js"
        )
        return script_path.read_text(encoding="utf-8")

    def _launch_browser(self, playwright):
        last_error = None
        for channel in ("msedge", "chrome"):
            try:
                return playwright.chromium.launch(channel=channel, headless=self._headless)
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"无法启动 Edge/Chrome 浏览器: {last_error}") from last_error

    def _login(self, page, site):
        login_url = f"https://www.{site}/bbwllogin/"
        username = f"Ad{site.replace('.com', '').strip()}min"
        page.goto(login_url, wait_until="domcontentloaded", timeout=120000)
        page.locator("#user_login, input[name='log']").first.fill(username)
        page.locator("#user_pass, input[name='pwd'], input[type='password']").first.fill(
            self._password
        )
        page.locator("#wp-submit, input[name='wp-submit']").first.click()
        page.wait_for_load_state("domcontentloaded", timeout=120000)

    def _open_plugins_page(self, page, site):
        plugins_url = f"https://www.{site}/wp-admin/plugins.php"
        page.goto(plugins_url, wait_until="domcontentloaded", timeout=120000)
        page.add_script_tag(content=self._script)
        page.wait_for_selector("#wp-oneclick-panel", timeout=30000)

    def _run_button(self, page, label, task_name, timeout_seconds=360):
        log_box = page.locator("#wp-oneclick-panel pre").first
        button = page.locator(f"#wp-oneclick-panel button:has-text('{label}')").first
        before = log_box.inner_text(timeout=10000)
        button.click()

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            current = log_box.inner_text(timeout=10000)
            delta = current[len(before):] if current.startswith(before) else current

            if task_name in delta and "任务结束" in delta and "成功" in delta:
                return
            if task_name in delta and "任务结束" in delta and "失败" in delta:
                tail = "\n".join(delta.strip().splitlines()[-8:])
                raise RuntimeError(tail or f"{task_name} 执行失败")

            time.sleep(1)

        raise RuntimeError(f"{task_name} 执行超时")

    def configure(self, site):
        with sync_playwright() as pw:
            browser = self._launch_browser(pw)
            page = browser.new_page(ignore_https_errors=True)
            try:
                self._login(page, site)
                self._open_plugins_page(page, site)
                self._run_button(page, "Yoast 一键启用 + 配置", "Yoast")
                self._run_button(page, "WP Rocket 一键启用 + 配置", "WP Rocket")
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(f"浏览器操作超时: {exc}") from exc
            finally:
                browser.close()
