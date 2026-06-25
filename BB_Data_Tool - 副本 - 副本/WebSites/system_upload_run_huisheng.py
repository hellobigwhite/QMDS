import os
import logging
from datetime import datetime
import re
import sys
import json
from logging import exception
import requests
import time
from bs4 import  BeautifulSoup


# 获取当前脚本所在目录及项目主目录
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
# 配置目录和文件路径
config_dir = os.path.join(project_root, '配置')
system_config_path = os.path.join(config_dir, '站群系统配置.json')
cred_config_path = os.path.join(config_dir, '站群系统账号密码.json')
# 日志文件路径 (项目主目录)
log_file_path = os.path.join(project_root, "运行日志", '站群系统上传日志-惠升版_log.txt')


def autouploadproduct(username,password,file_path,login_url,upload_page_url):
    # 配置日志
    logging.basicConfig(
        filename="站群系统上传日志-惠升版_log.txt",
        level=logging.INFO,
        format="%(asctime)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    #创建子任务
    session = requests.Session()
    login_data = {
        # 'main_page': 'login',
        # 'dongzuo': 'denglu',
        'username': username,
        'password': password
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
        "Referer": login_url
    }
    login_response = session.post(login_url, data=login_data, headers=headers, allow_redirects=True)

    # 检查登录是否成功
    if login_response.status_code == 200:
        # print(login_response.text)
        if 'msg' in login_response.text:
            failed = json.loads(login_response.text)['msg']
            print(failed)
            return failed
        print("登录成功！")


#————————————————————
        def uploadOneTable(session,file,upload_page_url,login_url): #用于上传一个.xlsx文件数据的函数  file格式:D:\分类\五金\nutboltblitz.com\hardwarehut.com0--5000.xlsx
            if '.xlsx' in file:
                print('开始上传',file)
            else:
                print(file,'不是一个.xlsx文件')
                return 0
            headers2 = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                # 必须要带user-agent 不然登录不上
                "X-Requested-With": "XMLHttpRequest",
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                "Referer": login_url
            }
            # 构造表单数据
            file_data = {
                'main_page': 'erp_products',
                'dongzuo': 'add_cp_pl',
                "file": open(file,'rb'),  # 打开文件并以二进制模式读取
            }
            # 发送POST请求  把上传数据的初始数据提交给上传产品页面  得到对应的ID和编号等初始启动数据
            response = session.post(upload_page_url, files=file_data,headers=headers)
            if response.status_code !=200:
                print(f"服务器无法处理,数据过大或格式不对,返回http码:{response.status_code},文件路径{file_path}")
                logging.info(f"服务器无法处理,数据过大或格式不对,返回http码:{response.status_code},文件路径{file_path}")
            cs = 2
            id = 0
            while True:
                try:
                    #  从上传产品页返回的内容中提取新的数据行起始点  其实可以用json.loads(text)返回一个字典
                    text = response.text
                    # print(text)
                    text = text.replace('\n','')
                    text = text.strip('{} ')
                    text = text.split(',')
                    d = {}

                    for c in text:
                        d[c.split(':')[0].strip('\"')] = c.split(':')[1].strip('\'').strip('\"')

                    if d['msg'] == '完成':  #网站返回完成则说明全部上传完毕
                        print(time.ctime(),file,'上传完毕')
                        logging.info(file + '   上传完毕')
                        return id
                    if d['code'] != '0':
                        if d['msg'] != 'https' or d['code']=="3":  #返回错误信息
                            print('——————————————————————————————————————————————————————————————————————————————————————————————————————————————————')
                            print(file,'上传失败,错误信息:',d['msg'].strip())
                            print('——————————————————————————————————————————————————————————————————————————————————————————————————————————————————')
                            logging.info(file+'上传失败,错误信息:'+d['msg'].strip())
                            return 0
                        cs = d['code']
                    else:
                        msg = d['msg']
                        id = d['yz']
                    # print('d',d)
                    #"upload_page_url": "https://erp.bbwl.site/index.php?main_page=erp_products&dongzuo=add_cp_pl",
                    # upload_url =f'{upload_page_url}do&cs={cs}&w={msg}&yz={d['yz']}&cat={d['cat']}'
                    upload_url = f"{upload_page_url}do&cs={cs}&w={msg}&yz={d['yz']}&cat={d['cat']}"
                    # print(upload_url)
                    upload_code = session.get(upload_url,headers=headers2)
                    response = upload_code
                    if response.status_code != 200:
                        print('error',response.status_code,response.text)
                        return 0
                except Exception as e:
                    print(e)
                    logging.exception(str(e))
                    return 0
