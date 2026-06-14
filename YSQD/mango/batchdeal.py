import os
import sys
import time
import requests
import re
import json
import threading
from loguru import logger
import pandas as pd
import shutil

os.system("chcp 65001 >nul")  # >nul 是为了隐藏命令的输出
# 配置日志（在主进程中配置）
logger.remove()  # 移除默认的控制台日志处理器
logger.add("../batch_deal_log.txt", rotation="100 MB", level="INFO", enqueue=True,
           format="{time} - {message}")  # enqueue=True 确保多进程安全
logger.add("../batch_deal_error.txt", rotation="100 MB", level="ERROR", enqueue=True, format="{time} - {message}")


def update_excel(cs: int, domain_name: str):
    domain_name = domain_name.replace('https://www.', '')
    excel_task[domain_name] = cs


def copy_excel(file_path: str, des_file_path: str):
    shutil.copy2(file_path, des_file_path)


def consumer_task():
    """
    5分钟执行一次的定时器
    获取dict的值

    :return:
    """
    if not os.path.exists(DES_FILE_PATH):
        print("全部处理完成,定时器退出")
        return

    print(f"消费任务执行 {time.ctime()}")
    # 消费任务逻辑

    dict_list = excel_task
    df = pd.read_excel(DES_FILE_PATH, engine='openpyxl')

    for key, value in dict_list.items():
        df.loc[df['site'] == key, 'cs'] = value

    df.to_excel(DES_FILE_PATH, index=False)
    print(f"消费任务执行完成{time.ctime()}")
    # 递归调用实现循环执行
    threading.Timer(10, consumer_task).start()


