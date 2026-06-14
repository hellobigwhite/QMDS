import os
import sys
import time
import requests
from bs4 import BeautifulSoup
import re
import json
import datetime
import multiprocessing
from multiprocessing import Pool
from loguru import logger
import pandas as pd
import ssl
import certifi
import urllib3

# 设置可信的CA证书包
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
os.environ['SSL_CERT_FILE'] = certifi.where()

# 配置日志（在主进程中配置）
logger.remove()  # 移除默认的控制台日志处理器
logger.add("batch_deal_log.txt", rotation="100 MB", level="INFO", enqueue=True,
           format="{time} - {message}")  # enqueue=True 确保多进程安全


def batchdealsite(site, idcode, cs, errortime):
    print('errortime', errortime)
    if errortime > 500:
        return 0

    # 创建自定义SSL上下文以处理证书问题
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    ssl_context.check_hostname = False

    session = requests.Session()
    session.verify = certifi.where()  # 使用可信证书包

    try:
        # 登录表单的 URL 和数据
        name = site.replace('.com', '').strip()
        site = 'https://www.' + site
        print(site)
        login_url = site + "/bbwllogin/"
        print(login_url)
        login_data = {
            'log': 'Ad' + name + 'min',
            'pwd': 'f!XsS$J2WneOkMyUgQ',
            'wp-submit': 'Log In',
            'redirect_to': site + '/wp-admin/',
            'testcookie': '1'
        }

        response = session.post(login_url, data=login_data, verify=False)
        # 检查登录是否成功
        if response.status_code == 200:
            print("登录成功！")
            # 保持会话激活
            keep_alive = session.get(site + "/wp-admin/", verify=False)

            # 访问后台页面
            wp_url = site + "/wp-admin/options-general.php"
            print(wp_url)
            wp_response = session.get(wp_url, verify=False)
            if wp_response.status_code == 200:
                print("成功访问general✔️")
            else:
                print(site, "无法访问登录后的页面，状态码：", wp_response.status_code)
                return 0
        else:
            print(f'{site} 登录失败,状态码为{response.status_code}')
            return 0

        # 从响应中提取更新URL
        soup = BeautifulSoup(wp_response.text, "html.parser")
        updateImg_element = soup.find('a', string='Update Img', class_='ab-item')
        if updateImg_element:
            updateImg = updateImg_element.get('href')
            print(f"找到更新URL: {updateImg}")
        else:
            # 如果找不到元素，使用默认路径
            updateImg = '/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB'
            print(f"⚠️ 未找到更新元素，使用默认URL: {updateImg}")

        idcode = str(idcode).replace(',', '%2C').strip()
        retrytime = 0
        success_count = 0  # 成功数量
        failure_count = 0  # 失败数量
        repeat_count = 0  # 重复数量
        brand_count = 0  # 名牌数量

        while True:
            upload_url = f"{site}{updateImg.replace('/plxztp.php?', '/dan_duopsot.php?')}&lv={idcode}&cs={cs}"
            print(f"上传URL: {upload_url}")

            headers2 = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "X-Requested-With": "XMLHttpRequest"
            }
            try:
                dealOne = session.get(upload_url, headers=headers2, verify=False)
            except Exception as e:
                print(f"请求异常: {e}")
                if "CERTIFICATE_VERIFY_FAILED" in str(e):
                    print("⚠️ 证书验证失败，尝试忽略证书验证")
                    dealOne = session.get(upload_url, headers=headers2, verify=False)
                else:
                    print(f"❌ 无法继续处理: {e}")
                    return -1

            # 处理响应
            if dealOne.status_code != 200:
                print(f'‼️{site} 上传失败 HTTP 状态码: {dealOne.status_code} ‼️')
                retrytime += 1
                print(f'{site} ℹ️尝试重传第{retrytime}次ℹ️️')
                logger.info(f'{site} ℹ️尝试重传第{retrytime}次ℹ️️ 断点为{cs}')
                if retrytime >= 10:
                    print(f'❌❌❌{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}   站点对应id为{idcode}❌❌❌')
                    logger.info(
                        f'{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}')
                    return -1
                time.sleep(2)  # 重试前等待
                continue
            else:
                retrytime = 0

            # 处理成功响应
            text = dealOne.text
            try:
                jssss = json.loads(text)
                print(f'{site} {jssss["msg"]}✔️')
                logger.info(f'{site} {jssss["msg"]}')
            except json.JSONDecodeError:
                print(f"⚠️ 无法解析JSON响应: {text[:200]}...")
                logger.error(f"{site} 无法解析JSON响应")
                time.sleep(2)
                continue

            # 使用正则表达式提取信息
            pattern = r"成功：(\d+)失败:(\d+)-重复(\d+)-名牌(\d+)已上传"
            match = re.search(pattern, jssss.get('msg', ''))

            if match:
                success_count += int(match.group(1))
                failure_count += int(match.group(2))
                repeat_count += int(match.group(3))
                brand_count += int(match.group(4))

            if '完成' in jssss.get('msg', ''):
                print(
                    f"{site} 完成  已上传{cs}  成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}")
                logger.info(
                    f"{site} 完成  已上传{cs}  成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}")
                print('🌈🌈🌈开始批量处理图片🌈🌈🌈')
                img_success_count = 0
                img_failure_count = 0

                while True:
                    dimg_url = f'{site}{updateImg.replace("/plxztp.php?", "/dimg.php?")}'
                    print(f"图片处理URL: {dimg_url}")

                    try:
                        dimg_response = session.get(dimg_url, headers=headers2, verify=False)
                    except Exception as e:
                        print(f"图片处理请求异常: {e}")
                        continue

                    if dimg_response.status_code != 200:
                        print(f'⚠️⚠️⚠️{site}处理图片错误 HTTP 状态码: {dimg_response.status_code}⚠️⚠️⚠️')
                        retrytime += 1
                        print(f'{site} ❕❕尝试重新处理图片第{retrytime}次❕❕')
                        logger.info(f'{site} 处理图片出现错误, HTTP 状态码: {dimg_response.status_code}')
                        if retrytime > 10:
                            print(f'🚫🚫🚫{site} 处理图片失败🚫🚫🚫')
                            logger.info(f'{site} 处理图片失败 成功数量{img_success_count}  失败数量{img_failure_count}')
                            return -1
                        time.sleep(2)
                        continue

                    try:
                        jsss = json.loads(dimg_response.text)
                    except json.JSONDecodeError:
                        print(f"⚠️ 无法解析图片处理JSON响应: {dimg_response.text[:200]}...")
                        continue

                    if '成功-0失败-0' in jsss.get('msg', ''):
                        print(f'🎉🎉🎉{site}  处理图片完成🎉🎉🎉  成功数量{img_success_count}  失败数量{img_failure_count}')
                        logger.info(
                            f'{site} -------- 处理图片完成  成功数量{img_success_count}  失败数量{img_failure_count}')
                        return 1

                    retrytime = 0
                    pattern = r"成功-(\d+)失败-(\d+)"
                    match = re.search(pattern, jsss.get('msg', ''))

                    if '失败-200' in jsss.get('msg', ''):
                        logger.info(f'{site} {jsss["msg"]}')

                    if match:
                        img_success_count += int(match.group(1))
                        img_failure_count += int(match.group(2))

                    print(f'{site} {jsss["msg"]}🌈')
                    time.sleep(1)  # 防止请求过快

                return 1

            # 更新断点继续处理
            if 'code' in jssss:
                cs = jssss['code']
                print(f"更新断点为: {cs}")
            else:
                print("⚠️ 响应中未找到代码字段")
                break

            time.sleep(1)  # 请求之间添加延迟

    except Exception as e:
        print(f"处理过程中发生错误: {e}")
        logger.error(f"{site} 处理过程中发生错误: {e}")
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


