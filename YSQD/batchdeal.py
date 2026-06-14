import os
import sys
import time
import requests
import re
import json
from loguru import logger
from bs4 import BeautifulSoup

os.system("chcp 65001 >nul")  # >nul 是为了隐藏命令的输出
# 配置日志
logger.remove()  # 移除默认的控制台日志处理器
logger.add("batch_deal_log.txt", rotation="100 MB", level="INFO", enqueue=True,
           format="{time} - {message}")  # enqueue=True 确保多进程安全
logger.add("batch_deal_error.txt", rotation="100 MB", level="ERROR", enqueue=True, format="{time} - {message}")


def _extract_json_payload(text):
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


def _discover_update_img_url(session, site):
    try:
        resp = session.get(f"{site}/wp-admin/options-general.php", timeout=60)
        if resp.status_code != 200:
            return '/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB'
        soup = BeautifulSoup(resp.text, "html.parser")
        update_img = soup.find("a", string="Update Img", class_="ab-item")
        if update_img and update_img.get("href"):
            return update_img.get("href")
    except requests.RequestException:
        pass
    return '/cf-updata/plxztp.php?p=OFjToUDQ5mmtU7GB'


def _has_effective_progress(success_count, repeat_count, brand_count):
    return any(count > 0 for count in (success_count, repeat_count, brand_count))


def _finalize_upload_result(site, cs, success_count, failure_count, repeat_count, brand_count, progress_callback=None):
    completion_msg = (
        f"{site} 上传结束 已上传{cs} 成功数量{success_count} "
        f"失败数量{failure_count} 重复数量{repeat_count} 名牌数量{brand_count}"
    )
    print(completion_msg)
    logger.info(completion_msg)
    if progress_callback:
        progress_callback(completion_msg)

    if failure_count == 0 and _has_effective_progress(success_count, repeat_count, brand_count):
        if progress_callback:
            progress_callback("上传结果已稳定，按完成处理")
        return 1
    return -1


