import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import os
import sys
from datetime import datetime
from models import Role, Status, WorkOrder, User
from datastore import (
    DataStore,
    WorkOrderError,
    PermissionError,
    StatusTransitionError,
    ConcurrentOperationError,
    ExportError,
)


PRIORITY_OPTIONS = ["高", "中", "低"]
CATEGORY_OPTIONS = ["空调维修", "电梯维修", "电路维修", "照明维修", "水管维修", "门禁维修", "办公设备", "网络维护", "其他"]


class MaintenanceApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("维修派工管理系统")
        self.root.geometry("1200x750")
        self.root.minsize(1000, 600)

        try:
            self.store = DataStore()
        except Exception as e:
            messagebox.showerror("初始化失败", f"数据存储初始化失败: {str(e)}")
            sys.exit(1)

        self.current_user: User = None
        self._build_ui()
        self._show_login()

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei", 16, "bold"))
        style.configure("Info.TLabel", font=("Microsoft YaHei", 11))
        style.configure("Bold.TLabel", font=("Microsoft YaHei", 11, "bold"))

        self.top_frame = ttk.Frame(self.root, padding=10)
        self.top_frame.pack(side=tk.TOP, fill=tk.X)

        self.content_frame = ttk.Frame(self.root, padding=(10, 0, 10, 10))
        self.content_frame.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.status_bar = ttk.Frame(self.root, padding=(10, 5))
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)
        self.status_label = ttk.Label(self.status_bar, text="", style="Info.TLabel")
        self.status_label.pack(side=tk.LEFT)

    def _show_login(self):
        for w in self.content_frame.winfo_children():
            w.destroy()
        for w in self.top_frame.winfo_children():
            w.destroy()

        ttk.Label(self.top_frame, text="维修派工管理系统", style="Title.TLabel").pack(side=tk.LEFT)

        login_frame = ttk.Frame(self.content_frame)
        login_frame.pack(expand=True)

        ttk.Label(login_frame, text="请选择用户登录", style="Title.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 30))
        ttk.Label(login_frame, text="用户:", style="Bold.TLabel").grid(row=1, column=0, sticky=tk.E, padx=5, pady=10)

        users = self.store.get_all_users()
        user_display = [f"{u.name} ({self._role_cn(u.role)})" for u in users]
        self.user_var = tk.StringVar()
        user_cb = ttk.Combobox(login_frame, textvariable=self.user_var, values=user_display, state="readonly", width=30, font=("Microsoft YaHei", 11))
        user_cb.grid(row=1, column=1, padx=5, pady=10)
        if user_display:
            user_cb.current(0)

        ttk.Button(login_frame, text="登录", width=20, command=self._do_login).grid(row=2, column=0, columnspan=2, pady=20)

    def _role_cn(self, role: Role) -> str:
        mapping = {Role.DISPATCHER: "调度员", Role.TECHNICIAN: "维修员", Role.INSPECTOR: "验收人"}
        return mapping.get(role, role.value)

    def _do_login(self):
        idx = None
        try:
            users = self.store.get_all_users()
            display = [f"{u.name} ({self._role_cn(u.role)})" for u in users]
            idx = display.index(self.user_var.get())
        except (ValueError, IndexError):
            pass
        if idx is None:
            messagebox.showwarning("提示", "请选择用户")
            return
        self.current_user = users[idx]
        self._show_main_view()

    def _show_main_view(self):
        for w in self.top_frame.winfo_children():
            w.destroy()
        for w in self.content_frame.winfo_children():
            w.destroy()

        ttk.Label(self.top_frame, text="维修派工管理系统", style="Title.TLabel").pack(side=tk.LEFT)
        ttk.Label(self.top_frame, text=f"   当前用户: {self.current_user.name} ({self._role_cn(self.current_user.role)})", style="Info.TLabel").pack(side=tk.LEFT, padx=20)
        ttk.Button(self.top_frame, text="切换用户", command=self._show_login).pack(side=tk.RIGHT)
        ttk.Button(self.top_frame, text="导出目录设置", command=self._config_export_dir).pack(side=tk.RIGHT, padx=5)

        self.notebook = ttk.Notebook(self.content_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        self._build_order_list_tab()
        self._build_history_tab()

        if self.current_user.role == Role.DISPATCHER:
            self._build_dispatcher_tab()
            self._build_import_export_tab()
        elif self.current_user.role == Role.TECHNICIAN:
            self._build_technician_tab()
        elif self.current_user.role == Role.INSPECTOR:
            self._build_inspector_tab()
            self._build_import_export_tab()

        self._update_status(f"就绪 | 导出目录: {self.store.get_config().export_dir or '(未设置)'}")
        self._refresh_order_tree()

    def _build_order_list_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="工单总览")

        filter_frame = ttk.LabelFrame(frame, text="筛选条件", padding=10)
        filter_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(filter_frame, text="状态:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.E)
        self.f_status_var = tk.StringVar(value="全部")
        status_values = ["全部"] + [s.value for s in Status]
        ttk.Combobox(filter_frame, textvariable=self.f_status_var, values=status_values, state="readonly", width=12).grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(filter_frame, text="位置:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.E)
        self.f_location_var = tk.StringVar()
        ttk.Entry(filter_frame, textvariable=self.f_location_var, width=15).grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(filter_frame, text="类别:").grid(row=0, column=4, padx=5, pady=5, sticky=tk.E)
        self.f_category_var = tk.StringVar(value="全部")
        cat_values = ["全部"] + CATEGORY_OPTIONS
        ttk.Combobox(filter_frame, textvariable=self.f_category_var, values=cat_values, state="readonly", width=12).grid(row=0, column=5, padx=5, pady=5)

        ttk.Label(filter_frame, text="优先级:").grid(row=0, column=6, padx=5, pady=5, sticky=tk.E)
        self.f_priority_var = tk.StringVar(value="全部")
        ttk.Combobox(filter_frame, textvariable=self.f_priority_var, values=["全部"] + PRIORITY_OPTIONS, state="readonly", width=8).grid(row=0, column=7, padx=5, pady=5)

        ttk.Button(filter_frame, text="查询", command=self._refresh_order_tree).grid(row=0, column=8, padx=10, pady=5)
        ttk.Button(filter_frame, text="重置", command=self._reset_filters).grid(row=0, column=9, padx=5, pady=5)

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("order_id", "title", "location", "category", "priority", "status", "assignee", "created_at")
        self.order_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        self.order_tree.heading("order_id", text="工单编号")
        self.order_tree.heading("title", text="标题")
        self.order_tree.heading("location", text="位置")
        self.order_tree.heading("category", text="类别")
        self.order_tree.heading("priority", text="优先级")
        self.order_tree.heading("status", text="状态")
        self.order_tree.heading("assignee", text="维修员")
        self.order_tree.heading("created_at", text="创建时间")

        self.order_tree.column("order_id", width=160, anchor=tk.W)
        self.order_tree.column("title", width=220, anchor=tk.W)
        self.order_tree.column("location", width=140, anchor=tk.W)
        self.order_tree.column("category", width=100, anchor=tk.W)
        self.order_tree.column("priority", width=70, anchor=tk.CENTER)
        self.order_tree.column("status", width=80, anchor=tk.CENTER)
        self.order_tree.column("assignee", width=90, anchor=tk.W)
        self.order_tree.column("created_at", width=150, anchor=tk.W)

        self.order_tree.tag_configure("high", background="#ffe5e5")
        self.order_tree.tag_configure("completed", background="#e5ffe5")

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.order_tree.yview)
        self.order_tree.configure(yscrollcommand=vsb.set)
        self.order_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.order_tree.bind("<<TreeviewSelect>>", self._on_order_select)
        self.order_tree.bind("<Double-1>", lambda e: self._show_order_detail())

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btn_frame, text="查看详情 / 历史", command=self._show_order_detail).pack(side=tk.LEFT, padx=5)

    def _reset_filters(self):
        self.f_status_var.set("全部")
        self.f_location_var.set("")
        self.f_category_var.set("全部")
        self.f_priority_var.set("全部")
        self._refresh_order_tree()

    def _build_history_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="状态历史")

        info_frame = ttk.Frame(frame)
        info_frame.pack(fill=tk.X, pady=(0, 10))
        self.history_order_label = ttk.Label(info_frame, text="请先选择一个工单", style="Bold.TLabel")
        self.history_order_label.pack(side=tk.LEFT)

        cols = ("timestamp", "status", "user", "note")
        self.history_tree = ttk.Treeview(frame, columns=cols, show="headings", height=15)
        self.history_tree.heading("timestamp", text="时间")
        self.history_tree.heading("status", text="状态")
        self.history_tree.heading("user", text="操作人")
        self.history_tree.heading("note", text="备注")
        self.history_tree.column("timestamp", width=170, anchor=tk.W)
        self.history_tree.column("status", width=100, anchor=tk.CENTER)
        self.history_tree.column("user", width=120, anchor=tk.W)
        self.history_tree.column("note", width=600, anchor=tk.W)
        vsb = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.history_tree.yview)
        self.history_tree.configure(yscrollcommand=vsb.set)
        self.history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        exc_frame = ttk.LabelFrame(frame, text="异常备注", padding=10)
        exc_frame.pack(side=tk.BOTTOM, fill=tk.X, pady=(10, 0))
        self.exception_text = tk.Text(exc_frame, height=6, font=("Microsoft YaHei", 10), state=tk.DISABLED)
        self.exception_text.pack(fill=tk.X)

    def _build_dispatcher_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="调度员操作")

        create_frame = ttk.LabelFrame(frame, text="登记报修单", padding=10)
        create_frame.pack(fill=tk.X, pady=(0, 10))

        ttk.Label(create_frame, text="标题*:").grid(row=0, column=0, padx=5, pady=5, sticky=tk.E)
        self.c_title_var = tk.StringVar()
        ttk.Entry(create_frame, textvariable=self.c_title_var, width=40).grid(row=0, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Label(create_frame, text="位置*:").grid(row=0, column=2, padx=5, pady=5, sticky=tk.E)
        self.c_location_var = tk.StringVar()
        ttk.Entry(create_frame, textvariable=self.c_location_var, width=30).grid(row=0, column=3, padx=5, pady=5, sticky=tk.W)

        ttk.Label(create_frame, text="类别:").grid(row=1, column=0, padx=5, pady=5, sticky=tk.E)
        self.c_category_var = tk.StringVar(value="其他")
        ttk.Combobox(create_frame, textvariable=self.c_category_var, values=CATEGORY_OPTIONS, state="readonly", width=20).grid(row=1, column=1, padx=5, pady=5, sticky=tk.W)

        ttk.Label(create_frame, text="优先级:").grid(row=1, column=2, padx=5, pady=5, sticky=tk.E)
        self.c_priority_var = tk.StringVar(value="中")
        ttk.Combobox(create_frame, textvariable=self.c_priority_var, values=PRIORITY_OPTIONS, state="readonly", width=10).grid(row=1, column=3, padx=5, pady=5, sticky=tk.W)

        ttk.Label(create_frame, text="描述:").grid(row=2, column=0, padx=5, pady=5, sticky=tk.NE)
        self.c_desc_text = tk.Text(create_frame, height=3, width=60, font=("Microsoft YaHei", 10))
        self.c_desc_text.grid(row=2, column=1, columnspan=3, padx=5, pady=5, sticky=tk.W)

        ttk.Button(create_frame, text=" 提交登记 ", command=self._create_order).grid(row=3, column=0, columnspan=4, pady=10)

        dispatch_frame = ttk.LabelFrame(frame, text="派工（选择待派单工单）", padding=10)
        dispatch_frame.pack(fill=tk.BOTH, expand=True)

        disp_tree_frame = ttk.Frame(dispatch_frame)
        disp_tree_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        dcols = ("order_id", "title", "location", "category", "priority", "created_at")
        self.dispatch_tree = ttk.Treeview(disp_tree_frame, columns=dcols, show="headings", selectmode="browse", height=8)
        for c in dcols:
            self.dispatch_tree.heading(c, text={"order_id": "工单编号", "title": "标题", "location": "位置", "category": "类别", "priority": "优先级", "created_at": "创建时间"}[c])
        self.dispatch_tree.column("order_id", width=160)
        self.dispatch_tree.column("title", width=280)
        self.dispatch_tree.column("location", width=140)
        self.dispatch_tree.column("category", width=100)
        self.dispatch_tree.column("priority", width=70, anchor=tk.CENTER)
        self.dispatch_tree.column("created_at", width=150)
        self.dispatch_tree.tag_configure("high", background="#ffe5e5")
        vsb2 = ttk.Scrollbar(disp_tree_frame, orient=tk.VERTICAL, command=self.dispatch_tree.yview)
        self.dispatch_tree.configure(yscrollcommand=vsb2.set)
        self.dispatch_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb2.pack(side=tk.RIGHT, fill=tk.Y)

        assign_frame = ttk.Frame(dispatch_frame)
        assign_frame.pack(fill=tk.X)
        ttk.Label(assign_frame, text="派给维修员:").pack(side=tk.LEFT, padx=5)
        techs = self.store.get_users_by_role(Role.TECHNICIAN)
        self.dispatch_tech_var = tk.StringVar()
        tech_display = [f"{t.name}" for t in techs]
        ttk.Combobox(assign_frame, textvariable=self.dispatch_tech_var, values=tech_display, state="readonly", width=15).pack(side=tk.LEFT, padx=5)
        if tech_display:
            self.dispatch_tech_var.set(tech_display[0])
        ttk.Button(assign_frame, text=" 派工 ", command=self._dispatch_order).pack(side=tk.LEFT, padx=10)
        ttk.Button(assign_frame, text=" 刷新列表 ", command=self._refresh_dispatch_tree).pack(side=tk.LEFT, padx=5)

    def _build_technician_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="维修员操作")

        ttk.Label(frame, text="可接工单（已派单）", style="Bold.TLabel").pack(anchor=tk.W)
        accept_frame = ttk.Frame(frame)
        accept_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        acols = ("order_id", "title", "location", "category", "priority", "assignee", "created_at")
        self.accept_tree = ttk.Treeview(accept_frame, columns=acols, show="headings", selectmode="browse", height=8)
        for c in acols:
            self.accept_tree.heading(c, text={"order_id": "工单编号", "title": "标题", "location": "位置", "category": "类别", "priority": "优先级", "assignee": "指派给", "created_at": "创建时间"}[c])
        self.accept_tree.column("order_id", width=160)
        self.accept_tree.column("title", width=260)
        self.accept_tree.column("location", width=130)
        self.accept_tree.column("category", width=90)
        self.accept_tree.column("priority", width=70, anchor=tk.CENTER)
        self.accept_tree.column("assignee", width=90)
        self.accept_tree.column("created_at", width=150)
        self.accept_tree.tag_configure("high", background="#ffe5e5")
        vsb_a = ttk.Scrollbar(accept_frame, orient=tk.VERTICAL, command=self.accept_tree.yview)
        self.accept_tree.configure(yscrollcommand=vsb_a.set)
        self.accept_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb_a.pack(side=tk.RIGHT, fill=tk.Y)

        btn1 = ttk.Frame(frame)
        btn1.pack(fill=tk.X, pady=5)
        ttk.Button(btn1, text=" 接单 ", command=self._accept_order).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn1, text=" 刷新 ", command=self._refresh_technician_trees).pack(side=tk.LEFT, padx=5)

        ttk.Separator(frame, orient=tk.HORIZONTAL).pack(fill=tk.X, pady=15)

        ttk.Label(frame, text="我处理中的工单（可完工）", style="Bold.TLabel").pack(anchor=tk.W)
        progress_frame = ttk.Frame(frame)
        progress_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        pcols = ("order_id", "title", "location", "category", "priority", "created_at")
        self.progress_tree = ttk.Treeview(progress_frame, columns=pcols, show="headings", selectmode="browse", height=8)
        for c in pcols:
            self.progress_tree.heading(c, text={"order_id": "工单编号", "title": "标题", "location": "位置", "category": "类别", "priority": "优先级", "created_at": "创建时间"}[c])
        self.progress_tree.column("order_id", width=160)
        self.progress_tree.column("title", width=280)
        self.progress_tree.column("location", width=140)
        self.progress_tree.column("category", width=100)
        self.progress_tree.column("priority", width=70, anchor=tk.CENTER)
        self.progress_tree.column("created_at", width=150)
        self.progress_tree.tag_configure("high", background="#ffe5e5")
        vsb_p = ttk.Scrollbar(progress_frame, orient=tk.VERTICAL, command=self.progress_tree.yview)
        self.progress_tree.configure(yscrollcommand=vsb_p.set)
        self.progress_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb_p.pack(side=tk.RIGHT, fill=tk.Y)

        btn2 = ttk.Frame(frame)
        btn2.pack(fill=tk.X, pady=5)
        ttk.Button(btn2, text=" 完工（申请验收） ", command=self._complete_order).pack(side=tk.LEFT, padx=5)

        self._refresh_technician_trees()

    def _build_inspector_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="验收人操作")

        ttk.Label(frame, text="待验收工单", style="Bold.TLabel").pack(anchor=tk.W)
        ins_frame = ttk.Frame(frame)
        ins_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        icols = ("order_id", "title", "location", "category", "priority", "assignee", "created_at")
        self.inspect_tree = ttk.Treeview(ins_frame, columns=icols, show="headings", selectmode="browse", height=12)
        for c in icols:
            self.inspect_tree.heading(c, text={"order_id": "工单编号", "title": "标题", "location": "位置", "category": "类别", "priority": "优先级", "assignee": "维修员", "created_at": "创建时间"}[c])
        self.inspect_tree.column("order_id", width=160)
        self.inspect_tree.column("title", width=260)
        self.inspect_tree.column("location", width=130)
        self.inspect_tree.column("category", width=90)
        self.inspect_tree.column("priority", width=70, anchor=tk.CENTER)
        self.inspect_tree.column("assignee", width=90)
        self.inspect_tree.column("created_at", width=150)
        self.inspect_tree.tag_configure("high", background="#ffe5e5")
        vsb_i = ttk.Scrollbar(ins_frame, orient=tk.VERTICAL, command=self.inspect_tree.yview)
        self.inspect_tree.configure(yscrollcommand=vsb_i.set)
        self.inspect_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb_i.pack(side=tk.RIGHT, fill=tk.Y)

        btn_frame = ttk.Frame(frame)
        btn_frame.pack(fill=tk.X, pady=10)
        ttk.Button(btn_frame, text=" 验收通过（完成） ", command=self._approve_order).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=" 验收退回（需重新处理） ", command=self._reject_order).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text=" 刷新 ", command=self._refresh_inspect_tree).pack(side=tk.LEFT, padx=5)

        self._refresh_inspect_tree()

    def _build_import_export_tab(self):
        frame = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(frame, text="导入 / 导出")

        imp_frame = ttk.LabelFrame(frame, text="导入报修单 (CSV)", padding=10)
        imp_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(imp_frame, text="CSV需包含列: title/标题, description/描述, location/位置, category/类别, priority/优先级", style="Info.TLabel").pack(anchor=tk.W)
        ttk.Button(imp_frame, text=" 选择CSV文件并导入 ", command=self._import_csv).pack(pady=10)

        exp_frame = ttk.LabelFrame(frame, text="导出工单", padding=10)
        exp_frame.pack(fill=tk.X)
        cfg = self.store.get_config()
        ttk.Label(exp_frame, text=f"当前导出目录: {cfg.export_dir or '(使用默认 ./exports)'}", style="Info.TLabel").pack(anchor=tk.W, pady=5)
        ttk.Button(exp_frame, text=" 设置导出目录... ", command=self._config_export_dir).pack(anchor=tk.W, pady=5)
        ttk.Separator(exp_frame).pack(fill=tk.X, pady=10)
        ttk.Button(exp_frame, text=" 导出全部工单为 JSON ", command=lambda: self._export("json", True)).pack(side=tk.LEFT, padx=5)
        ttk.Button(exp_frame, text=" 导出全部工单为 CSV ", command=lambda: self._export("csv", True)).pack(side=tk.LEFT, padx=5)
        ttk.Button(exp_frame, text=" 导出当前筛选结果为 JSON ", command=lambda: self._export("json", False)).pack(side=tk.LEFT, padx=5)
        ttk.Button(exp_frame, text=" 导出当前筛选结果为 CSV ", command=lambda: self._export("csv", False)).pack(side=tk.LEFT, padx=5)

    # --- Data refresh helpers ---
    def _update_status(self, text: str):
        self.status_label.config(text=text)

    def _refresh_order_tree(self):
        for i in self.order_tree.get_children():
            self.order_tree.delete(i)
        status_s = self.f_status_var.get()
        status = None if status_s == "全部" else Status(status_s)
        location = self.f_location_var.get().strip() or None
        cat_s = self.f_category_var.get()
        category = None if cat_s == "全部" else cat_s
        pr_s = self.f_priority_var.get()
        priority = None if pr_s == "全部" else pr_s

        orders = self.store.get_orders_by_filter(status=status, location=location, category=category, priority=priority)
        for o in orders:
            tags = ()
            if o.priority == "高":
                tags = ("high",)
            if o.status == Status.COMPLETED:
                tags = ("completed",)
            self.order_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.location, o.category, o.priority,
                o.status.value, o.assignee_name or "", o.created_at
            ), tags=tags)
        self._update_status(f"共查询到 {len(orders)} 条工单")

    def _refresh_dispatch_tree(self):
        for i in self.dispatch_tree.get_children():
            self.dispatch_tree.delete(i)
        orders = self.store.get_orders_by_filter(status=Status.PENDING_DISPATCH)
        for o in orders:
            tags = ("high",) if o.priority == "高" else ()
            self.dispatch_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.location, o.category, o.priority, o.created_at
            ), tags=tags)

    def _refresh_technician_trees(self):
        for i in self.accept_tree.get_children():
            self.accept_tree.delete(i)
        for i in self.progress_tree.get_children():
            self.progress_tree.delete(i)

        dispatched = self.store.get_orders_by_filter(status=Status.DISPATCHED)
        for o in dispatched:
            tags = ("high",) if o.priority == "高" else ()
            self.accept_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.location, o.category, o.priority,
                o.assignee_name or "未指派", o.created_at
            ), tags=tags)

        in_progress = self.store.get_orders_by_filter(status=Status.IN_PROGRESS, assignee_id=self.current_user.user_id)
        for o in in_progress:
            tags = ("high",) if o.priority == "高" else ()
            self.progress_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.location, o.category, o.priority, o.created_at
            ), tags=tags)

    def _refresh_inspect_tree(self):
        for i in self.inspect_tree.get_children():
            self.inspect_tree.delete(i)
        orders = self.store.get_orders_by_filter(status=Status.PENDING_INSPECTION)
        for o in orders:
            tags = ("high",) if o.priority == "高" else ()
            self.inspect_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.location, o.category, o.priority,
                o.assignee_name or "", o.created_at
            ), tags=tags)

    def _refresh_all_trees(self):
        self._refresh_order_tree()
        if hasattr(self, "dispatch_tree"):
            self._refresh_dispatch_tree()
        if hasattr(self, "accept_tree"):
            self._refresh_technician_trees()
        if hasattr(self, "inspect_tree"):
            self._refresh_inspect_tree()

    # --- Selection & detail ---
    def _get_selected_order(self) -> WorkOrder:
        for tree_attr in ["order_tree", "dispatch_tree", "accept_tree", "progress_tree", "inspect_tree"]:
            tree = getattr(self, tree_attr, None)
            if tree and tree.selection():
                oid = tree.selection()[0]
                return self.store.get_order(oid)
        return None

    def _on_order_select(self, _event=None):
        order = self._get_selected_order()
        if not order:
            return
        self.history_order_label.config(text=f"工单: {order.order_id} - {order.title}")
        for i in self.history_tree.get_children():
            self.history_tree.delete(i)
        for h in order.history:
            self.history_tree.insert("", tk.END, values=(h.timestamp, h.status.value, h.user_name, h.note))

        self.exception_text.config(state=tk.NORMAL)
        self.exception_text.delete(1.0, tk.END)
        if order.exception_notes:
            self.exception_text.insert(tk.END, "\n".join(order.exception_notes))
        else:
            self.exception_text.insert(tk.END, "(无异常备注)")
        self.exception_text.config(state=tk.DISABLED)

    def _show_order_detail(self):
        order = self._get_selected_order()
        if not order:
            messagebox.showinfo("提示", "请先选择一个工单")
            return
        self._on_order_select()
        try:
            self.notebook.select(1)
        except tk.TclError:
            pass

    # --- Actions ---
    def _create_order(self):
        title = self.c_title_var.get().strip()
        location = self.c_location_var.get().strip()
        category = self.c_category_var.get()
        priority = self.c_priority_var.get()
        description = self.c_desc_text.get(1.0, tk.END).strip()
        if not title:
            messagebox.showwarning("提示", "标题不能为空")
            return
        if not location:
            messagebox.showwarning("提示", "位置不能为空")
            return
        try:
            order = self.store.create_order(title, description, location, category, priority, self.current_user)
        except (PermissionError, WorkOrderError) as e:
            messagebox.showerror("登记失败", str(e))
            return
        messagebox.showinfo("成功", f"工单创建成功！\n编号: {order.order_id}")
        self.c_title_var.set("")
        self.c_location_var.set("")
        self.c_desc_text.delete(1.0, tk.END)
        self._refresh_all_trees()

    def _dispatch_order(self):
        sel = self.dispatch_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择待派单工单")
            return
        tech_name = self.dispatch_tech_var.get()
        if not tech_name:
            messagebox.showwarning("提示", "请选择维修员")
            return
        techs = self.store.get_users_by_role(Role.TECHNICIAN)
        assignee = next((t for t in techs if t.name == tech_name), None)
        if not assignee:
            messagebox.showerror("错误", "未找到维修员")
            return
        order_id = sel[0]
        try:
            self.store.dispatch_order(order_id, assignee, self.current_user)
        except (PermissionError, StatusTransitionError, WorkOrderError) as e:
            self.store.add_exception_note(order_id, f"派工失败: {str(e)}")
            messagebox.showerror("派工失败", str(e) + "\n已保存异常备注，已保存记录未被修改。")
            return
        messagebox.showinfo("成功", f"已派工给 {assignee.name}")
        self._refresh_all_trees()

    def _accept_order(self):
        sel = self.accept_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择要接的工单")
            return
        order_id = sel[0]
        try:
            self.store.accept_order(order_id, self.current_user)
        except ConcurrentOperationError as e:
            self.store.add_exception_note(order_id, f"抢单失败[{self.current_user.name}]: {str(e)}")
            messagebox.showerror("抢单失败", str(e) + "\n已保存异常备注，已保存记录未被修改。")
            return
        except (PermissionError, StatusTransitionError, WorkOrderError) as e:
            self.store.add_exception_note(order_id, f"接单失败: {str(e)}")
            messagebox.showerror("接单失败", str(e) + "\n已保存异常备注，已保存记录未被修改。")
            return
        messagebox.showinfo("成功", "接单成功，开始处理")
        self._refresh_all_trees()

    def _complete_order(self):
        sel = self.progress_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择处理中的工单")
            return
        order_id = sel[0]
        try:
            self.store.complete_order(order_id, self.current_user)
        except (PermissionError, StatusTransitionError, WorkOrderError) as e:
            self.store.add_exception_note(order_id, f"完工失败: {str(e)}")
            messagebox.showerror("完工失败", str(e) + "\n已保存异常备注，已保存记录未被修改。")
            return
        messagebox.showinfo("成功", "已提交完工，等待验收")
        self._refresh_all_trees()

    def _approve_order(self):
        sel = self.inspect_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择待验收工单")
            return
        order_id = sel[0]
        order = self.store.get_order(order_id)
        if order and order.status == Status.IN_PROGRESS:
            self.store.add_exception_note(order_id, f"违规操作失败[{self.current_user.name}]: 试图从处理中直接完成(需走验收流程)")
            messagebox.showerror("验收失败", "非法操作：不能从处理中直接完成，必须走【处理中→待验收→已完成】的完整路径。\n已保存异常备注，已保存记录未被修改。")
            return
        try:
            self.store.approve_order(order_id, self.current_user)
        except (PermissionError, StatusTransitionError, WorkOrderError) as e:
            self.store.add_exception_note(order_id, f"验收失败: {str(e)}")
            messagebox.showerror("验收失败", str(e) + "\n已保存异常备注，已保存记录未被修改。")
            return
        messagebox.showinfo("成功", "验收通过，工单已完成")
        self._refresh_all_trees()

    def _reject_order(self):
        sel = self.inspect_tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择待验收工单")
            return
        reason = simpledialog.askstring("验收退回", "请输入退回原因：", parent=self.root)
        if not reason or not reason.strip():
            messagebox.showwarning("提示", "退回原因不能为空")
            return
        order_id = sel[0]
        try:
            self.store.reject_order(order_id, self.current_user, reason.strip())
        except (PermissionError, StatusTransitionError, WorkOrderError) as e:
            self.store.add_exception_note(order_id, f"退回失败: {str(e)}")
            messagebox.showerror("退回失败", str(e) + "\n已保存异常备注，已保存记录未被修改。")
            return
        messagebox.showinfo("成功", f"已退回，维修员需重新处理。\n原因: {reason.strip()}")
        self._refresh_all_trees()

    def _config_export_dir(self):
        current = self.store.get_config().export_dir or os.getcwd()
        path = filedialog.askdirectory(title="选择导出目录", initialdir=current)
        if not path:
            return
        self.store.set_export_dir(path)
        messagebox.showinfo("成功", f"导出目录已设置为:\n{path}")
        self._update_status(f"就绪 | 导出目录: {path}")

    def _import_csv(self):
        path = filedialog.askopenfilename(
            title="选择CSV文件",
            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            count, errors = self.store.import_orders_csv(path, self.current_user)
        except (PermissionError, WorkOrderError) as e:
            messagebox.showerror("导入失败", str(e))
            return
        msg = f"成功导入 {count} 条工单"
        if errors:
            msg += f"\n\n{len(errors)} 条失败:\n" + "\n".join(errors[:20])
            if len(errors) > 20:
                msg += f"\n... 共 {len(errors)} 条错误"
        messagebox.showinfo("导入结果", msg)
        self._refresh_all_trees()

    def _export(self, fmt: str, all_orders: bool):
        if all_orders:
            orders = None
        else:
            status_s = self.f_status_var.get()
            status = None if status_s == "全部" else Status(status_s)
            location = self.f_location_var.get().strip() or None
            cat_s = self.f_category_var.get()
            category = None if cat_s == "全部" else cat_s
            pr_s = self.f_priority_var.get()
            priority = None if pr_s == "全部" else pr_s
            orders = self.store.get_orders_by_filter(status=status, location=location, category=category, priority=priority)
        try:
            if fmt == "json":
                path = self.store.export_orders_json(orders)
            else:
                path = self.store.export_orders_csv(orders)
        except ExportError as e:
            messagebox.showerror("导出失败", str(e) + "\n已保存记录未被修改。")
            return
        except WorkOrderError as e:
            messagebox.showerror("导出失败", str(e))
            return
        messagebox.showinfo("导出成功", f"已导出到:\n{path}")


def main():
    root = tk.Tk()
    app = MaintenanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
