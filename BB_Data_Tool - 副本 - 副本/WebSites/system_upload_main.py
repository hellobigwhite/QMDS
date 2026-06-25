import os
import time
import json
from DrissionPage import Chromium
import sys
import ctypes





# 加载配置文件
def load_config():
    # 加载主配置文件
    main_config_path = "配置\\站群系统配置.json"

    try:
        with open(main_config_path, "r", encoding="utf-8") as f:
            main_config = json.load(f)
    except FileNotFoundError:
        print(f"错误: 配置文件 {main_config_path} 未找到！")
        exit(1)

    return main_config


# 记录日志
def log_upload_status(file_path, status, elapsed_time=None):
    """
    记录文件上传情况到日志文件。
    :param file_path: 文件路径
    :param status: 上传状态（成功/失败/超时等）
    :param elapsed_time: 上传耗时（秒），可选
    """
    log_file_path = "运行日志\\站群系统上传日志_log.txt"
    
    # 确保日志文件所在的目录存在
    os.makedirs(os.path.dirname(log_file_path), exist_ok=True)

    # 如果日志文件不存在，则创建
    if not os.path.exists(log_file_path):
        with open(log_file_path, "w", encoding="utf-8") as log_file:
            log_file.write("日志文件创建成功。\n")

    with open(log_file_path, "a", encoding="utf-8") as log_file:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        if elapsed_time is not None:
            log_file.write(f"[{timestamp}] 文件: {file_path} | 状态: {status} | 耗时: {elapsed_time:.2f} 秒\n")
        else:
            log_file.write(f"[{timestamp}] 文件: {file_path} | 状态: {status}\n")

# 弹出错误对话框
def show_error_and_exit(message):
    """
    使用 ctypes 弹出 Windows 消息框显示错误，并退出程序。
    :param message: 错误信息
    """
    # 弹出消息框
    ctypes.windll.user32.MessageBoxW(0, message, "错误", 0x10)  # 0x10 表示显示错误图标
    # 退出程序
    sys.exit(1)

# 上传文件
def upload_file(tab, file_path):

    # 找到上传按钮
    ele = tab.ele('tag:button@@class=layui-btn@@id=schdhs@@text()=上传批量')
    if ele and ele.text == "上传批量":
        # 给个延迟怕网页还没加载完全
        time.sleep(5)
        print(f"找到上传批量按钮，开始上传文件: {file_path}")
        ele.click.to_upload(file_path)

        # 开始计时
        start_time = time.time()

        # 循环等待完成标识，间隔10秒检查一次
        timeout = 7200  # 单表超时时间2小时
        while time.time() - start_time < timeout:
            ele2 = tab.ele('@id=displayss')  # 检查上传状态元素
            if ele2:
                # 如果上传成功
                if ele2.text == "完成":
                    elapsed_time = time.time() - start_time
                    print(f"上传成功，耗时：{elapsed_time:.2f} 秒\n --------------------------------------")
                    log_upload_status(file_path, "成功", elapsed_time)  # 记录日志
                    # 延迟5秒后继续下一个表
                    time.sleep(5)
                    return
                # 如果提示 "表错了" 或 "轮询中断"，弹出错误框并停止
                elif ele2.text == "表错了":
                    print("表格错误，上传停止。\n --------------------------------------")
                    log_upload_status(file_path, "表格错误")
                    show_error_and_exit("检测到表格错误，上传停止。")
                elif ele2.text == "中断":
                    print("轮询中断，上传停止。\n --------------------------------------")
                    log_upload_status(file_path, "轮询中断")
                    show_error_and_exit("轮询中断，上传停止。")
            # print("未找到上传成功标识，等待10秒后重试...")
            time.sleep(10)  # 间隔10秒检测一次

        # 超时处理
        print("上传超时或失败。\n --------------------------------------")
        log_upload_status(file_path, "超时")
        show_error_and_exit(f"上传文件 {file_path} 超时或失败。")
    else:
        print("未找到上传按钮。\n --------------------------------------")
        log_upload_status(file_path, "失败")
        show_error_and_exit("未找到上传按钮，上传失败。")


def run(folder, headless_mode=True):  # 添加参数 headless_mode，默认为 True（显性模式）
    # 加载配置
    try:
        config = load_config()
    except FileNotFoundError as e:
        print(e)
        exit(1)

    # 输入文件夹路径
    if not os.path.isdir(folder):
        print("输入的路径无效或不是文件夹。")
        exit(1)

    # 获取文件夹中所有 .xlsx 文件
    files_to_upload = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".xlsx")]

    if not files_to_upload:
        print("文件夹中没有找到任何 .xlsx 文件。")
        exit(1)

    # 连接浏览器
    browser = Chromium()
    tab = browser.latest_tab

    try:
        # 批量上传文件
        for file_path in files_to_upload:
            # 访问登陆页
            tab.get(config['login_url'])

            # 等待输入账号元素出现  超时:2分钟
            tab.wait.ele_displayed(
                'tag:input@@class=layui-input@@name=username@@placeholder=请输入登录账号@@autocomplete=off@@lay-vertype=tips@@lay-verify=required',
                timeout=120
            )

            # 输入账号
            account = tab.ele(
                'tag:input@@class=layui-input@@name=username@@placeholder=请输入登录账号@@autocomplete=off@@lay-vertype=tips@@lay-verify=required'
            )
            account.input(config['username'])

            # 输入密码并且回车
            password = tab.ele(
                'tag:input@@class=layui-input@@name=password@@placeholder=请输入登录密码@@type=password@@lay-vertype=tips@@lay-verify=required'
            )
            password.input(f"{config['password']}")

            # 点击登陆按钮
            login_button = tab.ele(
                'tag:button@@class=layui-btn layui-btn-fluid@@lay-filter=loginSubmit@@text()=登录'
            )
            login_button.click(by_js=True)

            # 点击登陆后给个3秒延迟
            time.sleep(3)

            # 等待网站管理页面元素出现 超时:2分钟
            tab.wait.ele_displayed('tag:cite@@text()=网站管理', timeout=120)

            print("登陆后台成功，开始跳转到产品上传页面")

            # 访问上传批量页面
            tab.get(config['upload_page_url'])
            tab.wait.ele_displayed('tag:button@@class=layui-btn@@id=schdhs@@text()=上传批量', timeout=180)

            print(f"准备上传文件: {file_path}")
            upload_file(tab, file_path)

        return True

    finally:
        # 关闭浏览器
        browser.quit()