def batchdealsite(site, idcode, cs, errortime, progress_callback=None):
    """
    模拟上传任务，进行站点数据的上传与处理
    :param site: 网站地址
    :param idcode: 网站对应的idcode
    :param cs: 上传的起始点或断点
    :param errortime: 错误尝试次数
    :param progress_callback: 进度回调函数
    :return: 上传成功返回1，失败返回-1
    """
    if progress_callback:
        progress_callback(f"开始处理站点: {site}")
    print(f"线程开始执行：{site}, cs={cs}, errortime={errortime}")
    if errortime > 50:
        return 0
    session = requests.Session()
    try:
        site = 'https://www.' + site
        updateImg = _discover_update_img_url(session, site)
        idcode = str(idcode).replace(',', '%2C').strip()
        retrytime = 0
        success_count = 0  # 成功数量
        failure_count = 0  # 失败数量
        repeat_count = 0  # 重复数量
        brand_count = 0  # 名牌数量

        if progress_callback:
            progress_callback(f"开始上传数据，断点: {cs}")
        
        headers2 = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            "X-Requested-With": "XMLHttpRequest"
        }
        
        for i in range(800):
            if progress_callback and i % 10 == 0:
                progress_callback(f"上传中 ({i}/800)")
                
            dealOne = session.get(f'{site}{updateImg.replace("/plxztp.php?", "/dan_duopsot.php?")}&lv={idcode}&cs={cs}',
                                  headers=headers2, timeout=120)
            jssss = _extract_json_payload(dealOne.text) if dealOne.status_code == 200 else None
            if dealOne.status_code != 200 or jssss is None:
                print(f'‼️{site} 上传失败 error code:{dealOne.status_code}  ‼️')
                retrytime += 1
                if progress_callback:
                    progress_callback(f"请求失败，重试 {retrytime}/10")
                print(f'{site} ℹ️尝试重传第{retrytime}次ℹ️️')
                logger.info(f'{site} ℹ️尝试重传第{retrytime}次ℹ️️ 断点为{cs}')

                if retrytime >= 10:
                    error_msg = f'{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  错误信息为{dealOne.text}   站点对应id为{idcode}'
                    print(f'❌❌❌{error_msg}❌❌❌')
                    logger.info(
                        f'{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  错误信息为{dealOne.text}   站点对应id为{idcode} 成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}')
                    logger.error(
                        f'{site}上传失败 断点为{cs} 错误代码为{dealOne.status_code}  错误信息为{dealOne.text}   站点对应id为{idcode} 成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}')
                    if progress_callback:
                        progress_callback(f"上传失败: {error_msg}")
                    return -1
                time.sleep(2)
                continue
            else:
                retrytime = 0

            msg = jssss.get("msg", "")
            print(f'{site}  {msg}✔️')
            logger.info(f'{site}  {msg}')
            
            if progress_callback:
                progress_callback(f"上传状态: {msg}")
            
            # 使用正则表达式提取信息
            pattern = r"成功：(\d+)失败:(\d+)-重复(\d+)-名牌(\d+)已上传-(\d+)执行时间"
            match = re.search(pattern, msg)

            if match:
                success_count += int(match.group(1))  # 成功数量
                failure_count += int(match.group(2))  # 失败数量
                repeat_count += int(match.group(3))  # 重复数量
                brand_count += int(match.group(4))  # 名牌数量
                new_cs = int(match.group(5))  # 新的cs值
                if new_cs > int(cs):
                    cs = str(new_cs)
                    if progress_callback:
                        progress_callback(f"更新断点: {cs}")

            if '成功' in msg:
                if progress_callback:
                    progress_callback(f"上传成功，累计: {success_count}")
                    
            if '完成' in msg:
                completion_msg = f"{site} 完成  已上传{cs}  成功数量{success_count}  失败数量{failure_count}   重复数量{repeat_count}  名牌数量{brand_count}"
                print(completion_msg)
                logger.info(completion_msg)
                if progress_callback:
                    progress_callback(completion_msg)

                if progress_callback:
                    progress_callback("开始批量处理图片")
                print('🌈🌈🌈开始批量处理图片🌈🌈🌈')
                img_success_count = 0
                img_failure_count = 0
                
                for j in range(400):
                    if progress_callback and j % 50 == 0:
                        progress_callback(f"图片处理中 ({j}/400)")
                        
                    dimg_url = f'{site}{updateImg.replace("/plxztp.php?", "/dimg.php?")}'

                    dealOne = session.get(dimg_url, headers=headers2, timeout=120)
                    jsss = _extract_json_payload(dealOne.text) if dealOne.status_code == 200 else None

                    if dealOne.status_code != 200 or jsss is None:
                        print(f'⚠️⚠️⚠️{site}处理图片错误 error 状态码:{dealOne.status_code}⚠️⚠️⚠️')
                        retrytime += 1
                        if progress_callback:
                            progress_callback(f"图片处理失败，重试 {retrytime}/10")
                        print(f'{site} ❕❕尝试重新处理图片第{retrytime}次❕❕')
                        logger.info(f'{site} 处理图片出现错误,error 状态码:{dealOne.status_code}')
                        if retrytime > 10:
                            print(f'🚫🚫🚫{site} 处理图片失败🚫🚫🚫')
                            logger.info(f'{site} 处理图片失败 成功数量{img_success_count}  失败数量{img_failure_count}')
                            if progress_callback:
                                progress_callback("图片处理失败")
                            return -1
                        time.sleep(2)
                        continue

                    img_msg = jsss.get("msg", "")
                    if '成功-0失败-0' in img_msg:
                        img_completion_msg = f'🎉🎉🎉{site}  处理图片完成🎉🎉🎉  成功数量{img_success_count}  失败数量{img_failure_count}'
                        print(img_completion_msg)
                        logger.info(
                            f'{site} -------- 处理图片完成  成功数量{img_success_count}  失败数量{img_failure_count}')
                        if progress_callback:
                            progress_callback(img_completion_msg)
                        return 1
                    retrytime = 0
                    pattern = r"成功-(\d+)失败-(\d+)"
                    match = re.search(pattern, img_msg)
                    if '失败-200' in img_msg:
                        logger.info(f'{site} {img_msg}')
                    if match:
                        img_success_count += int(match.group(1))  # 成功数量
                        img_failure_count += int(match.group(2))  # 失败数量
                    print(f'{site} {img_msg}🌈')
                    if progress_callback:
                        progress_callback(f"图片处理: {img_msg}")
                    time.sleep(1)
                return 1
            
            if 'code' in jssss:
                cs = jssss['code']
                if progress_callback:
                    progress_callback(f"更新断点: {cs}")
            else:
                if progress_callback:
                    progress_callback("无code返回，结束上传")
                return _finalize_upload_result(
                    site,
                    cs,
                    success_count,
                    failure_count,
                    repeat_count,
                    brand_count,
                    progress_callback=progress_callback,
                )
            
            time.sleep(1)

        return _finalize_upload_result(
            site,
            cs,
            success_count,
            failure_count,
            repeat_count,
            brand_count,
            progress_callback=progress_callback,
        )

    except Exception as e:
        error_msg = str(e)
        print(error_msg)
        if session:
            session.close()
        logger.info(f'{site}上传中止 断点为{cs}')
        if progress_callback:
            progress_callback(f"上传中止: {error_msg}")
        time.sleep(100)
        site = site.replace('https://www.', '')
        tryagain = batchdealsite(site, idcode, cs, errortime + 1, progress_callback)
        if tryagain:
            return tryagain
        else:
            return -2
    finally:
        if session:
            session.close()


if __name__ == '__main__':
    # 示例调用
    import argparse
    
    parser = argparse.ArgumentParser(description='批量处理站点数据')
    parser.add_argument('site', help='网站地址')
    parser.add_argument('idcode', help='网站对应的idcode')
    parser.add_argument('--cs', default='0', help='上传的起始点或断点')
    
    args = parser.parse_args()
    
    def progress_callback(message):
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {args.site}: {message}")
        logger.info(f"{args.site}: {message}")
    
    result = batchdealsite(args.site, args.idcode, args.cs, 0, progress_callback)
    print(f"处理结果: {'成功' if result == 1 else '失败'}")
    sys.exit(0 if result == 1 else 1)
