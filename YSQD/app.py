import os
import shutil
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import ttk, messagebox, filedialog
try:
    import pandas as pd
except ImportError:
    pd = None

from config_window import ConfigWindow
from constants import (
    CATEGORY_ID_MAP,
    CATEGORY_OPTIONS,
    COLUMNS,
    DB_PATH,
    DOMAIN_STATUS_LABELS,
    EXCEL_COLS,
    EXTRA_COLUMNS,
    MEDIA_STATUS_COL,
    MEDIA_TIME_COL,
    REPORT_STATUS_COL,
    REPORT_TIME_COL,
    DOMAIN_RESOLVED_TIME_COL,
    SCHEDULE_ENABLED_COL,
    SCHEDULE_TIME_COL,
    DOMAIN_NUMBER_COL,
    BUILD_STATUS_COL,
    BUILD_TIME_COL,
    TABLE_NAME,
)
from datastore import DataStore
from domain_reporter_client import DomainReporter


class StationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("站群客户端")
        self.geometry("1200x640")
        self.minsize(980, 560)
        self._media_root = r"E:\logo"

        self.store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)

        self._editing = None  # (row_id, col_id)
        self._cur_col_index = 5
        
        self._schedule_warned = False
        self._auto_refresh_enabled = False  # 自动刷新开关，默认关闭
        
        self._build_ui()
        self._load_rows()
        self._load_reported()
        self._load_built()
        # 启动定时任务
        self._schedule_tick()

    def _build_ui(self):
        style = ttk.Style(self)
        style.configure(
            "Custom.Treeview",
            rowheight=26,
            bordercolor="#8A8A8A",
            borderwidth=1,
            relief="solid",
            background="white",
            fieldbackground="white",
        )
        style.configure(
            "Custom.Treeview.Heading",
            background="#E6E6E6",
            bordercolor="#8A8A8A",
            borderwidth=1,
            relief="raised",
        )
        style.map(
            "Custom.Treeview",
            background=[("selected", "#DCE9FF")],
        )

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

        local_tab = ttk.Frame(notebook)
        report_tab = ttk.Frame(notebook)
        built_tab = ttk.Frame(notebook)
        notebook.add(local_tab, text="本地站点管理")
        notebook.add(report_tab, text="已报域名")
        notebook.add(built_tab, text="已建站")

        top = ttk.Frame(local_tab)
        top.pack(fill=tk.X, padx=6, pady=6)

        ttk.Label(top, text="搜索:").pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(top, textvariable=self.search_var, width=30)
        search_entry.pack(side=tk.LEFT, padx=(6, 10))
        search_entry.bind("<Return>", lambda _e: self._load_rows())

        ttk.Button(top, text="搜索/刷新", command=self._load_rows).pack(side=tk.LEFT)
        ttk.Button(top, text="新增行", command=self._add_row).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(top, text="删除所选", command=self._delete_selected).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="导出CSV", command=self._export_csv).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="导入Excel", command=self._import_excel).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="配置", command=self._open_config).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="主分类管理", command=self._open_main_category_config).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="域名上报(勾选)", command=self._report_selected).pack(side=tk.LEFT, padx=(10, 0))
        ttk.Button(top, text="批量修改", command=self._open_batch_edit).pack(side=tk.LEFT, padx=(6, 0))
        
        # 添加自动刷新开关
        auto_refresh_frame = ttk.Frame(top)
        auto_refresh_frame.pack(side=tk.RIGHT, padx=(10, 0))
        self.auto_refresh_var = tk.BooleanVar(value=self._auto_refresh_enabled)
        ttk.Checkbutton(auto_refresh_frame, text="自动刷新", variable=self.auto_refresh_var, command=self._toggle_auto_refresh).pack(side=tk.RIGHT)

        # 添加待处理站点小界面
        self._build_pending_panel(local_tab, "未报站点", self._get_pending_local_sites, self._process_pending_local)

        mid = ttk.Frame(local_tab)
        mid.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        columns = ["_plan", "_status", "_media", "_row", "_sel"] + [name for name, _ in COLUMNS] + [SCHEDULE_TIME_COL]
        self.tree = ttk.Treeview(
            mid,
            columns=columns,
            show="headings",
            selectmode="browse",
            style="Custom.Treeview",
        )

        self.tree.heading("_plan", text="计划")
        self.tree.column("_plan", width=60, anchor=tk.CENTER, stretch=False)
        self.tree.heading("_status", text="状态")
        self.tree.column("_status", width=80, anchor=tk.CENTER, stretch=False)
        self.tree.heading("_media", text="媒体")
        self.tree.column("_media", width=80, anchor=tk.CENTER, stretch=False)
        self.tree.heading("_row", text="序号")
        self.tree.column("_row", width=60, anchor=tk.CENTER, stretch=False)
        self.tree.heading("_sel", text="✓")
        self.tree.column("_sel", width=40, anchor=tk.CENTER, stretch=False)

        for idx, (name, title) in enumerate(COLUMNS):
            self.tree.heading(name, text=f"{EXCEL_COLS[idx]}  {title}")
            self.tree.column(name, width=140, anchor=tk.CENTER)
        self.tree.heading(SCHEDULE_TIME_COL, text="计划时间")
        self.tree.column(SCHEDULE_TIME_COL, width=160, anchor=tk.CENTER)

        self.tree.tag_configure("odd", background="#ECECEC")
        self.tree.tag_configure("even", background="white")

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(mid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)

        self.tree.bind("<Double-1>", self._begin_edit)
        self.tree.bind("<Return>", self._begin_edit)
        self.tree.bind("<Button-1>", self._on_click)
        self.bind_all("<Button-1>", self._global_click, add="+")
        self.tree.bind("<Configure>", self._on_tree_configure)

        # Keyboard navigation like Excel
        self.tree.bind("<Up>", lambda e: self._move_focus(-1))
        self.tree.bind("<Down>", lambda e: self._move_focus(1))
        self.tree.bind("<Left>", lambda e: self._move_focus(0, -1))
        self.tree.bind("<Right>", lambda e: self._move_focus(0, 1))

        self._build_report_tab(report_tab)
        # 添加已解析站点小界面
        self._build_pending_panel(report_tab, "已解析站点", self._get_pending_reported_sites, self._process_pending_reported)
        
        self._build_built_tab(built_tab)
        # 添加未完成状态站点小界面
        self._build_pending_panel(built_tab, "未完成站点", self._get_pending_built_sites, self._process_pending_built)

    def _query_rows(self, keyword):
        return self.store.query_rows(keyword)

    def _filter_rows_by_domain(self, rows, keyword):
        keyword = (keyword or "").strip().lower()
        if not keyword:
            return rows
        return [row for row in rows if keyword in ((row["domain"] or "").strip().lower())]

    def _load_rows(self):
        # 清空现有数据
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        keyword = self.search_var.get().strip()
        rows = self._query_rows(keyword)
        
        # 排序：未报的在前面，已报的在后面
        def sort_key(row):
            status = row[REPORT_STATUS_COL] or "未报"
            if status == "未报":
                return (0, row["id"])
            else:
                return (1, row["id"])
        
        rows.sort(key=sort_key)
        
        # 批量插入数据
        self.tree.yview_moveto(0)  # 滚动到顶部
        self.tree.update_idletasks()  # 更新界面
        
        for idx, row in enumerate(rows, start=1):
            status = row[REPORT_STATUS_COL] or "未报"
            media_status = row[MEDIA_STATUS_COL] or "未配置"
            plan_enabled = (row[SCHEDULE_ENABLED_COL] or "").strip()
            plan_flag = "✓" if plan_enabled in {"1", "true", "True"} else ""
            schedule_time = row[SCHEDULE_TIME_COL] or ""
            display_time = self._format_schedule_display(plan_flag, schedule_time)
            values = [plan_flag, status, media_status, str(idx), ""] + [row[name] or "" for name, _ in COLUMNS] + [display_time]
            tag = "even" if idx % 2 == 0 else "odd"
            self.tree.insert("", tk.END, iid=str(row["id"]), values=values, tags=(tag,))
        
        self.tree.update_idletasks()  # 更新界面

    def _add_row(self):
        self.store.add_row()
        self._load_rows()

    def _delete_selected(self):
        ids = []
        for item in self.tree.get_children():
            if self.tree.set(item, "_sel") == "✓":
                ids.append(item)
        if not ids:
            messagebox.showinfo("提示", "请先勾选要删除的行。")
            return
        if not messagebox.askyesno("确认删除", f"确定删除 {len(ids)} 行吗？"):
            return
        self.store.delete_rows(ids)
        self._load_rows()

    def _export_csv(self):
        path = filedialog.asksaveasfilename(
            title="导出CSV",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        rows = self._query_rows(self.search_var.get().strip())
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(",".join([title for _n, title in COLUMNS]) + "\n")
            for row in rows:
                values = [row[name] or "" for name, _ in COLUMNS]
                safe = [v.replace('"', '""') for v in values]
                f.write(",".join([f"\"{v}\"" for v in safe]) + "\n")
        messagebox.showinfo("完成", f"已导出 {len(rows)} 行。")

    def _import_excel(self):
        if pd is None:
            messagebox.showerror("错误", "需要安装 pandas 和 openpyxl 库。请运行: pip install pandas openpyxl")
            return
        
        path = filedialog.askopenfilename(
            title="导入Excel",
            filetypes=[("Excel文件", "*.xlsx *.xls")],
        )
        if not path:
            return
        
        # 在新窗口中运行导入操作
        import subprocess
        import sys
        import json
        import os
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        importer_path = os.path.join(current_dir, "import_worker.py")
        
        # 创建导入工作器脚本
        import_worker_content = """
import sys
import pandas as pd
import os
from datastore import DataStore
from constants import (
    COLUMNS,
    DB_PATH,
    TABLE_NAME,
    REPORT_STATUS_COL,
    EXTRA_COLUMNS,
)

# 验证并处理输入值，确保数字后面不能带小数点
def validate_value(value, is_main_category=False):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    str_value = str(value).strip()
    if not str_value or str_value.lower() == "nan":
        return ""
    # 检查是否是纯数字且包含小数点
    if str_value.replace('.', '', 1).isdigit() and '.' in str_value:
        # 去掉小数点，只保留整数部分
        str_value = str_value.split('.')[0]
        print(f"警告：将带小数点的数字 '{value}' 转换为整数 '{str_value}'")
    # 处理主分类，只保存最后一级分类名称
    if is_main_category and str_value:
        # 如果是多级分类，只取最后一级
        if "|||" in str_value:
            str_value = str_value.split("|||")[-1].strip()
    return str_value

if __name__ == "__main__":
    path = sys.argv[1]
    media_root = sys.argv[2]
    
    store = DataStore(DB_PATH, TABLE_NAME, COLUMNS, REPORT_STATUS_COL, EXTRA_COLUMNS)
    
    try:
        df = pd.read_excel(path)
        count = 0
        
        print(f"开始导入Excel文件: {path}")
        print(f"共 {len(df)} 行数据")
        
        for idx, row in df.iterrows():
            classification = validate_value(row.get("分类", ""))
            build_flag = validate_value(row.get("是否建站", ""))
            domain = validate_value(row.get("域名", ""))
            template = validate_value(row.get("底板", ""))
            main_data_source_id = validate_value(row.get("主分类数据码", ""))
            extra_data_source_id = validate_value(row.get("站群数据码", ""))
            main_category = validate_value(row.get("主分类", ""), is_main_category=True)
            category = validate_value(row.get("大类", ""))
            schedule_time = validate_value(row.get("时间", ""))
            title = validate_value(row.get("SEO Title（最大58字符）", ""))
            title_translation = validate_value(row.get("中文标题翻译", ""))
            description = validate_value(row.get("Meta Description", ""))
            description_translation = validate_value(row.get("中文描述翻译", ""))
            main_keyword = validate_value(row.get("主关键词", ""))
            long_tail_keywords = validate_value(row.get("长尾关键词", ""))
            address = validate_value(row.get("地址", ""))
            server = validate_value(row.get("服务器", ""))
            row_values = {
                "classification": classification,
                "build_flag": build_flag,
                "domain": domain,
                "template": template,
                "main_data_source_id": main_data_source_id,
                "extra_data_source_id": extra_data_source_id,
                "main_category": main_category,
                "category": category,
                "schedule_time": schedule_time,
                "title": title,
                "title_translation": title_translation,
                "description": description,
                "description_translation": description_translation,
                "main_keyword": main_keyword,
                "long_tail_keywords": long_tail_keywords,
                "address": address,
                "server": server,
            }

            if domain:
                logo_path = os.path.join(media_root, domain, "logo.png")
                banner_path = os.path.join(media_root, domain, "banner.jpg")
                icon_path = os.path.join(media_root, domain, "icon.png")

                if os.path.exists(logo_path):
                    row_values["logo"] = logo_path
                if os.path.exists(banner_path):
                    row_values["banner"] = banner_path
                if os.path.exists(icon_path):
                    row_values["icon"] = icon_path

            if domain:
                existing_rows = store.get_rows_by_field("domain", domain)
                if existing_rows:
                    latest_row = existing_rows[0]
                    store.update_fields(latest_row["id"], row_values, commit=False)

                    duplicate_ids = [item["id"] for item in existing_rows[1:]]
                    if duplicate_ids:
                        store.delete_rows(duplicate_ids)
                    print(f"已覆盖更新域名: {domain}")
                else:
                    store.add_row(row_values, commit=False)
                    print(f"已新增域名: {domain}")
            else:
                store.add_row(row_values, commit=False)
            count += 1
            
            if (idx + 1) % 10 == 0:
                print(f"已导入 {idx + 1} 行")
        
        store.commit()
        store.close()
        
        print(f"\n导入完成：成功导入 {count} 行数据")
    
    except Exception as e:
        import traceback
        error_msg = f"导入失败: {str(e)}\n\n{traceback.format_exc()}"
        print(error_msg)
    
    input("按回车键退出...")
"""
        
        with open(importer_path, 'w', encoding='utf-8') as f:
            f.write(import_worker_content)
        
        cmd = [
            sys.executable,
            importer_path,
            path,
            self._media_root
        ]
        
        messagebox.showinfo("提示", "开始导入Excel数据，正在打开新窗口...")
        
        # 在新控制台窗口运行
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=current_dir
        )
        
        # 稍后刷新一下界面
        self.after(5000, self._load_rows)

    def _open_batch_edit(self):
        """
        打开批量修改对话框
        """
        # 首先获取勾选的行
        selected_ids = []
        for item in self.tree.get_children():
            if self.tree.set(item, "_sel") == "✓":
                selected_ids.append(item)
        
        if not selected_ids:
            messagebox.showinfo("提示", "请先勾选要批量修改的行。")
            return
        
        if hasattr(self, "_batch_edit_win") and self._batch_edit_win.winfo_exists():
            self._batch_edit_win.focus_set()
            return
        
        win = tk.Toplevel(self)
        win.title("批量修改")
        win.geometry("600x350")
        win.transient(self)
        self._batch_edit_win = win
        
        # 创建字段选择下拉框
        ttk.Label(win, text="选择要修改的字段:").pack(pady=(20, 10))
        
        # 可编辑的字段列表（排除logo、banner、icon这些文件字段）
        editable_fields = [
            ("template", "模板"),
            ("main_data_source_id", "主数据源ID"),
            ("extra_data_source_id", "补充数据源ID"),
            ("main_category", "主打类目"),
            ("category", "大类"),
            ("title", "SEO Title（最大58字符）"),
            ("description", "Meta Description"),
            ("address", "地址"),
            ("server", "服务器"),
        ]
        
        field_var = tk.StringVar()
        field_combobox = ttk.Combobox(
            win, 
            values=[f"{name} - {title}" for name, title in editable_fields],
            state="readonly",
            width=40
        )
        field_combobox.pack(pady=(0, 10))
        
        # 新值输入区域
        ttk.Label(win, text="输入新值:").pack(pady=(10, 5))
        
        # 对于有下拉选项的字段，使用Combobox，否则使用Entry
        value_var = tk.StringVar()
        value_entry = ttk.Entry(win, textvariable=value_var, width=40)
        value_combobox = None
        
        def on_field_change(event):
            nonlocal value_entry, value_combobox
            # 清除旧的输入控件
            if value_entry:
                value_entry.destroy()
            if value_combobox:
                value_combobox.destroy()
            
            selected = field_combobox.get()
            if not selected:
                return
            
            field_name = selected.split(" - ")[0]
            
            # 根据字段类型创建不同的输入控件
            if field_name == "category":
                value_combobox = ttk.Combobox(
                    win, 
                    values=CATEGORY_OPTIONS,
                    state="readonly",
                    width=40
                )
                value_combobox.pack(pady=(0, 20))
            elif field_name == "main_category":
                # 获取所有主分类，包括多级分类
                cur = self.store._db.execute("SELECT id, name, parent_id FROM main_category_options ORDER BY id ASC")
                categories = []
                category_map = {}
                for row in cur.fetchall():
                    category_map[row["id"]] = {
                        "name": row["name"],
                        "parent_id": row["parent_id"]
                    }
                
                def get_category_path(category_id):
                    path = []
                    current = category_id
                    while current in category_map:
                        path.insert(0, category_map[current]["name"])
                        current = category_map[current]["parent_id"]
                        if current == 0:
                            break
                    return "|||".join(path)
                
                for category_id in category_map:
                    categories.append(get_category_path(category_id))
                
                value_combobox = ttk.Combobox(
                    win, 
                    values=categories,
                    state="readonly",
                    width=40
                )
                value_combobox.pack(pady=(0, 20))
            elif field_name in ["template", "server"]:
                options = self.store.get_option_values(f"{field_name}_options")
                if options:
                    value_combobox = ttk.Combobox(
                        win, 
                        values=options,
                        state="readonly",
                        width=40
                    )
                    value_combobox.pack(pady=(0, 20))
                else:
                    value_entry = ttk.Entry(win, textvariable=value_var, width=40)
                    value_entry.pack(pady=(0, 20))
            else:
                value_entry = ttk.Entry(win, textvariable=value_var, width=40)
                value_entry.pack(pady=(0, 20))
        
        field_combobox.bind("<<ComboboxSelected>>", on_field_change)
        
        # 显示选中的行数
        ttk.Label(win, text=f"将修改 {len(selected_ids)} 行数据").pack(pady=(10, 10))
        
        # 按钮区域
        button_frame = ttk.Frame(win)
        button_frame.pack(pady=20)
        
        def apply_batch_edit():
            selected = field_combobox.get()
            if not selected:
                messagebox.showinfo("提示", "请选择要修改的字段。")
                return
            
            field_name = selected.split(" - ")[0]
            
            # 获取新值
            if value_combobox:
                new_value = value_combobox.get()
            else:
                new_value = value_var.get().strip()
            
            if not new_value:
                messagebox.showinfo("提示", "请输入新值。")
                return
            
            # 验证数字输入
            if new_value and new_value.replace('.', '', 1).isdigit() and '.' in new_value:
                messagebox.showinfo("提示", "数字后面不能带小数点，请输入整数。")
                return
            
            # 处理主分类，只保存最后一级分类名称
            if field_name == "main_category" and new_value:
                if "|||" in new_value:
                    new_value = new_value.split("|||")[-1].strip()
            
            if not messagebox.askyesno("确认", f"确定要将 {len(selected_ids)} 行的 '{selected.split(' - ')[1]}' 修改为 '{new_value}' 吗？"):
                return
            
            # 执行批量更新
            success_count = 0
            for row_id in selected_ids:
                try:
                    self.store.update_cell(row_id, field_name, new_value)
                    success_count += 1
                except Exception as e:
                    print(f"更新行 {row_id} 失败: {e}")
            
            messagebox.showinfo("完成", f"批量修改完成！成功更新 {success_count} 行。")
            self._load_rows()
            self._load_reported()
            self._load_built()
            win.destroy()
        
        ttk.Button(button_frame, text="应用", command=apply_batch_edit).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="取消", command=win.destroy).pack(side=tk.LEFT, padx=5)

    def _open_config(self):
        if hasattr(self, "_config_win") and self._config_win.winfo_exists():
            self._config_win.focus_set()
            return
        self._config_win = ConfigWindow(self, self.store)

    def _open_main_category_config(self):
        """
        打开主分类管理对话框
        """
        if hasattr(self, "_main_category_win") and self._main_category_win.winfo_exists():
            self._main_category_win.focus_set()
            return
        
        win = tk.Toplevel(self)
        win.title("主分类管理")
        win.geometry("700x400")
        win.transient(self)
        self._main_category_win = win
        
        # 主分类列表
        list_frame = ttk.Frame(win)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        columns = ["id", "name", "parent_id", "is_main_menu", "slug"]
        tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        tree.heading("id", text="分类ID")
        tree.column("id", width=80, anchor=tk.CENTER)
        tree.heading("name", text="分类名")
        tree.column("name", width=200, anchor=tk.CENTER)
        tree.heading("parent_id", text="父级ID")
        tree.column("parent_id", width=100, anchor=tk.CENTER)
        tree.heading("is_main_menu", text="是否主菜单")
        tree.column("is_main_menu", width=100, anchor=tk.CENTER)
        tree.heading("slug", text="Slug")
        tree.column("slug", width=200, anchor=tk.CENTER)
        
        # 加载现有主分类
        def load_main_categories():
            for item in tree.get_children():
                tree.delete(item)
            # 直接从数据库查询主分类
            cur = self.store._db.execute("SELECT id, name, parent_id, is_main_menu, slug FROM main_category_options ORDER BY id ASC")
            for row in cur.fetchall():
                is_main_menu_text = "是" if row["is_main_menu"] else "否"
                tree.insert("", tk.END, iid=str(row["id"]), values=[
                    row["id"],
                    row["name"],
                    row["parent_id"],
                    is_main_menu_text,
                    row["slug"] or ""
                ])
        
        load_main_categories()
        
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        
        # 操作区域
        button_frame = ttk.Frame(win)
        button_frame.pack(fill=tk.X, padx=10, pady=(0, 10))
        
        # 添加主分类
        def add_main_category():
            add_win = tk.Toplevel(win)
            add_win.title("添加主分类")
            add_win.geometry("400x200")
            add_win.transient(win)
            
            ttk.Label(add_win, text="分类名称:").grid(row=0, column=0, sticky="w", padx=10, pady=10)
            name_var = tk.StringVar()
            name_entry = ttk.Entry(add_win, textvariable=name_var, width=20)
            name_entry.grid(row=0, column=1, sticky="w", padx=10, pady=10)
            
            ttk.Label(add_win, text="父级ID:").grid(row=1, column=0, sticky="w", padx=10, pady=10)
            parent_id_var = tk.StringVar(value="0")
            parent_id_entry = ttk.Entry(add_win, textvariable=parent_id_var, width=20)
            parent_id_entry.grid(row=1, column=1, sticky="w", padx=10, pady=10)
            
            ttk.Label(add_win, text="是否主菜单:").grid(row=2, column=0, sticky="w", padx=10, pady=10)
            is_main_menu_var = tk.BooleanVar(value=False)
            is_main_menu_check = ttk.Checkbutton(add_win, variable=is_main_menu_var)
            is_main_menu_check.grid(row=2, column=1, sticky="w", padx=10, pady=10)
            
            ttk.Label(add_win, text="Slug:").grid(row=3, column=0, sticky="w", padx=10, pady=10)
            slug_var = tk.StringVar()
            slug_entry = ttk.Entry(add_win, textvariable=slug_var, width=20)
            slug_entry.grid(row=3, column=1, sticky="w", padx=10, pady=10)
            
            name_entry.focus_set()
            
            def save():
                name = name_var.get().strip()
                if not name:
                    messagebox.showinfo("提示", "请输入分类名称")
                    return
                
                try:
                    parent_id = int(parent_id_var.get().strip() or "0")
                except ValueError:
                    messagebox.showinfo("提示", "父级ID必须是数字")
                    return
                
                is_main_menu = 1 if is_main_menu_var.get() else 0
                slug = slug_var.get().strip()
                
                try:
                    # 直接插入数据到数据库
                    self.store._db.execute(
                        "INSERT INTO main_category_options (name, parent_id, is_main_menu, slug) VALUES (?, ?, ?, ?)",
                        (name, parent_id, is_main_menu, slug)
                    )
                    self.store._db.commit()
                    load_main_categories()
                    add_win.destroy()
                except Exception as e:
                    messagebox.showinfo("错误", f"添加失败: {e}")
            
            ttk.Button(add_win, text="保存", command=save).grid(row=4, column=0, padx=10, pady=10)
            ttk.Button(add_win, text="取消", command=add_win.destroy).grid(row=4, column=1, padx=10, pady=10)
        
        # 编辑主分类
        def edit_main_category():
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("提示", "请选择要编辑的主分类")
                return
            
            category_id = selected[0]
            # 从数据库获取分类信息
            cur = self.store._db.execute("SELECT id, name, parent_id, is_main_menu, slug FROM main_category_options WHERE id = ?", (category_id,))
            row = cur.fetchone()
            if not row:
                messagebox.showinfo("提示", "分类不存在")
                return
            
            edit_win = tk.Toplevel(win)
            edit_win.title("编辑主分类")
            edit_win.geometry("400x200")
            edit_win.transient(win)
            
            ttk.Label(edit_win, text="分类名称:").grid(row=0, column=0, sticky="w", padx=10, pady=10)
            name_var = tk.StringVar(value=row["name"])
            name_entry = ttk.Entry(edit_win, textvariable=name_var, width=20)
            name_entry.grid(row=0, column=1, sticky="w", padx=10, pady=10)
            
            ttk.Label(edit_win, text="父级ID:").grid(row=1, column=0, sticky="w", padx=10, pady=10)
            parent_id_var = tk.StringVar(value=str(row["parent_id"]))
            parent_id_entry = ttk.Entry(edit_win, textvariable=parent_id_var, width=20)
            parent_id_entry.grid(row=1, column=1, sticky="w", padx=10, pady=10)
            
            ttk.Label(edit_win, text="是否主菜单:").grid(row=2, column=0, sticky="w", padx=10, pady=10)
            is_main_menu_var = tk.BooleanVar(value=bool(row["is_main_menu"]))
            is_main_menu_check = ttk.Checkbutton(edit_win, variable=is_main_menu_var)
            is_main_menu_check.grid(row=2, column=1, sticky="w", padx=10, pady=10)
            
            ttk.Label(edit_win, text="Slug:").grid(row=3, column=0, sticky="w", padx=10, pady=10)
            slug_var = tk.StringVar(value=row["slug"] or "")
            slug_entry = ttk.Entry(edit_win, textvariable=slug_var, width=20)
            slug_entry.grid(row=3, column=1, sticky="w", padx=10, pady=10)
            
            name_entry.focus_set()
            name_entry.select_range(0, tk.END)
            
            def save():
                name = name_var.get().strip()
                if not name:
                    messagebox.showinfo("提示", "请输入分类名称")
                    return
                
                try:
                    parent_id = int(parent_id_var.get().strip() or "0")
                except ValueError:
                    messagebox.showinfo("提示", "父级ID必须是数字")
                    return
                
                is_main_menu = 1 if is_main_menu_var.get() else 0
                slug = slug_var.get().strip()
                
                try:
                    # 直接更新数据库
                    self.store._db.execute(
                        "UPDATE main_category_options SET name = ?, parent_id = ?, is_main_menu = ?, slug = ? WHERE id = ?",
                        (name, parent_id, is_main_menu, slug, category_id)
                    )
                    self.store._db.commit()
                    load_main_categories()
                    edit_win.destroy()
                except Exception as e:
                    messagebox.showinfo("错误", f"编辑失败: {e}")
            
            ttk.Button(edit_win, text="保存", command=save).grid(row=4, column=0, padx=10, pady=10)
            ttk.Button(edit_win, text="取消", command=edit_win.destroy).grid(row=4, column=1, padx=10, pady=10)
        
        # 删除主分类
        def delete_main_category():
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("提示", "请选择要删除的主分类")
                return
            
            category_id = selected[0]
            # 从数据库获取分类信息
            cur = self.store._db.execute("SELECT name FROM main_category_options WHERE id = ?", (category_id,))
            row = cur.fetchone()
            if not row:
                messagebox.showinfo("提示", "分类不存在")
                return
            
            name = row["name"]
            if messagebox.askyesno("确认删除", f"确定删除分类 '{name}' 吗？"):
                try:
                    # 直接从数据库删除
                    self.store._db.execute("DELETE FROM main_category_options WHERE id = ?", (category_id,))
                    self.store._db.commit()
                    load_main_categories()
                except Exception as e:
                    messagebox.showinfo("错误", f"删除失败: {e}")
        
        # 设为主分类
        def set_as_main_category():
            selected = tree.selection()
            if not selected:
                messagebox.showinfo("提示", "请选择要设为主分类的分类")
                return
            
            category_id = selected[0]
            # 从数据库获取分类信息
            cur = self.store._db.execute("SELECT name FROM main_category_options WHERE id = ?", (category_id,))
            row = cur.fetchone()
            if not row:
                messagebox.showinfo("提示", "分类不存在")
                return
            
            try:
                # 先将所有分类的is_main_menu设置为0
                self.store._db.execute("UPDATE main_category_options SET is_main_menu = 0")
                # 然后将选中的分类设置为1
                self.store._db.execute("UPDATE main_category_options SET is_main_menu = 1 WHERE id = ?", (category_id,))
                self.store._db.commit()
                load_main_categories()
                messagebox.showinfo("成功", f"已将分类 '{row['name']}' 设为主分类")
            except Exception as e:
                messagebox.showinfo("错误", f"设置失败: {e}")
        
        ttk.Button(button_frame, text="添加", command=add_main_category).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="编辑", command=edit_main_category).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="删除", command=delete_main_category).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="设为主分类", command=set_as_main_category).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="刷新", command=load_main_categories).pack(side=tk.RIGHT, padx=5)

    def _upload_media(self, row_id, kind):
        if not row_id:
            messagebox.showinfo("提示", "请先选中要上传的行。")
            return
        domain = (self.tree.set(row_id, "domain") or "").strip()
        if not domain:
            messagebox.showinfo("提示", "请先填写域名。")
            return

        filetypes = [("Image", "*.png;*.jpg;*.jpeg")]
        path = filedialog.askopenfilename(title=f"选择{kind}", filetypes=filetypes)
        if not path:
            return

        ext = os.path.splitext(path)[1].lower()
        if kind in {"logo", "icon"} and ext != ".png":
            messagebox.showinfo("提示", f"{kind} 请使用 PNG 图片。")
            return
        if kind == "banner" and ext not in {".jpg", ".jpeg"}:
            messagebox.showinfo("提示", "banner 请使用 JPG/JPEG 图片。")
            return

        filename = {"logo": "logo.png", "banner": "banner.jpg", "icon": "icon.png"}[kind]
        dest_dir = os.path.join(self._media_root, domain)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, filename)
        shutil.copy2(path, dest_path)

        rel_path = os.path.join(self._media_root, domain, filename)
        self.store.update_cell(row_id, kind, rel_path)
        self.tree.set(row_id, kind, rel_path)

    def _report_selected(self):
        ids = []
        for item in self.tree.get_children():
            if self.tree.set(item, "_sel") == "✓":
                ids.append(item)
        if not ids:
            messagebox.showinfo("提示", "请先勾选要上报的行。")
            return

        username = self.store.get_setting("report_username", "").strip()
        password = self.store.get_setting("report_password", "").strip()
        if not username or not password:
            messagebox.showinfo("提示", "请先在配置页设置上报账号和密码。")
            self._open_config()
            return

        from domain_reporter_client import DomainReporter
        from constants import CATEGORY_ID_MAP, DOMAIN_NUMBER_COL, REPORT_STATUS_COL, REPORT_TIME_COL, DOMAIN_RESOLVED_TIME_COL, SCHEDULE_ENABLED_COL, COLUMNS
        from datetime import datetime

        # 获取当前最大域名编号
        cur = self.store._db.execute("SELECT MAX(domain_number) FROM sites WHERE report_status = '已报'")
        max_number = cur.fetchone()[0] or 0
        # 确保max_number是整数类型
        try:
            max_number = int(max_number)
        except (ValueError, TypeError):
            max_number = 0
        current_number = max_number + 1

        reporter = DomainReporter("http://123.60.135.93:8099", username, password)

        success = 0
        failed = []
        for row_id in ids:
            # 直接从数据库获取行数据，确保即使搜索条件改变也能获取到
            row = self.store.get_row(row_id)
            if not row:
                failed.append((row_id, "数据不存在"))
                continue
            missing = []
            for col, _title in COLUMNS:
                if not (row[col] or "").strip():
                    missing.append(col)
            if missing:
                failed.append((row_id, "字段缺失"))
                continue

            domain = (row["domain"] or "").strip()
            server = (row["server"] or "").strip()
            template = (row["template"] or "").strip()
            category_name = (row["category"] or "").strip()
            category_id = CATEGORY_ID_MAP.get(category_name)
            if not category_id:
                failed.append((row_id, "分类无效"))
                continue

            payload = {
                "name": domain,
                "serverip": server,
                "template": template,
                "category": category_id,
                "categoryTag": None,
                "language": None,
            }
            try:
                reporter.submit_domain(payload)
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                update_values = {
                    DOMAIN_NUMBER_COL: str(current_number),
                    REPORT_STATUS_COL: "\u5df2\u62a5",
                    REPORT_TIME_COL: now,
                    SCHEDULE_ENABLED_COL: "0",
                }
                current_number += 1
                try:
                    info = reporter.fetch_domain_info(domain)
                    status_val = info.get("status")
                    update_values["report_id"] = str(info.get("id") or "")
                    update_values["domain_status"] = str(status_val) if status_val is not None else ""
                    # 记录已解析时间
                    if status_val in {3, "3"}:
                        update_values[DOMAIN_RESOLVED_TIME_COL] = now
                except Exception as e:
                    print(f"获取域名信息失败: {e}")
                    update_values["report_id"] = ""
                    update_values["domain_status"] = ""
                self.store.update_fields(row_id, update_values)
                success += 1
            except Exception as exc:
                failed.append((row_id, str(exc)))

        # 重新加载数据以更新显示
        self._load_rows()
        self._load_reported()

        # 显示结果
        if failed:
            error_msg = "\n".join([f"ID: {row_id}, 错误: {error}" for row_id, error in failed])
            messagebox.showinfo("提示", f"成功上报 {success} 个域名，失败 {len(failed)} 个。\n\n失败详情:\n{error_msg}")
        else:
            messagebox.showinfo("提示", f"成功上报 {success} 个域名。")

    def _on_click(self, event):
        if self._editing:
            self._save_edit(None)
        
        # 确定是哪个树组件被点击
        widget = event.widget
        if widget == self.tree:
            tree = self.tree
            is_local_tree = True
        elif widget == getattr(self, "report_tree", None):
            tree = self.report_tree
            is_local_tree = False
        else:
            return
        
        region = tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = tree.identify_row(event.y)
        col_id = tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_index = int(col_id[1:]) - 1
        col_name = tree["columns"][col_index]
        
        if col_name == "_sel":
            current = tree.set(row_id, "_sel")
            tree.set(row_id, "_sel", "" if current == "✓" else "✓")
            return
        
        if col_name == "_plan":
            if is_local_tree:
                status = tree.set(row_id, "_status")
                if status == "已报":
                    messagebox.showinfo("提示", "已报的站点无法设置计划。")
                    return
            else:
                # 已报域名标签页的计划检查
                row = self.store.get_row(row_id)
                if not row:
                    return
                build_status = (row[BUILD_STATUS_COL] or "").strip()
                if build_status == "已建站":
                    messagebox.showinfo("提示", "已建站的站点无法设置计划。")
                    return
                domain_status = row["domain_status"] if "domain_status" in row.keys() else None
                if domain_status not in {"3", 3}:
                    messagebox.showinfo("提示", "只有域名状态为\"已解析\"的站点才能设置计划建站。")
                    return
            
            current = tree.set(row_id, "_plan")
            enabled = "" if current == "✓" else "✓"
            tree.set(row_id, "_plan", enabled)
            self.store.update_cell(row_id, SCHEDULE_ENABLED_COL, "1" if enabled == "✓" else "0")
            schedule_time = self.store.get_row(row_id)[SCHEDULE_TIME_COL] or ""
            display_time = self._format_schedule_display(enabled, schedule_time)
            
            if is_local_tree:
                tree.set(row_id, SCHEDULE_TIME_COL, display_time)
            else:
                tree.set(row_id, "schedule_time", display_time)
            
            if enabled == "✓":
                self._open_schedule_dialog(row_id, require_time=True)
            return
        
        if is_local_tree and col_name not in {"_status", "_media", "_row", "_sel", "_plan"}:
            self._cur_col_index = col_index
        
        tree.focus(row_id)
        tree.selection_set(row_id)



    def _begin_edit(self, event):
        if self._editing:
            return
        
        # 确定是哪个树组件被点击
        widget = event.widget
        if widget == self.tree:
            tree = self.tree
        elif widget == getattr(self, "built_tree", None):
            tree = self.built_tree
        elif widget == getattr(self, "report_tree", None):
            tree = self.report_tree
        else:
            return
        
        row_id = tree.focus()
        if not row_id:
            return
        
        if hasattr(event, "x") and hasattr(event, "y"):
            col_id = tree.identify_column(event.x)
            col_index = int(col_id[1:]) - 1
        else:
            col_index = self._cur_col_index
            col_id = f"#{col_index + 1}"
        
        col_name = tree["columns"][col_index]
        
        # 处理本地站点管理的状态列
        if tree == self.tree:
            if col_name in {"_plan", "_media", "_row", "_sel"}:
                return
            self._cur_col_index = col_index

            x, y, w, h = tree.bbox(row_id, col_id)
            value = tree.set(row_id, tree["columns"][col_index])

            if col_name in {"logo", "banner", "icon"}:
                self._upload_media(row_id, col_name)
                return
            if col_name == SCHEDULE_TIME_COL or col_name == "schedule_time":
                self._open_schedule_dialog(row_id, require_time=False)
                return
            
            # 为状态列添加下拉菜单
            if col_name == "_status":
                status_options = ["未报", "已报"]
                entry = ttk.Combobox(tree, values=status_options, state="readonly")
                entry.set(value)
            else:
                dropdown_values = self._get_dropdown_values(col_name)
                if dropdown_values is not None:
                    entry = ttk.Combobox(tree, values=dropdown_values, state="readonly")
                    entry.set(value)
                else:
                    entry = ttk.Entry(tree)
                    entry.insert(0, value)
        # 处理已报域名的状态列
        elif tree == self.report_tree:
            # 只允许编辑域名状态列
            if col_name != "domain_status":
                return
            
            x, y, w, h = tree.bbox(row_id, col_id)
            value = tree.set(row_id, tree["columns"][col_index])
            
            # 为域名状态列添加下拉菜单
            status_options = ["未买", "新增", "已购买", "已解析", "已建站"]
            entry = ttk.Combobox(tree, values=status_options, state="readonly")
            entry.set(value)
        # 处理已建站的状态列
        elif tree == self.built_tree:
            # 只允许编辑状态列
            if col_name not in {"main_data_status", "extra_data_status"}:
                return
            
            x, y, w, h = tree.bbox(row_id, col_id)
            value = tree.set(row_id, tree["columns"][col_index])
            
            # 为状态列添加下拉菜单
            status_options = ["未上传", "已上传"]
            entry = ttk.Combobox(tree, values=status_options, state="readonly")
            entry.set(value)
        
        entry.select_range(0, tk.END)
        entry.focus_set()
        entry.place(x=x, y=y, width=w, height=h)

        self._editing = (row_id, col_id, entry, tree)
        entry.bind("<Return>", self._save_edit)
        entry.bind("<Escape>", self._cancel_edit)
        entry.bind("<FocusOut>", self._save_edit)
        if isinstance(entry, ttk.Combobox):
            entry.bind("<<ComboboxSelected>>", self._save_edit)

    def _save_edit(self, _event):
        if not self._editing:
            return
        row_id, col_id, entry, tree = self._editing
        col_index = int(col_id[1:]) - 1
        col_name = tree["columns"][col_index]
        new_value = entry.get().strip()
        
        # 验证输入内容，确保数字后面不能带小数点
        if new_value:
            # 检查是否是纯数字且包含小数点
            if new_value.replace('.', '', 1).isdigit() and '.' in new_value:
                messagebox.showinfo("提示", "数字后面不能带小数点，请输入整数。")
                entry.destroy()
                self._editing = None
                return
        
        # 处理主分类，只保存最后一级分类名称
        if col_name == "main_category" and new_value:
            # 如果是多级分类，只取最后一级
            if "|||" in new_value:
                new_value = new_value.split("|||")[-1].strip()
        
        entry.destroy()
        self._editing = None
        tree.set(row_id, col_name, new_value)
        
        # 处理本地站点管理的状态列
        if tree == self.tree:
            if col_name == "_status":
                # 更新数据库中的报告状态
                self.store.update_cell(row_id, REPORT_STATUS_COL, new_value)
                # 重新加载数据以更新显示
                self._load_rows()
                self._load_reported()
                self._load_built()
            else:
                # 其他列正常更新
                self.store.update_cell(row_id, col_name, new_value)
                # 重新加载所有标签页，确保信息同步更新
                self._load_rows()
                self._load_reported()
                self._load_built()
        # 处理已报域名的状态列
        elif tree == self.report_tree:
            if col_name == "domain_status":
                # 更新数据库中的域名状态
                # 从状态文本反向查找状态值
                status_value = None
                for val, label in DOMAIN_STATUS_LABELS.items():
                    if label == new_value:
                        status_value = val
                        break
                if status_value is not None:
                    self.store.update_cell(row_id, "domain_status", str(status_value))
                
                # 如果状态变为"已建站"，同步更新build_status
                if new_value == "已建站":
                    current_row = self.store.get_row(row_id)
                    if current_row:
                        self.store.update_fields(row_id, self._build_uploaded_sync_updates(current_row))
                
                # 重新加载数据以更新显示
                self._load_reported()
                self._load_rows()
                self._load_built()
        # 处理已建站的状态列
        elif tree == self.built_tree:
            if col_name == "main_data_status":
                # 更新数据库中的主数据状态
                self.store.update_cell(row_id, "main_data_status", new_value)
                # 如果状态为已上传，更新时间
                if new_value == "已上传":
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.store.update_cell(row_id, "main_data_time", now)
                # 重新加载数据以更新显示
                self._load_built()
            elif col_name == "extra_data_status":
                # 更新数据库中的补充数据状态
                self.store.update_cell(row_id, "extra_data_status", new_value)
                # 如果状态为已上传，更新时间
                if new_value == "已上传":
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self.store.update_cell(row_id, "extra_data_time", now)
                # 重新加载数据以更新显示
                self._load_built()

    def _cancel_edit(self, _event):
        if not self._editing:
            return
        if len(self._editing) == 3:
            _row_id, _col_id, entry = self._editing
        else:
            _row_id, _col_id, entry, _tree = self._editing
        entry.destroy()
        self._editing = None

    def _move_focus(self, row_delta=0, col_delta=0):
        items = self.tree.get_children()
        if not items:
            return
        row_id = self.tree.focus() or items[0]
        if row_id not in items:
            row_id = items[0]
        row_index = items.index(row_id)
        if row_delta:
            row_index = max(0, min(len(items) - 1, row_index + row_delta))
            row_id = items[row_index]
            self.tree.focus(row_id)
            self.tree.selection_set(row_id)
        if col_delta:
            columns = self.tree["columns"]
            new_index = max(5, min(len(columns) - 1, self._cur_col_index + col_delta))
            self._cur_col_index = new_index
        self.tree.see(row_id)

    def _get_dropdown_values(self, col_name):
        if col_name == "category":
            return CATEGORY_OPTIONS
        if col_name == "template":
            return self.store.get_option_values("template_options")
        if col_name == "server":
            return self.store.get_option_values("server_options")
        if col_name == "main_category":
            # 获取所有主分类，包括多级分类
            cur = self.store._db.execute("SELECT id, name, parent_id FROM main_category_options ORDER BY id ASC")
            categories = []
            # 构建分类树
            category_map = {}
            for row in cur.fetchall():
                category_map[row["id"]] = {
                    "name": row["name"],
                    "parent_id": row["parent_id"]
                }
            # 生成完整的分类路径
            def get_category_path(category_id):
                path = []
                current = category_id
                while current in category_map:
                    path.insert(0, category_map[current]["name"])
                    current = category_map[current]["parent_id"]
                    if current == 0:
                        break
                return "|||".join(path)
            # 生成所有分类的路径
            for category_id in category_map:
                categories.append(get_category_path(category_id))
            return categories
        return None

    def _format_schedule_display(self, enabled_flag, schedule_time):
        if enabled_flag != "✓":
            return "无计划"
        return schedule_time or ""

    def _open_schedule_dialog(self, row_id, require_time=False):
        if hasattr(self, "_schedule_win") and self._schedule_win.winfo_exists():
            self._schedule_win.destroy()

        win = tk.Toplevel(self)
        win.title("计划时间")
        win.geometry("360x240")
        win.transient(self)
        self._schedule_win = win

        row = self.store.get_row(row_id)
        current = row[SCHEDULE_TIME_COL] or ""
        enabled_before = (row[SCHEDULE_ENABLED_COL] or "").strip()
        date_var = tk.StringVar()
        time_var = tk.StringVar()
        parsed = self._parse_schedule_time(current) if current else None
        if parsed:
            date_var.set(parsed.strftime("%Y-%m-%d"))
            time_var.set(parsed.strftime("%H:%M"))

        ttk.Label(win, text="日期(YYYY-MM-DD)").grid(row=0, column=0, sticky="w", padx=8, pady=(10, 6))
        ttk.Label(win, text="时间(HH:MM)").grid(row=1, column=0, sticky="w", padx=8, pady=6)

        date_entry = ttk.Entry(win, textvariable=date_var, width=16)
        time_entry = ttk.Entry(win, textvariable=time_var, width=8)
        date_entry.grid(row=0, column=1, sticky="w", padx=6, pady=(10, 6))
        time_entry.grid(row=1, column=1, sticky="w", padx=6, pady=6)

        def set_preset(dt):
            date_var.set(dt.strftime("%Y-%m-%d"))
            time_var.set(dt.strftime("%H:%M"))

        presets = ttk.Frame(win)
        presets.grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 6))
        def set_today_14():
            candidate = datetime.now().replace(hour=14, minute=0, second=0)
            if candidate < datetime.now():
                messagebox.showinfo("提示", "搁这跟我玩软件测试呢？")
                return
            set_preset(candidate)

        ttk.Button(presets, text="今天14:00", command=set_today_14).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(presets, text="明天09:00", command=lambda: set_preset((datetime.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0))).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(presets, text="明天14:00", command=lambda: set_preset((datetime.now() + timedelta(days=1)).replace(hour=14, minute=0, second=0))).pack(side=tk.LEFT)

        def save():
            date_text = date_var.get().strip()
            time_text = time_var.get().strip()
            if not date_text or not time_text:
                messagebox.showinfo("提示", "请填写日期和时间。")
                return
            if len(time_text) == 5:
                dt_text = f"{date_text} {time_text}"
            elif len(time_text) == 8:
                dt_text = f"{date_text} {time_text}"
            else:
                messagebox.showinfo("提示", "时间格式应为 HH:MM。")
                return
            run_at = self._parse_schedule_time(dt_text)
            if not run_at:
                messagebox.showinfo("提示", "时间格式不正确。")
                return
            value = run_at.strftime("%Y-%m-%d %H:%M")
            self.store.update_cell(row_id, SCHEDULE_TIME_COL, value)
            self.store.update_cell(row_id, SCHEDULE_ENABLED_COL, "1")
            display_value = self._format_schedule_display("✓", value)
            
            # 更新本地站点管理标签页（如果存在）
            if hasattr(self, "tree") and row_id in self.tree.get_children():
                self.tree.set(row_id, "_plan", "✓")
                self.tree.set(row_id, SCHEDULE_TIME_COL, display_value)
            
            # 更新已报域名标签页（如果存在）
            if hasattr(self, "report_tree") and row_id in self.report_tree.get_children():
                self.report_tree.set(row_id, "_plan", "✓")
                self.report_tree.set(row_id, "schedule_time", display_value)
            
            win.destroy()

        def clear_plan():
            self.store.update_cell(row_id, SCHEDULE_TIME_COL, "")
            self.store.update_cell(row_id, SCHEDULE_ENABLED_COL, "0")
            
            # 更新本地站点管理标签页（如果存在）
            if hasattr(self, "tree") and row_id in self.tree.get_children():
                self.tree.set(row_id, "_plan", "")
                self.tree.set(row_id, SCHEDULE_TIME_COL, "无计划")
            
            # 更新已报域名标签页（如果存在）
            if hasattr(self, "report_tree") and row_id in self.report_tree.get_children():
                self.report_tree.set(row_id, "_plan", "")
                self.report_tree.set(row_id, "schedule_time", "无计划")
            
            win.destroy()

        def cancel():
            if require_time and not current:
                self.store.update_cell(row_id, SCHEDULE_ENABLED_COL, "0")
                self.tree.set(row_id, "_plan", "")
                self.tree.set(row_id, SCHEDULE_TIME_COL, "无计划")
            elif not require_time and enabled_before in {"1", "true", "True"}:
                self.tree.set(row_id, "_plan", "✓")
            win.destroy()

        buttons = ttk.Frame(win)
        buttons.grid(row=3, column=0, columnspan=2, sticky="e", padx=8, pady=(10, 8))
        ttk.Button(buttons, text="清除计划", command=clear_plan).pack(side=tk.LEFT)
        ttk.Button(buttons, text="保存", command=save).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(buttons, text="取消", command=cancel).pack(side=tk.LEFT, padx=(6, 0))
        win.protocol("WM_DELETE_WINDOW", cancel)

    def _build_report_tab(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(toolbar, text="域名搜索:").pack(side=tk.LEFT)
        self.report_search_var = tk.StringVar()
        report_search_entry = ttk.Entry(toolbar, textvariable=self.report_search_var, width=26)
        report_search_entry.pack(side=tk.LEFT, padx=(6, 10))
        report_search_entry.bind("<Return>", lambda _e: self._load_reported())
        ttk.Button(toolbar, text="刷新", command=self._refresh_today).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="搜索", command=self._load_reported).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="删除所选", command=self._delete_reported).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="建站(所选)", command=self._build_selected_reported).pack(side=tk.LEFT, padx=(6, 0))

        body = ttk.Frame(parent)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        columns = ["_plan", "schedule_time", "domain", "template", "server", "domain_status", "report_time", "resolved_time", "report_id"]
        self.report_tree = ttk.Treeview(
            body,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Custom.Treeview",
        )
        self.report_tree.heading("_plan", text="计划")
        self.report_tree.column("_plan", width=60, anchor=tk.CENTER, stretch=False)
        self.report_tree.heading("schedule_time", text="计划时间")
        self.report_tree.column("schedule_time", width=160, anchor=tk.CENTER, stretch=False)
        self.report_tree.heading("domain", text="域名")
        self.report_tree.heading("template", text="模板")
        self.report_tree.heading("server", text="服务器")
        self.report_tree.heading("domain_status", text="域名状态")
        self.report_tree.heading("report_time", text="上报时间")
        self.report_tree.heading("resolved_time", text="已解析时间")
        self.report_tree.heading("report_id", text="ID")

        self.report_tree.column("domain", width=220, anchor=tk.CENTER)
        self.report_tree.column("template", width=120, anchor=tk.CENTER)
        self.report_tree.column("server", width=160, anchor=tk.CENTER)
        self.report_tree.column("domain_status", width=120, anchor=tk.CENTER)
        self.report_tree.column("report_time", width=160, anchor=tk.CENTER)
        self.report_tree.column("resolved_time", width=160, anchor=tk.CENTER)
        self.report_tree.column("report_id", width=120, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(body, orient="vertical", command=self.report_tree.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.report_tree.xview)
        self.report_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.report_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.report_tree.bind("<Double-1>", self._begin_edit)
        self.report_tree.bind("<Button-1>", self._on_click)

    def _load_reported(self):
        if not hasattr(self, "report_tree"):
            return
        
        # 清空现有数据
        for item in self.report_tree.get_children():
            self.report_tree.delete(item)
        
        # 直接从数据库查询已报域名，按域名编号从小到大排序
        # 使用store的数据库连接直接查询，确保按域名编号升序排序
        cur = self.store._db.execute("SELECT * FROM sites WHERE report_status = '已报' ORDER BY domain_number ASC")
        reported_rows = cur.fetchall()
        reported_rows = self._filter_rows_by_domain(
            reported_rows,
            getattr(self, "report_search_var", None).get() if hasattr(self, "report_search_var") else "",
        )
        
        # 批量插入数据
        self.report_tree.yview_moveto(0)  # 滚动到顶部
        self.report_tree.update_idletasks()  # 更新界面
        
        for row in reported_rows:
            domain_status = row["domain_status"]
            domain_status_text = DOMAIN_STATUS_LABELS.get(domain_status, "其他状态")
            report_id = row["report_id"] or ""
            report_time = row[REPORT_TIME_COL] or ""
            resolved_time = row[DOMAIN_RESOLVED_TIME_COL] or ""
            plan_enabled = (row[SCHEDULE_ENABLED_COL] or "").strip()
            plan_flag = "✓" if plan_enabled in {"1", "true", "True"} else ""
            schedule_time = row[SCHEDULE_TIME_COL] or ""
            display_time = self._format_schedule_display(plan_flag, schedule_time)
            
            self.report_tree.insert(
                "",
                tk.END,
                iid=str(row["id"]),
                values=[
                    plan_flag,
                    display_time,
                    row["domain"] or "",
                    row["template"] or "",
                    row["server"] or "",
                    domain_status_text,
                    report_time,
                    resolved_time,
                    report_id if report_id else "获取",
                ],
            )
        
        self.report_tree.update_idletasks()  # 更新界面

    def _build_built_tab(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, padx=6, pady=6)
        ttk.Label(toolbar, text="域名搜索:").pack(side=tk.LEFT)
        self.built_search_var = tk.StringVar()
        built_search_entry = ttk.Entry(toolbar, textvariable=self.built_search_var, width=26)
        built_search_entry.pack(side=tk.LEFT, padx=(6, 10))
        built_search_entry.bind("<Return>", lambda _e: self._load_built())
        ttk.Button(toolbar, text="刷新", command=self._load_built).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="搜索", command=self._load_built).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="上传主数据", command=self._upload_main_data).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="上传补充数据", command=self._upload_extra_data).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="设置主分类", command=self._upload_main_category).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="设置菜单", command=self._configure_menu).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="配置站点", command=self._configure_site).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="主数据设为已上传", command=lambda: self._set_built_status("main", "已上传")).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(toolbar, text="补充设为已上传", command=lambda: self._set_built_status("extra", "已上传")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="主数据重置未上传", command=lambda: self._set_built_status("main", "未上传")).pack(side=tk.LEFT, padx=(18, 0))
        ttk.Button(toolbar, text="补充重置未上传", command=lambda: self._set_built_status("extra", "未上传")).pack(side=tk.LEFT, padx=(6, 0))

        body = ttk.Frame(parent)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=(0, 6))

        columns = ["domain", "template", "server", "main_data_source_id", "extra_data_source_id", "main_data_status", "extra_data_status"]
        self.built_tree = ttk.Treeview(
            body,
            columns=columns,
            show="headings",
            selectmode="extended",
            style="Custom.Treeview",
        )
        self.built_tree.heading("domain", text="域名")
        self.built_tree.heading("template", text="模板")
        self.built_tree.heading("server", text="服务器")
        self.built_tree.heading("main_data_source_id", text="主数据源ID")
        self.built_tree.heading("extra_data_source_id", text="补充数据源ID")
        self.built_tree.heading("main_data_status", text="主数据状态")
        self.built_tree.heading("extra_data_status", text="补充数据状态")

        self.built_tree.column("domain", width=220, anchor=tk.CENTER)
        self.built_tree.column("template", width=120, anchor=tk.CENTER)
        self.built_tree.column("server", width=160, anchor=tk.CENTER)
        self.built_tree.column("main_data_source_id", width=140, anchor=tk.CENTER)
        self.built_tree.column("extra_data_source_id", width=140, anchor=tk.CENTER)
        self.built_tree.column("main_data_status", width=120, anchor=tk.CENTER)
        self.built_tree.column("extra_data_status", width=120, anchor=tk.CENTER)

        vsb = ttk.Scrollbar(body, orient="vertical", command=self.built_tree.yview)
        hsb = ttk.Scrollbar(body, orient="horizontal", command=self.built_tree.xview)
        self.built_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.built_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)
        self.built_tree.bind("<Double-1>", self._begin_edit)

    def _set_built_status(self, target, status):
        selected = self.built_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要修改状态的站点。")
            return

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if target == "main":
            status_col = "main_data_status"
            time_col = "main_data_time"
            label = "主数据状态"
        else:
            status_col = "extra_data_status"
            time_col = "extra_data_time"
            label = "补充数据状态"

        for row_id in selected:
            values = {status_col: status}
            values[time_col] = now if status == "已上传" else ""
            self.store.update_fields(row_id, values)

        self._load_built()
        if hasattr(self, "_pending_panels") and "未完成站点_panel" in self._pending_panels:
            self._refresh_pending_panel("未完成站点_panel")
        messagebox.showinfo("提示", f"已更新 {len(selected)} 个站点的{label}。")

    def _build_uploaded_sync_updates(self, row, now=None):
        now = now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        updates = {
            BUILD_STATUS_COL: "已建站",
            "main_data_status": "已上传",
            "extra_data_status": "已上传",
        }
        if not (row[BUILD_TIME_COL] or "").strip():
            updates[BUILD_TIME_COL] = now
        if not (row["main_data_time"] or "").strip():
            updates["main_data_time"] = now
        if not (row["extra_data_time"] or "").strip():
            updates["extra_data_time"] = now
        return updates

    def _sync_uploaded_status_for_built_sites(self):
        cur = self.store._db.execute(
            f"""
            SELECT id, report_status, domain_status, {BUILD_STATUS_COL}, {BUILD_TIME_COL},
                   main_data_status, main_data_time, extra_data_status, extra_data_time
            FROM sites
            WHERE {REPORT_STATUS_COL} = '已报' AND domain_status = '4'
            """
        )
        changed = False
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for row in cur.fetchall():
            build_status = (row[BUILD_STATUS_COL] or "").strip()
            main_data_status = (row["main_data_status"] or "").strip()
            extra_data_status = (row["extra_data_status"] or "").strip()
            build_time = (row[BUILD_TIME_COL] or "").strip()
            main_data_time = (row["main_data_time"] or "").strip()
            extra_data_time = (row["extra_data_time"] or "").strip()
            if (
                build_status == "已建站"
                and main_data_status == "已上传"
                and extra_data_status == "已上传"
                and build_time
                and main_data_time
                and extra_data_time
            ):
                continue
            self.store.update_fields(row["id"], self._build_uploaded_sync_updates(row, now))
            changed = True
        return changed

    def _build_pending_panel(self, parent, title, get_sites_func, process_func):
        """
        创建可折叠的待处理站点小界面
        :param parent: 父容器
        :param title: 面板标题
        :param get_sites_func: 获取待处理站点的函数
        :param process_func: 处理选中站点的函数
        """
        panel = tk.Frame(parent, relief=tk.GROOVE, borderwidth=2)
        panel.pack(fill=tk.X, padx=6, pady=(0, 6))
        
        # 标题栏，包含展开/折叠按钮
        header = ttk.Frame(panel)
        header.pack(fill=tk.X, padx=5, pady=5)
        
        self._pending_panels = getattr(self, "_pending_panels", {})
        panel_key = f"{title}_panel"
        self._pending_panels[panel_key] = {
            "panel": panel,
            "is_expanded": False,
            "content": None,
            "get_sites": get_sites_func,
            "process": process_func
        }
        
        def toggle_panel():
            info = self._pending_panels[panel_key]
            info["is_expanded"] = not info["is_expanded"]
            
            # 更新按钮文本
            toggle_btn.config(text="▼" if info["is_expanded"] else "▶")
            
            if info["is_expanded"]:
                if info["content"] is None:
                    # 创建内容区域
                    content = ttk.Frame(panel)
                    
                    # 工具栏
                    toolbar = ttk.Frame(content)
                    toolbar.pack(fill=tk.X, pady=(0, 5))
                    
                    ttk.Button(toolbar, text="刷新", command=lambda: self._refresh_pending_panel(panel_key)).pack(side=tk.LEFT)
                    ttk.Button(toolbar, text="全选", command=lambda: self._select_all_pending(panel_key)).pack(side=tk.LEFT, padx=(5, 0))
                    ttk.Button(toolbar, text="取消全选", command=lambda: self._deselect_all_pending(panel_key)).pack(side=tk.LEFT, padx=(5, 0))
                    ttk.Button(toolbar, text="处理所选", command=lambda: self._process_pending(panel_key)).pack(side=tk.RIGHT)
                    
                    # 站点列表
                    tree_frame = ttk.Frame(content)
                    tree_frame.pack(fill=tk.BOTH, expand=True)
                    
                    columns = ["_sel", "domain", "status"]
                    tree = ttk.Treeview(
                        tree_frame,
                        columns=columns,
                        show="headings",
                        selectmode="extended",
                        style="Custom.Treeview",
                    )
                    tree.heading("_sel", text="✓")
                    tree.heading("domain", text="域名")
                    tree.heading("status", text="状态")
                    
                    tree.column("_sel", width=40, anchor=tk.CENTER)
                    tree.column("domain", width=200, anchor=tk.CENTER)
                    tree.column("status", width=100, anchor=tk.CENTER)
                    
                    vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=tree.yview)
                    tree.configure(yscrollcommand=vsb.set)
                    
                    tree.grid(row=0, column=0, sticky="nsew")
                    vsb.grid(row=0, column=1, sticky="ns")
                    tree_frame.grid_rowconfigure(0, weight=1)
                    tree_frame.grid_columnconfigure(0, weight=1)
                    
                    # 绑定点击事件
                    tree.bind("<Button-1>", lambda e, t=tree: self._on_pending_click(e, t))
                    
                    info["content"] = {
                        "content_frame": content,
                        "tree": tree
                    }
                
                # 显示内容并加载数据
                info["content"]["content_frame"].pack(fill=tk.X, padx=5, pady=(0, 5))
                # 强制更新UI
                panel.update_idletasks()
                self._refresh_pending_panel(panel_key)
            else:
                if info["content"]:
                    info["content"]["content_frame"].pack_forget()
                    # 强制更新UI
                    panel.update_idletasks()
        
        toggle_btn = ttk.Button(header, text="▶", width=2, command=toggle_panel)
        toggle_btn.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Label(header, text=f"{title}").pack(side=tk.LEFT)

    def _on_pending_click(self, event, tree):
        """
        处理待处理站点列表的点击事件
        """
        region = tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = tree.identify_row(event.y)
        col_id = tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_index = int(col_id[1:]) - 1
        col_name = tree["columns"][col_index]
        if col_name == "_sel":
            current = tree.set(row_id, "_sel")
            tree.set(row_id, "_sel", "" if current == "✓" else "✓")

    def _refresh_pending_panel(self, panel_key):
        """
        刷新待处理站点面板
        """
        info = self._pending_panels.get(panel_key)
        if not info or not info["is_expanded"] or not info["content"]:
            return
        
        tree = info["content"]["tree"]
        # 清空现有数据
        for item in tree.get_children():
            tree.delete(item)
        
        try:
            # 加载新数据
            sites = info["get_sites"]()
            if not sites:
                # 如果没有站点，显示提示信息
                tree.insert(
                    "",
                    tk.END,
                    values=["", "无待处理站点", ""]
                )
            else:
                for site in sites:
                    tree.insert(
                        "",
                        tk.END,
                        iid=str(site["id"]),
                        values=["", site["domain"], site.get("status", "")]
                    )
        except Exception as e:
            # 显示错误信息
            tree.insert(
                "",
                tk.END,
                values=["", f"加载失败: {str(e)}", ""]
            )

    def _select_all_pending(self, panel_key):
        """
        全选待处理站点
        """
        info = self._pending_panels.get(panel_key)
        if not info or not info["is_expanded"] or not info["content"]:
            return
        
        tree = info["content"]["tree"]
        for item in tree.get_children():
            tree.set(item, "_sel", "✓")

    def _deselect_all_pending(self, panel_key):
        """
        取消全选待处理站点
        """
        info = self._pending_panels.get(panel_key)
        if not info or not info["is_expanded"] or not info["content"]:
            return
        
        tree = info["content"]["tree"]
        for item in tree.get_children():
            tree.set(item, "_sel", "")

    def _process_pending(self, panel_key):
        """
        处理选中的待处理站点
        """
        info = self._pending_panels.get(panel_key)
        if not info or not info["is_expanded"] or not info["content"]:
            return
        
        tree = info["content"]["tree"]
        selected = []
        for item in tree.get_children():
            if tree.set(item, "_sel") == "✓":
                selected.append(item)
        
        if not selected:
            messagebox.showinfo("提示", "请先选择要处理的站点。")
            return
        
        info["process"](selected)
        # 刷新面板
        self._refresh_pending_panel(panel_key)

    def _get_pending_local_sites(self):
        """
        获取未报的站点
        """
        # 直接从数据库查询未报的站点
        cur = self.store._db.execute(f"SELECT id, domain, {REPORT_STATUS_COL} FROM sites WHERE {REPORT_STATUS_COL} != '已报' ORDER BY id DESC")
        pending_sites = []
        for row in cur.fetchall():
            pending_sites.append({
                "id": row["id"],
                "domain": row["domain"],
                "status": row[REPORT_STATUS_COL] or "未报"
            })
        return pending_sites

    def _get_pending_reported_sites(self):
        """
        获取已解析的站点
        """
        # 直接从数据库查询已解析的站点
        cur = self.store._db.execute(f"SELECT id, domain FROM sites WHERE {REPORT_STATUS_COL} = '已报' AND domain_status IN ('3', 3) ORDER BY id DESC")
        pending_sites = []
        for row in cur.fetchall():
            pending_sites.append({
                "id": row["id"],
                "domain": row["domain"],
                "status": "已解析"
            })
        return pending_sites

    def _get_pending_built_sites(self):
        """
        获取有未完成状态的站点
        """
        # 直接从数据库查询已建站但数据未上传的站点
        cur = self.store._db.execute(f"SELECT id, domain, main_data_status, extra_data_status FROM sites WHERE build_status = '已建站' ORDER BY id DESC")
        pending_sites = []
        for row in cur.fetchall():
            main_data_status = row["main_data_status"] or "未上传"
            extra_data_status = row["extra_data_status"] or "未上传"
            if main_data_status != "已上传" or extra_data_status != "已上传":
                status = "未完成"
                if main_data_status != "已上传" and extra_data_status != "已上传":
                    status = "主数据和补充数据未上传"
                elif main_data_status != "已上传":
                    status = "主数据未上传"
                else:
                    status = "补充数据未上传"
                pending_sites.append({
                    "id": row["id"],
                    "domain": row["domain"],
                    "status": status
                })
        return pending_sites

    def _process_pending_local(self, selected_ids):
        """
        处理未报站点（域名上报）
        """
        # 调用现有的域名上报功能
        # 临时修改tree的_sel列，然后调用_report_selected
        for row_id in selected_ids:
            self.tree.set(row_id, "_sel", "✓")
        
        self._report_selected()
        
        # 恢复_sel列状态
        for row_id in selected_ids:
            self.tree.set(row_id, "_sel", "")

    def _process_pending_reported(self, selected_ids):
        """
        处理已解析站点（建站）
        """
        # 调用建站功能
        # 临时修改report_tree的选择，然后调用_build_selected_reported
        for row_id in selected_ids:
            self.report_tree.selection_add(row_id)
        
        self._build_selected_reported()
        
        # 清除选择
        self.report_tree.selection_remove(selected_ids)

    def _process_pending_built(self, selected_ids):
        """
        处理未完成站点（上传数据）
        """
        # 检查每个站点的状态，分别处理
        for row_id in selected_ids:
            row = self.store.get_row(row_id)
            if not row:
                continue
            
            main_data_status = row["main_data_status"] if "main_data_status" in row.keys() else "未上传"
            extra_data_status = row["extra_data_status"] if "extra_data_status" in row.keys() else "未上传"
            
            # 临时修改built_tree的选择，然后调用相应的上传功能
            self.built_tree.selection_set(row_id)
            
            if main_data_status != "已上传":
                self._upload_main_data()
            if extra_data_status != "已上传":
                self._upload_extra_data()
        
        # 清除选择
        self.built_tree.selection_remove(selected_ids)

    def _load_built(self):
        if not hasattr(self, "built_tree"):
            return

        self._sync_uploaded_status_for_built_sites()
        
        # 清空现有数据
        for item in self.built_tree.get_children():
            self.built_tree.delete(item)
        
        # 直接从数据库查询已建站的站点
        cur = self.store._db.execute("SELECT * FROM sites WHERE build_status = '已建站' ORDER BY domain_number ASC")
        built_rows = cur.fetchall()
        built_rows = self._filter_rows_by_domain(
            built_rows,
            getattr(self, "built_search_var", None).get() if hasattr(self, "built_search_var") else "",
        )
        
        # 批量插入数据
        self.built_tree.yview_moveto(0)  # 滚动到顶部
        self.built_tree.update_idletasks()  # 更新界面
        
        for row in built_rows:
            main_data_status = row["main_data_status"] or "未上传"
            extra_data_status = row["extra_data_status"] or "未上传"
            self.built_tree.insert(
                "",
                tk.END,
                iid=str(row["id"]),
                values=[
                    row["domain"] or "",
                    row["template"] or "",
                    row["server"] or "",
                    row["main_data_source_id"] or "",
                    row["extra_data_source_id"] or "",
                    main_data_status,
                    extra_data_status,
                ],
            )
        
        self.built_tree.update_idletasks()  # 更新界面





    def _delete_reported(self):
        if not hasattr(self, "report_tree"):
            return
        selected = self.report_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要删除的记录。")
            return
        if not messagebox.askyesno("确认删除", f"确定删除 {len(selected)} 条记录吗？"):
            return
        username = self.store.get_setting("report_username", "").strip()
        password = self.store.get_setting("report_password", "").strip()
        if not username or not password:
            messagebox.showinfo("提示", "请先在配置页设置上报账号和密码。")
            self._open_config()
            return
        reporter = DomainReporter("http://123.60.135.93:8099", username, password)

        success = 0
        failed = []
        for row_id in selected:
            row = self.store.get_row(row_id)
            if not row:
                continue
            report_id = (row["report_id"] or "").strip()
            if not report_id:
                failed.append((row_id, "缺少ID"))
                continue
            try:
                reporter.delete_domain(report_id)
                self.store.update_cell(row_id, REPORT_STATUS_COL, "未报")
                self.store.update_cell(row_id, "report_id", "")
                self.store.update_cell(row_id, "domain_status", "")
                success += 1
            except Exception as exc:
                failed.append((row_id, str(exc)))

        self._load_reported()
        self._load_rows()
        if failed:
            messagebox.showinfo(
                "删除结果",
                f"成功 {success} 条，失败 {len(failed)} 条。请检查ID或网络。",
            )
        elif success:
            messagebox.showinfo("删除结果", f"成功删除 {success} 条。")

    def _build_selected_reported(self):
        selected = self.report_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要建站的站点。")
            return
        
        # 获取选中的站点信息
        sites_to_build = []
        site_ids_to_update = []
        rows = self._query_rows("")
        row_map = {str(r["id"]): r for r in rows}
        
        for row_id in selected:
            row = row_map.get(str(row_id))
            if row:
                domain = (row["domain"] or "").strip()
                if domain:
                    # 检查D盘logo文件夹中是否有对应的域名文件夹
                    domain_folder = os.path.join(self._media_root, domain)
                    if os.path.exists(domain_folder):
                        sites_to_build.append(row)
                        site_ids_to_update.append(row_id)
                    else:
                        messagebox.showwarning("警告", f"在 E:\\logo 文件夹中未找到 {domain} 的资源文件夹，将跳过。")
        
        if not sites_to_build:
            messagebox.showinfo("提示", "没有找到有效的站点。请确保E:\\logo文件夹中有对应的域名资源。")
            return
        
        site_names = [row["domain"] for row in sites_to_build]
        
        # 自动建站流程
        try:
            import subprocess
            import sys
            import pandas as pd
            import shutil
            
            # 创建临时Excel文件
            build_work_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_work")
            os.makedirs(build_work_dir, exist_ok=True)
            temp_excel_path = os.path.join(build_work_dir, "temp_build_sites.xlsx")
            
            # 准备数据
            df_data = []
            for row in sites_to_build:
                df_data.append({
                    "域名": row["domain"],
                    "服务器": row["server"],
                    "模板": row["template"],
                    "SEO Title（最大58字符）": row["title"],
                    "Meta Description": row["description"],
                    "地址": row["address"],
                    "大类": row["category"]
                })
            
            df = pd.DataFrame(df_data)
            df.to_excel(temp_excel_path, index=False)
            
            # 复制建站.py并修改配置
            project_dir = os.path.dirname(os.path.abspath(__file__))
            original_build_path = os.path.join(project_dir, "mango", "建站.py")
            modified_build_path = os.path.join(build_work_dir, "建站_auto.py")
            
            # 读取原文件内容
            with open(original_build_path, 'r', encoding='utf-8') as f:
                build_content = f.read()
            
            # 修改配置路径
            modified_content = build_content.replace(
                r'Website_path = r"C:\Users\Administrator\Desktop\建站域名管理.xlsx"',
                f'Website_path = r"{temp_excel_path}"'
            ).replace(
                r'IMAGE_BASE_DIR = r"C:\Users\Administrator\Desktop\logo\未建站\开始建站"',
                f'IMAGE_BASE_DIR = r"{self._media_root}"'
            )
            
            # 移除用户输入，让它自动运行
            modified_content = modified_content.replace(
                'ans = input("是否尝试重新建立这些失败域名？(y/n): ").strip().lower()',
                'ans = "n"'
            ).replace(
                'retry_failed = ans == "y"',
                'retry_failed = False'
            )
            
            # 写入修改后的文件
            with open(modified_build_path, 'w', encoding='utf-8') as f:
                f.write(modified_content)
            
            messagebox.showinfo("提示", f"开始为 {len(sites_to_build)} 个站点建站，请查看控制台输出...")
            
            # 运行建站脚本
            build_result = subprocess.run(
                [sys.executable, modified_build_path], 
                capture_output=False, 
                text=True, 
                cwd=project_dir
            )
            
            # 清理临时文件
            try:
                if os.path.exists(temp_excel_path):
                    os.remove(temp_excel_path)
                if os.path.exists(modified_build_path):
                    os.remove(modified_build_path)
            except:
                pass
            
            if build_result.returncode == 0:
                # 建站成功，更新状态到已建站
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for row_id in site_ids_to_update:
                    self.store.update_cell(row_id, BUILD_STATUS_COL, "已建站")
                    self.store.update_cell(row_id, BUILD_TIME_COL, now)
                
                self._load_reported()
                self._load_built()
                self._load_rows()
                
                messagebox.showinfo("成功", "建站流程完成！请在上传数据后手动配置站点。")
            else:
                messagebox.showinfo("错误", "建站过程中出现错误。请查看控制台输出。")
                
        except Exception as exc:
            messagebox.showinfo("错误", f"建站失败：{exc}")
            import traceback
            traceback.print_exc()

    def _configure_after_build(self, site_names, site_ids_to_update=None):
        """建站完成后执行图片设置，并通过网页按钮完成 Yoast / WP Rocket 配置"""
        try:
            import subprocess
            import sys
            import json
            import os

            wp_password = self.store.get_setting("wp_password", "").strip()
            if not wp_password:
                messagebox.showinfo("提示", "请先在配置页设置WordPress密码。")
                self._open_config()
                return

            current_dir = os.path.dirname(os.path.abspath(__file__))
            worker_path = os.path.join(current_dir, "wp_site_config_worker.py")
            cmd = [
                sys.executable,
                worker_path,
                json.dumps(site_names),
                wp_password,
            ]
            
            messagebox.showinfo("提示", "开始配置站点，请查看控制台输出...")
            
            result = subprocess.run(cmd, capture_output=False, text=True, cwd=current_dir)
            
            if result.returncode == 0:
                messagebox.showinfo("完成", f"已完成 {len(site_names)} 个站点的建站和配置！")
            else:
                messagebox.showinfo("警告", "站点配置过程中出现错误。请查看控制台输出。")
                
        except Exception as exc:
            messagebox.showinfo("错误", f"配置失败：{exc}")





























    def _refresh_today(self):
        username = self.store.get_setting("report_username", "").strip()
        password = self.store.get_setting("report_password", "").strip()
        if not username or not password:
            messagebox.showinfo("提示", "请先在配置页设置上报账号和密码。")
            self._open_config()
            return
        reporter = DomainReporter("http://123.60.135.93:8099", username, password)
        rows = self.store.query_rows("")
        pending_rows = []
        for row in rows:
            if (row[REPORT_STATUS_COL] or "") != "已报":
                continue
            domain = (row["domain"] or "").strip()
            if not domain:
                continue
            pending_rows.append((row["id"], domain))

        updated = 0
        for row_id, domain in pending_rows:
            try:
                info = reporter.fetch_domain_info(domain)
            except Exception:
                continue
            report_id = info.get("id")
            status_val = info.get("status")
            update_values = {
                "report_id": str(report_id) if report_id is not None else "",
            }

            # 检查状态是否变为已解析
            current_row = self.store.get_row(row_id)
            current_status = current_row["domain_status"] if current_row and "domain_status" in current_row else ""
            new_status = str(status_val) if status_val is not None else ""
            update_values["domain_status"] = new_status

            # 如果状态从非已解析变为已解析，记录已解析时间
            if current_status not in {"3", 3} and new_status in {"3", 3}:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                update_values[DOMAIN_RESOLVED_TIME_COL] = now

            # 如果状态变为已建站，更新build_status和build_time
            if new_status in {"4", 4}:
                update_values.update(self._build_uploaded_sync_updates(current_row))

            self.store.update_fields(row_id, update_values)
            updated += 1
        self._load_reported()
        self._load_rows()
        self._load_built()
        messagebox.showinfo("刷新完成", f"已更新 {updated} 条记录。")

    def _schedule_tick(self):
        try:
            self._run_scheduled_reports()
            self._run_scheduled_builds()
            # 每5分钟刷新一次已报域名状态
            if self._auto_refresh_enabled:
                if hasattr(self, "_last_refresh_time"):
                    if (datetime.now() - self._last_refresh_time).total_seconds() > 300:
                        self._auto_refresh_reported()
                else:
                    self._last_refresh_time = datetime.now()
        finally:
            self.after(30000, self._schedule_tick)

    def _auto_refresh_reported(self):
        """自动刷新已报域名状态"""
        username = self.store.get_setting("report_username", "").strip()
        password = self.store.get_setting("report_password", "").strip()
        if not username or not password:
            return
        
        try:
            reporter = DomainReporter("http://123.60.135.93:8099", username, password)
            rows = self.store.query_rows("")
            pending_rows = []
            for row in rows:
                if (row[REPORT_STATUS_COL] or "") != "已报":
                    continue
                if (row[BUILD_STATUS_COL] or "") != "已建站":
                    continue
                domain = (row["domain"] or "").strip()
                if not domain:
                    continue
                pending_rows.append((row["id"], domain))

            updated = 0
            for row_id, domain in pending_rows:
                try:
                    info = reporter.fetch_domain_info(domain)
                except Exception:
                    continue
                report_id = info.get("id")
                status_val = info.get("status")
                update_values = {
                    "report_id": str(report_id) if report_id is not None else "",
                }

                # 检查状态是否变为已解析
                current_row = self.store.get_row(row_id)
                current_status = current_row["domain_status"] if current_row and "domain_status" in current_row else ""
                new_status = str(status_val) if status_val is not None else ""
                update_values["domain_status"] = new_status

                # 如果状态从非已解析变为已解析，记录已解析时间
                if current_status not in {"3", 3} and new_status in {"3", 3}:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    update_values[DOMAIN_RESOLVED_TIME_COL] = now

                # 如果状态变为已建站，更新build_status和build_time
                if new_status in {"4", 4}:
                    update_values.update(self._build_uploaded_sync_updates(current_row))

                self.store.update_fields(row_id, update_values)
                updated += 1
            
            if updated > 0:
                self._load_reported()
                self._load_rows()
                self._load_built()
                # 可以添加一个状态提示，但不要弹出消息框影响用户
                print(f"自动刷新：已更新 {updated} 条已报域名状态")
                
        except Exception as e:
            # 静默处理错误，避免影响定时任务
            print(f"自动刷新失败: {e}")
        finally:
            self._last_refresh_time = datetime.now()

    def _run_scheduled_reports(self):
        rows = self.store.query_rows("")
        due_rows = []
        now = datetime.now()
        for row in rows:
            if (row[REPORT_STATUS_COL] or "") == "已报":
                continue
            enabled = (row[SCHEDULE_ENABLED_COL] or "").strip()
            if enabled not in {"1", "true", "True"}:
                continue
            schedule_time = (row[SCHEDULE_TIME_COL] or "").strip()
            if not schedule_time:
                continue
            run_at = self._parse_schedule_time(schedule_time)
            if not run_at or run_at > now:
                continue
            due_rows.append(row)

        if not due_rows:
            return

        username = self.store.get_setting("report_username", "").strip()
        password = self.store.get_setting("report_password", "").strip()
        if not username or not password:
            if not self._schedule_warned:
                messagebox.showinfo("提示", "请先在配置页设置上报账号和密码。")
                self._schedule_warned = True
            return

        reporter = DomainReporter("http://123.60.135.93:8099", username, password)
        for row in due_rows:
            row_id = str(row["id"])
            domain = (row["domain"] or "").strip()
            server = (row["server"] or "").strip()
            template = (row["template"] or "").strip()
            category_name = (row["category"] or "").strip()
            category_id = CATEGORY_ID_MAP.get(category_name)
            if not domain or not server or not template or not category_id:
                continue
            payload = {
                "name": domain,
                "serverip": server,
                "template": template,
                "category": category_id,
                "categoryTag": None,
                "language": None,
            }
            try:
                reporter.submit_domain(payload)
                update_values = {
                    REPORT_STATUS_COL: "\u5df2\u62a5",
                    SCHEDULE_ENABLED_COL: "0",
                }
                try:
                    info = reporter.fetch_domain_info(domain)
                    status_val = info.get("status")
                    update_values["report_id"] = str(info.get("id") or "")
                    update_values["domain_status"] = str(status_val) if status_val is not None else ""
                except Exception:
                    update_values["report_id"] = ""
                    update_values["domain_status"] = ""
                self.store.update_fields(row_id, update_values)
            except Exception:
                continue
        self._load_reported()
        self._load_rows()

    def _run_scheduled_builds(self):
        """处理计划建站任务"""
        rows = self.store.query_rows("")
        due_rows = []
        now = datetime.now()
        for row in rows:
            # 只处理已报的、域名状态为已解析的、还没有建站的
            if (row[REPORT_STATUS_COL] or "") != "已报":
                continue
            if (row[BUILD_STATUS_COL] or "") == "已建站":
                continue
            domain_status = row["domain_status"] if "domain_status" in row.keys() else None
            if domain_status not in {"3", 3}:
                continue
            # 检查计划是否启用
            enabled = (row[SCHEDULE_ENABLED_COL] or "").strip()
            if enabled not in {"1", "true", "True"}:
                continue
            schedule_time = (row[SCHEDULE_TIME_COL] or "").strip()
            if not schedule_time:
                continue
            run_at = self._parse_schedule_time(schedule_time)
            if not run_at or run_at > now:
                continue
            due_rows.append(row)

        if not due_rows:
            return

        # 准备建站
        sites_to_build = []
        site_ids_to_update = []
        for row in due_rows:
            domain = (row["domain"] or "").strip()
            if not domain:
                continue
            # 检查D盘logo文件夹中是否有对应的域名文件夹
            domain_folder = os.path.join(self._media_root, domain)
            if os.path.exists(domain_folder):
                sites_to_build.append(row)
                site_ids_to_update.append(str(row["id"]))
            else:
                print(f"计划建站：在 E:\\logo 文件夹中未找到 {domain} 的资源文件夹，跳过。")

        if not sites_to_build:
            return

        # 自动建站流程
        try:
            import subprocess
            import sys
            import pandas as pd
            import shutil

            # 创建临时Excel文件
            build_work_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build_work")
            os.makedirs(build_work_dir, exist_ok=True)
            temp_excel_path = os.path.join(build_work_dir, "temp_scheduled_build.xlsx")

            # 准备数据
            df_data = []
            for row in sites_to_build:
                df_data.append({
                    "域名": row["domain"],
                    "服务器": row["server"],
                    "模板": row["template"],
                    "SEO Title（最大58字符）": row["title"],
                    "Meta Description": row["description"],
                    "地址": row["address"],
                    "大类": row["category"]
                })

            df = pd.DataFrame(df_data)
            df.to_excel(temp_excel_path, index=False)

            # 复制建站.py并修改配置
            project_dir = os.path.dirname(os.path.abspath(__file__))
            original_build_path = os.path.join(project_dir, "mango", "建站.py")
            modified_build_path = os.path.join(build_work_dir, "建站_scheduled.py")

            # 读取原文件内容
            with open(original_build_path, 'r', encoding='utf-8') as f:
                build_content = f.read()

            # 修改配置路径
            modified_content = build_content.replace(
                r'Website_path = r"C:\Users\Administrator\Desktop\建站域名管理.xlsx"',
                f'Website_path = r"{temp_excel_path}"'
            ).replace(
                r'IMAGE_BASE_DIR = r"C:\Users\Administrator\Desktop\logo\未建站\开始建站"',
                f'IMAGE_BASE_DIR = r"{self._media_root}"'
            )

            # 移除用户输入，让它自动运行
            modified_content = modified_content.replace(
                'ans = input("是否尝试重新建立这些失败域名？(y/n): ").strip().lower()',
                'ans = "n"'
            ).replace(
                'retry_failed = ans == "y"',
                'retry_failed = False'
            )

            # 写入修改后的文件
            with open(modified_build_path, 'w', encoding='utf-8') as f:
                f.write(modified_content)

            print(f"计划建站：开始为 {len(sites_to_build)} 个站点建站...")

            # 运行建站脚本
            build_result = subprocess.run(
                [sys.executable, modified_build_path],
                capture_output=False,
                text=True,
                cwd=project_dir
            )

            # 清理临时文件
            try:
                if os.path.exists(temp_excel_path):
                    os.remove(temp_excel_path)
                if os.path.exists(modified_build_path):
                    os.remove(modified_build_path)
            except:
                pass

            if build_result.returncode == 0:
                # 建站成功，更新状态到已建站
                build_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for row_id in site_ids_to_update:
                    self.store.update_fields(row_id, {
                        BUILD_STATUS_COL: "\u5df2\u5efa\u7ad9",
                        BUILD_TIME_COL: build_now,
                        SCHEDULE_ENABLED_COL: "0",
                    })

                self._load_reported()
                self._load_built()
                self._load_rows()

                print(f"计划建站：建站流程完成！请在上传数据后手动配置站点。")
            else:
                print(f"计划建站：建站过程中出现错误，返回码: {build_result.returncode}")

        except Exception as exc:
            print(f"计划建站失败：{exc}")
            import traceback
            traceback.print_exc()

    def _parse_schedule_time(self, text):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None



    def _on_report_double_click(self, event):
        region = self.report_tree.identify("region", event.x, event.y)
        if region != "cell":
            return
        row_id = self.report_tree.identify_row(event.y)
        col_id = self.report_tree.identify_column(event.x)
        if not row_id or not col_id:
            return
        col_index = int(col_id[1:]) - 1
        col_name = self.report_tree["columns"][col_index]
        if col_name != "report_id":
            return
        if self.report_tree.set(row_id, "report_id") != "获取":
            return
        self._refresh_report_info(row_id)

    def _refresh_report_info(self, row_id):
        row = self.store.get_row(row_id)
        if not row:
            return
        domain = (row["domain"] or "").strip()
        if not domain:
            messagebox.showinfo("提示", "域名为空，无法获取。")
            return
        username = self.store.get_setting("report_username", "").strip()
        password = self.store.get_setting("report_password", "").strip()
        if not username or not password:
            messagebox.showinfo("提示", "请先在配置页设置上报账号和密码。")
            self._open_config()
            return
        reporter = DomainReporter("http://123.60.135.93:8099", username, password)
        try:
            info = reporter.fetch_domain_info(domain)
        except Exception as exc:
            messagebox.showinfo("提示", f"获取失败：{exc}")
            return
        report_id = info.get("id")
        domain_status = info.get("status")
        update_values = {
            "report_id": str(report_id) if report_id is not None else "",
            "domain_status": str(domain_status) if domain_status is not None else "",
        }
        if domain_status in {"4", 4}:
            current_row = self.store.get_row(row_id)
            if current_row:
                update_values.update(self._build_uploaded_sync_updates(current_row))
        self.store.update_fields(row_id, update_values)
        self._load_reported()
        self._load_rows()
        self._load_built()

    def _upload_main_data(self):
        selected = self.built_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要上传主数据的站点。")
            return
        
        wp_password = self.store.get_setting("wp_password", "").strip()
        if not wp_password:
            messagebox.showinfo("提示", "请先在配置页设置WordPress密码。")
            self._open_config()
            return
        
        import subprocess
        import sys
        import json
        import os
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        worker_path = os.path.join(current_dir, "upload_worker.py")
        
        site_ids = list(selected)
        
        cmd = [
            sys.executable,
            worker_path,
            "main",
            json.dumps(site_ids),
            wp_password
        ]
        
        messagebox.showinfo("提示", f"开始为 {len(site_ids)} 个站点上传主数据，正在打开新窗口...")
        
        # 在新控制台窗口运行
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=current_dir
        )
        
        # 稍后刷新一下界面
        self.after(2000, self._load_built)

    def _upload_extra_data(self):
        selected = self.built_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要上传补充数据的站点。")
            return
        
        import subprocess
        import sys
        import json
        import os
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        worker_path = os.path.join(current_dir, "upload_worker.py")
        
        site_ids = list(selected)
        
        cmd = [
            sys.executable,
            worker_path,
            "extra",
            json.dumps(site_ids),
            ""  # 补充数据不需要wp密码，传空字符串
        ]
        
        messagebox.showinfo("提示", f"开始为 {len(site_ids)} 个站点上传补充数据，正在打开新窗口...")
        
        # 在新控制台窗口运行
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=current_dir
        )
        
        # 稍后刷新一下界面
        self.after(2000, self._load_built)

    def _upload_main_category(self):
        """
        上传主分类数据到网站
        """
        selected = self.built_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要设置主分类的站点。")
            return
        
        import subprocess
        import sys
        import json
        import os
        
        current_dir = os.path.dirname(os.path.abspath(__file__))
        worker_path = os.path.join(current_dir, "upload_worker.py")
        
        site_ids = list(selected)
        
        cmd = [
            sys.executable,
            worker_path,
            "main_category",
            json.dumps(site_ids),
            ""  # 主分类上传不需要wp密码，传空字符串
        ]
        
        messagebox.showinfo("提示", f"开始为 {len(site_ids)} 个站点设置主分类，正在打开新窗口...")
        
        # 在新控制台窗口运行
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            cwd=current_dir
        )
        
        # 稍后刷新一下界面
        self.after(2000, self._load_built)

    def _configure_site(self):
        selected = self.built_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要配置的站点。")
            return
        
        # 获取选中的站点信息
        sites = []
        for row_id in selected:
            row = self.store.get_row(row_id)
            if row:
                domain = (row["domain"] or "").strip()
                if domain:
                    sites.append(domain)
        
        if not sites:
            messagebox.showinfo("提示", "未找到有效的站点。")
            return

        try:
            import subprocess
            import sys
            import json
            import os

            wp_password = self.store.get_setting("wp_password", "").strip()
            if not wp_password:
                messagebox.showinfo("提示", "请先在配置页设置WordPress密码。")
                self._open_config()
                return

            current_dir = os.path.dirname(os.path.abspath(__file__))
            worker_path = os.path.join(current_dir, "wp_site_config_worker.py")
            cmd = [
                sys.executable,
                worker_path,
                json.dumps(sites),
                wp_password,
            ]
            
            # 在新窗口中运行命令，不阻塞主进程
            subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                cwd=current_dir
            )
            
            messagebox.showinfo(
                "提示",
                f"已开始配置 {len(sites)} 个站点。\n前面的图片设置保持原流程，后面的 Yoast / WP Rocket 会通过网页里的两个按钮执行，请查看新打开的窗口了解进度。"
            )
            
        except Exception as exc:
            messagebox.showinfo("错误", f"配置失败：{exc}")


    def _configure_menu(self):
        selected = self.built_tree.selection()
        if not selected:
            messagebox.showinfo("提示", "请先选择要设置菜单的站点。")
            return

        wp_password = self.store.get_setting("wp_password", "").strip()
        if not wp_password:
            messagebox.showinfo("提示", "请先在配置页设置WordPress密码。")
            self._open_config()
            return

        sites = []
        for row_id in selected:
            row = self.store.get_row(row_id)
            if row:
                domain = (row["domain"] or "").strip()
                if domain:
                    sites.append((row_id, domain))

        if not sites:
            messagebox.showinfo("提示", "未找到有效的站点。")
            return

        import threading
        from wp_menu_config import WpMenuConfigurator
        from datetime import datetime

        results = []

        def worker():
            for row_id, domain in sites:
                try:
                    cfg = WpMenuConfigurator(wp_password)
                    cfg.configure(domain)
                    self.store.update_fields(row_id, {
                        "auto_category_status": "已配置",
                        "auto_category_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    results.append((domain, True, ""))
                except Exception as e:
                    results.append((domain, False, str(e)))
            self.after(0, self._on_menu_done, results)

        self._menu_thread = threading.Thread(target=worker, daemon=True)
        self._menu_thread.start()
        messagebox.showinfo("提示", f"已开始为 {len(sites)} 个站点配置菜单，请稍候...")

    def _on_menu_done(self, results):
        self._load_built()
        ok = sum(1 for _, s, _ in results if s)
        fail = [(d, e) for d, s, e in results if not s]
        msg = f"已完成 {len(results)} 个站点菜单配置。\n成功: {ok}"
        if fail:
            msg += f"\n失败: {len(fail)}"
            for d, e in fail[:5]:
                msg += f"\n  {d}: {e}"
        messagebox.showinfo("菜单配置完成", msg)


    def _on_tree_configure(self, _event):
        total_width = self.tree.winfo_width()
        if total_width <= 0:
            return
        fixed_width = 60 + 80 + 80 + 60 + 40 + 160 + 2  # _plan + _status + _media + _row + _sel + plan_time
        data_cols = [name for name, _ in COLUMNS]
        remaining = max(200, total_width - fixed_width)
        per_col = max(120, remaining // max(1, len(data_cols)))
        self.tree.column("_plan", width=60, stretch=False)
        self.tree.column("_status", width=80, stretch=False)
        self.tree.column("_media", width=80, stretch=False)
        self.tree.column("_row", width=60, stretch=False)
        self.tree.column("_sel", width=40, stretch=False)
        self.tree.column(SCHEDULE_TIME_COL, width=160, stretch=False)
        for name in data_cols:
            self.tree.column(name, width=per_col, stretch=True)

    def _global_click(self, event):
        if not self._editing:
            return
        if len(self._editing) == 4:
            _row_id, _col_id, entry, _tree = self._editing
        else:
            _row_id, _col_id, entry = self._editing
        if event.widget == self.tree or str(event.widget).startswith(str(self.tree)):
            return
        if event.widget == entry or str(event.widget).startswith(str(entry)):
            return
        self._save_edit(None)

    def _toggle_auto_refresh(self):
        """切换自动刷新状态"""
        self._auto_refresh_enabled = self.auto_refresh_var.get()
        if self._auto_refresh_enabled:
            # 重新设置最后刷新时间，确保自动刷新能正常启动
            self._last_refresh_time = datetime.now()
            print("自动刷新已开启")
        else:
            print("自动刷新已关闭")



    def on_close(self):
        self.store.close()
        self.destroy()


if __name__ == "__main__":
    app = StationApp()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
