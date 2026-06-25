import os
import sys

def run(*paths, start_path: str = None, marker_files: list = None) -> str:
    """
    根据项目根目录拼接路径，同时整合查找项目根目录和拼接路径的功能。
    
    参数：
        *paths: 要拼接的多个路径片段。
        start_path: 查找起点，如果为 None，则使用当前脚本的路径。
        marker_files: 用来判断是否为项目根目录的文件或文件夹列表，
                      默认为 ['venv']，可以根据需要修改。
    
    返回：
        拼接后的完整路径；如果该路径不存在，则返回 None。
    """
    # 确定起始路径
    if start_path is None:
        try:
            start_path = os.path.abspath(__file__)
        except NameError:
            start_path = os.path.abspath(sys.argv[0])
    
    # 默认标识文件列表
    if marker_files is None:
        marker_files = ['venv']  # 这里用 venv 嵌入式环境的文件夹名称作为项目根目录的判断标准
    
    # 从当前文件所在目录开始向上查找项目根目录
    current_dir = os.path.dirname(start_path)
    root = current_dir
    while current_dir and current_dir != os.path.dirname(current_dir):
        for marker in marker_files:
            if os.path.exists(os.path.join(current_dir, marker)):
                root = current_dir
                break
        else:
            # 没有找到标识文件则继续向上查找
            current_dir = os.path.dirname(current_dir)
            continue
        break

    # 根据项目根目录拼接传入的路径片段
    full_path = os.path.join(root, *paths)
    
    # 验证拼接后的路径是否存在
    if not os.path.exists(full_path):
        return None
    return full_path

# 示例：打印拼接后的文件路径
if __name__ == "__main__":
    file_path = run("data", "requirements.txt")
    if file_path:
        print("拼接后的文件路径：", file_path)
    else:
        print("拼接的文件路径无效,不存在！")