if __name__ == '__main__':
    multiprocessing.freeze_support()

    # 确保安装了必要的证书
    if not os.path.exists(certifi.where()):
        print("⚠️ 找不到证书文件，请安装certifi包")
        os.system("pip install certifi")
        os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()

    # 修改文件路径
    file_path = r'batch.xlsx'
    df = pd.read_excel(file_path)
    sites = list(df['site'])  # 要上传产品的域名
    idcodes = list(df['idcode'])  # 要上传产品的域名对应ID
    cs = list(df['cs'])  # 上传的起始点或者断点  初始为1

    if len(sites) == 0:
        print('无事发生')
        sys.exit()
    elif len(sites) > os.cpu_count():  # 网站数多于cpu核心数量则进程数按核心数量来设置
        n = os.cpu_count()
    else:
        n = len(sites)

    if len(sites) == len(idcodes) and len(sites) == len(cs):
        errortimes = [0] * len(sites)
        n = 2  # 固定线程数为4
        if len(sites) == len(idcodes) and len(sites) == len(cs):
            errortimes = [0] * len(sites)
            with Pool(processes=n) as pool:  # 开启4个进程
                # 将参数打包成元组列表
                args = [(site, idcode, c, errortime) for site, idcode, c, errortime in
                        zip(sites, idcodes, cs, errortimes)]
                results = pool.starmap(batchdealsite, args)
            print(results)
    else:
        print('参数长度不一致')
    os.system("pause")