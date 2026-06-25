import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import os
import json
import sys


class ScriptLauncher:
    def __init__(self, root):
        self.root = root
        self.root.title("AHU-TOOLS")
        self.root.configure(bg='#2d2d2d')
        
        # 配置暗黑主题样式
        style = ttk.Style()
        style.theme_use('alt')
        self.configure_styles(style)

        # 主容器
        main_frame = ttk.Frame(root, style='Dark.TFrame')
        main_frame.pack(padx=10, pady=10, fill='both', expand=True)

        # 加载配置
        self.categories = self.load_scripts_config("功能脚本配置.json")
        
        # 创建分类区块
        self.create_category_blocks(main_frame)
        
        # 底部功能按钮
        self.create_bottom_buttons(main_frame)


        # 窗口尺寸自适应
        self.root.update_idletasks()
        self.root.minsize(400, 300)  # 最小尺寸限制
        self.root.resizable(True, True)  # 允许拉伸

        # 适配高清屏
        self.root.tk.call('tk', 'scaling', 1.3)

    def configure_styles(self, style):
        """配置界面样式"""
        style.configure('Dark.TFrame', background='#2d2d2d')
        style.configure(
            'Category.TLabelframe', 
            background='#3d3d3d', 
            foreground='#e0e0e0',
            font=('微软雅黑', 11, 'bold'),
            borderwidth=2,
            relief='ridge'
        )
        style.configure(
            'TButton',
            font=('微软雅黑', 10),
            padding=6,
            foreground='#ffffff',
            background='#404040',
            relief='flat'
        )
        style.map('TButton',
                background=[('active', '#007acc'), ('!active', '#404040')],
                foreground=[('active', 'white'), ('!active', 'white')])

    def load_scripts_config(self, file_name):
        """加载带分类的配置文件"""
        # 适配打包和未打包环境
        if getattr(sys, 'frozen', False):  # 打包后的 exe 运行环境
            base_dir = os.path.dirname(sys.executable)
        else:  # 开发模式（Python 直接运行）
            base_dir = os.path.dirname(os.path.abspath(__file__))

        config_path = os.path.join(base_dir, "配置", file_name)

        # 如果配置文件不存在，则创建默认文件
        if not os.path.exists(config_path):
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            default_config = {
                "categories": [
                    {
                        "name": "默认分类",
                        "scripts": [
                            {
                                "name": "示例脚本",
                                "path": "示例脚本路径"
                            }
                        ]
                    }
                ]
            }
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(default_config, f, indent=4, ensure_ascii=False)
                messagebox.showinfo("提示", f"已创建默认配置文件: {config_path}")
            except Exception as e:
                messagebox.showerror("错误", f"无法创建配置文件: {str(e)}")
                self.root.destroy()
                exit(1)

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                return config.get("categories", [])
        except Exception as e:
            messagebox.showerror("错误", f"配置文件解析失败: {str(e)}")
            self.root.destroy()
            exit(1)

    def create_category_blocks(self, parent):
        """创建分类区块"""
        for category in self.categories:
            # 分类容器
            frame = ttk.LabelFrame(
                parent,
                text=f" {category['name']} ",
                style='Category.TLabelframe'
            )
            frame.pack(pady=6, padx=5, fill='x', expand=True)

            # 按钮容器（自适应布局）
            btn_container = ttk.Frame(frame, style='Dark.TFrame')
            btn_container.pack(fill='both', expand=True, padx=5, pady=5)

            # 动态创建按钮（每行2列）
            columns = 2
            for i, script in enumerate(category['scripts']):
                row = i // columns
                col = i % columns
                if col == 0:
                    btn_row = ttk.Frame(btn_container, style='Dark.TFrame')
                    btn_row.pack(fill='x', pady=2)

                btn = ttk.Button(
                    btn_row,
                    text=script['name'],
                    command=lambda p=script['path']: self.execute_script(p),
                    style='TButton'
                )
                btn.pack(side='left', padx=3, pady=2, fill='x', expand=True)