def batchdealsite(site, idcode, cs, errortime):
    """
    模拟上传任务，进行站点数据的上传与处理
    :param site: 网站地址
    :param idcode: 网站对应的idcode
    :param cs: 上传的起始点或断点
    :param errortime: 错误尝试次数
    :return: 上传成功返回1，失败返回-1
    """
    print(f"线程开始执行：{site}, cs={cs}, errortime={errortime}")
    if errortime > 50:
        return 0
    session = requests.Session()
    try:
        site = 'https://www.' + site
        updateImg = '/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB'
        idcode = str(idcode).replace(',', '%2C').strip()
        retrytime = 0
        success_count = 0  # 成功数量
        failure_count = 0  # 失败数量
        repeat_count = 0  # 重复数量
        brand_count = 0  # 名牌数量

        while True:
            headers2 = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest"
            }
            dealOne = session.get(f'{site}{updateImg.replace("/plxztp.php?", "/dan_duopsot.php?")}&lv={idcode}&cs={cs}',
                                  headers=headers2, timeout=120)
            if dealOne.status_code != 200 or 'Warning' in dealOne.text:
                print(f'‼️{site} 上传失败 error code:{dealOne.status_code}  ‼️')
                retrytime += 1
                print(f'{site} ℹ️尝试重传第{retrytime}次ℹ️️')
                logger.info(f'{site} ℹ️尝试重传第{retrytime}次ℹ️️ 断点为{cs}')

                if retrytime >= 10:
                    print(
                        f'❌❌❌{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  错误信息为{dealOne.text}   站点对应id为{idcode}❌❌❌')
                    logger.info(
                        f'{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  错误信息为{dealOne.text}   站点对应id为{idcode} 成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}')
                    logger.error(
                        f'{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  错误信息为{dealOne.text}   站点对应id为{idcode} 成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}')
                    return -1
                continue
            else:
                retrytime = 0

            #  从上传产品页返回的内容中提取新的数据行起始点  赋值给cs
            text = dealOne.text
            jssss = json.loads(text)
            print(f'{site}  {jssss["msg"]}✔️')
            logger.info(f'{site}  {jssss["msg"]}')
            # 使用正则表达式提取信息
            pattern = r"成功：(/d+)失败:(/d+)-重复(/d+)-名牌(/d+)已上传-(/d+)执行时间"
            match = re.search(pattern, jssss['msg'])

            if match:
                success_count += int(match.group(1))  # 成功数量
                failure_count += int(match.group(2))  # 失败数量
                repeat_count += int(match.group(3))  # 重复数量
                brand_count += int(match.group(4))  # 名牌数量
                new_cs = int(match.group(5))  # 新的cs值
                if new_cs > int(cs):
                    update_excel(new_cs, site)

            if '完成' in jssss['msg']:
                print(
                    f"{site} 完成  已上传{cs}  成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}")
                logger.info(
                    f"{site} 完成  已上传{cs}  成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}")

                print('🌈🌈🌈开始批量处理图片🌈🌈🌈')
                img_success_count = 0
                img_failure_count = 0
                while True:
                    dimg_url = f'{site}{updateImg.replace("/plxztp.php?", "/dimg.php?")}'

                    dealOne = session.get(dimg_url, headers=headers2)

                    if dealOne.status_code != 200 or 'Warning' in dealOne.text:
                        print(f'⚠️⚠️⚠️{site}处理图片错误 error 状态码:{dealOne.status_code}⚠️⚠️⚠️')
                        retrytime += 1
                        print(f'{site} ❕❕尝试重新处理图片第{retrytime}次❕❕')
                        logger.info(f'{site} 处理图片出现错误,error 状态码:{dealOne.status_code}')
                        if retrytime > 10:
                            print(f'🚫🚫🚫{site} 处理图片失败🚫🚫🚫')
                            logger.info(f'{site} 处理图片失败 成功数量{img_success_count}  失败数量{img_failure_count}')
                            return -1
                        continue

                    jsss = json.loads(dealOne.text)
                    if '成功-0失败-0' in jsss['msg']:
                        print(f'🎉🎉🎉{site}  处理图片完成🎉🎉🎉  成功数量{img_success_count}  失败数量{img_failure_count}')
                        logger.info(
                            f'{site} -------- 处理图片完成  成功数量{img_success_count}  失败数量{img_failure_count}')
                        return 1
                    retrytime = 0
                    pattern = r"成功-(/d+)失败-(/d+)"
                    match = re.search(pattern, jsss['msg'])
                    if '失败-200' in jsss['msg']:
                        logger.info(f'{site} {jsss["msg"]}')
                    if match:
                        img_success_count += int(match.group(1))  # 成功数量
                        img_failure_count += int(match.group(2))  # 失败数量
                    print(f'{site} {jsss["msg"]}🌈')
                return 1
            cs = jssss['code']

    except Exception as e:
        print(e)
        if session:
            session.close()
        logger.info(f'{site}上传中止 断点为{cs}')
        time.sleep(100)
        site = site.replace('https://www.', '')
        tryagain = batchdealsite(site, idcode, cs, errortime + 1)
        if tryagain:
            return tryagain
        else:
            return -2
    finally:
        if session:
            session.close()


DES_FILE_PATH = r"C:/Users/Administrator/Desktop/updata_product.xlsx"
SOURCE_FILE_PATH = r"C:\Users\Administrator\Desktop\js.xlsx"
excel_task = {}

if __name__ == '__main__':
    # 文件路径
    file_path = SOURCE_FILE_PATH

    if os.path.exists(DES_FILE_PATH):
        copy_excel(DES_FILE_PATH, file_path)
    else:
        copy_excel(file_path, DES_FILE_PATH)

    df = pd.read_excel(file_path, engine='openpyxl', dtype={'idcode': str})  # 强制 idcode 列为字符串
    print("Excel 列名：", df.columns.tolist())

    # 确保 idcode 为字符串并去除空格
    excel_task = {str(row['idcode']).strip(): row['cs'] for index, row in df.iterrows() if str(row['idcode']).strip()}

    sites = list(df['site'])  # 站点列表
    idcodes = list(df['idcode'])  # ID 列表
    cs = list(df['cs'])  # 上传起始点或断点

    if len(sites) == 0:
        print('无事发生')
        sys.exit()

    # 开启定时器
    consumer_task()

    # 使用多线程执行任务
    threads = []
    for site, idcode, cs_value, errortime in zip(sites, idcodes, cs, [0] * len(sites)):
        t = threading.Thread(target=batchdealsite, args=(site, idcode, cs_value, errortime))
        threads.append(t)
        t.start()  # 启动线程

    # 等待所有线程完成
    for t in threads:
        t.join()  # 等待所有线程完成

    print("所有线程任务完成！")
