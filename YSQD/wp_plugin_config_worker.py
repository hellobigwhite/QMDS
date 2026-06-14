import json
import sys

from wp_plugin_config import WpPluginConfigurator


def main():
    if len(sys.argv) < 3:
        print("用法: python wp_plugin_config_worker.py <站点列表JSON> <WP密码>")
        return 1

    sites = json.loads(sys.argv[1])
    wp_password = sys.argv[2]

    if not sites:
        print("没有可配置的站点。")
        return 0

    print("=" * 60)
    print(f"开始配置站点，共 {len(sites)} 个")
    print("将依次执行：Yoast 一键启用 + 配置、WP Rocket 一键启用 + 配置")
    print("=" * 60)

    success = 0
    failed = []

    for site in sites:
        site = (site or "").strip()
        if not site:
            continue
        print(f"\n[{site}] 开始配置...")
        try:
            configurator = WpPluginConfigurator(wp_password)
            configurator.configure_all(site)
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