#------------------------------------------
#
#------------------------------------------
    def create_bottom_buttons(self, parent):
        """创建底部操作按钮"""
        btn_frame = ttk.Frame(parent, style='Dark.TFrame')
        btn_frame.pack(side='bottom', fill='x', pady=10)

        style = ttk.Style()
        style.configure('Bottom.TButton', font=('微软雅黑', 10, 'bold'))

        update_deps_btn = ttk.Button(
            btn_frame,
            text="一键更新依赖库",
            style='Bottom.TButton',
            command=self.update_dependencies
        )
        update_deps_btn.pack(side='left', expand=True, padx=5)


        

 #------------------------------------------   
    def update_dependencies(self):
        """执行依赖库更新（新控制台窗口显示进度）"""
        # 适配开发和打包环境
        if getattr(sys, 'frozen', False):  # 如果是打包的 exe 运行
            base_dir = os.path.dirname(sys.executable)
        else:  # 开发模式下运行
            base_dir = os.path.dirname(os.path.abspath(__file__))

        # Python解释器路径
        python_path = os.path.normpath(os.path.join(base_dir, "venv", "python.exe"))

        # 验证关键路径
        validation = []
        if not os.path.isfile(python_path):
            validation.append(f"Python解释器路径不存在：\n{python_path}")
        req_file = os.path.join(base_dir, "requirements.txt")
        if not os.path.isfile(req_file):
            validation.append(f"依赖文件不存在：\n{req_file}")

        if validation:
            return False, "环境验证失败：\n\n" + "\n\n".join(validation)

        # 构建多步骤命令
        cmd_sequence = [
            'chcp 65001',  # 设置控制台编码
            'echo 正在初始化依赖更新...',
            f'echo 虚拟环境Python路径: {python_path}',
            f'echo 依赖文件路径: {req_file}',
            f'"{python_path}" -m pip install -r "{req_file}" --upgrade '
            f'-i https://mirrors.aliyun.com/pypi/simple/ '
            f'--trusted-host mirrors.aliyun.com',  # 添加信任参数
            'echo. && echo 操作已完成，请检查上方输出！',
            'pause'
        ]

        try:
            # 启动独立控制台窗口
            subprocess.Popen(
                f'start cmd /k "{ " && ".join(cmd_sequence) }"',
                shell=True,
                cwd=base_dir
            )
            return True, "已启动依赖更新，请查看控制台窗口..."
        except Exception as e:
            return False, f"启动更新进程失败：{str(e)}"

#-----------------------------
    def execute_script(self, script_path):
        """执行指定脚本"""
        # 适配开发和打包环境
        if getattr(sys, 'frozen', False):  # 如果是打包的 exe 运行
            base_dir = os.path.dirname(sys.executable)
            venv_python = os.path.join(base_dir, "venv", "python.exe")  # 指向 venv Python
        else:  # 开发模式下运行
            base_dir = os.path.abspath(os.path.dirname(__file__))
            venv_python = os.path.join(base_dir, "venv", "python.exe")

         # 规范化路径，适配不同环境
        target_script = os.path.normpath(os.path.join(base_dir, script_path))

        # 验证路径有效性
        if not os.path.isfile(target_script):
            messagebox.showerror("错误", f"脚本文件 {target_script} 不存在！")
            return
        if not os.path.isfile(venv_python):
            messagebox.showerror("错误", f"Python 嵌入式环境未找到！")
            return

        # 构造 CMD 命令
        cmd_sequence = (
            f'chcp 65001 && '
            f'echo 正在使用 Python 路径: "{venv_python}" && '
            f'echo 正在执行脚本: "{target_script}" && '
            f'"{venv_python}" "{target_script}" && '
            'pause'
        )

        try:
            # 启动独立的控制台窗口
            subprocess.Popen(
                f'start cmd /k "{cmd_sequence}"',
                shell=True,
                cwd=base_dir
            )
        except Exception as e:
            messagebox.showerror("错误", f"启动脚本失败：{str(e)}")
    





if __name__ == "__main__":
    root = tk.Tk()
    app = ScriptLauncher(root)  # 运行主程序
    root.mainloop()