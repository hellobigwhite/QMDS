# client.py  ——  终极版：本地秒替换 + 扔队列
import os, requests, pandas as pd, datetime, time
from tqdm import tqdm

os.environ['NO_PROXY'] = '*'
BACKEND_URL = "http://194.195.86.228:8000/create_task"   # ← 改成你真实 IP
SECRET_KEY = "xzIHXio2iohsO973"

def process_and_upload(excel_path):
    df = pd.read_excel(excel_path)
    task_name = f"ruralking_{int(time.time())}"
    payload = {"task_name": task_name, "images": []}

    print("正在秒替换链接（完全不等下载）...")
    for idx, row in tqdm(df.iterrows(), total=len(df)):
        cell = str(row["Images"])
        links = [x.strip() for x in cell.split("|||") if x.strip().startswith("http")]
        new_links = []

        for i, url in enumerate(links):
            new_name = f"{task_name}_{idx}_{i}.jpg"
            final_url = f"http://194.195.86.228/Imags/{task_name}/{new_name}"   # ← 直接生成最终外链

            # 扔给后端下载
            payload["images"].append({
                "original_url": url,
                "save_as": new_name                     # ← 关键！告诉后端存在这个名字
            })

            new_links.append(final_url)

        # 直接替换 Excel（秒级完成！）
        df.at[idx, "Images"] = "|||" .join(new_links)

    # 立刻保存新 Excel
    new_excel = f"【Img_bed】{os.path.basename(excel_path)}"
    df.to_excel(new_excel, index=False)
    print(f"链接替换完成！新文件：{new_excel}")

    # 扔给后端队列慢慢下
    r = requests.post(BACKEND_URL, json=payload, headers={
        "Authorization": f"Bearer {SECRET_KEY}",
        "Content-Type": "application/json"
    }, timeout=10000)
    print("后端回复：", r.json())
    print("图片已经下载，数据最保守上传时间在第二天")

if __name__ == "__main__":
    path = input("拖 Excel 到这里 → ").strip().strip('"')
    process_and_upload(path)