import json
import sys
import urllib3

from wp_media_config import WPMediaConfigurator
from wp_plugin_button_clicker import WpPluginButtonClicker


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def main():
    if len(sys.argv) < 3:
        print("用法: python wp_site_config_worker.py <站点列表JSON> <WP密码>")
        return 1

    sites = json.loads(sys.argv[1])
    wp_password = sys.argv[2]

    if not sites:
        print("没有可配置的站点。")
        return 0

    print("=" * 60)
    print(f"开始配置站点，共 {len(sites)} 个")
    print("将依次执行：图片设置保持原流程、然后点击网页里的 Yoast / WP Rocket 按钮")
    print("=" * 60)

    success = 0
    failed = []

    for site in sites:
        site = (site or "").strip()
        if not site:
            continue

        print(f"\n[{site}] 开始配置...")
        try:
            print(f"[{site}] 1/2 图片设置开始")
            media_configurator = WPMediaConfigurator(wp_password)
            media_configurator.configure(site)
            print(f"[{site}] 1/2 图片设置完成")

            print(f"[{site}] 2/2 打开网页按钮开始")
            button_clicker = WpPluginButtonClicker(wp_password, headless=True)
            button_clicker.configure(site)
            print(f"[{site}] 2/2 网页按钮完成")

            success += 1
            print(f"[{site}] 配置完成")
        except Exception as exc:
            failed.append((site, str(exc)))
            print(f"[{site}] 配置失败: {exc}")

    print("\n" + "=" * 60)
    print(f"完成: 成功 {success}，失败 {len(failed)}")
    print("=" * 60)

    if failed:
        print("\n失败详情:")
        for site, error in failed:
            print(f"  - {site}: {error}")

    print("\n按回车键关闭窗口...")
    input()
    return 0 if not failed else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\n发生错误: {exc}")
        print("\n按回车键关闭窗口...")
        input()
        raise
