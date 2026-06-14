import os
import json
import pandas as pd
from tkinter import Tk
from tkinter.filedialog import askopenfilename
from sentence_transformers import SentenceTransformer, util

# ========= 配置 =========
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "分类")
MODEL_NAME = "all-MiniLM-L6-v2"
SPLIT = "|||"
MAIN_SIM_THRESHOLD = 0.4  # 主类匹配阈值
PATH_SIM_THRESHOLD = 0.5  # 子分类匹配阈值

# ========= 工具函数 =========
def get_json_files():
    return [f for f in os.listdir() if f.endswith(".json")]

def flatten_categories(node, path=None, result=None):
    """展开 JSON 层级，返回 path + text"""
    if path is None:
        path = []
    if result is None:
        result = []
    if isinstance(node, dict):
        for k, v in node.items():
            flatten_categories(v, path + [k], result)
    elif isinstance(node, list):
        combined_text = " ".join(node)
        result.append({
            "path": path,
            "text": combined_text
        })
    return result

# ========= 加载 JSON 并预编码 =========
def load_json(json_file, model):
    with open(json_file, encoding="utf-8") as f:
        data = json.load(f)

    cat_index = {}
    for main_cat, info in data.items():
        domain_text = " ".join(info.get("domain_keywords", []))
        domain_emb = model.encode(domain_text, convert_to_tensor=True)
        paths = flatten_categories(info.get("categories", {}))
        # 每个路径提前编码
        for p in paths:
            p["emb"] = model.encode(p["text"], convert_to_tensor=True)
        cat_index[main_cat] = {
            "domain_text": domain_text,
            "domain_emb": domain_emb,
            "paths": paths
        }
    return cat_index

# ========= 语义分类 =========
def classify_semantic(title, desc, cat_index, model):
    text = f"{title} {desc}"
    text_emb = model.encode(text, convert_to_tensor=True)

    best_main = None
    best_path = None
    best_score = 0.0
    best_depth = 0

    for main, info in cat_index.items():
        main_sim = util.pytorch_cos_sim(text_emb, info["domain_emb"]).item()
        if main_sim < MAIN_SIM_THRESHOLD:
            continue

        # 遍历路径，找最深匹配
        for path_item in info["paths"]:
            path_sim = util.pytorch_cos_sim(text_emb, path_item["emb"]).item()
            total_score = (main_sim + path_sim) / 2
            depth = len(path_item["path"])
            # 只更新最优匹配或者更深匹配且相似度达标
            if (total_score > best_score) or (path_sim >= PATH_SIM_THRESHOLD and depth > best_depth):
                best_score = total_score
                best_main = main
                best_path = path_item["path"]
                best_depth = depth

    # 回退处理
    if not best_main:
        return "Others"
    if best_path:
        path_clean = [p.replace("Ultimate", "").strip() for p in best_path]
        return SPLIT.join([best_main] + path_clean)
    else:
        return best_main

# ========= Excel 处理 =========
def process_xlsx():
    # 选择 JSON
    json_files = get_json_files()
    print("可用 JSON 文件：")
    for i, f in enumerate(json_files):
        print(f"{i}: {f}")
    idx = int(input("选择使用的 JSON 序号: "))
    selected_json = json_files[idx]

    # 选择 Excel
    root = Tk()
    root.withdraw()
    file_path = askopenfilename(
        title="选择 Excel 文件",
        initialdir=DEFAULT_DIR,
        filetypes=[("Excel files", "*.xlsx *.xls")]
    )
    if not file_path:
        print("未选择文件，程序退出")
        return

    df = pd.read_excel(file_path)

    model = SentenceTransformer(MODEL_NAME)
    cat_index = load_json(selected_json, model)

    # 分类
    df["分类"] = df.apply(lambda r: classify_semantic(r["标题"], r["描述"], cat_index, model), axis=1)

    # 输出
    out_dir = os.path.join(os.path.dirname(file_path), "分类结果")
    os.makedirs(out_dir, exist_ok=True)
    output_file = os.path.join(out_dir, os.path.basename(file_path))
    df.to_excel(output_file, index=False)
    print(f"分类完成，结果已保存到 {output_file}")

# ========= 执行 =========
if __name__ == "__main__":
    process_xlsx()
