import requests
from src.utils.logger import setup_logger

logger = setup_logger('crontab', 'logs/crontab.log')



def del_crontab(admin_id, group_id):
    url = f"https://erp.yswl.site/cf-updata/dimg.php?p=OFjToUDQ5mmtU7GB&lx=yz&erp_id={group_id}&admin_id={admin_id}"
    response = requests.get(url)
    return response.status_code


if __name__ == "__main__":
    # ===================== 固定ID =====================
    admin_id = 98  # 这个固定不变

    # ===================== 手动输入 group_id =====================
    print("===== 删除Crontab任务 =====")
    group_id_str = input("请输入 group_id，多个用英文逗号分隔：").strip()

    if not group_id_str:
        logger.error("未输入任何 group_id，程序退出")
        input("\n按回车退出...")
        exit()

    group_ids = group_id_str.split(",")
    fail_ids = []

    print("\n开始执行删除...\n")

    for group_id in group_ids:
        group_id = group_id.strip()
        if not group_id.isdigit():
            logger.error(f"无效ID：{group_id}，跳过")
            fail_ids.append(group_id)
            continue

        status_code = del_crontab(admin_id, group_id)
        if status_code == 200:
            logger.info(f"删除成功: {group_id}")
        else:
            fail_ids.append(group_id)
            logger.error(f"删除失败: {group_id}, 状态码: {status_code}")

    if fail_ids:
        logger.error(f"\n以下任务删除失败: {', '.join(fail_ids)}")
    else:
        logger.info("\n✅ 所有ID删除完成！")

    input("\n执行完毕，按回车键退出...")