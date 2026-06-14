#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shopify 管理工具 GUI - 独立终端执行脚本
- 左右双栏布局（更合理）
- 自动运行（Windows / macOS / Linux）
- 动态加载 MongoDB 集合列表
- 新增按钮：运行 transter.py
- 新增：删除 Crontab 任务（放入数据清理区）
"""

import os
import sys
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class ShopifyToolApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("🛒 Shopify 管理工具")
        self.geometry("900x780")
        self.resizable(True, True)

        self.selected_collection = tk.StringVar(value="electronics")

        # MongoDB 配置
        self.MONGO_URI = "mongodb://localhost:27017/"
        self.DB_NAME = "shopify_data_new"

        self._setup_style()
        self._build_ui()
        self.refresh_collections()

    # ---------- 样式 ----------
    def _setup_style(self):
        style = ttk.Style()
        style.theme_use("default")

        style.configure(
            "Big.TButton",
            font=("Segoe UI Emoji", 12),
            padding=(14, 10)
        )

        style.configure(
            "Section.TLabelframe",
            font=("Segoe UI Emoji", 11, "bold")
        )

        style.configure(
            "Section.TLabelframe.Label",
            font=("Segoe UI Emoji", 11, "bold")
        )

    # ---------- UI ----------
    def _build_ui(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)

        # 左右两栏
        left = ttk.Frame(main, width=420)
        left.pack(side="left", fill="y", padx=(0, 10))
        left.pack_propagate(False)

        right = ttk.Frame(main)
        right.pack(side="right", fill="both", expand=True)

        def section(parent, title):
            lf = ttk.LabelFrame(
                parent,
                text=title,
                padding=10,
                style="Section.TLabelframe"
            )
            lf.pack(fill="x", pady=8)
            return lf

        def big_btn(parent, text, cmd):
            ttk.Button(
                parent,
                text=text,
                command=cmd,
                style="Big.TButton"
            ).pack(fill="x", pady=4)

        # ================= 左侧：流程型操作 =================
        s1 = section(left, "📥 数据采集")
        big_btn(s1, "🔗 爬取 URL", lambda: self.run_script("google_url.py"))
        big_btn(s1, "🧠 自动分类 / 过滤 URL", lambda: self.run_script("shopify_category_v3.py"))
        big_btn(s1, "📦 抓取数据", lambda: self.run_script("Crawling_data_version2.py"))

        s2 = section(left, "🗂 分类处理")
        big_btn(s2, "♻️ 优化分类 / 移动回收站", lambda: self.run_script("dbCategorySet.py"))
        big_btn(s2, "🧩 主类分类", lambda: self.run_script("mainfenle.py"))
        big_btn(s2, "🔁 分类替换（Excel）", lambda: self.run_script("分类替换.py"))

        s3 = section(left, "🧹 数据清理")
        big_btn(s3, "🧼 清理数据", lambda: self.run_script("dbCleaning.py"))
        big_btn(s3, "🖼 清理图片异常", lambda: self.run_script("dbImageCleaning.py"))
        # ===================== 在这里加入 crontab 删除 =====================
        big_btn(s3, "🗑️ 删除 Crontab 任务", lambda: self.run_script("crontab 1.py"))

        # ================= 右侧：关键 / 高风险操作 =================
        s4 = section(right, "✨ 导出前去重（重要）")

        ttk.Label(
            s4,
            text="📚 选择集合：",
            font=("Segoe UI Emoji", 10)
        ).pack(anchor="w", pady=(0, 4))

        self.combo_collection = ttk.Combobox(
            s4,
            textvariable=self.selected_collection,
            state="readonly",
            font=("Segoe UI Emoji", 10)
        )
        self.combo_collection.pack(fill="x", pady=(0, 8))

        refresh_frame = ttk.Frame(s4)
        refresh_frame.pack(fill="x", pady=(0, 8))

        ttk.Button(
            refresh_frame,
            text="🔄 刷新集合列表",
            command=self.refresh_collections,
            style="Big.TButton"
        ).pack(side="left")

        ttk.Label(
            refresh_frame,
            text="新增集合后点击刷新",
            font=("Segoe UI Emoji", 9),
            foreground="gray"
        ).pack(side="left", padx=(10, 0))

        big_btn(s4, "🚀 执行去重", self.run_dedup)
        big_btn(s4, "📤 导出并备份", self.run_export)

        s5 = section(right, "🚀 扩展功能")

        scripts = [
            ("📤 上传数据", "batchdeal.py"),
            ("🌐 seo与wp以及图片设置", "set.py"),
            ("🔄 数据转移 / 转换", "transter.py"),
            ("🏗️ 一键建站", "建站.py"),
            ("🖼️ 下载图片", "client.py"),
            ("🖼️ 收录查询", "收录查询.py"),
            ("📋 txt爬取", "txt爬取.py"),
            ("📁 URL导出", "URL导出.py"),
        ]

        for text, filename in scripts:
            big_btn(s5, text, lambda f=filename: self.run_script(f))

        # 底部说明
        ttk.Label(
            right,
            text="💡 所有脚本将在独立终端中运行\n"
                 "💾 集合列表来自 MongoDB，可随时刷新",
            foreground="green",
            font=("Segoe UI Emoji", 9),
            justify="center"
        ).pack(pady=12)

    # ---------- MongoDB 集合 ----------
    def refresh_collections(self):
        try:
            import pymongo
            client = pymongo.MongoClient(self.MONGO_URI)
            db = client[self.DB_NAME]

            collections = [
                name for name in db.list_collection_names()
                if not name.startswith("system.")
            ]

            if not collections:
                collections = ["<无集合>"]
                messagebox.showinfo("提示", "数据库中未找到任何集合。")

            collections.sort()
            self.combo_collection["values"] = collections

            if self.selected_collection.get() not in collections:
                self.selected_collection.set(collections[0])

            self.title(f"🛒 Shopify 管理工具 - 已加载 {len(collections)} 个集合")

        except Exception as e:
            messagebox.showerror(
                "MongoDB 连接失败",
                f"{e}\n\n将使用默认集合"
            )
            fallback = ["electronics", "toy"]
            self.combo_collection["values"] = fallback
            self.selected_collection.set(fallback[0])

    # ---------- 执行脚本 ----------
    def run_script(self, script, args=None):
        path = os.path.join(BASE_DIR, script)
        if not os.path.exists(path):
            messagebox.showerror("错误", f"脚本不存在: {script}")
            return

        args = args or []

        if sys.platform.startswith("win"):
            cmd = f'{sys.executable} "{path}" {" ".join(args)}'
            subprocess.Popen(f'start cmd /k "{cmd}"', shell=True)

        elif sys.platform.startswith("linux"):
            subprocess.Popen(
                ["gnome-terminal", "--", sys.executable, path] + args
            )

        elif sys.platform.startswith("darwin"):
            cmd = f'{sys.executable} "{path}" {" ".join(args)}'
            subprocess.Popen([
                "osascript", "-e",
                f'tell application "Terminal" to do script "{cmd}"'
            ])

    def run_export(self):
        collection = self.selected_collection.get()
        if collection == "<无集合>":
            messagebox.showwarning("警告", "没有可用的集合")
            return
        self.run_script(
            "mongodb_export_data_version4.py",
            ["--collection", collection]
        )

    def run_dedup(self):
        collection = self.selected_collection.get()
        if collection == "<无集合>":
            messagebox.showwarning("警告", "没有可用的集合")
            return
        self.run_script(
            "qvchng.py",
            ["--collection", collection]
        )


if __name__ == "__main__":
    try:
        import pymongo  # noqa
    except ImportError:
        messagebox.showerror(
            "缺少依赖",
            "请先运行：pip install pymongo"
        )
        sys.exit(1)

    ShopifyToolApp().mainloop()