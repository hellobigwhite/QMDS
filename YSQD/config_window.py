import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox


class ConfigWindow(tk.Toplevel):
    def __init__(self, parent, store):
        super().__init__(parent)
        self.title("配置")
        self.geometry("520x360")
        self.transient(parent)
        self._store = store

        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self._build_option_tab(notebook, "模板", "template_options")
        self._build_option_tab(notebook, "服务器", "server_options")
        self._build_account_tab(notebook)
        self._build_workflow_tab(notebook)

    def _build_option_tab(self, notebook, title, table):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)

        listbox = tk.Listbox(frame, height=12)
        listbox.grid(row=0, column=0, rowspan=4, sticky="nsew", padx=(0, 10))

        entry_var = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=entry_var, width=30)
        entry.grid(row=0, column=1, sticky="ew", pady=(0, 6))

        ttk.Button(
            frame,
            text="新增",
            command=lambda: self._option_add(table, entry_var, listbox),
        ).grid(row=1, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(
            frame,
            text="修改",
            command=lambda: self._option_update(table, entry_var, listbox),
        ).grid(row=2, column=1, sticky="ew", pady=(0, 6))
        ttk.Button(
            frame,
            text="删除",
            command=lambda: self._option_delete(table, listbox),
        ).grid(row=3, column=1, sticky="ew")

        frame.grid_rowconfigure(0, weight=1)
        frame.grid_columnconfigure(0, weight=1)

        listbox.bind(
            "<<ListboxSelect>>",
            lambda _e: self._option_on_select(listbox, entry_var),
        )
        self._option_load(table, listbox)

    def _option_load(self, table, listbox):
        listbox.delete(0, tk.END)
        for name in self._store.get_option_values(table):
            listbox.insert(tk.END, name)

    def _option_on_select(self, listbox, entry_var):
        sel = listbox.curselection()
        if not sel:
            return
        entry_var.set(listbox.get(sel[0]))

    def _option_add(self, table, entry_var, listbox):
        name = entry_var.get().strip()
        if not name:
            return
        try:
            self._store.add_option(table, name)
        except sqlite3.IntegrityError:
            messagebox.showinfo("提示", "该名称已存在。")
            return
        entry_var.set("")
        self._option_load(table, listbox)

    def _option_update(self, table, entry_var, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        old = listbox.get(sel[0])
        new = entry_var.get().strip()
        if not new:
            return
        try:
            self._store.update_option(table, old, new)
        except sqlite3.IntegrityError:
            messagebox.showinfo("提示", "该名称已存在。")
            return
        self._option_load(table, listbox)

    def _option_delete(self, table, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        name = listbox.get(sel[0])
        if not messagebox.askyesno("确认删除", f"确定删除“{name}”吗？"):
            return
        self._store.delete_option(table, name)
        self._option_load(table, listbox)

    def _build_account_tab(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="账号")

        ttk.Label(frame, text="上报账号").grid(row=0, column=0, sticky="w", padx=6, pady=(10, 6))
        ttk.Label(frame, text="上报密码").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        ttk.Label(frame, text="ERP账号").grid(row=2, column=0, sticky="w", padx=6, pady=(12, 6))
        ttk.Label(frame, text="ERP密码").grid(row=3, column=0, sticky="w", padx=6, pady=6)
        ttk.Label(frame, text="ERP管理员ID").grid(row=4, column=0, sticky="w", padx=6, pady=6)
        ttk.Label(frame, text="WP密码").grid(row=5, column=0, sticky="w", padx=6, pady=6)
        ttk.Label(frame, text="主数据并发数").grid(row=6, column=0, sticky="w", padx=6, pady=6)

        username_var = tk.StringVar(value=self._store.get_setting("report_username", ""))
        password_var = tk.StringVar(value=self._store.get_setting("report_password", ""))
        erp_user_var = tk.StringVar(value=self._store.get_setting("erp_username", ""))
        erp_pass_var = tk.StringVar(value=self._store.get_setting("erp_password", ""))
        erp_admin_var = tk.StringVar(value=self._store.get_setting("erp_admin_id", ""))
        wp_pass_var = tk.StringVar(value=self._store.get_setting("wp_password", ""))
        main_data_conc_var = tk.StringVar(value=self._store.get_setting("main_data_concurrency", "2"))

        username_entry = ttk.Entry(frame, textvariable=username_var, width=30)
        password_entry = ttk.Entry(frame, textvariable=password_var, width=30, show="*")
        erp_user_entry = ttk.Entry(frame, textvariable=erp_user_var, width=30)
        erp_pass_entry = ttk.Entry(frame, textvariable=erp_pass_var, width=30, show="*")
        erp_admin_entry = ttk.Entry(frame, textvariable=erp_admin_var, width=30)
        wp_pass_entry = ttk.Entry(frame, textvariable=wp_pass_var, width=30, show="*")
        main_data_conc_entry = ttk.Entry(frame, textvariable=main_data_conc_var, width=10)
        username_entry.grid(row=0, column=1, sticky="w", padx=6, pady=(10, 6))
        password_entry.grid(row=1, column=1, sticky="w", padx=6, pady=6)
        erp_user_entry.grid(row=2, column=1, sticky="w", padx=6, pady=(12, 6))
        erp_pass_entry.grid(row=3, column=1, sticky="w", padx=6, pady=6)
        erp_admin_entry.grid(row=4, column=1, sticky="w", padx=6, pady=6)
        wp_pass_entry.grid(row=5, column=1, sticky="w", padx=6, pady=6)
        main_data_conc_entry.grid(row=6, column=1, sticky="w", padx=6, pady=6)

        def save():
            self._store.set_setting("report_username", username_var.get().strip())
            self._store.set_setting("report_password", password_var.get().strip())
            self._store.set_setting("erp_username", erp_user_var.get().strip())
            self._store.set_setting("erp_password", erp_pass_var.get().strip())
            self._store.set_setting("erp_admin_id", erp_admin_var.get().strip())
            self._store.set_setting("wp_password", wp_pass_var.get().strip())
            self._store.set_setting("main_data_concurrency", main_data_conc_var.get().strip())
            messagebox.showinfo("提示", "账号配置已保存。")

        ttk.Button(frame, text="保存", command=save).grid(row=7, column=1, sticky="w", padx=6, pady=(10, 6))

    def _build_workflow_tab(self, notebook):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text="自动化流程")

        ttk.Label(frame, text="最大重试次数").grid(row=0, column=0, sticky="w", padx=6, pady=(10, 6))
        ttk.Label(frame, text="流程说明：").grid(row=1, column=0, sticky="nw", padx=6, pady=(10, 0))

        workflow_max_retry_var = tk.StringVar(value=self._store.get_setting("workflow_max_retry", "3"))

        workflow_max_retry_entry = ttk.Entry(frame, textvariable=workflow_max_retry_var, width=10)
        workflow_max_retry_entry.grid(row=0, column=1, sticky="w", padx=6, pady=(10, 6))

        steps_text = (
            "1. 建站\n"
            "2. 健康检查\n"
            "3. 插件配置\n"
            "4. 媒体配置\n"
            "5. 上传主数据\n"
            "6. 主分类设置\n"
        )
        steps_label = ttk.Label(frame, text=steps_text, justify="left")
        steps_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=20, pady=0)

        def save_workflow():
            self._store.set_setting("workflow_max_retry", workflow_max_retry_var.get().strip())
            messagebox.showinfo("提示", "自动化流程配置已保存。")

        ttk.Button(frame, text="保存", command=save_workflow).grid(row=3, column=1, sticky="w", padx=6, pady=(10, 6))
