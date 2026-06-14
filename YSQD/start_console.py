
import subprocess
import sys
import os

if __name__ == "__main__":
    # 获取当前脚本所在目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    app_path = os.path.join(current_dir, "app.py")
    
    # 使用 subprocess 在新控制台窗口运行 app.py
    # 在 Windows 上使用 CREATE_NEW_CONSOLE 标志
    subprocess.Popen(
        [sys.executable, app_path],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        cwd=current_dir
    )