#————————————————————


#————————————————————
        def uploadOneDir(session,dir,upload_page_url,finished_id,login_url):#用于上传一个文件夹内所有.xlsx文件的函数  主要是拿来嵌套递归以实现多级文件夹的上传  dir格式:D:\分类\五金\nutboltblitz.com
            try:
                if os.path.isfile(dir):
                    if '.xlsx' in dir:#如果dir是一个.xlsx文件那直接上传
                        finished_id.add(uploadOneTable(session,dir,upload_page_url,login_url))
                    else:#dir是一个非.xlsx的文件 无视
                        print(f'{dir}不是.xlsx文件')
                for name in os.listdir(dir):   #递归遍历子文件夹
                    finished_id = uploadOneDir(session,dir+'\\'+name,upload_page_url,finished_id,login_url)
            except FileNotFoundError:
                print('该文件不存在')
            finally:
                return finished_id
#————————————————————

        #从总文件夹开始调用uploadOneDir函数
        xzyt_id = set()
        uploadOneDir(session,file_path,upload_page_url,xzyt_id,login_url)
        headers2 = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
            # 必须要带user-agent 不然登录不上
            "X-Requested-With": "XMLHttpRequest",
        }
        xzyt_id.add(0)
        xzyt_id.remove(0)
        print(','.join(sorted(xzyt_id)))
        for id in xzyt_id:
            # 上传完毕后下载原图
            # xzyt_url = f'{upload_page_url.replace('dongzuo=add_cp_pl','dz=xztp')}&p_id={id}&lx=yz'
            xzyt_url = f"{upload_page_url.replace('dongzuo=add_cp_pl','dz=xztp')}&p_id={id}&lx=yz"
            xzyt_response = session.get(xzyt_url, headers=headers2)
            if '已添加' in xzyt_response.text:
                print(f'id:{id}已开始下载原图')
                logging.info(f'id:{id}已开始下载原图')
            else:
                print(f'{id} 下载原图失败')
                logging.info(f'{id} 下载原图失败')

    else:
        print('登录失败',login_response.status_code)
        return 0


if __name__ =='__main__':

    # 打印使用说明
    print("====================================================")
    print("数据表上传站群系统辅助工具-惠升版")
    print("====================================================")
    print("使用说明：")
    print("1. 运行脚本后，按提示输入要上传的文件或文件夹路径。")
    print("2. 脚本会遍历所有的表格文件,包括子文件夹夹")
    print("3. 脚本会自动执行下图动作。")
    print("4. 上传过程中日志会记录到以下路径：")
    print(f"   {log_file_path}")
    print("====================================================")



    # 加载上传页面配置和账号信息
    if not os.path.exists(system_config_path):
        print(f'缺少配置文件: {system_config_path}')
        sys.exit(1)

    with open(system_config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    login_url = cfg.get('login_url_2')
    upload_page_url = cfg.get('upload_page_url_2')
    username = cfg.get('username')
    password = cfg.get('password')

    if not login_url or not upload_page_url:
        print(f'{system_config_path} 中缺少 login_url_2 或 upload_page_url_2 字段')
        sys.exit(1)

    if not username or not password:
        print(f'{system_config_path} 中账号或密码配置不完整，请检查文件内容')
        sys.exit(1)

    path = input('请输入要上传的文件或文件夹路径：')
    autouploadproduct(username, password, path, login_url, upload_page_url)


