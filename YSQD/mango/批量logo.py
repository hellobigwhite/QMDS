import base64
import requests
import os
import time
import random
import subprocess

# -----------------------
# 配置区域（完全保留你原来的）
# -----------------------
all_entries = os.listdir(".")
domains = [d for d in all_entries if os.path.isdir(d) and d.endswith(".com")]
print(f"Detected domain folders: {domains}")

fonts = {
    "rosmatika-regular.png": ("BWA45", "OTUyODZmMTcwZjJjNDI4OWFiNjEwZjJlODU4NzA2MzUudHRm"),
    "border-wall.png": ("OG55o", "YjRjYzFiYTY5ZjcxNDJkYzljYWU5NzE0NGFiZmRiNGMub3Rm"),
    "remalos-regular.png": ("aYj1m", "ZGE0MjZkMzBjNzliNDllYmE0YTI3MjcwZTUwOWQxYTgudHRm"),
    "blush-asliring-regular.png": ("OGP66", "MmViNTViMmRjYWZiNDg1ZmI1NDljMmExNDIxYmRhMTIub3Rm"),
    "granika.png": ("MAm6r", "YjhhYmI2NDM1ZGI2NDQzOGIzMTk5ZDlkYTIyNjU3NmUub3Rm"),
    "billionery-regular.png": ("drXjg", "NWUyOThkN2E2MGJiNDA4N2FkZDk0OTA3Yjc4Y2VlZjkub3Rm"),
    "kingsman-demo.png": ("1GVgg", "OTI2YjVlNjExZGJlNDMyMzk3ZTA2YzUxNjIyOGIwYmMudHRm"),
    "shifty-notes-regular.png": ("BWZ6d", "N2NjMWFjYTM2M2M2NGYyMjhhZTg1NjliNWM4ZTJhMWMudHRm"),
}

height = 65
width = 1000
fg_color = "000000"
bg_color = "FFFFFF"
size = 65
tb = 1
delay = 2

# -----------------------
# 强制关闭文件占用 + 重命名为 icon
# -----------------------
def force_rename_to_icon(folder_path):
    icon_path = os.path.join(folder_path, "icon.png")
    if os.path.exists(icon_path):
        print(f"[→] 已存在 icon.png，跳过")
        return

    for filename in os.listdir(folder_path):
        file_path = os.path.join(folder_path, filename)
        if not os.path.isfile(file_path):
            continue
        if not filename.lower().endswith(".png"):
            continue

        # 强制关闭资源管理器占用（Windows）
        try:
            subprocess.run(f'taskkill /f /im explorer.exe', shell=True, capture_output=True)
            time.sleep(0.2)
            subprocess.run(f'start explorer.exe', shell=True, capture_output=True)
            time.sleep(0.3)
        except:
            pass

        # 尝试重命名
        try:
            os.rename(file_path, icon_path)
            print(f"[✓] 强制重命名成功：{filename} → icon.png")
            return
        except Exception as e:
            print(f"[!] 重命名失败：{e}")

    print(f"[!] 未找到可重命名的图片")

# -----------------------
# 第一步：批量处理 icon（强制解锁）
# -----------------------
print("\n========== 开始处理 icon ==========\n")
for domain_folder in domains:
    print(f"\n处理：{domain_folder}")
    force_rename_to_icon(domain_folder)

# -----------------------
# 第二步：你原来的 logo 生成逻辑（完全没改！）
# -----------------------
print("\n========== 开始生成 logo ==========\n")
for domain_folder in domains:
    text = domain_folder.replace(".com", "")
    output_dir = domain_folder

    font_name, (font_id, code) = random.choice(list(fonts.items()))
    encoded_text = base64.urlsafe_b64encode(text.encode()).decode()
    print(f"Domain: {domain_folder} → Text: {text}")

    url = f"https://see.fontimg.com/api/rf5/{font_id}/{code}/{encoded_text}/{font_name}?r=fs&h={height}&w={width}&fg={fg_color}&bg={bg_color}&tb={tb}&s={size}"

    try:
        response = requests.get(url)
        if response.status_code == 200:
            filepath = os.path.join(output_dir, "logo.png")
            with open(filepath, "wb") as f:
                f.write(response.content)
            print(f"[✓] 保存成功：{filepath}")
        else:
            print(f"[✗] 失败：{text}")
    except Exception as e:
        print(f"[✗] 异常：{e}")

    time.sleep(delay)

print("\n✅ 全部完成！")