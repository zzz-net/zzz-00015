import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import os
import sys
from datetime import datetime
from typing import List, Dict, Optional
from models import (
    Role, Status, WorkOrder, User, TimeSlot, CATEGORY_SKILL_MAP,
    BatchDraftItem, BatchReassignmentDraft, BatchReassignmentResult,
    BatchItemResult, ConflictType, RevocationStatus, RevocationConflictType,
    SparePart, SparePartRequest, SparePartRequestStatus, SparePartAuditLog,
    RescheduleStatus, RescheduleCandidateSlot, RescheduleRequest,
    RescheduleConfirmLog, ArrivalConfirmation,
)
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
DAY_OPTIONS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
SKILL_OPTIONS = ["空调", "电梯", "电路", "水管", "门禁", "办公设备", "网络", "通用"]
DAY_MAP = {d: i for i, d in enumerate(DAY_OPTIONS)}


def get_score_color(score):
    if score >= 80:
        return "#2ecc71"
    elif score >= 50:
        return "#f39c12"
    else:
        return "#e74c3c"


def get_priority_color(priority):
    if priority == "高":
        return "#e74c3c"
    elif priority == "中":
        return "#f39c12"
    else:
        return "#3498db"


def get_status_color(status):
    color_map = {
        Status.PENDING_DISPATCH: "#95a5a6",
        Status.DISPATCHED: "#3498db",
        Status.IN_PROGRESS: "#f39c12",
        Status.PENDING_INSPECTION: "#9b59b6",
        Status.COMPLETED: "#2ecc71",
    }
    return color_map.get(status, "#000000")


def get_reschedule_status_color(status):
    color_map = {
        RescheduleStatus.PENDING: "#f39c12",
        RescheduleStatus.CONFIRMED: "#2ecc71",
        RescheduleStatus.REJECTED: "#e74c3c",
        RescheduleStatus.CANCELLED: "#95a5a6",
        RescheduleStatus.EXPIRED: "#7f8c8d",
    }
    return color_map.get(status, "#000000")


class ReassignDialog(tk.Toplevel):
    def __init__(self, parent, store, dispatcher, order):
        super().__init__(parent)
        self.store = store
        self.dispatcher = dispatcher
        self.order = order
        self.result = None
        self.title("改派工单")
        self.geometry("620x560")
        self.configure(bg="#f5f6fa")
        self.grab_set()
        self.transient(parent)
        self._build_ui()
        self._try_load_draft()

    def _build_ui(self):
        self._loaded_draft = None
        style = ttk.Style()
        style.configure("Reassign.Treeview", rowheight=28, font=("Microsoft YaHei", 9))
        style.configure("Reassign.Treeview.Heading", font=("Microsoft YaHei", 10, "bold"))

        info_frame = tk.Frame(self, bg="#f5f6fa")
        info_frame.pack(fill=tk.X, padx=15, pady=10)
        tk.Label(info_frame, text=f"工单: {self.order.order_id}", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").grid(row=0, column=0, sticky="w")
        tk.Label(info_frame, text=f"标题: {self.order.title}", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=1, column=0, sticky="w", pady=2)
        tk.Label(info_frame, text=f"类别: {self.order.category}  优先级: {self.order.priority}",
                 font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=2, column=0, sticky="w", pady=2)
        current = self.order.assignee_name or "(未指派)"
        tk.Label(info_frame, text=f"当前维修员: {current}  当前版本: v{self.order.version}",
                 font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=3, column=0, sticky="w", pady=2)

        self.draft_info_label = tk.Label(info_frame, text="", font=("Microsoft YaHei", 9, "bold"),
                                          bg="#fff3cd", fg="#856404", anchor="w", padx=8, pady=4)
        self.draft_info_label.grid(row=4, column=0, sticky="we", pady=(6, 0))
        self.draft_info_label.grid_remove()

        tk.Label(self, text="选择新维修员（按匹配度排序）:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15)

        tree_frame = tk.Frame(self, bg="#f5f6fa")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        columns = ("tech_id", "name", "score", "skill", "available", "capacity", "warnings")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", style="Reassign.Treeview")
        self.tree.heading("tech_id", text="工号")
        self.tree.heading("name", text="姓名")
        self.tree.heading("score", text="匹配分")
        self.tree.heading("skill", text="技能")
        self.tree.heading("available", text="时间")
        self.tree.heading("capacity", text="负载")
        self.tree.heading("warnings", text="备注")
        self.tree.column("tech_id", width=70, anchor="center")
        self.tree.column("name", width=80, anchor="center")
        self.tree.column("score", width=70, anchor="center")
        self.tree.column("skill", width=60, anchor="center")
        self.tree.column("available", width=60, anchor="center")
        self.tree.column("capacity", width=70, anchor="center")
        self.tree.column("warnings", width=180, anchor="w")
        self.tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=sb.set)

        self.tree.tag_configure("good", background="#eafaf1")
        self.tree.tag_configure("partial", background="#fef9e7")
        self.tree.tag_configure("bad", background="#fdedec")

        self._load_technicians()

        tk.Label(self, text="改派原因（必填）:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15, pady=(10, 2))
        self.reason_text = tk.Text(self, height=3, font=("Microsoft YaHei", 10))
        self.reason_text.pack(fill=tk.X, padx=15, pady=2)

        btn_frame = tk.Frame(self, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=15, pady=15)
        tk.Button(btn_frame, text="确认改派", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=12, command=self._on_confirm).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="保存草稿", font=("Microsoft YaHei", 10), bg="#f39c12", fg="white",
                  width=12, command=self._on_save_draft).pack(side=tk.RIGHT, padx=5)
        self.clear_draft_btn = tk.Button(btn_frame, text="清除草稿", font=("Microsoft YaHei", 10),
                                          bg="#95a5a6", fg="white", width=12, command=self._on_clear_draft)
        self.clear_draft_btn.pack(side=tk.RIGHT, padx=5)
        self.clear_draft_btn.pack_forget()
        tk.Button(btn_frame, text="取消", font=("Microsoft YaHei", 10), bg="#7f8c8d", fg="white",
                  width=12, command=self.destroy).pack(side=tk.RIGHT, padx=5)

    def _load_technicians(self):
        for i in self.tree.get_children():
            self.tree.delete(i)
        ranked = self.store.rank_technicians_for_order(self.order)
        for tech, match in ranked:
            if tech.user_id == self.order.assignee_id:
                continue
            score = match.score
            if score >= 80:
                tag = "good"
            elif score >= 50:
                tag = "partial"
            else:
                tag = "bad"
            self.tree.insert("", tk.END, iid=tech.user_id, values=(
                tech.user_id, tech.name, score,
                "是" if match.skill_match else "否",
                "是" if match.available_now else "否",
                f"{match.current_load}/{match.max_parallel}",
                "; ".join(match.warnings) if match.warnings else "推荐",
            ), tags=(tag,))

    def _try_load_draft(self):
        self._loaded_draft = None
        draft = self.store.get_reassignment_draft(self.order.order_id, self.dispatcher)
        if draft is None:
            return
        if self.tree.exists(draft.target_technician_id):
            self.tree.selection_set(draft.target_technician_id)
            self.tree.see(draft.target_technician_id)
        self.reason_text.delete("1.0", tk.END)
        self.reason_text.insert("1.0", draft.reason)
        tech = self.store.get_user(draft.target_technician_id)
        tech_name = tech.name if tech else draft.target_technician_id
        self.draft_info_label.configure(
            text=f"已载入改派草稿（创建于 {draft.created_at}，目标: {tech_name}，原工单版本 v{draft.order_version}）"
        )
        self.draft_info_label.grid()
        self.clear_draft_btn.pack(side=tk.RIGHT, padx=5)
        self._loaded_draft = draft

    def _on_save_draft(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请选择新维修员", parent=self)
            return
        new_tech_id = selection[0]
        reason = self.reason_text.get("1.0", tk.END).strip()
        if not reason:
            messagebox.showwarning("提示", "请填写改派原因后再保存草稿", parent=self)
            return
        try:
            new_tech = self.store.get_user(new_tech_id)
            draft = self.store.save_reassignment_draft(self.order.order_id, self.dispatcher, new_tech, reason)
            self._loaded_draft = draft
            messagebox.showinfo("成功", "改派草稿已保存", parent=self)
            self.draft_info_label.configure(
                text=f"改派草稿已保存（目标: {new_tech.name}，原工单版本 v{draft.order_version}）"
            )
            self.draft_info_label.grid()
            self.clear_draft_btn.pack(side=tk.RIGHT, padx=5)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e), parent=self)

    def _on_clear_draft(self):
        if not messagebox.askyesno("确认", "确定清除该工单的改派草稿？", parent=self):
            return
        deleted = self.store.delete_reassignment_draft(self.order.order_id, self.dispatcher)
        if deleted:
            self.draft_info_label.grid_remove()
            self.clear_draft_btn.pack_forget()
            self.reason_text.delete("1.0", tk.END)
            self.tree.selection_remove(self.tree.selection())
            self._loaded_draft = None
            messagebox.showinfo("成功", "草稿已清除", parent=self)

    def _on_confirm(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请选择新维修员", parent=self)
            return
        new_tech_id = selection[0]
        reason = self.reason_text.get("1.0", tk.END).strip()
        if not reason:
            messagebox.showwarning("提示", "请填写改派原因", parent=self)
            return
        try:
            fresh_order = self.store.get_order(self.order.order_id)
            if not fresh_order:
                messagebox.showerror("错误", "工单不存在", parent=self)
                return

            expected_version = (
                self._loaded_draft.order_version
                if self._loaded_draft is not None
                else self.order.version
            )

            if self._loaded_draft is not None and self._loaded_draft.order_version != fresh_order.version:
                messagebox.showerror("并发冲突",
                    f"草稿是基于工单旧版本 v{self._loaded_draft.order_version} 创建的，"
                    f"当前工单已被其他人改动至 v{fresh_order.version}。\n"
                    f"本次改派已被拦截，工单数据未被覆盖，草稿和现场输入已保留，请刷新后再试。",
                    parent=self)
                return

            allowed, msg = self.store.can_reassign(fresh_order, self.dispatcher)
            if not allowed:
                messagebox.showerror("无法改派",
                    f"工单当前状态已变更，不能改派：{msg}\n"
                    f"改派草稿和现场输入已保留，请刷新后再试。",
                    parent=self)
                return

            new_tech = self.store.get_user(new_tech_id)
            if new_tech is None:
                messagebox.showerror("错误",
                    "目标维修员不存在，可能已被删除。草稿和现场输入已保留。",
                    parent=self)
                return
            if new_tech.role != Role.TECHNICIAN:
                messagebox.showerror("错误",
                    f"目标用户【{new_tech.name}】已不是维修员。草稿和现场输入已保留。",
                    parent=self)
                return

            self.store.reassign_order(fresh_order.order_id, new_tech, self.dispatcher,
                                       reason, expected_version)
            self.result = True
            messagebox.showinfo("成功", f"工单已改派给 {new_tech.name}", parent=self)
            self.destroy()
        except ConcurrentOperationError as e:
            messagebox.showerror("并发冲突",
                f"{str(e)}\n改派草稿和现场输入已保留，请刷新工单后再试。",
                parent=self)
        except PermissionError as e:
            messagebox.showerror("权限不足",
                f"{str(e)}\n改派草稿和现场输入已保留。",
                parent=self)
        except WorkOrderError as e:
            messagebox.showerror("错误",
                f"{str(e)}\n改派草稿和现场输入已保留。",
                parent=self)


class BatchReassignDialog(tk.Toplevel):
    def __init__(self, parent, store, dispatcher, existing_draft: Optional[BatchReassignmentDraft] = None):
        super().__init__(parent)
        self.store = store
        self.dispatcher = dispatcher
        self.existing_draft = existing_draft
        self.current_draft: Optional[BatchReassignmentDraft] = existing_draft
        self.draft_items: List[BatchDraftItem] = []
        self.conflicts: Dict[str, List[ConflictType]] = {}
        self.last_result: Optional[BatchReassignmentResult] = None
        self.result_filter_status: str = "all"
        self.result_filter_conflict: str = "all"
        self.result_filter_revocation: str = "all"
        self.title("批量改派预案")
        self.geometry("1280x880")
        self.configure(bg="#f5f6fa")
        self.grab_set()
        self.transient(parent)
        self._build_ui()
        self._try_load_latest_result()
        if existing_draft:
            self._load_existing_draft(existing_draft)

    def _build_ui(self):
        style = ttk.Style()
        style.configure("Batch.Treeview", rowheight=32, font=("Microsoft YaHei", 9))
        style.configure("Batch.Treeview.Heading", font=("Microsoft YaHei", 10, "bold"))

        top_bar = tk.Frame(self, bg="#2c3e50", height=50)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)
        tk.Label(top_bar, text="批量改派预案管理", font=("Microsoft YaHei", 14, "bold"),
                 bg="#2c3e50", fg="white").pack(side=tk.LEFT, padx=15)
        self.draft_status_label = tk.Label(top_bar, text="", font=("Microsoft YaHei", 10),
                                            bg="#2c3e50", fg="#f39c12")
        self.draft_status_label.pack(side=tk.LEFT, padx=10)

        picker_frame = tk.LabelFrame(self, text="第1步：勾选要批量改派的工单（待派/已派）",
                                      font=("Microsoft YaHei", 11, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        picker_frame.pack(fill=tk.X, padx=10, pady=8)

        picker_inner = tk.Frame(picker_frame, bg="#f5f6fa")
        picker_inner.pack(fill=tk.X, padx=8, pady=6)

        tk.Label(picker_inner, text="状态筛选:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=0, padx=5, pady=5)
        self.batch_filter_status = ttk.Combobox(picker_inner, values=["待派+已派", "仅待派单", "仅已派单"],
                                                 state="readonly", width=12, font=("Microsoft YaHei", 10))
        self.batch_filter_status.set("待派+已派")
        self.batch_filter_status.grid(row=0, column=1, padx=5, pady=5)
        self.batch_filter_status.bind("<<ComboboxSelected>>", lambda e: self._refresh_order_picker())

        tk.Button(picker_inner, text="刷新工单列表", font=("Microsoft YaHei", 10),
                  bg="#3498db", fg="white", width=12,
                  command=self._refresh_order_picker).grid(row=0, column=2, padx=10, pady=5)
        tk.Button(picker_inner, text="全选", font=("Microsoft YaHei", 10),
                  bg="#95a5a6", fg="white", width=8,
                  command=self._select_all_orders).grid(row=0, column=3, padx=3, pady=5)
        tk.Button(picker_inner, text="全不选", font=("Microsoft YaHei", 10),
                  bg="#95a5a6", fg="white", width=8,
                  command=self._clear_all_orders).grid(row=0, column=4, padx=3, pady=5)
        tk.Button(picker_inner, text="生成推荐并加入预案", font=("Microsoft YaHei", 10, "bold"),
                  bg="#27ae60", fg="white", width=18,
                  command=self._generate_recommendations).grid(row=0, column=5, padx=10, pady=5)

        picker_tree_frame = tk.Frame(picker_inner, bg="#f5f6fa")
        picker_tree_frame.grid(row=1, column=0, columnspan=6, sticky="we", pady=(4, 0))
        picker_inner.grid_columnconfigure(0, weight=1)

        p_cols = ("select", "order_id", "title", "category", "priority", "status", "assignee", "location")
        self.picker_tree = ttk.Treeview(picker_tree_frame, columns=p_cols, show="headings",
                                         height=6, style="Batch.Treeview")
        self.picker_tree.heading("select", text="选")
        self.picker_tree.heading("order_id", text="工单编号")
        self.picker_tree.heading("title", text="标题")
        self.picker_tree.heading("category", text="类别")
        self.picker_tree.heading("priority", text="优先级")
        self.picker_tree.heading("status", text="状态")
        self.picker_tree.heading("assignee", text="维修员")
        self.picker_tree.heading("location", text="位置")
        for c, w in [("select", 30), ("order_id", 140), ("title", 180), ("category", 80),
                      ("priority", 60), ("status", 70), ("assignee", 70), ("location", 100)]:
            self.picker_tree.column(c, width=w, anchor="center")
        self.picker_tree.column("title", anchor="w")
        self.picker_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        p_sb = ttk.Scrollbar(picker_tree_frame, orient="vertical", command=self.picker_tree.yview)
        p_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.picker_tree.configure(yscrollcommand=p_sb.set)
        self.picker_tree.tag_configure("priority_high", background="#fdedec")
        self.picker_tree.tag_configure("priority_mid", background="#fef9e7")
        self.picker_tree.tag_configure("priority_low", background="#eaf2f8")
        self._configure_tree_tags(self.picker_tree)
        self._refresh_order_picker()

        detail_frame = tk.LabelFrame(self, text="第2步：调整目标维修员和改派原因（可逐条修改）",
                                      font=("Microsoft YaHei", 11, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        detail_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        toolbar = tk.Frame(detail_frame, bg="#f5f6fa")
        toolbar.pack(fill=tk.X, padx=8, pady=6)
        tk.Label(toolbar, text="预案条目:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(side=tk.LEFT)
        self.items_count_label = tk.Label(toolbar, text="0 条", font=("Microsoft YaHei", 10),
                                           bg="#f5f6fa", fg="#2980b9")
        self.items_count_label.pack(side=tk.LEFT, padx=5)
        tk.Label(toolbar, text="冲突:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(side=tk.LEFT, padx=(15, 0))
        self.conflicts_count_label = tk.Label(toolbar, text="0 条", font=("Microsoft YaHei", 10),
                                               bg="#f5f6fa", fg="#e74c3c")
        self.conflicts_count_label.pack(side=tk.LEFT, padx=5)
        tk.Button(toolbar, text="删除选中条目", font=("Microsoft YaHei", 10),
                  bg="#e74c3c", fg="white", width=12,
                  command=self._remove_selected_items).pack(side=tk.RIGHT, padx=3)
        tk.Button(toolbar, text="检测冲突", font=("Microsoft YaHei", 10),
                  bg="#f39c12", fg="white", width=10,
                  command=self._detect_and_show_conflicts).pack(side=tk.RIGHT, padx=3)
        tk.Button(toolbar, text="清空预案", font=("Microsoft YaHei", 10),
                  bg="#95a5a6", fg="white", width=10,
                  command=self._clear_all_items).pack(side=tk.RIGHT, padx=3)

        detail_tree_frame = tk.Frame(detail_frame, bg="#f5f6fa")
        detail_tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        d_cols = ("order_id", "order_title", "old_assignee", "target_tech", "score",
                   "risk_warnings", "reason", "conflicts")
        self.detail_tree = ttk.Treeview(detail_tree_frame, columns=d_cols, show="headings",
                                         style="Batch.Treeview")
        for c, text, w in [
            ("order_id", "工单编号", 130),
            ("order_title", "标题", 150),
            ("old_assignee", "原维修员", 80),
            ("target_tech", "目标维修员*", 100),
            ("score", "匹配分", 60),
            ("risk_warnings", "风险提示", 220),
            ("reason", "改派原因*", 180),
            ("conflicts", "冲突", 160),
        ]:
            self.detail_tree.heading(c, text=text)
            self.detail_tree.column(c, width=w, anchor="center")
        self.detail_tree.column("order_title", anchor="w")
        self.detail_tree.column("risk_warnings", anchor="w")
        self.detail_tree.column("reason", anchor="w")
        self.detail_tree.column("conflicts", anchor="w")
        self.detail_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        d_sb = ttk.Scrollbar(detail_tree_frame, orient="vertical", command=self.detail_tree.yview)
        d_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.detail_tree.configure(yscrollcommand=d_sb.set)
        self.detail_tree.tag_configure("conflict", background="#fdecea")
        self.detail_tree.tag_configure("good", background="#eafaf1")
        self.detail_tree.tag_configure("partial", background="#fef9e7")
        self.detail_tree.tag_configure("bad", background="#fdedec")
        self.detail_tree.bind("<Double-1>", self._on_detail_double_click)

        edit_frame = tk.Frame(detail_frame, bg="#f5f6fa")
        edit_frame.pack(fill=tk.X, padx=8, pady=6)
        tk.Label(edit_frame, text="选中条目后编辑（双击也可修改）:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=0, padx=3, pady=4)
        tk.Label(edit_frame, text="目标维修员:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=1, padx=3, pady=4)
        self.edit_tech_combo = ttk.Combobox(edit_frame, state="readonly", width=14, font=("Microsoft YaHei", 10))
        self.edit_tech_combo.grid(row=0, column=2, padx=3, pady=4)
        tk.Label(edit_frame, text="改派原因:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=3, padx=3, pady=4)
        self.edit_reason_entry = tk.Entry(edit_frame, width=28, font=("Microsoft YaHei", 10))
        self.edit_reason_entry.grid(row=0, column=4, padx=3, pady=4)
        tk.Button(edit_frame, text="应用修改", font=("Microsoft YaHei", 10),
                  bg="#3498db", fg="white", width=10,
                  command=self._apply_item_edit).grid(row=0, column=5, padx=8, pady=4)

        btn_frame = tk.Frame(self, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        tk.Button(btn_frame, text="关闭", font=("Microsoft YaHei", 11),
                  bg="#7f8c8d", fg="white", width=10,
                  command=self.destroy).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="导出结果(CSV)", font=("Microsoft YaHei", 11),
                  bg="#9b59b6", fg="white", width=13,
                  command=lambda: self._export_result("csv")).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="导出结果(JSON)", font=("Microsoft YaHei", 11),
                  bg="#9b59b6", fg="white", width=13,
                  command=lambda: self._export_result("json")).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="保存预案草稿", font=("Microsoft YaHei", 11, "bold"),
                  bg="#f39c12", fg="white", width=13,
                  command=self._on_save_draft).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="提交批量改派", font=("Microsoft YaHei", 11, "bold"),
                  bg="#27ae60", fg="white", width=13,
                  command=self._on_submit_batch).pack(side=tk.RIGHT, padx=5)

        result_frame = tk.LabelFrame(self, text="第3步：执行结果明细（可筛选、可定位原草稿）",
                                      font=("Microsoft YaHei", 11, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        result_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        result_toolbar = tk.Frame(result_frame, bg="#f5f6fa")
        result_toolbar.pack(fill=tk.X, padx=8, pady=6)

        tk.Label(result_toolbar, text="结果编号:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(side=tk.LEFT, padx=(0, 3))
        self.result_id_label = tk.Label(result_toolbar, text="(暂无)", font=("Microsoft YaHei", 9),
                                         bg="#f5f6fa", fg="#7f8c8d")
        self.result_id_label.pack(side=tk.LEFT, padx=(0, 15))

        tk.Label(result_toolbar, text="状态筛选:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").pack(side=tk.LEFT, padx=(10, 3))
        self.result_status_combo = ttk.Combobox(
            result_toolbar,
            values=["全部", "仅成功", "仅跳过", "仅失败"],
            state="readonly", width=10, font=("Microsoft YaHei", 10),
        )
        self.result_status_combo.set("全部")
        self.result_status_combo.pack(side=tk.LEFT, padx=3)
        self.result_status_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_result_detail_view())

        tk.Label(result_toolbar, text="冲突类型:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").pack(side=tk.LEFT, padx=(15, 3))
        self.result_conflict_combo = ttk.Combobox(
            result_toolbar,
            values=["全部"],
            state="readonly", width=16, font=("Microsoft YaHei", 10),
        )
        self.result_conflict_combo.set("全部")
        self.result_conflict_combo.pack(side=tk.LEFT, padx=3)
        self.result_conflict_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_result_detail_view())

        tk.Label(result_toolbar, text="撤销状态:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").pack(side=tk.LEFT, padx=(15, 3))
        self.result_revocation_combo = ttk.Combobox(
            result_toolbar,
            values=["全部", "已撤销", "可撤销", "不可撤销", "冲突跳过"],
            state="readonly", width=10, font=("Microsoft YaHei", 10),
        )
        self.result_revocation_combo.set("全部")
        self.result_revocation_combo.pack(side=tk.LEFT, padx=3)
        self.result_revocation_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_result_detail_view())

        tk.Button(result_toolbar, text="刷新结果", font=("Microsoft YaHei", 10),
                  bg="#3498db", fg="white", width=10,
                  command=self._refresh_result_detail_view).pack(side=tk.LEFT, padx=(15, 3))
        tk.Button(result_toolbar, text="撤销选中", font=("Microsoft YaHei", 10, "bold"),
                  bg="#e74c3c", fg="white", width=10,
                  command=self._on_revoke_selected).pack(side=tk.LEFT, padx=3)
        tk.Button(result_toolbar, text="撤销全部可撤销", font=("Microsoft YaHei", 10, "bold"),
                  bg="#c0392b", fg="white", width=14,
                  command=self._on_revoke_all_revocable).pack(side=tk.LEFT, padx=3)
        tk.Button(result_toolbar, text="定位原草稿", font=("Microsoft YaHei", 10),
                  bg="#e67e22", fg="white", width=12,
                  command=self._locate_original_draft).pack(side=tk.LEFT, padx=3)
        tk.Button(result_toolbar, text="恢复最近一次结果", font=("Microsoft YaHei", 10),
                  bg="#8e44ad", fg="white", width=16,
                  command=self._restore_latest_result).pack(side=tk.LEFT, padx=3)

        self.result_summary_label = tk.Label(result_toolbar, text="", font=("Microsoft YaHei", 10),
                                              bg="#f5f6fa", fg="#2980b9")
        self.result_summary_label.pack(side=tk.RIGHT, padx=10)

        result_tree_frame = tk.Frame(result_frame, bg="#f5f6fa")
        result_tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        r_cols = (
            "status", "revocation_status", "order_id", "order_title",
            "orig_assignee", "target_assignee",
            "version", "permission", "skill", "capacity", "schedule",
            "log_written", "conflict_types", "reason", "error",
            "revocation_reason", "revocation_operator", "revocation_timestamp",
            "item_timestamp",
        )
        self.result_detail_tree = ttk.Treeview(
            result_tree_frame, columns=r_cols, show="headings", style="Batch.Treeview",
        )
        col_config = [
            ("status", "结果", 60),
            ("revocation_status", "撤销状态", 80),
            ("order_id", "工单编号", 130),
            ("order_title", "工单标题", 140),
            ("orig_assignee", "原维修员", 75),
            ("target_assignee", "新维修员", 75),
            ("version", "版本", 50),
            ("permission", "权限", 50),
            ("skill", "技能", 50),
            ("capacity", "容量", 50),
            ("schedule", "排班", 50),
            ("log_written", "日志", 50),
            ("conflict_types", "冲突类型", 140),
            ("reason", "改派原因", 130),
            ("error", "错误/跳过原因", 180),
            ("revocation_reason", "撤销原因", 130),
            ("revocation_operator", "撤销操作人", 80),
            ("revocation_timestamp", "撤销时间", 140),
            ("item_timestamp", "处理时间", 140),
        ]
        for c, text, w in col_config:
            self.result_detail_tree.heading(c, text=text)
            self.result_detail_tree.column(c, width=w, anchor="center")
        self.result_detail_tree.column("order_title", anchor="w")
        self.result_detail_tree.column("conflict_types", anchor="w")
        self.result_detail_tree.column("reason", anchor="w")
        self.result_detail_tree.column("error", anchor="w")
        self.result_detail_tree.column("revocation_reason", anchor="w")
        self.result_detail_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        r_sb = ttk.Scrollbar(result_tree_frame, orient="vertical", command=self.result_detail_tree.yview)
        r_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.result_detail_tree.configure(yscrollcommand=r_sb.set)
        self.result_detail_tree.tag_configure("success", background="#eafaf1")
        self.result_detail_tree.tag_configure("skipped", background="#fef9e7")
        self.result_detail_tree.tag_configure("failed", background="#fdedec")
        self.result_detail_tree.tag_configure("log_fail", background="#fdecea")
        self.result_detail_tree.tag_configure("revoked", background="#e8daef")
        self.result_detail_tree.tag_configure("revocable", background="#d5f5e3")
        self.result_detail_tree.tag_configure("conflict_skipped", background="#fadbd8")
        self.result_detail_tree.bind("<Double-1>", self._on_result_double_click)

        result_text_frame = tk.Frame(result_frame, bg="#f5f6fa")
        result_text_frame.pack(fill=tk.X, padx=8, pady=(0, 6))
        self.result_text = tk.Text(result_text_frame, height=4, font=("Microsoft YaHei", 9),
                                    state=tk.DISABLED, bg="#ffffff", wrap=tk.WORD)
        self.result_text.pack(fill=tk.X)

    def _configure_tree_tags(self, tree):
        tree.tag_configure("good", background="#eafaf1")
        tree.tag_configure("partial", background="#fef9e7")
        tree.tag_configure("bad", background="#fdedec")

    def _refresh_order_picker(self):
        for i in self.picker_tree.get_children():
            self.picker_tree.delete(i)
        filter_val = self.batch_filter_status.get()
        statuses = []
        if filter_val in ("待派+已派", "仅待派单"):
            statuses.append(Status.PENDING_DISPATCH)
        if filter_val in ("待派+已派", "仅已派单"):
            statuses.append(Status.DISPATCHED)
        try:
            orders: List[WorkOrder] = []
            for s in statuses:
                orders.extend(self.store.get_orders_by_filter(status=s))
            orders.sort(key=lambda o: o.created_at, reverse=True)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e), parent=self)
            return
        existing_in_draft = {it.order_id for it in self.draft_items}
        for o in orders:
            if o.order_id in existing_in_draft:
                continue
            tag = f"priority_{'high' if o.priority == '高' else 'mid' if o.priority == '中' else 'low'}"
            self.picker_tree.insert("", tk.END, iid=o.order_id, values=(
                "", o.order_id, o.title, o.category, o.priority,
                o.status.value, o.assignee_name or "未指派", o.location,
            ), tags=(tag,))

    def _select_all_orders(self):
        for iid in self.picker_tree.get_children():
            vals = list(self.picker_tree.item(iid, "values"))
            vals[0] = "✓"
            self.picker_tree.item(iid, values=vals)

    def _clear_all_orders(self):
        for iid in self.picker_tree.get_children():
            vals = list(self.picker_tree.item(iid, "values"))
            vals[0] = ""
            self.picker_tree.item(iid, values=vals)

    def _generate_recommendations(self):
        selected_ids = []
        for iid in self.picker_tree.get_children():
            vals = self.picker_tree.item(iid, "values")
            if vals and vals[0] == "✓":
                selected_ids.append(iid)
        if not selected_ids:
            messagebox.showwarning("提示", "请先勾选要加入预案的工单", parent=self)
            return
        try:
            items = self.store.generate_batch_recommendations(selected_ids, self.dispatcher)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e), parent=self)
            return
        existing_ids = {it.order_id for it in self.draft_items}
        added = 0
        for it in items:
            if it.order_id not in existing_ids:
                self.draft_items.append(it)
                existing_ids.add(it.order_id)
                added += 1
        self._refresh_detail_view()
        self._refresh_order_picker()
        self._refresh_edit_combo()
        messagebox.showinfo("成功", f"已为 {added} 条工单生成推荐，可在下方调整", parent=self)

    def _refresh_edit_combo(self):
        techs = self.store.get_users_by_role(Role.TECHNICIAN)
        values = [f"{t.user_id} - {t.name}" for t in techs]
        self.edit_tech_combo["values"] = values

    def _refresh_detail_view(self):
        for i in self.detail_tree.get_children():
            self.detail_tree.delete(i)
        for idx, it in enumerate(self.draft_items):
            order = self.store.get_order(it.order_id)
            order_title = order.title if order else "(已删除)"
            old_name = ""
            if it.original_assignee_id:
                old_user = self.store.get_user(it.original_assignee_id)
                old_name = old_user.name if old_user else it.original_assignee_id
            tech = self.store.get_user(it.target_technician_id)
            tech_label = f"{tech.user_id}-{tech.name}" if tech else it.target_technician_id
            score = it.match_score or 0
            if score >= 80:
                tag = "good"
            elif score >= 50:
                tag = "partial"
            else:
                tag = "bad"
            conflict_list = self.conflicts.get(it.order_id, [])
            conflict_str = ",".join(conflict_list) if conflict_list else ""
            if conflict_list:
                tag = "conflict"
            row_tags = (tag,)
            self.detail_tree.insert("", tk.END, iid=str(idx), values=(
                it.order_id, order_title, old_name or "(未指派)",
                tech_label, score,
                "; ".join(it.risk_warnings) if it.risk_warnings else (
                    "推荐" if it.recommended else ""),
                it.reason, conflict_str,
            ), tags=row_tags)
        self.items_count_label.configure(text=f"{len(self.draft_items)} 条")
        conflict_cnt = len(self.conflicts)
        self.conflicts_count_label.configure(text=f"{conflict_cnt} 条")

    def _on_detail_double_click(self, event):
        sel = self.detail_tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self.draft_items):
            return
        item = self.draft_items[idx]
        tech = self.store.get_user(item.target_technician_id)
        if tech:
            combo_val = f"{tech.user_id} - {tech.name}"
            vals = self.edit_tech_combo["values"]
            if combo_val in vals:
                self.edit_tech_combo.set(combo_val)
        self.edit_reason_entry.delete(0, tk.END)
        self.edit_reason_entry.insert(0, item.reason)

    def _apply_item_edit(self):
        sel = self.detail_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在下方列表选中要修改的条目", parent=self)
            return
        idx = int(sel[0])
        if idx >= len(self.draft_items):
            return
        tech_val = self.edit_tech_combo.get()
        if not tech_val:
            messagebox.showwarning("提示", "请选择目标维修员", parent=self)
            return
        tech_id = tech_val.split(" - ")[0]
        tech = self.store.get_user(tech_id)
        if tech is None:
            messagebox.showerror("错误", "目标维修员不存在", parent=self)
            return
        if tech.role != Role.TECHNICIAN:
            messagebox.showerror("错误", "目标必须是维修员", parent=self)
            return
        reason = self.edit_reason_entry.get().strip()
        if not reason:
            messagebox.showwarning("提示", "请填写改派原因", parent=self)
            return
        item = self.draft_items[idx]
        order = self.store.get_order(item.order_id)
        new_match = self.store.calculate_match(order, tech) if order else None
        item.target_technician_id = tech.user_id
        item.reason = reason
        item.tech_skills_snapshot = list(tech.skills)
        item.tech_schedule_snapshot = [ts.to_dict() for ts in tech.time_slots]
        item.tech_max_parallel_snapshot = tech.max_parallel_orders
        if new_match:
            item.risk_warnings = list(new_match.warnings)
            item.match_score = new_match.score
            item.recommended = new_match.is_recommended
        self._refresh_detail_view()
        messagebox.showinfo("成功", "条目已更新", parent=self)

    def _remove_selected_items(self):
        sel = self.detail_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择要删除的条目", parent=self)
            return
        idxs = sorted([int(s) for s in sel], reverse=True)
        for idx in idxs:
            if idx < len(self.draft_items):
                del self.draft_items[idx]
        self.conflicts = {}
        self._refresh_detail_view()
        self._refresh_order_picker()

    def _clear_all_items(self):
        if not self.draft_items:
            return
        if not messagebox.askyesno("确认", "确定清空预案中的所有条目？", parent=self):
            return
        self.draft_items = []
        self.conflicts = {}
        self._refresh_detail_view()
        self._refresh_order_picker()

    def _detect_and_show_conflicts(self):
        if not self.draft_items:
            self.conflicts = {}
            self._refresh_detail_view()
            messagebox.showinfo("提示", "预案为空", parent=self)
            return
        temp_draft = BatchReassignmentDraft(
            draft_id="_tmp",
            dispatcher_id=self.dispatcher.user_id,
            dispatcher_name=self.dispatcher.name,
            items=list(self.draft_items),
        )
        self.conflicts = self.store.detect_batch_conflicts(temp_draft)
        self._refresh_detail_view()
        if self.conflicts:
            conflict_labels = {
                ConflictType.VERSION_MISMATCH: "版本变更",
                ConflictType.STATUS_CHANGED: "状态变更",
                ConflictType.TECHNICIAN_REMOVED: "维修员已删除",
                ConflictType.TECHNICIAN_ROLE_CHANGED: "维修员角色变更",
                ConflictType.TECHNICIAN_SKILLS_CHANGED: "技能变更",
                ConflictType.TECHNICIAN_SCHEDULE_CHANGED: "排班变更",
                ConflictType.TECHNICIAN_CAPACITY_CHANGED: "容量变更",
                ConflictType.ORDER_REMOVED: "工单已删除",
            }
            parts = []
            for oid, ctypes in self.conflicts.items():
                labels = [conflict_labels.get(c, c) for c in ctypes]
                parts.append(f"  {oid}: {', '.join(labels)}")
            msg = f"检测到 {len(self.conflicts)} 条冲突:\n" + "\n".join(parts)
            messagebox.showwarning("冲突检测", msg, parent=self)
        else:
            messagebox.showinfo("冲突检测", "未检测到冲突", parent=self)

    def _load_existing_draft(self, draft: BatchReassignmentDraft):
        self.draft_items = list(draft.items)
        self.draft_status_label.configure(text=f"已载入预案草稿 {draft.draft_id}（创建于 {draft.created_at}）")
        self._detect_and_show_conflicts()
        self._refresh_edit_combo()
        self._refresh_order_picker()

    def _on_save_draft(self):
        if not self.draft_items:
            messagebox.showwarning("提示", "预案为空，无法保存", parent=self)
            return
        for it in self.draft_items:
            if not it.reason or not it.reason.strip():
                messagebox.showwarning("提示", f"工单 {it.order_id} 的改派原因不能为空", parent=self)
                return
            if not it.target_technician_id:
                messagebox.showwarning("提示", f"工单 {it.order_id} 未指定目标维修员", parent=self)
                return
        try:
            draft_id = self.current_draft.draft_id if self.current_draft else None
            draft = self.store.save_batch_reassignment_draft(self.dispatcher, self.draft_items, draft_id)
            self.current_draft = draft
            self.draft_status_label.configure(text=f"草稿已保存: {draft.draft_id}（更新于 {draft.updated_at}）")
            messagebox.showinfo("成功", f"批量预案草稿已保存: {draft.draft_id}", parent=self)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("保存失败", str(e), parent=self)

    def _on_submit_batch(self):
        if not self.draft_items:
            messagebox.showwarning("提示", "预案为空，无法提交", parent=self)
            return
        for it in self.draft_items:
            if not it.reason or not it.reason.strip():
                messagebox.showwarning("提示", f"工单 {it.order_id} 的改派原因不能为空", parent=self)
                return
            if not it.target_technician_id:
                messagebox.showwarning("提示", f"工单 {it.order_id} 未指定目标维修员", parent=self)
                return
        if not messagebox.askyesno("确认",
                                    f"确定提交批量改派？共 {len(self.draft_items)} 条。\n单条失败将自动跳过，不影响其他条目。",
                                    parent=self):
            return
        temp_draft = BatchReassignmentDraft(
            draft_id=self.current_draft.draft_id if self.current_draft else "_submit",
            dispatcher_id=self.dispatcher.user_id,
            dispatcher_name=self.dispatcher.name,
            items=list(self.draft_items),
        )
        try:
            result = self.store.execute_batch_reassignment(temp_draft, self.dispatcher)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("提交失败", str(e), parent=self)
            return
        self.last_result = result
        self._show_result(result)
        if self.current_draft:
            refreshed = self.store.get_batch_reassignment_draft(self.current_draft.draft_id, self.dispatcher)
            if refreshed is None:
                self.current_draft = None
                self.draft_status_label.configure(text="所有条目执行成功，草稿已清理")
            else:
                self.current_draft = refreshed
                self.draft_items = list(refreshed.items)
                self.draft_status_label.configure(
                    text=f"部分成功，剩余 {len(self.draft_items)} 条草稿（更新于 {refreshed.updated_at}）")
        else:
            remaining_ids = {r.order_id for r in result.results if not r.success}
            self.draft_items = [it for it in self.draft_items if it.order_id in remaining_ids]
            self.conflicts = {}
        self._refresh_detail_view()
        self._refresh_order_picker()

    def _show_result(self, result: BatchReassignmentResult):
        self.result_text.configure(state=tk.NORMAL)
        self.result_text.delete("1.0", tk.END)
        lines = [
            f"批量改派执行完成  结果编号: {result.result_id}  提交人: {result.dispatcher_name}  时间: {result.timestamp}",
            f"  总计: {result.total_count} 条  成功: {result.success_count} 条    跳过: {result.skipped_count} 条    失败: {result.failed_count} 条"
            f"    已撤销: {result.revoked_count} 条    可撤销: {result.revocable_count} 条    不可撤销: {result.not_revocable_count} 条    冲突跳过: {result.revocation_conflict_skipped_count} 条",
        ]
        for r in result.results:
            status_label = r.status_label
            info = f"  [{status_label}] {r.order_id} -> {r.target_technician_name or '?'}"
            if r.revoked:
                info += f"  已撤销（{r.revocation_operator_name or '?'}，原因: {r.revocation_reason or '无'}）"
            elif r.error_message:
                info += f"  原因: {r.error_message}"
            lines.append(info)
        lines.append("提示：上方明细表格可筛选结果和撤销状态，选中成功条目后可点击“撤销选中”或“撤销全部可撤销”")
        self.result_text.insert(tk.END, "\n".join(lines))
        self.result_text.configure(state=tk.DISABLED)

        if result.result_id:
            self.result_id_label.configure(text=result.result_id, fg="#2c3e50")
        self._refresh_result_detail_view()

    def _try_load_latest_result(self):
        try:
            latest = self.store.get_latest_batch_result(self.dispatcher)
            if latest is not None:
                self.last_result = latest
                self._show_result(latest)
        except Exception:
            pass

    def _restore_latest_result(self):
        try:
            latest = self.store.get_latest_batch_result(self.dispatcher)
        except Exception as e:
            messagebox.showerror("错误", f"读取最近结果失败: {e}", parent=self)
            return
        if latest is None:
            messagebox.showinfo("提示", "当前没有可恢复的历史改派结果", parent=self)
            return
        self.last_result = latest
        self._show_result(latest)
        messagebox.showinfo(
            "已恢复",
            f"已恢复最近一次结果：{latest.result_id}\n时间：{latest.timestamp}\n"
            f"共 {latest.total_count} 条（成功 {latest.success_count} / 跳过 {latest.skipped_count} / 失败 {latest.failed_count}）",
            parent=self,
        )

    def _apply_status_filter(self, item: BatchItemResult) -> bool:
        selected = self.result_status_combo.get()
        if selected == "仅成功":
            return item.success
        elif selected == "仅跳过":
            return item.skipped
        elif selected == "仅失败":
            return (not item.success) and (not item.skipped)
        return True

    def _apply_conflict_filter(self, item: BatchItemResult) -> bool:
        selected = self.result_conflict_combo.get()
        if selected == "全部":
            return True
        if not item.conflict_types:
            return False
        conflict_labels = {
            ConflictType.VERSION_MISMATCH: "版本变更",
            ConflictType.STATUS_CHANGED: "状态变更",
            ConflictType.TECHNICIAN_REMOVED: "维修员已删除",
            ConflictType.TECHNICIAN_ROLE_CHANGED: "维修员角色变更",
            ConflictType.TECHNICIAN_SKILLS_CHANGED: "技能变更",
            ConflictType.TECHNICIAN_SCHEDULE_CHANGED: "排班变更",
            ConflictType.TECHNICIAN_CAPACITY_CHANGED: "容量变更",
            ConflictType.ORDER_REMOVED: "工单已删除",
        }
        return selected in {conflict_labels.get(c, c) for c in item.conflict_types}

    def _apply_revocation_filter(self, item: BatchItemResult) -> bool:
        selected = self.result_revocation_combo.get()
        if selected == "全部":
            return True
        if selected == "已撤销":
            return item.revoked
        if selected == "可撤销":
            return item.success and not item.revoked and item.revocation_status == RevocationStatus.REVOCABLE
        if selected == "不可撤销":
            return item.success and not item.revoked and item.revocation_status == RevocationStatus.NOT_REVOCABLE
        if selected == "冲突跳过":
            return item.revocation_status == RevocationStatus.CONFLICT_SKIPPED
        return True

    def _refresh_result_detail_view(self):
        for i in self.result_detail_tree.get_children():
            self.result_detail_tree.delete(i)
        result = self.last_result
        if result is None:
            self.result_summary_label.configure(text="")
            self.result_conflict_combo["values"] = ["全部"]
            self.result_conflict_combo.set("全部")
            return

        conflict_labels = {
            ConflictType.VERSION_MISMATCH: "版本变更",
            ConflictType.STATUS_CHANGED: "状态变更",
            ConflictType.TECHNICIAN_REMOVED: "维修员已删除",
            ConflictType.TECHNICIAN_ROLE_CHANGED: "维修员角色变更",
            ConflictType.TECHNICIAN_SKILLS_CHANGED: "技能变更",
            ConflictType.TECHNICIAN_SCHEDULE_CHANGED: "排班变更",
            ConflictType.TECHNICIAN_CAPACITY_CHANGED: "容量变更",
            ConflictType.ORDER_REMOVED: "工单已删除",
        }

        all_conflicts = sorted(result.all_conflict_types)
        combo_values = ["全部"] + [conflict_labels.get(c, c) for c in all_conflicts]
        self.result_conflict_combo["values"] = combo_values
        current = self.result_conflict_combo.get()
        if current not in combo_values:
            self.result_conflict_combo.set("全部")

        def _flag(passed):
            if passed is True:
                return "✓"
            elif passed is False:
                return "✗"
            return "-"

        shown = 0
        for idx, r in enumerate(result.results):
            if not self._apply_status_filter(r):
                continue
            if not self._apply_conflict_filter(r):
                continue
            if not self._apply_revocation_filter(r):
                continue
            shown += 1
            tags = []
            if r.revoked:
                tags.append("revoked")
                base_tag = "revoked"
            elif r.success and not r.revoked and r.revocation_status == RevocationStatus.REVOCABLE:
                tags.append("revocable")
                base_tag = "revocable"
            elif r.revocation_status == RevocationStatus.CONFLICT_SKIPPED:
                tags.append("conflict_skipped")
                base_tag = "conflict_skipped"
            elif r.success:
                tags.append("success")
                base_tag = "success"
            elif r.skipped:
                tags.append("skipped")
                base_tag = "skipped"
            else:
                tags.append("failed")
                base_tag = "failed"
            if not r.log_written and r.success and not r.revoked:
                tags.append("log_fail")
            ctypes_display = ",".join(conflict_labels.get(c, c) for c in (r.conflict_types or []))
            self.result_detail_tree.insert("", tk.END, iid=str(idx), values=(
                r.status_label,
                r.revocation_status_label,
                r.order_id,
                r.order_title or "",
                r.original_assignee_name or "(未指派)",
                r.target_technician_name or "",
                _flag(r.version_passed),
                _flag(r.permission_passed),
                _flag(r.skill_passed),
                _flag(r.capacity_passed),
                _flag(r.schedule_passed),
                "✓" if r.log_written else ("✗:" + (r.log_write_error or "")[:16]),
                ctypes_display,
                r.reason or "",
                r.error_message or "",
                r.revocation_reason or "",
                r.revocation_operator_name or "",
                r.revocation_timestamp or "",
                r.item_timestamp or "",
            ), tags=tuple(tags))
        self.result_summary_label.configure(
            text=f"显示 {shown} / {result.total_count} 条  "
                 f"(成功 {result.success_count} / 跳过 {result.skipped_count} / 失败 {result.failed_count}"
                 f" / 已撤销 {result.revoked_count} / 可撤销 {result.revocable_count}"
                 f" / 不可撤销 {result.not_revocable_count} / 冲突跳过 {result.revocation_conflict_skipped_count})",
        )

    def _prompt_revocation_reason(self) -> Optional[str]:
        reason = simpledialog.askstring(
            "撤销原因",
            "请填写撤销原因（必填）：",
            parent=self,
        )
        if reason is None:
            return None
        reason = reason.strip()
        if not reason:
            messagebox.showwarning("提示", "撤销原因不能为空", parent=self)
            return None
        return reason

    def _on_revoke_selected(self):
        if self.last_result is None:
            messagebox.showwarning("提示", "当前没有批量改派结果可撤销", parent=self)
            return
        sel = self.result_detail_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在结果明细中选中要撤销的条目", parent=self)
            return
        order_ids = []
        for s in sel:
            idx = int(s)
            if idx < len(self.last_result.results):
                r = self.last_result.results[idx]
                if r.success and not r.revoked:
                    order_ids.append(r.order_id)
        if not order_ids:
            messagebox.showwarning("提示", "选中的条目中没有可撤销的成功改派项", parent=self)
            return
        reason = self._prompt_revocation_reason()
        if not reason:
            return
        if not messagebox.askyesno(
            "确认撤销",
            f"确定撤销选中的 {len(order_ids)} 条成功改派？\n"
            f"工单将恢复到改派前的维修员和状态。",
            parent=self,
        ):
            return
        self._execute_revocation(order_ids, reason)

    def _on_revoke_all_revocable(self):
        if self.last_result is None:
            messagebox.showwarning("提示", "当前没有批量改派结果可撤销", parent=self)
            return
        order_ids = [
            r.order_id for r in self.last_result.results
            if r.success and not r.revoked and r.revocation_status == RevocationStatus.REVOCABLE
        ]
        if not order_ids:
            messagebox.showinfo("提示", "当前没有可撤销的条目", parent=self)
            return
        reason = self._prompt_revocation_reason()
        if not reason:
            return
        if not messagebox.askyesno(
            "确认撤销",
            f"确定撤销所有 {len(order_ids)} 条可撤销的成功改派？\n"
            f"工单将恢复到改派前的维修员和状态。\n"
            f"已被再次改派、已完成、原维修员不存在等情况将自动跳过。",
            parent=self,
        ):
            return
        self._execute_revocation(order_ids, reason)

    def _execute_revocation(self, order_ids: List[str], reason: str):
        try:
            rev_result = self.store.revoke_batch_items(
                self.last_result, order_ids, self.dispatcher, reason
            )
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("撤销失败", str(e), parent=self)
            return
        refreshed = self.store.get_batch_result(self.last_result.result_id)
        if refreshed:
            self.last_result = refreshed
        self._show_result(self.last_result)
        msg_lines = [
            f"撤销操作完成！",
            f"总计: {rev_result['total']} 条",
            f"成功撤销: {rev_result['success']} 条",
            f"冲突跳过: {rev_result['skipped']} 条",
            f"失败: {rev_result['failed']} 条",
        ]
        if rev_result["skipped"] > 0:
            msg_lines.append("")
            msg_lines.append("冲突跳过的条目详情可在结果明细的“撤销状态”列中查看。")
        messagebox.showinfo("撤销完成", "\n".join(msg_lines), parent=self)

    def _on_result_double_click(self, event):
        self._locate_original_draft()

    def _locate_original_draft(self):
        sel = self.result_detail_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先在下方结果明细中选中一条记录", parent=self)
            return
        if self.last_result is None:
            return
        idx = int(sel[0])
        if idx >= len(self.last_result.results):
            return
        item_result = self.last_result.results[idx]
        order_id = item_result.order_id

        for di, draft_item in enumerate(self.draft_items):
            if draft_item.order_id == order_id:
                iid = str(di)
                if iid in self.detail_tree.get_children():
                    self.detail_tree.selection_set(iid)
                    self.detail_tree.see(iid)
                    self.detail_tree.focus(iid)
                messagebox.showinfo(
                    "已定位",
                    f"工单 {order_id} 已定位到预案第 {di + 1} 条\n"
                    f"结果: {item_result.status_label}\n"
                    f"原因: {item_result.error_message or '(无)'}",
                    parent=self,
                )
                return

        draft_id = item_result.draft_id or (self.last_result.draft_id)
        if draft_id:
            try:
                draft = self.store.get_batch_reassignment_draft(draft_id, self.dispatcher)
                if draft is not None:
                    if messagebox.askyesno(
                        "定位草稿",
                        f"当前预案中未找到工单 {order_id}，\n但存在关联草稿 {draft_id}，是否载入该草稿？",
                        parent=self,
                    ):
                        self._load_existing_draft(draft)
                        self.current_draft = draft
                        for di, draft_item in enumerate(self.draft_items):
                            if draft_item.order_id == order_id:
                                iid = str(di)
                                if iid in self.detail_tree.get_children():
                                    self.detail_tree.selection_set(iid)
                                    self.detail_tree.see(iid)
                                break
                        return
            except Exception:
                pass

        messagebox.showinfo(
            "提示",
            f"工单 {order_id} 未在当前预案中（可能已成功改派或草稿被清理）。\n"
            f"结果: {item_result.status_label}\n"
            f"处理时间: {item_result.item_timestamp}\n"
            f"原因: {item_result.error_message or '(无)'}",
            parent=self,
        )

    def _export_result(self, fmt: str):
        if self.last_result is None:
            messagebox.showwarning("提示", "暂无执行结果可导出，请先提交批量改派", parent=self)
            return
        try:
            if fmt == "json":
                path = self.store.export_batch_result_json(self.last_result)
            else:
                path = self.store.export_batch_result_csv(self.last_result)
            messagebox.showinfo("导出成功", f"批量改派结果已导出到:\n{path}", parent=self)
        except (ExportError, WorkOrderError) as e:
            messagebox.showerror("导出失败", str(e), parent=self)


class LoginDialog(tk.Toplevel):
    def __init__(self, parent, store):
        super().__init__(parent)
        self.store = store
        self.user = None
        self.title("维修派工系统 - 登录")
        self.geometry("400x350")
        self.configure(bg="#ecf0f1")
        self.resizable(False, False)
        self.grab_set()
        self.transient(parent)
        self._build_ui()

    def _build_ui(self):
        tk.Label(self, text="维修派工管理系统", font=("Microsoft YaHei", 20, "bold"),
                 bg="#ecf0f1", fg="#2c3e50").pack(pady=(30, 10))
        tk.Label(self, text="Maintenance Dispatch System", font=("Microsoft YaHei", 9),
                 bg="#ecf0f1", fg="#7f8c8d").pack(pady=(0, 20))

        tk.Label(self, text="请选择用户登录:", font=("Microsoft YaHei", 11),
                 bg="#ecf0f1").pack(pady=(0, 8))

        frame = tk.Frame(self, bg="#ecf0f1")
        frame.pack(padx=40, fill=tk.X, pady=5)

        self.user_listbox = tk.Listbox(frame, height=8, font=("Microsoft YaHei", 11),
                                        selectmode=tk.SINGLE, activestyle="none")
        self.user_listbox.pack(fill=tk.X, pady=5)
        self.users = self.store.get_all_users()
        role_labels = {Role.DISPATCHER: "调度员", Role.TECHNICIAN: "维修员", Role.INSPECTOR: "验收员"}
        for u in self.users:
            self.user_listbox.insert(tk.END, f"{u.name}  ({role_labels.get(u.role, u.role.value)})")
        if self.users:
            self.user_listbox.selection_set(0)

        btn_frame = tk.Frame(self, bg="#ecf0f1")
        btn_frame.pack(pady=20)
        tk.Button(btn_frame, text="登录", font=("Microsoft YaHei", 12, "bold"),
                  bg="#3498db", fg="white", width=15, height=1,
                  command=self._on_login).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="退出", font=("Microsoft YaHei", 12),
                  bg="#95a5a6", fg="white", width=15, height=1,
                  command=self._on_cancel).pack(side=tk.LEFT, padx=8)

    def _on_login(self):
        sel = self.user_listbox.curselection()
        if not sel:
            messagebox.showwarning("提示", "请选择用户", parent=self)
            return
        self.user = self.users[sel[0]]
        self.destroy()

    def _on_cancel(self):
        self.user = None
        self.destroy()


class MaintenanceApp:
    def __init__(self, root):
        self.root = root
        self.store = DataStore()
        self.current_user = None
        self.root.title("维修派工管理系统")
        self.root.geometry("1280x800")
        self.root.configure(bg="#ecf0f1")

        self._configure_styles()
        self._show_login()

    def _configure_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei", 10, "bold"))
        style.configure("TNotebook", background="#ecf0f1", borderwidth=0)
        style.configure("TNotebook.Tab", font=("Microsoft YaHei", 11, "bold"),
                        padding=(15, 8))
        style.map("TNotebook.Tab",
                  background=[("selected", "#3498db")],
                  foreground=[("selected", "white")])

    def _show_login(self):
        for w in self.root.winfo_children():
            w.destroy()
        login = LoginDialog(self.root, self.store)
        self.root.wait_window(login)
        if login.user:
            self.current_user = login.user
            self._build_main_ui()
        else:
            self.root.destroy()

    def _build_main_ui(self):
        for w in self.root.winfo_children():
            w.destroy()

        role_labels = {Role.DISPATCHER: "调度员", Role.TECHNICIAN: "维修员", Role.INSPECTOR: "验收员"}

        header = tk.Frame(self.root, bg="#2c3e50", height=60)
        header.pack(fill=tk.X)
        header.pack_propagate(False)
        tk.Label(header, text="维修派工管理系统", font=("Microsoft YaHei", 16, "bold"),
                 bg="#2c3e50", fg="white").pack(side=tk.LEFT, padx=20)
        user_info = f"当前用户: {self.current_user.name}  ({role_labels.get(self.current_user.role, '')})"
        tk.Label(header, text=user_info, font=("Microsoft YaHei", 11),
                 bg="#2c3e50", fg="#bdc3c7").pack(side=tk.RIGHT, padx=20)
        tk.Button(header, text="切换用户", font=("Microsoft YaHei", 10),
                  bg="#e74c3c", fg="white", relief=tk.FLAT, width=10,
                  command=self._show_login).pack(side=tk.RIGHT, padx=10)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        perms = {
            Role.DISPATCHER: ["orders", "history", "dispatcher", "schedule", "spare_parts", "reschedule", "import_export"],
            Role.TECHNICIAN: ["orders", "history", "technician", "spare_parts", "reschedule"],
            Role.INSPECTOR: ["orders", "history", "inspector", "import_export"],
        }
        tabs = perms.get(self.current_user.role, [])

        if "orders" in tabs:
            self._build_orders_tab()
        if "history" in tabs:
            self._build_history_tab()
        if "dispatcher" in tabs:
            self._build_dispatcher_tab()
        if "schedule" in tabs:
            self._build_schedule_tab()
        if "technician" in tabs:
            self._build_technician_tab()
        if "inspector" in tabs:
            self._build_inspector_tab()
        if "spare_parts" in tabs:
            self._build_spare_parts_tab()
        if "reschedule" in tabs:
            self._build_reschedule_tab()
        if "import_export" in tabs:
            self._build_import_export_tab()

    def _configure_tree_tags(self, tree):
        tree.tag_configure("priority_high", background="#fdedec")
        tree.tag_configure("priority_mid", background="#fef9e7")
        tree.tag_configure("priority_low", background="#eaf2f8")
        tree.tag_configure("good", background="#eafaf1")
        tree.tag_configure("partial", background="#fef9e7")
        tree.tag_configure("bad", background="#fdedec")

    # ==================== 工单列表 Tab ====================
    def _build_orders_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="工单列表")

        filter_frame = tk.Frame(frame, bg="#f5f6fa")
        filter_frame.pack(fill=tk.X, padx=10, pady=10)

        tk.Label(filter_frame, text="状态:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.filter_status = ttk.Combobox(filter_frame, values=["全部"] + [s.value for s in Status], state="readonly", width=12, font=("Microsoft YaHei", 10))
        self.filter_status.set("全部")
        self.filter_status.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(filter_frame, text="位置:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.filter_location = tk.Entry(filter_frame, width=15, font=("Microsoft YaHei", 10))
        self.filter_location.grid(row=0, column=3, padx=5, pady=5)

        tk.Label(filter_frame, text="类别:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=4, padx=5, pady=5, sticky="e")
        self.filter_category = ttk.Combobox(filter_frame, values=["全部"] + CATEGORY_OPTIONS, state="readonly", width=12, font=("Microsoft YaHei", 10))
        self.filter_category.set("全部")
        self.filter_category.grid(row=0, column=5, padx=5, pady=5)

        tk.Label(filter_frame, text="优先级:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=6, padx=5, pady=5, sticky="e")
        self.filter_priority = ttk.Combobox(filter_frame, values=["全部"] + PRIORITY_OPTIONS, state="readonly", width=8, font=("Microsoft YaHei", 10))
        self.filter_priority.set("全部")
        self.filter_priority.grid(row=0, column=7, padx=5, pady=5)

        tk.Button(filter_frame, text="查询", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=8, command=self._refresh_orders).grid(row=0, column=8, padx=10, pady=5)
        tk.Button(filter_frame, text="重置", font=("Microsoft YaHei", 10), bg="#95a5a6", fg="white",
                  width=8, command=self._reset_filters).grid(row=0, column=9, padx=5, pady=5)

        tree_frame = tk.Frame(frame, bg="#f5f6fa")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("order_id", "title", "location", "category", "priority", "status", "assignee", "scheduled", "creator", "created_at")
        self.orders_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        for c, text, w in [
            ("order_id", "工单编号", 150), ("title", "标题", 180), ("location", "位置", 100),
            ("category", "类别", 90), ("priority", "优先级", 60), ("status", "状态", 80),
            ("assignee", "维修员", 80), ("scheduled", "排程时间", 170), ("creator", "创建人", 70), ("created_at", "创建时间", 140),
        ]:
            self.orders_tree.heading(c, text=text)
            self.orders_tree.column(c, width=w, anchor="center")
        self.orders_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.orders_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.orders_tree.configure(yscrollcommand=sb.set)
        self._configure_tree_tags(self.orders_tree)

        btn_frame = tk.Frame(frame, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        if self.current_user.role == Role.DISPATCHER:
            tk.Button(btn_frame, text="改派", font=("Microsoft YaHei", 10), bg="#e67e22", fg="white",
                      width=12, command=self._on_reassign).pack(side=tk.LEFT, padx=5)
            tk.Button(btn_frame, text="发起改约", font=("Microsoft YaHei", 10), bg="#16a085", fg="white",
                      width=12, command=self._on_create_reschedule).pack(side=tk.LEFT, padx=5)
        if self.current_user.role in (Role.DISPATCHER, Role.TECHNICIAN):
            tk.Button(btn_frame, text="到场确认", font=("Microsoft YaHei", 10), bg="#27ae60", fg="white",
                      width=12, command=self._on_confirm_arrival).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="刷新", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=12, command=self._refresh_orders).pack(side=tk.LEFT, padx=5)

        self._refresh_orders()

    def _reset_filters(self):
        self.filter_status.set("全部")
        self.filter_location.delete(0, tk.END)
        self.filter_category.set("全部")
        self.filter_priority.set("全部")
        self._refresh_orders()

    def _refresh_orders(self):
        for i in self.orders_tree.get_children():
            self.orders_tree.delete(i)
        status_val = self.filter_status.get()
        status = None
        if status_val and status_val != "全部":
            for s in Status:
                if s.value == status_val:
                    status = s
                    break
        location = self.filter_location.get().strip() or None
        category_val = self.filter_category.get()
        category = category_val if category_val and category_val != "全部" else None
        priority_val = self.filter_priority.get()
        priority = priority_val if priority_val and priority_val != "全部" else None

        assignee_id = None
        if self.current_user.role == Role.TECHNICIAN:
            assignee_id = self.current_user.user_id

        try:
            orders = self.store.get_orders_by_filter(status=status, location=location,
                                                      category=category, priority=priority,
                                                      assignee_id=assignee_id)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return

        for o in orders:
            tag = f"priority_{'high' if o.priority == '高' else 'mid' if o.priority == '中' else 'low'}"
            scheduled = ""
            if o.scheduled_start and o.scheduled_end:
                scheduled = f"{o.scheduled_start} ~ {o.scheduled_end.split(' ')[1] if ' ' in o.scheduled_end else o.scheduled_end}"
            self.orders_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.location, o.category, o.priority,
                o.status.value, o.assignee_name or "未指派", scheduled, o.creator_name, o.created_at,
            ), tags=(tag,))

    def _on_reassign(self):
        sel = self.orders_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要改派的工单")
            return
        order_id = sel[0]
        try:
            order = self.store.get_order(order_id)
            if not order:
                messagebox.showerror("错误", "工单不存在")
                return
            allowed, msg = self.store.can_reassign(order, self.current_user)
            if not allowed:
                messagebox.showwarning("无法改派", msg)
                return
            dlg = ReassignDialog(self.root, self.store, self.current_user, order)
            self.root.wait_window(dlg)
            if dlg.result:
                self._refresh_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_create_reschedule(self):
        sel = self.orders_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个工单")
            return
        order_id = sel[0]
        try:
            order = self.store.get_order(order_id)
            if not order:
                messagebox.showerror("错误", "工单不存在")
                return
            dlg = CreateRescheduleDialog(self.root, self.store, self.current_user, order)
            self.root.wait_window(dlg)
            if dlg.result:
                self._refresh_orders()
                if hasattr(self, "reschedule_tree"):
                    self._refresh_reschedules()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))

    def _on_confirm_arrival(self):
        sel = self.orders_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请先选择一个工单")
            return
        order_id = sel[0]
        note = simpledialog.askstring("到场确认", "请输入到场备注（可选）:", parent=self.root)
        if note is None:
            return
        try:
            confirm = self.store.confirm_arrival(order_id, self.current_user, note.strip() or None)
            messagebox.showinfo("成功", f"已记录到场确认\n时间: {confirm.confirmed_at}\n工单: {confirm.order_id}")
            self._refresh_orders()
            if hasattr(self, "arrival_tree"):
                self._refresh_arrivals()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))

    # ==================== 上门改约 Tab ====================
    def _build_reschedule_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="上门改约")

        top = tk.Frame(frame, bg="#f5f6fa")
        top.pack(fill=tk.X, padx=10, pady=8)
        tk.Label(top, text="工单编号:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=0, padx=3)
        self.filter_rs_order = tk.Entry(top, width=16, font=("Microsoft YaHei", 10))
        self.filter_rs_order.grid(row=0, column=1, padx=3)
        tk.Label(top, text="状态:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=2, padx=3)
        self.filter_rs_status = ttk.Combobox(top, values=["全部", "待确认", "已确认", "已拒绝", "已取消", "已过期"],
                                             state="readonly", width=10, font=("Microsoft YaHei", 10))
        self.filter_rs_status.grid(row=0, column=3, padx=3)
        self.filter_rs_status.set("全部")
        tk.Button(top, text="查询", bg="#3498db", fg="white", width=8,
                  font=("Microsoft YaHei", 10), command=self._refresh_reschedules).grid(row=0, column=4, padx=6)
        tk.Button(top, text="重置", bg="#95a5a6", fg="white", width=8,
                  font=("Microsoft YaHei", 10), command=self._reset_reschedule_filters).grid(row=0, column=5, padx=3)

        left_pane = tk.Frame(frame, bg="#f5f6fa")
        left_pane.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(10, 5), pady=5)
        tk.Label(left_pane, text="改约申请列表", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").pack(anchor="w")
        rs_cols = ("reschedule_id", "order_id", "order_title", "reason", "status", "creator", "created_at")
        self.reschedule_tree = ttk.Treeview(left_pane, columns=rs_cols, show="headings", height=10)
        for c, t, w in [("reschedule_id", "改约编号", 150), ("order_id", "工单编号", 140),
                        ("order_title", "工单标题", 160), ("reason", "原因", 180),
                        ("status", "状态", 80), ("creator", "创建人", 80), ("created_at", "创建时间", 150)]:
            self.reschedule_tree.heading(c, text=t)
            self.reschedule_tree.column(c, width=w, anchor="center")
        self.reschedule_tree.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb1 = ttk.Scrollbar(left_pane, orient=tk.VERTICAL, command=self.reschedule_tree.yview)
        sb1.pack(side=tk.RIGHT, fill=tk.Y)
        self.reschedule_tree.configure(yscrollcommand=sb1.set)
        self.reschedule_tree.tag_configure("status待确认", background="#fef9e7")
        self.reschedule_tree.tag_configure("status已确认", background="#eafaf1")
        self.reschedule_tree.tag_configure("status已拒绝", background="#fdedec")
        self.reschedule_tree.tag_configure("status已取消", background="#f4f6f7")
        self.reschedule_tree.tag_configure("status已过期", background="#f4f6f7")
        self.reschedule_tree.bind("<<TreeviewSelect>>", lambda e: self._on_reschedule_select())

        right_pane = tk.Frame(frame, bg="#f5f6fa")
        right_pane.pack(fill=tk.BOTH, expand=True, side=tk.LEFT, padx=(5, 10), pady=5)

        tk.Label(right_pane, text="改约详情与候选时间窗", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").pack(anchor="w")
        self.rs_detail = tk.Text(right_pane, height=8, font=("Microsoft YaHei", 10), state=tk.DISABLED,
                                 bg="white", wrap=tk.WORD)
        self.rs_detail.pack(fill=tk.X, pady=3)

        tk.Label(right_pane, text="确认日志", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").pack(anchor="w", pady=(5, 0))
        log_cols = ("log_id", "confirmer", "decision", "selected_slot", "reject_reason", "confirmed_at")
        self.confirm_log_tree = ttk.Treeview(right_pane, columns=log_cols, show="headings", height=6)
        for c, t, w in [("log_id", "日志ID", 60), ("confirmer", "确认人", 80),
                        ("decision", "决定", 70), ("selected_slot", "选中时间窗", 200),
                        ("reject_reason", "拒绝原因", 160), ("confirmed_at", "确认时间", 150)]:
            self.confirm_log_tree.heading(c, text=t)
            self.confirm_log_tree.column(c, width=w, anchor="center")
        self.confirm_log_tree.pack(fill=tk.BOTH, expand=True)

        btn_frame = tk.Frame(frame, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=10, pady=8)
        if self.current_user.role == Role.DISPATCHER:
            tk.Button(btn_frame, text="撤销改约", bg="#e67e22", fg="white", width=12,
                      font=("Microsoft YaHei", 10), command=self._on_cancel_reschedule).pack(side=tk.LEFT, padx=5)
        if self.current_user.role in (Role.DISPATCHER, Role.TECHNICIAN):
            tk.Button(btn_frame, text="确认/拒绝", bg="#16a085", fg="white", width=12,
                      font=("Microsoft YaHei", 10), command=self._on_confirm_reschedule).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="刷新", bg="#3498db", fg="white", width=10,
                  font=("Microsoft YaHei", 10), command=self._refresh_reschedules).pack(side=tk.LEFT, padx=5)

        sep = tk.Frame(frame, height=2, bg="#bdc3c7")
        sep.pack(fill=tk.X, padx=10, pady=5)

        tk.Label(frame, text="到场确认记录", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=10)
        arrival_cols = ("arrival_id", "order_id", "confirmer", "note", "confirmed_at")
        self.arrival_tree = ttk.Treeview(frame, columns=arrival_cols, show="headings", height=6)
        for c, t, w in [("arrival_id", "确认ID", 80), ("order_id", "工单编号", 150),
                        ("confirmer", "确认人", 80), ("note", "备注", 250),
                        ("confirmed_at", "确认时间", 160)]:
            self.arrival_tree.heading(c, text=t)
            self.arrival_tree.column(c, width=w, anchor="center")
        self.arrival_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        tk.Button(frame, text="刷新到场记录", bg="#3498db", fg="white", width=12,
                  font=("Microsoft YaHei", 10), command=self._refresh_arrivals).pack(anchor="e", padx=10, pady=5)

        self._refresh_reschedules()
        self._refresh_arrivals()

    def _reset_reschedule_filters(self):
        self.filter_rs_order.delete(0, tk.END)
        self.filter_rs_status.set("全部")
        self._refresh_reschedules()

    def _refresh_reschedules(self):
        for i in self.reschedule_tree.get_children():
            self.reschedule_tree.delete(i)
        order_id = self.filter_rs_order.get().strip() or None
        status_val = self.filter_rs_status.get()
        status = None
        if status_val and status_val != "全部":
            for s in RescheduleStatus:
                if s.value == status_val:
                    status = s
                    break
        try:
            reqs = self.store.get_reschedule_requests(order_id=order_id, status=status, viewer=self.current_user)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))
            return
        for r in reqs:
            self.reschedule_tree.insert("", tk.END, iid=r.reschedule_id, values=(
                r.reschedule_id, r.order_id, r.order_title, r.reason, r.status.value,
                r.dispatcher_name, r.created_at,
            ), tags=(f"status{r.status.value}",))

    def _on_reschedule_select(self):
        sel = self.reschedule_tree.selection()
        self.rs_detail.configure(state=tk.NORMAL)
        self.rs_detail.delete("1.0", tk.END)
        for i in self.confirm_log_tree.get_children():
            self.confirm_log_tree.delete(i)
        if not sel:
            self.rs_detail.configure(state=tk.DISABLED)
            return
        reschedule_id = sel[0]
        try:
            req = self.store.get_reschedule_request(reschedule_id)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))
            self.rs_detail.configure(state=tk.DISABLED)
            return
        if not req:
            self.rs_detail.configure(state=tk.DISABLED)
            return
        info = f"改约编号: {req.reschedule_id}\n工单: {req.order_id}  {req.order_title}\n"
        info += f"创建人: {req.dispatcher_name}    创建时间: {req.created_at}\n"
        info += f"原因: {req.reason}\n"
        if req.note:
            info += f"备注: {req.note}\n"
        info += f"原排程: {req.original_scheduled_start or '(无)'} ~ {req.original_scheduled_end or '(无)'}\n"
        info += f"当前状态: {req.status.value}    版本: v{req.version}\n"
        info += "候选时间窗:\n"
        for i, slot in enumerate(req.candidate_slots):
            info += f"  [{i+1}] {slot.start_time} ~ {slot.end_time}\n"
        self.rs_detail.insert("1.0", info)
        self.rs_detail.configure(state=tk.DISABLED)
        try:
            logs = self.store.get_reschedule_confirm_logs(reschedule_id=req.reschedule_id)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))
            return
        for log in logs:
            self.confirm_log_tree.insert("", tk.END, values=(
                log.log_id, log.confirmer_name, log.decision,
                f"{log.selected_slot_start or ''} ~ {log.selected_slot_end or ''}".strip(" ~"),
                log.reject_reason or "", log.confirmed_at,
            ))

    def _on_cancel_reschedule(self):
        sel = self.reschedule_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要撤销的改约申请")
            return
        reschedule_id = sel[0]
        if not messagebox.askyesno("确认撤销", f"确认要撤销改约申请 {reschedule_id}?"):
            return
        try:
            req = self.store.cancel_reschedule_request(reschedule_id, self.current_user)
            messagebox.showinfo("成功", f"已撤销: {req.reschedule_id}")
            self._refresh_reschedules()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))

    def _on_confirm_reschedule(self):
        sel = self.reschedule_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要处理的改约申请")
            return
        reschedule_id = sel[0]
        try:
            req = self.store.get_reschedule_request(reschedule_id)
            if not req:
                messagebox.showerror("错误", "改约申请不存在")
                return
            dlg = ConfirmRescheduleDialog(self.root, self.store, self.current_user, req)
            self.root.wait_window(dlg)
            if dlg.result:
                self._refresh_reschedules()
                self._on_reschedule_select()
                self._refresh_orders()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))

    def _refresh_arrivals(self):
        if not hasattr(self, "arrival_tree"):
            return
        for i in self.arrival_tree.get_children():
            self.arrival_tree.delete(i)
        try:
            items = self.store.get_arrival_confirmations(viewer=self.current_user)
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("错误", str(e))
            return
        for a in items:
            self.arrival_tree.insert("", tk.END, values=(
                a.arrival_id, a.order_id, a.confirmer_name, a.note or "", a.confirmed_at,
            ))

    # ==================== 历史记录 Tab ====================
    def _build_history_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="历史记录")

        top_frame = tk.Frame(frame, bg="#f5f6fa")
        top_frame.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(top_frame, text="选择工单:", font=("Microsoft YaHei", 10), bg="#f5f6fa").pack(side=tk.LEFT, padx=5)
        self.history_order_combo = ttk.Combobox(top_frame, state="readonly", width=40, font=("Microsoft YaHei", 10))
        self.history_order_combo.pack(side=tk.LEFT, padx=5)
        self.history_order_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_history())
        tk.Button(top_frame, text="刷新工单列表", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=12, command=self._refresh_history_order_list).pack(side=tk.LEFT, padx=10)

        content = tk.Frame(frame, bg="#f5f6fa")
        content.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        tk.Label(content, text="状态变更历史", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").grid(row=0, column=0, sticky="w", pady=(0, 5))
        h_frame = tk.Frame(content, bg="#f5f6fa")
        h_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        h_cols = ("status", "user", "timestamp", "note")
        self.history_tree = ttk.Treeview(h_frame, columns=h_cols, show="headings", height=12)
        self.history_tree.heading("status", text="状态")
        self.history_tree.heading("user", text="操作人")
        self.history_tree.heading("timestamp", text="时间")
        self.history_tree.heading("note", text="备注")
        self.history_tree.column("status", width=100, anchor="center")
        self.history_tree.column("user", width=100, anchor="center")
        self.history_tree.column("timestamp", width=150, anchor="center")
        self.history_tree.column("note", width=280, anchor="w")
        self.history_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        h_sb = ttk.Scrollbar(h_frame, orient="vertical", command=self.history_tree.yview)
        h_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.history_tree.configure(yscrollcommand=h_sb.set)

        tk.Label(content, text="改派记录", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").grid(row=0, column=1, sticky="w", pady=(0, 5))
        r_frame = tk.Frame(content, bg="#f5f6fa")
        r_frame.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        r_cols = ("from_user", "to_user", "reason", "dispatcher", "timestamp")
        self.reassign_tree = ttk.Treeview(r_frame, columns=r_cols, show="headings", height=12)
        self.reassign_tree.heading("from_user", text="原维修员")
        self.reassign_tree.heading("to_user", text="新维修员")
        self.reassign_tree.heading("reason", text="原因")
        self.reassign_tree.heading("dispatcher", text="调度员")
        self.reassign_tree.heading("timestamp", text="时间")
        self.reassign_tree.column("from_user", width=90, anchor="center")
        self.reassign_tree.column("to_user", width=90, anchor="center")
        self.reassign_tree.column("reason", width=140, anchor="w")
        self.reassign_tree.column("dispatcher", width=80, anchor="center")
        self.reassign_tree.column("timestamp", width=140, anchor="center")
        self.reassign_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        r_sb = ttk.Scrollbar(r_frame, orient="vertical", command=self.reassign_tree.yview)
        r_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.reassign_tree.configure(yscrollcommand=r_sb.set)

        note_frame = tk.Frame(frame, bg="#f5f6fa")
        note_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        tk.Label(note_frame, text="异常备注", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").pack(anchor="w", pady=(0, 5))
        note_text_frame = tk.Frame(note_frame, bg="#f5f6fa")
        note_text_frame.pack(fill=tk.BOTH, expand=True)
        self.exception_notes_text = tk.Text(note_text_frame, height=8, font=("Microsoft YaHei", 10),
                                            state=tk.DISABLED, wrap=tk.WORD)
        self.exception_notes_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        n_sb = ttk.Scrollbar(note_text_frame, orient="vertical", command=self.exception_notes_text.yview)
        n_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.exception_notes_text.configure(yscrollcommand=n_sb.set)

        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=1)
        content.grid_rowconfigure(1, weight=1)

        self._refresh_history_order_list()

    def _refresh_history_order_list(self):
        try:
            orders = self.store.get_all_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        values = [f"{o.order_id} - {o.title}" for o in orders]
        self.history_order_combo["values"] = values
        if values:
            self.history_order_combo.current(0)
            self._refresh_history()

    def _refresh_history(self):
        for i in self.history_tree.get_children():
            self.history_tree.delete(i)
        for i in self.reassign_tree.get_children():
            self.reassign_tree.delete(i)
        self.exception_notes_text.configure(state=tk.NORMAL)
        self.exception_notes_text.delete("1.0", tk.END)

        sel = self.history_order_combo.get()
        if not sel:
            self.exception_notes_text.configure(state=tk.DISABLED)
            return
        order_id = sel.split(" - ")[0]
        try:
            order = self.store.get_order(order_id)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        if not order:
            self.exception_notes_text.configure(state=tk.DISABLED)
            return

        for h in order.history:
            self.history_tree.insert("", tk.END, values=(h.status.value, h.user_name, h.timestamp, h.note))

        for r in order.reassignment_logs:
            self.reassign_tree.insert("", tk.END, values=(
                r.from_user_name, r.to_user_name, r.reason, r.dispatcher_name, r.timestamp
            ))

        for note in order.exception_notes:
            self.exception_notes_text.insert(tk.END, note + "\n\n")
        self.exception_notes_text.configure(state=tk.DISABLED)

    # ==================== 调度员 Tab ====================
    def _build_dispatcher_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="调度派工")

        left = tk.Frame(frame, bg="#f5f6fa")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=10, pady=10)

        tk.Label(left, text="创建新工单", font=("Microsoft YaHei", 13, "bold"),
                 bg="#f5f6fa").pack(anchor="w", pady=(0, 10))

        form = tk.Frame(left, bg="#f5f6fa")
        form.pack(anchor="w")
        fields = [
            ("标题*", "entry"), ("描述", "text"), ("位置*", "entry"),
            ("类别*", "combo"), ("优先级*", "combo"),
        ]
        self.create_vars = {}
        for i, (label, ftype) in enumerate(fields):
            tk.Label(form, text=label, font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=i, column=0, sticky="e", pady=6, padx=5)
            if ftype == "entry":
                var = tk.StringVar()
                self.create_vars[label] = var
                tk.Entry(form, width=28, textvariable=var, font=("Microsoft YaHei", 10)).grid(row=i, column=1, pady=6)
            elif ftype == "text":
                self.create_vars[label] = None
                txt = tk.Text(form, width=28, height=4, font=("Microsoft YaHei", 10))
                txt.grid(row=i, column=1, pady=6)
                self.create_vars["描述_widget"] = txt
            elif ftype == "combo":
                var = tk.StringVar()
                self.create_vars[label] = var
                vals = CATEGORY_OPTIONS if label.startswith("类别") else PRIORITY_OPTIONS
                cb = ttk.Combobox(form, values=vals, textvariable=var, state="readonly", width=26, font=("Microsoft YaHei", 10))
                cb.grid(row=i, column=1, pady=6)
                if label.startswith("优先级"):
                    var.set("中")

        tk.Button(left, text="创建工单", font=("Microsoft YaHei", 11, "bold"), bg="#27ae60", fg="white",
                  width=25, command=self._on_create_order).pack(anchor="w", pady=15)

        right = tk.Frame(frame, bg="#f5f6fa")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        tk.Label(right, text="待派工单列表（选择后查看匹配度并派工）", font=("Microsoft YaHei", 13, "bold"),
                 bg="#f5f6fa").pack(anchor="w", pady=(0, 8))

        order_list_frame = tk.Frame(right, bg="#f5f6fa")
        order_list_frame.pack(fill=tk.X, pady=(0, 8))
        d_cols = ("order_id", "title", "category", "priority", "location")
        self.dispatch_order_tree = ttk.Treeview(order_list_frame, columns=d_cols, show="headings", height=8)
        for c, text, w in [("order_id", "工单编号", 150), ("title", "标题", 200),
                            ("category", "类别", 90), ("priority", "优先级", 70), ("location", "位置", 120)]:
            self.dispatch_order_tree.heading(c, text=text)
            self.dispatch_order_tree.column(c, width=w, anchor="center")
        self.dispatch_order_tree.pack(side=tk.LEFT, fill=tk.X, expand=True)
        d_sb = ttk.Scrollbar(order_list_frame, orient="vertical", command=self.dispatch_order_tree.yview)
        d_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.dispatch_order_tree.configure(yscrollcommand=d_sb.set)
        self.dispatch_order_tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_dispatch_matches())
        self._configure_tree_tags(self.dispatch_order_tree)

        tk.Label(right, text="维修员匹配度（按分数排序）", font=("Microsoft YaHei", 12, "bold"),
                 bg="#f5f6fa").pack(anchor="w", pady=(10, 5))
        match_frame = tk.Frame(right, bg="#f5f6fa")
        match_frame.pack(fill=tk.BOTH, expand=True)
        m_cols = ("tech_id", "name", "score", "skill", "available", "capacity", "warnings")
        self.match_tree = ttk.Treeview(match_frame, columns=m_cols, show="headings")
        for c, text, w in [("tech_id", "工号", 60), ("name", "姓名", 70), ("score", "匹配分", 65),
                            ("skill", "技能", 55), ("available", "时间", 55), ("capacity", "负载", 70),
                            ("warnings", "备注", 250)]:
            self.match_tree.heading(c, text=text)
            self.match_tree.column(c, width=w, anchor="center")
        self.match_tree.column("warnings", anchor="w")
        self.match_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        m_sb = ttk.Scrollbar(match_frame, orient="vertical", command=self.match_tree.yview)
        m_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.match_tree.configure(yscrollcommand=m_sb.set)
        self._configure_tree_tags(self.match_tree)

        btn_frame = tk.Frame(right, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, pady=10)
        tk.Button(btn_frame, text="派工给选中维修员", font=("Microsoft YaHei", 11, "bold"),
                  bg="#3498db", fg="white", width=18, command=self._on_dispatch).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="批量改派预案", font=("Microsoft YaHei", 11, "bold"),
                  bg="#e67e22", fg="white", width=15, command=self._on_batch_reassign).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="恢复批量草稿", font=("Microsoft YaHei", 10),
                  bg="#8e44ad", fg="white", width=12, command=self._on_restore_batch_draft).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="刷新列表", font=("Microsoft YaHei", 10),
                  bg="#95a5a6", fg="white", width=12, command=self._refresh_dispatch_orders).pack(side=tk.LEFT, padx=5)

        self._refresh_dispatch_orders()

    def _refresh_dispatch_orders(self):
        for i in self.dispatch_order_tree.get_children():
            self.dispatch_order_tree.delete(i)
        try:
            orders = self.store.get_orders_by_filter(status=Status.PENDING_DISPATCH)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        for o in orders:
            tag = f"priority_{'high' if o.priority == '高' else 'mid' if o.priority == '中' else 'low'}"
            self.dispatch_order_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.category, o.priority, o.location
            ), tags=(tag,))
        self._refresh_dispatch_matches()

    def _refresh_dispatch_matches(self):
        for i in self.match_tree.get_children():
            self.match_tree.delete(i)
        sel = self.dispatch_order_tree.selection()
        if not sel:
            return
        order_id = sel[0]
        try:
            order = self.store.get_order(order_id)
            if not order:
                return
            ranked = self.store.rank_technicians_for_order(order)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        for tech, match in ranked:
            score = match.score
            if score >= 80:
                tag = "good"
            elif score >= 50:
                tag = "partial"
            else:
                tag = "bad"
            self.match_tree.insert("", tk.END, iid=tech.user_id, values=(
                tech.user_id, tech.name, score,
                "是" if match.skill_match else "否",
                "是" if match.available_now else "否",
                f"{match.current_load}/{match.max_parallel}",
                "; ".join(match.warnings) if match.warnings else "推荐",
            ), tags=(tag,))

    def _on_create_order(self):
        title = self.create_vars["标题*"].get().strip()
        desc_widget = self.create_vars["描述_widget"]
        description = desc_widget.get("1.0", tk.END).strip()
        location = self.create_vars["位置*"].get().strip()
        category = self.create_vars["类别*"].get()
        priority = self.create_vars["优先级*"].get()

        if not title:
            messagebox.showwarning("提示", "请填写标题")
            return
        if not location:
            messagebox.showwarning("提示", "请填写位置")
            return
        if not category:
            messagebox.showwarning("提示", "请选择类别")
            return
        if not priority:
            messagebox.showwarning("提示", "请选择优先级")
            return

        try:
            self.store.create_order(title, description, location, category, priority, self.current_user)
            messagebox.showinfo("成功", "工单创建成功")
            self.create_vars["标题*"].set("")
            desc_widget.delete("1.0", tk.END)
            self.create_vars["位置*"].set("")
            self.create_vars["类别*"].set("")
            self.create_vars["优先级*"].set("中")
            self._refresh_dispatch_orders()
            self._refresh_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_dispatch(self):
        order_sel = self.dispatch_order_tree.selection()
        if not order_sel:
            messagebox.showwarning("提示", "请选择待派工单")
            return
        tech_sel = self.match_tree.selection()
        if not tech_sel:
            messagebox.showwarning("提示", "请选择维修员")
            return
        order_id = order_sel[0]
        tech_id = tech_sel[0]
        try:
            order = self.store.get_order(order_id)
            tech = self.store.get_user(tech_id)
            if not order or not tech:
                messagebox.showerror("错误", "工单或维修员不存在")
                return
            self.store.dispatch_order(order_id, tech, self.current_user)
            messagebox.showinfo("成功", f"已派工给 {tech.name}")
            self._refresh_dispatch_orders()
            self._refresh_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_batch_reassign(self):
        try:
            dlg = BatchReassignDialog(self.root, self.store, self.current_user)
            self.root.wait_window(dlg)
            self._refresh_all_tabs()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_restore_batch_draft(self):
        try:
            drafts = self.store.get_batch_drafts_by_dispatcher(self.current_user)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        if not drafts:
            messagebox.showinfo("提示", "您暂未保存任何批量改派预案草稿")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("恢复批量改派预案草稿")
        dlg.geometry("560x380")
        dlg.configure(bg="#f5f6fa")
        dlg.grab_set()
        dlg.transient(self.root)
        tk.Label(dlg, text="选择要恢复的预案草稿（按更新时间倒序）:",
                 font=("Microsoft YaHei", 11, "bold"), bg="#f5f6fa").pack(anchor="w", padx=15, pady=(15, 5))
        cols = ("draft_id", "item_count", "created_at", "updated_at")
        tree = ttk.Treeview(dlg, columns=cols, show="headings", height=10)
        for c, text, w in [("draft_id", "草稿编号", 180), ("item_count", "条目数", 70),
                            ("created_at", "创建时间", 150), ("updated_at", "更新时间", 150)]:
            tree.heading(c, text=text)
            tree.column(c, width=w, anchor="center")
        tree.pack(fill=tk.BOTH, expand=True, padx=15, pady=5)
        drafts_sorted = sorted(drafts, key=lambda d: d.updated_at, reverse=True)
        for d in drafts_sorted:
            tree.insert("", tk.END, iid=d.draft_id, values=(
                d.draft_id, len(d.items), d.created_at, d.updated_at,
            ))
        btn_frame = tk.Frame(dlg, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=15, pady=12)

        def _on_restore():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请选择要恢复的草稿", parent=dlg)
                return
            draft_id = sel[0]
            draft = self.store.get_batch_reassignment_draft(draft_id, self.current_user)
            if draft is None:
                messagebox.showerror("错误", "草稿不存在或不属于您", parent=dlg)
                return
            dlg.destroy()
            try:
                batch_dlg = BatchReassignDialog(self.root, self.store, self.current_user, draft)
                self.root.wait_window(batch_dlg)
                self._refresh_all_tabs()
            except WorkOrderError as e:
                messagebox.showerror("错误", str(e))

        def _on_delete():
            sel = tree.selection()
            if not sel:
                messagebox.showwarning("提示", "请选择要删除的草稿", parent=dlg)
                return
            draft_id = sel[0]
            if not messagebox.askyesno("确认", f"确定删除草稿 {draft_id}？", parent=dlg):
                return
            deleted = self.store.delete_batch_reassignment_draft(draft_id, self.current_user)
            if deleted:
                tree.delete(draft_id)
                messagebox.showinfo("成功", "草稿已删除", parent=dlg)
            else:
                messagebox.showerror("错误", "删除失败", parent=dlg)

        tk.Button(btn_frame, text="取消", font=("Microsoft YaHei", 10),
                  bg="#7f8c8d", fg="white", width=10,
                  command=dlg.destroy).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="删除选中", font=("Microsoft YaHei", 10),
                  bg="#e74c3c", fg="white", width=10,
                  command=_on_delete).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="恢复选中", font=("Microsoft YaHei", 10, "bold"),
                  bg="#3498db", fg="white", width=10,
                  command=_on_restore).pack(side=tk.RIGHT, padx=5)

    # ==================== 排班管理 Tab ====================
    def _build_schedule_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="排班管理")

        top = tk.Frame(frame, bg="#f5f6fa")
        top.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(top, text="选择维修员:", font=("Microsoft YaHei", 11), bg="#f5f6fa").pack(side=tk.LEFT, padx=5)
        self.schedule_tech_combo = ttk.Combobox(top, state="readonly", width=30, font=("Microsoft YaHei", 10))
        self.schedule_tech_combo.pack(side=tk.LEFT, padx=5)
        self.schedule_tech_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_schedule())
        tk.Button(top, text="刷新", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=10, command=self._refresh_schedule_tech_list).pack(side=tk.LEFT, padx=10)

        content = tk.Frame(frame, bg="#f5f6fa")
        content.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        info_frame = tk.LabelFrame(content, text="维修员信息", font=("Microsoft YaHei", 11, "bold"),
                                    bg="#f5f6fa", fg="#2c3e50")
        info_frame.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.schedule_info_label = tk.Label(info_frame, text="", font=("Microsoft YaHei", 10),
                                             bg="#f5f6fa", justify=tk.LEFT)
        self.schedule_info_label.pack(anchor="w", padx=10, pady=10)

        skills_frame = tk.LabelFrame(content, text="技能管理", font=("Microsoft YaHei", 11, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        skills_frame.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        sk_top = tk.Frame(skills_frame, bg="#f5f6fa")
        sk_top.pack(fill=tk.X, padx=8, pady=8)
        self.skills_listbox = tk.Listbox(sk_top, height=8, font=("Microsoft YaHei", 10))
        self.skills_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sk_btns = tk.Frame(sk_top, bg="#f5f6fa")
        sk_btns.pack(side=tk.LEFT, padx=8, fill=tk.Y)
        tk.Label(sk_btns, text="添加技能:", font=("Microsoft YaHei", 9), bg="#f5f6fa").pack(anchor="w")
        self.new_skill_combo = ttk.Combobox(sk_btns, values=SKILL_OPTIONS, width=10, font=("Microsoft YaHei", 9))
        self.new_skill_combo.pack(anchor="w", pady=2)
        tk.Button(sk_btns, text="添加", font=("Microsoft YaHei", 9), bg="#27ae60", fg="white",
                  width=10, command=self._on_add_skill).pack(anchor="w", pady=2)
        tk.Button(sk_btns, text="移除选中", font=("Microsoft YaHei", 9), bg="#e74c3c", fg="white",
                  width=10, command=self._on_remove_skill).pack(anchor="w", pady=2)
        tk.Button(sk_btns, text="保存技能", font=("Microsoft YaHei", 9, "bold"), bg="#3498db", fg="white",
                  width=10, command=self._on_save_skills).pack(anchor="w", pady=8)

        parallel_frame = tk.LabelFrame(content, text="最大并行工单", font=("Microsoft YaHei", 11, "bold"),
                                        bg="#f5f6fa", fg="#2c3e50")
        parallel_frame.grid(row=2, column=0, sticky="nsew", padx=5, pady=5)
        p_frame = tk.Frame(parallel_frame, bg="#f5f6fa")
        p_frame.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(p_frame, text="最大并行数:", font=("Microsoft YaHei", 10), bg="#f5f6fa").pack(side=tk.LEFT, padx=5)
        self.max_parallel_spin = tk.Spinbox(p_frame, from_=1, to=20, width=6, font=("Microsoft YaHei", 11))
        self.max_parallel_spin.pack(side=tk.LEFT, padx=5)
        tk.Button(p_frame, text="保存设置", font=("Microsoft YaHei", 10, "bold"), bg="#3498db", fg="white",
                  width=12, command=self._on_save_max_parallel).pack(side=tk.LEFT, padx=15)

        slots_frame = tk.LabelFrame(content, text="工作时段管理", font=("Microsoft YaHei", 11, "bold"),
                                     bg="#f5f6fa", fg="#2c3e50")
        slots_frame.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=5, pady=5)

        sl_top = tk.Frame(slots_frame, bg="#f5f6fa")
        sl_top.pack(fill=tk.X, padx=8, pady=8)
        self.slots_listbox = tk.Listbox(sl_top, height=12, font=("Microsoft YaHei", 10))
        self.slots_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        add_frame = tk.Frame(slots_frame, bg="#f5f6fa")
        add_frame.pack(fill=tk.X, padx=8, pady=5)
        tk.Label(add_frame, text="星期:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=0, padx=3, pady=4, sticky="e")
        self.slot_day = ttk.Combobox(add_frame, values=DAY_OPTIONS, state="readonly", width=8, font=("Microsoft YaHei", 9))
        self.slot_day.grid(row=0, column=1, padx=3, pady=4)
        tk.Label(add_frame, text="开始:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=2, padx=3, pady=4, sticky="e")
        self.slot_start = tk.Entry(add_frame, width=6, font=("Microsoft YaHei", 10))
        self.slot_start.insert(0, "09:00")
        self.slot_start.grid(row=0, column=3, padx=3, pady=4)
        tk.Label(add_frame, text="结束:", font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=0, column=4, padx=3, pady=4, sticky="e")
        self.slot_end = tk.Entry(add_frame, width=6, font=("Microsoft YaHei", 10))
        self.slot_end.insert(0, "18:00")
        self.slot_end.grid(row=0, column=5, padx=3, pady=4)

        sl_btn_frame = tk.Frame(slots_frame, bg="#f5f6fa")
        sl_btn_frame.pack(fill=tk.X, padx=8, pady=8)
        tk.Button(sl_btn_frame, text="添加时段", font=("Microsoft YaHei", 9), bg="#27ae60", fg="white",
                  width=10, command=self._on_add_slot).pack(side=tk.LEFT, padx=3)
        tk.Button(sl_btn_frame, text="移除选中", font=("Microsoft YaHei", 9), bg="#e74c3c", fg="white",
                  width=10, command=self._on_remove_slot).pack(side=tk.LEFT, padx=3)
        tk.Button(sl_btn_frame, text="清空全部", font=("Microsoft YaHei", 9), bg="#95a5a6", fg="white",
                  width=10, command=self._on_clear_slots).pack(side=tk.LEFT, padx=3)
        tk.Button(sl_btn_frame, text="保存排班", font=("Microsoft YaHei", 9, "bold"), bg="#3498db", fg="white",
                  width=10, command=self._on_save_slots).pack(side=tk.LEFT, padx=3)

        self._temp_skills = []
        self._temp_slots = []

        content.grid_columnconfigure(0, weight=1)
        content.grid_columnconfigure(1, weight=2)
        content.grid_rowconfigure(0, weight=0)
        content.grid_rowconfigure(1, weight=1)
        content.grid_rowconfigure(2, weight=0)

        self._refresh_schedule_tech_list()

    def _refresh_schedule_tech_list(self):
        try:
            techs = self.store.get_users_by_role(Role.TECHNICIAN)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        values = [f"{t.user_id} - {t.name}" for t in techs]
        self.schedule_tech_combo["values"] = values
        if values:
            self.schedule_tech_combo.current(0)
            self._refresh_schedule()

    def _get_selected_tech(self):
        sel = self.schedule_tech_combo.get()
        if not sel:
            return None
        tech_id = sel.split(" - ")[0]
        return self.store.get_user(tech_id)

    def _refresh_schedule(self):
        tech = self._get_selected_tech()
        if not tech:
            return
        try:
            sched = self.store.get_technician_schedule(tech.user_id)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        self.schedule_info_label.configure(
            text=f"姓名: {tech.name}\n工号: {tech.user_id}\n"
                 f"当前负载: {sched['current_load']} / {sched['max_parallel_orders']}"
        )
        self.max_parallel_spin.delete(0, tk.END)
        self.max_parallel_spin.insert(0, str(sched["max_parallel_orders"]))

        self._temp_skills = list(sched["skills"])
        self._temp_slots = [TimeSlot.from_dict(d) for d in sched["time_slots"]]
        self._refresh_skills_listbox()
        self._refresh_slots_listbox()

    def _refresh_skills_listbox(self):
        self.skills_listbox.delete(0, tk.END)
        for s in self._temp_skills:
            self.skills_listbox.insert(tk.END, s)

    def _refresh_slots_listbox(self):
        self.slots_listbox.delete(0, tk.END)
        for ts in self._temp_slots:
            self.slots_listbox.insert(tk.END, repr(ts))

    def _on_add_skill(self):
        s = self.new_skill_combo.get().strip()
        if not s:
            messagebox.showwarning("提示", "请选择或输入技能")
            return
        if s in self._temp_skills:
            messagebox.showwarning("提示", "该技能已存在")
            return
        self._temp_skills.append(s)
        self._refresh_skills_listbox()
        self.new_skill_combo.set("")

    def _on_remove_skill(self):
        sel = self.skills_listbox.curselection()
        if not sel:
            messagebox.showwarning("提示", "请选择要移除的技能")
            return
        del self._temp_skills[sel[0]]
        self._refresh_skills_listbox()

    def _on_save_skills(self):
        tech = self._get_selected_tech()
        if not tech:
            return
        try:
            self.store.set_technician_skills(tech.user_id, self._temp_skills, self.current_user)
            messagebox.showinfo("成功", "技能已保存")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_save_max_parallel(self):
        tech = self._get_selected_tech()
        if not tech:
            return
        try:
            val = int(self.max_parallel_spin.get())
            self.store.set_technician_max_parallel(tech.user_id, val, self.current_user)
            messagebox.showinfo("成功", "最大并行数已保存")
            self._refresh_schedule()
        except ValueError:
            messagebox.showwarning("提示", "请输入有效数字")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_add_slot(self):
        day_str = self.slot_day.get()
        start = self.slot_start.get().strip()
        end = self.slot_end.get().strip()
        if not day_str:
            messagebox.showwarning("提示", "请选择星期")
            return
        day_idx = DAY_MAP[day_str]
        slot = TimeSlot(day_idx, start, end)
        if not slot.is_valid():
            messagebox.showwarning("提示", "时段无效，请检查时间格式（HH:MM）和先后顺序")
            return
        for s in self._temp_slots:
            if s == slot:
                messagebox.showwarning("提示", "该时段已存在")
                return
        self._temp_slots.append(slot)
        self._refresh_slots_listbox()

    def _on_remove_slot(self):
        sel = self.slots_listbox.curselection()
        if not sel:
            messagebox.showwarning("提示", "请选择要移除的时段")
            return
        del self._temp_slots[sel[0]]
        self._refresh_slots_listbox()

    def _on_clear_slots(self):
        if messagebox.askyesno("确认", "确定清空所有时段？"):
            self._temp_slots = []
            self._refresh_slots_listbox()

    def _on_save_slots(self):
        tech = self._get_selected_tech()
        if not tech:
            return
        try:
            self.store.set_technician_time_slots(tech.user_id, self._temp_slots, self.current_user)
            messagebox.showinfo("成功", "排班已保存")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    # ==================== 维修员 Tab ====================
    def _build_technician_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="维修员工作台")

        info_frame = tk.LabelFrame(frame, text="个人信息", font=("Microsoft YaHei", 11, "bold"),
                                    bg="#f5f6fa", fg="#2c3e50")
        info_frame.pack(fill=tk.X, padx=10, pady=10)
        self.tech_info_label = tk.Label(info_frame, text="", font=("Microsoft YaHei", 10),
                                         bg="#f5f6fa", justify=tk.LEFT)
        self.tech_info_label.pack(anchor="w", padx=15, pady=10)

        dispatched_frame = tk.LabelFrame(frame, text="待接单（已派工给我）", font=("Microsoft YaHei", 11, "bold"),
                                          bg="#f5f6fa", fg="#2c3e50")
        dispatched_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        d_cols = ("order_id", "title", "category", "priority", "location", "created_at")
        self.tech_dispatched_tree = ttk.Treeview(dispatched_frame, columns=d_cols, show="headings", height=6)
        for c, text, w in [("order_id", "工单编号", 150), ("title", "标题", 200), ("category", "类别", 90),
                            ("priority", "优先级", 70), ("location", "位置", 140), ("created_at", "派工时间", 150)]:
            self.tech_dispatched_tree.heading(c, text=text)
            self.tech_dispatched_tree.column(c, width=w, anchor="center")
        self.tech_dispatched_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        d_sb = ttk.Scrollbar(dispatched_frame, orient="vertical", command=self.tech_dispatched_tree.yview)
        d_sb.pack(side=tk.RIGHT, fill=tk.Y, pady=5)
        self.tech_dispatched_tree.configure(yscrollcommand=d_sb.set)
        self._configure_tree_tags(self.tech_dispatched_tree)

        dispatched_btn = tk.Frame(dispatched_frame, bg="#f5f6fa")
        dispatched_btn.pack(fill=tk.X, padx=5, pady=5)
        tk.Button(dispatched_btn, text="接单处理", font=("Microsoft YaHei", 11, "bold"),
                  bg="#27ae60", fg="white", width=15, command=self._on_tech_accept).pack(side=tk.LEFT, padx=5)
        tk.Button(dispatched_btn, text="刷新", font=("Microsoft YaHei", 10),
                  bg="#95a5a6", fg="white", width=10, command=self._refresh_tech_tab).pack(side=tk.LEFT, padx=5)

        inprog_frame = tk.LabelFrame(frame, text="处理中（我的工单）", font=("Microsoft YaHei", 11, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        inprog_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        i_cols = ("order_id", "title", "category", "priority", "location", "started_at")
        self.tech_inprog_tree = ttk.Treeview(inprog_frame, columns=i_cols, show="headings", height=6)
        for c, text, w in [("order_id", "工单编号", 150), ("title", "标题", 200), ("category", "类别", 90),
                            ("priority", "优先级", 70), ("location", "位置", 140), ("started_at", "开始时间", 150)]:
            self.tech_inprog_tree.heading(c, text=text)
            self.tech_inprog_tree.column(c, width=w, anchor="center")
        self.tech_inprog_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5, pady=5)
        i_sb = ttk.Scrollbar(inprog_frame, orient="vertical", command=self.tech_inprog_tree.yview)
        i_sb.pack(side=tk.RIGHT, fill=tk.Y, pady=5)
        self.tech_inprog_tree.configure(yscrollcommand=i_sb.set)
        self._configure_tree_tags(self.tech_inprog_tree)

        inprog_btn = tk.Frame(inprog_frame, bg="#f5f6fa")
        inprog_btn.pack(fill=tk.X, padx=5, pady=5)
        tk.Button(inprog_btn, text="完工申请验收", font=("Microsoft YaHei", 11, "bold"),
                  bg="#3498db", fg="white", width=15, command=self._on_tech_complete).pack(side=tk.LEFT, padx=5)

        self._refresh_tech_tab()

    def _refresh_tech_tab(self):
        try:
            sched = self.store.get_technician_schedule(self.current_user.user_id)
            tech = self.current_user
            slots_str = "\n".join(repr(ts) for ts in tech.time_slots) if tech.time_slots else "(未设置，全天可接单)"
            self.tech_info_label.configure(
                text=f"姓名: {tech.name}    工号: {tech.user_id}\n"
                     f"技能: {', '.join(sched['skills']) if sched['skills'] else '(无)'}\n"
                     f"当前负载: {sched['current_load']} / {sched['max_parallel_orders']}\n"
                     f"工作时段:\n{slots_str}"
            )
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

        for i in self.tech_dispatched_tree.get_children():
            self.tech_dispatched_tree.delete(i)
        for i in self.tech_inprog_tree.get_children():
            self.tech_inprog_tree.delete(i)

        try:
            dispatched = self.store.get_orders_by_filter(status=Status.DISPATCHED,
                                                          assignee_id=self.current_user.user_id)
            inprog = self.store.get_orders_by_filter(status=Status.IN_PROGRESS,
                                                      assignee_id=self.current_user.user_id)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return

        for o in dispatched:
            tag = f"priority_{'high' if o.priority == '高' else 'mid' if o.priority == '中' else 'low'}"
            started = ""
            for h in o.history:
                if h.status == Status.DISPATCHED:
                    started = h.timestamp
                    break
            self.tech_dispatched_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.category, o.priority, o.location, started
            ), tags=(tag,))

        for o in inprog:
            tag = f"priority_{'high' if o.priority == '高' else 'mid' if o.priority == '中' else 'low'}"
            started = ""
            for h in o.history:
                if h.status == Status.IN_PROGRESS:
                    started = h.timestamp
                    break
            self.tech_inprog_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.category, o.priority, o.location, started
            ), tags=(tag,))

    def _on_tech_accept(self):
        sel = self.tech_dispatched_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要接单的工单")
            return
        order_id = sel[0]
        try:
            self.store.accept_order(order_id, self.current_user)
            messagebox.showinfo("成功", "已接单")
            self._refresh_tech_tab()
            self._refresh_orders()
        except ConcurrentOperationError as e:
            messagebox.showerror("抢单失败", str(e))
            self._refresh_tech_tab()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_tech_complete(self):
        sel = self.tech_inprog_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要完工的工单")
            return
        order_id = sel[0]
        try:
            self.store.complete_order(order_id, self.current_user)
            messagebox.showinfo("成功", "已提交验收")
            self._refresh_tech_tab()
            self._refresh_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    # ==================== 验收员 Tab ====================
    def _build_inspector_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="验收工作台")

        tk.Label(frame, text="待验收工单列表", font=("Microsoft YaHei", 13, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=10, pady=(10, 5))

        tree_frame = tk.Frame(frame, bg="#f5f6fa")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        i_cols = ("order_id", "title", "category", "priority", "location",
                   "assignee", "completed_at")
        self.inspector_tree = ttk.Treeview(tree_frame, columns=i_cols, show="headings")
        for c, text, w in [("order_id", "工单编号", 160), ("title", "标题", 220),
                            ("category", "类别", 90), ("priority", "优先级", 70),
                            ("location", "位置", 130), ("assignee", "维修员", 90),
                            ("completed_at", "完工时间", 150)]:
            self.inspector_tree.heading(c, text=text)
            self.inspector_tree.column(c, width=w, anchor="center")
        self.inspector_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.inspector_tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.inspector_tree.configure(yscrollcommand=sb.set)
        self._configure_tree_tags(self.inspector_tree)

        detail_frame = tk.LabelFrame(frame, text="工单详情", font=("Microsoft YaHei", 11, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        detail_frame.pack(fill=tk.X, padx=10, pady=5)
        self.inspector_detail = tk.Label(detail_frame, text="", font=("Microsoft YaHei", 10),
                                          bg="#f5f6fa", justify=tk.LEFT, wraplength=1100)
        self.inspector_detail.pack(anchor="w", padx=10, pady=8)
        self.inspector_tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_inspector_detail())

        btn_frame = tk.Frame(frame, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        tk.Button(btn_frame, text="验收通过", font=("Microsoft YaHei", 11, "bold"),
                  bg="#27ae60", fg="white", width=15, command=self._on_approve).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="退回（需原因）", font=("Microsoft YaHei", 11, "bold"),
                  bg="#e74c3c", fg="white", width=15, command=self._on_reject).pack(side=tk.LEFT, padx=5)
        tk.Button(btn_frame, text="刷新", font=("Microsoft YaHei", 10),
                  bg="#95a5a6", fg="white", width=12, command=self._refresh_inspector_tab).pack(side=tk.LEFT, padx=10)

        self._refresh_inspector_tab()

    def _refresh_inspector_tab(self):
        for i in self.inspector_tree.get_children():
            self.inspector_tree.delete(i)
        try:
            orders = self.store.get_orders_by_filter(status=Status.PENDING_INSPECTION)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        for o in orders:
            tag = f"priority_{'high' if o.priority == '高' else 'mid' if o.priority == '中' else 'low'}"
            completed_at = ""
            for h in reversed(o.history):
                if h.status == Status.PENDING_INSPECTION:
                    completed_at = h.timestamp
                    break
            self.inspector_tree.insert("", tk.END, iid=o.order_id, values=(
                o.order_id, o.title, o.category, o.priority, o.location,
                o.assignee_name or "未指派", completed_at
            ), tags=(tag,))
        self.inspector_detail.configure(text="")

    def _refresh_inspector_detail(self):
        sel = self.inspector_tree.selection()
        if not sel:
            self.inspector_detail.configure(text="")
            return
        order_id = sel[0]
        try:
            order = self.store.get_order(order_id)
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))
            return
        if not order:
            return
        self.inspector_detail.configure(
            text=f"标题: {order.title}\n描述: {order.description}\n位置: {order.location}\n"
                 f"类别: {order.category}    优先级: {order.priority}\n"
                 f"维修员: {order.assignee_name or '未指派'}    创建人: {order.creator_name}"
        )

    def _on_approve(self):
        sel = self.inspector_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要验收的工单")
            return
        order_id = sel[0]
        if not messagebox.askyesno("确认", "确认验收通过？"):
            return
        try:
            self.store.approve_order(order_id, self.current_user)
            messagebox.showinfo("成功", "验收通过，工单已完成")
            self._refresh_inspector_tab()
            self._refresh_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_reject(self):
        sel = self.inspector_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要退回的工单")
            return
        order_id = sel[0]
        reason = simpledialog.askstring("退回原因", "请填写退回原因（必填）:", parent=self.root)
        if not reason or not reason.strip():
            messagebox.showwarning("提示", "退回原因不能为空")
            return
        try:
            self.store.reject_order(order_id, self.current_user, reason)
            messagebox.showinfo("成功", "已退回给维修员")
            self._refresh_inspector_tab()
            self._refresh_orders()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    # ==================== 导入导出 Tab ====================
    def _build_import_export_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="导入导出")

        left = tk.Frame(frame, bg="#f5f6fa")
        left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        import_frame = tk.LabelFrame(left, text="数据导入（仅调度员）", font=("Microsoft YaHei", 12, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        import_frame.pack(fill=tk.X, pady=(0, 10))

        tk.Label(import_frame, text="工单CSV导入:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=0, sticky="w", padx=10, pady=8)
        tk.Button(import_frame, text="选择文件并导入", font=("Microsoft YaHei", 10),
                  bg="#27ae60", fg="white", width=18, command=self._on_import_orders).grid(row=0, column=1, padx=5, pady=8)

        tk.Label(import_frame, text="维修员排班CSV导入:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=1, column=0, sticky="w", padx=10, pady=8)
        tk.Button(import_frame, text="选择文件并导入", font=("Microsoft YaHei", 10),
                  bg="#27ae60", fg="white", width=18, command=self._on_import_techs).grid(row=1, column=1, padx=5, pady=8)

        tk.Label(import_frame, text="改约申请CSV导入:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=2, column=0, sticky="w", padx=10, pady=8)
        tk.Button(import_frame, text="选择文件并导入", font=("Microsoft YaHei", 10),
                  bg="#27ae60", fg="white", width=18, command=self._on_import_reschedules).grid(row=2, column=1, padx=5, pady=8)

        if self.current_user.role != Role.DISPATCHER:
            for w in import_frame.winfo_children():
                try:
                    w.configure(state=tk.DISABLED)
                except tk.TclError:
                    pass

        dir_frame = tk.LabelFrame(left, text="导出目录设置", font=("Microsoft YaHei", 12, "bold"),
                                   bg="#f5f6fa", fg="#2c3e50")
        dir_frame.pack(fill=tk.X, pady=(0, 10))
        tk.Label(dir_frame, text="当前导出目录:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").pack(anchor="w", padx=10, pady=(8, 2))
        self.export_dir_label = tk.Label(dir_frame, text="", font=("Microsoft YaHei", 9),
                                          bg="#ffffff", fg="#2c3e50", anchor="w", relief=tk.SUNKEN)
        self.export_dir_label.pack(fill=tk.X, padx=10, pady=(0, 8))
        tk.Button(dir_frame, text="选择导出目录", font=("Microsoft YaHei", 10),
                  bg="#3498db", fg="white", width=18, command=self._on_set_export_dir).pack(anchor="w", padx=10, pady=(0, 10))
        self._refresh_export_dir_label()

        right = tk.Frame(frame, bg="#f5f6fa")
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        export_frame = tk.LabelFrame(right, text="数据导出", font=("Microsoft YaHei", 12, "bold"),
                                      bg="#f5f6fa", fg="#2c3e50")
        export_frame.pack(fill=tk.BOTH, expand=True)

        row = 0
        self.export_filtered_var = tk.BooleanVar(value=False)
        tk.Checkbutton(export_frame, text="仅导出当前工单列表筛选结果", variable=self.export_filtered_var,
                       font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=row, column=0, sticky="w", padx=10, pady=8, columnspan=2)
        row += 1
        tk.Label(export_frame, text="工单数据:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").grid(row=row, column=0, sticky="w", padx=10, pady=5, columnspan=2)
        row += 1
        tk.Button(export_frame, text="导出 JSON", font=("Microsoft YaHei", 10),
                  bg="#3498db", fg="white", width=15, command=lambda: self._on_export_orders("json")).grid(row=row, column=0, padx=10, pady=4)
        tk.Button(export_frame, text="导出 CSV", font=("Microsoft YaHei", 10),
                  bg="#3498db", fg="white", width=15, command=lambda: self._on_export_orders("csv")).grid(row=row, column=1, padx=10, pady=4)
        row += 1
        tk.Label(export_frame, text="维修员数据:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").grid(row=row, column=0, sticky="w", padx=10, pady=5, columnspan=2)
        row += 1
        tk.Button(export_frame, text="导出 JSON", font=("Microsoft YaHei", 10),
                  bg="#9b59b6", fg="white", width=15, command=lambda: self._on_export_techs("json")).grid(row=row, column=0, padx=10, pady=4)
        tk.Button(export_frame, text="导出 CSV", font=("Microsoft YaHei", 10),
                  bg="#9b59b6", fg="white", width=15, command=lambda: self._on_export_techs("csv")).grid(row=row, column=1, padx=10, pady=4)
        row += 1
        tk.Label(export_frame, text="改派记录:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").grid(row=row, column=0, sticky="w", padx=10, pady=5, columnspan=2)
        row += 1
        tk.Button(export_frame, text="导出 JSON", font=("Microsoft YaHei", 10),
                  bg="#e67e22", fg="white", width=15, command=lambda: self._on_export_reassign("json")).grid(row=row, column=0, padx=10, pady=4)
        tk.Button(export_frame, text="导出 CSV", font=("Microsoft YaHei", 10),
                  bg="#e67e22", fg="white", width=15, command=lambda: self._on_export_reassign("csv")).grid(row=row, column=1, padx=10, pady=4)
        row += 1
        tk.Label(export_frame, text="改约申请:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").grid(row=row, column=0, sticky="w", padx=10, pady=5, columnspan=2)
        row += 1
        tk.Button(export_frame, text="导出 JSON", font=("Microsoft YaHei", 10),
                  bg="#16a085", fg="white", width=15, command=lambda: self._on_export_reschedules("json")).grid(row=row, column=0, padx=10, pady=4)
        tk.Button(export_frame, text="导出 CSV", font=("Microsoft YaHei", 10),
                  bg="#16a085", fg="white", width=15, command=lambda: self._on_export_reschedules("csv")).grid(row=row, column=1, padx=10, pady=4)
        row += 1
        tk.Label(export_frame, text="改约确认日志:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").grid(row=row, column=0, sticky="w", padx=10, pady=5, columnspan=2)
        row += 1
        tk.Button(export_frame, text="导出 JSON", font=("Microsoft YaHei", 10),
                  bg="#8e44ad", fg="white", width=15, command=lambda: self._on_export_reschedule_logs("json")).grid(row=row, column=0, padx=10, pady=4)
        tk.Button(export_frame, text="导出 CSV", font=("Microsoft YaHei", 10),
                  bg="#8e44ad", fg="white", width=15, command=lambda: self._on_export_reschedule_logs("csv")).grid(row=row, column=1, padx=10, pady=4)

        self.export_log = tk.Text(export_frame, height=8, font=("Microsoft YaHei", 9), state=tk.DISABLED,
                                   bg="#ffffff", wrap=tk.WORD)
        self.export_log.grid(row=row + 1, column=0, columnspan=2, sticky="nsew", padx=10, pady=10)
        export_frame.grid_rowconfigure(row + 1, weight=1)
        export_frame.grid_columnconfigure(0, weight=1)
        export_frame.grid_columnconfigure(1, weight=1)

    def _refresh_export_dir_label(self):
        try:
            cfg = self.store.get_config()
            path = cfg.export_dir or "(未设置，默认: ./exports)"
            self.export_dir_label.configure(text=path)
        except WorkOrderError as e:
            self.export_dir_label.configure(text=str(e))

    def _append_export_log(self, msg):
        self.export_log.configure(state=tk.NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        self.export_log.insert(tk.END, f"[{ts}] {msg}\n")
        self.export_log.see(tk.END)
        self.export_log.configure(state=tk.DISABLED)

    def _on_set_export_dir(self):
        path = filedialog.askdirectory(title="选择导出目录")
        if not path:
            return
        try:
            self.store.set_export_dir(path)
            self._refresh_export_dir_label()
            messagebox.showinfo("成功", f"导出目录已设置为:\n{path}")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _get_filtered_orders(self):
        if not self.export_filtered_var.get():
            return None
        status_val = self.filter_status.get()
        status = None
        if status_val and status_val != "全部":
            for s in Status:
                if s.value == status_val:
                    status = s
                    break
        location = self.filter_location.get().strip() or None
        category_val = self.filter_category.get()
        category = category_val if category_val and category_val != "全部" else None
        priority_val = self.filter_priority.get()
        priority = priority_val if priority_val and priority_val != "全部" else None
        assignee_id = None
        if self.current_user.role == Role.TECHNICIAN:
            assignee_id = self.current_user.user_id
        return self.store.get_orders_by_filter(status=status, location=location,
                                                 category=category, priority=priority,
                                                 assignee_id=assignee_id)

    def _on_import_orders(self):
        if self.current_user.role != Role.DISPATCHER:
            messagebox.showwarning("提示", "仅调度员可导入工单")
            return
        path = filedialog.askopenfilename(title="选择工单CSV文件",
                                            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            count, errors = self.store.import_orders_csv(path, self.current_user)
            msg = f"导入完成: 成功 {count} 条"
            if errors:
                msg += f"，失败 {len(errors)} 条:\n" + "\n".join(errors[:5])
                if len(errors) > 5:
                    msg += f"\n... 还有 {len(errors) - 5} 条错误"
            messagebox.showinfo("导入结果", msg)
            self._append_export_log(f"工单CSV导入: 成功{count}条, 失败{len(errors)}条")
            self._refresh_all_tabs()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_import_techs(self):
        if self.current_user.role != Role.DISPATCHER:
            messagebox.showwarning("提示", "仅调度员可导入维修员排班")
            return
        path = filedialog.askopenfilename(title="选择维修员排班CSV文件",
                                            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            count, errors = self.store.import_technicians_csv(path, self.current_user)
            msg = f"导入完成: 成功 {count} 条"
            if errors:
                msg += f"，失败 {len(errors)} 条:\n" + "\n".join(errors[:5])
                if len(errors) > 5:
                    msg += f"\n... 还有 {len(errors) - 5} 条错误"
            messagebox.showinfo("导入结果", msg)
            self._append_export_log(f"维修员排班CSV导入: 成功{count}条, 失败{len(errors)}条")
            self._refresh_all_tabs()
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_export_orders(self, fmt):
        try:
            orders = self._get_filtered_orders()
            if fmt == "json":
                path = self.store.export_orders_json(orders)
            else:
                path = self.store.export_orders_csv(orders)
            scope = "筛选结果" if orders is not None else "全部"
            msg = f"工单{scope}已导出到: {path}"
            messagebox.showinfo("导出成功", msg)
            self._append_export_log(msg)
        except ExportError as e:
            messagebox.showerror("导出失败", str(e))
            self._append_export_log(f"工单导出失败: {e}")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_export_techs(self, fmt):
        try:
            if fmt == "json":
                path = self.store.export_technicians_json()
            else:
                path = self.store.export_technicians_csv()
            msg = f"维修员数据已导出到: {path}"
            messagebox.showinfo("导出成功", msg)
            self._append_export_log(msg)
        except ExportError as e:
            messagebox.showerror("导出失败", str(e))
            self._append_export_log(f"维修员导出失败: {e}")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_export_reassign(self, fmt):
        try:
            orders = self._get_filtered_orders()
            if fmt == "json":
                path = self.store.export_reassignment_logs_json(orders)
            else:
                path = self.store.export_reassignment_logs_csv(orders)
            scope = "筛选结果" if orders is not None else "全部"
            msg = f"改派记录{scope}已导出到: {path}"
            messagebox.showinfo("导出成功", msg)
            self._append_export_log(msg)
        except ExportError as e:
            messagebox.showerror("导出失败", str(e))
            self._append_export_log(f"改派记录导出失败: {e}")
        except WorkOrderError as e:
            messagebox.showerror("错误", str(e))

    def _on_import_reschedules(self):
        path = filedialog.askopenfilename(title="选择改约申请CSV", filetypes=[("CSV文件", "*.csv")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                result = self.store.import_reschedule_requests_csv(f, self.current_user)
            imported = len(result.imported)
            failed = len(result.errors)
            msg = f"成功导入 {imported} 条改约申请"
            if failed:
                msg += f"，跳过 {failed} 条非法行"
                detail = "\n".join(f"第 {e.line_no} 行: {e.reason}" for e in result.errors[:10])
                msg += "\n\n跳过明细:\n" + detail
            messagebox.showinfo("导入完成", msg)
            self._append_export_log(f"改约申请CSV导入完成: 成功{imported}, 失败{failed}")
            if hasattr(self, "reschedule_tree"):
                self._refresh_reschedules()
        except (WorkOrderError, PermissionError, ExportError) as e:
            messagebox.showerror("导入失败", str(e))
            self._append_export_log(f"改约申请CSV导入失败: {e}")

    def _on_export_reschedules(self, fmt):
        try:
            if fmt == "json":
                path = self.store.export_reschedule_requests_json()
            else:
                path = self.store.export_reschedule_requests_csv()
            msg = f"改约申请已导出到: {path}"
            messagebox.showinfo("导出成功", msg)
            self._append_export_log(msg)
        except ExportError as e:
            messagebox.showerror("导出失败", str(e))
            self._append_export_log(f"改约申请导出失败: {e}")

    def _on_export_reschedule_logs(self, fmt):
        try:
            if fmt == "json":
                path = self.store.export_reschedule_confirm_logs_json()
            else:
                path = self.store.export_reschedule_confirm_logs_csv()
            msg = f"改约确认日志已导出到: {path}"
            messagebox.showinfo("导出成功", msg)
            self._append_export_log(msg)
        except ExportError as e:
            messagebox.showerror("导出失败", str(e))
            self._append_export_log(f"改约确认日志导出失败: {e}")

    # ==================== 备件库存 & 领用核销 Tab ====================
    def _build_spare_parts_tab(self):
        frame = tk.Frame(self.notebook, bg="#f5f6fa")
        self.notebook.add(frame, text="备件库存")

        top_bar = tk.Frame(frame, bg="#2c3e50", height=45)
        top_bar.pack(fill=tk.X)
        top_bar.pack_propagate(False)
        tk.Label(top_bar, text="备件库存与领用核销管理", font=("Microsoft YaHei", 13, "bold"),
                 bg="#2c3e50", fg="white").pack(side=tk.LEFT, padx=15)
        self.sp_role_label = tk.Label(top_bar, text="", font=("Microsoft YaHei", 10),
                                      bg="#2c3e50", fg="#f39c12")
        self.sp_role_label.pack(side=tk.RIGHT, padx=15)
        if self.current_user.role == Role.DISPATCHER:
            self.sp_role_label.configure(text="调度员视角 - 可维护档案/审核申请")
        else:
            self.sp_role_label.configure(text="维修员视角 - 仅查看可用库存和自己的申请")

        content = tk.PanedWindow(frame, orient=tk.HORIZONTAL, bg="#f5f6fa", sashrelief=tk.RAISED)
        content.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        left_pane = tk.Frame(content, bg="#f5f6fa")
        content.add(left_pane, minsize=500)

        right_pane = tk.Frame(content, bg="#f5f6fa")
        content.add(right_pane, minsize=500)

        self._build_spare_parts_inventory_panel(left_pane)
        self._build_spare_parts_request_panel(right_pane)
        self._refresh_spare_parts_tab()

    def _build_spare_parts_inventory_panel(self, parent):
        panel = tk.LabelFrame(parent, text="备件档案 & 库存", font=("Microsoft YaHei", 11, "bold"),
                               bg="#f5f6fa", fg="#2c3e50")
        panel.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        toolbar = tk.Frame(panel, bg="#f5f6fa")
        toolbar.pack(fill=tk.X, padx=8, pady=6)

        tk.Label(toolbar, text="类别筛选:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=0, padx=3, pady=3)
        self.sp_filter_category = ttk.Combobox(toolbar, values=["全部"] + CATEGORY_OPTIONS,
                                                state="readonly", width=12, font=("Microsoft YaHei", 10))
        self.sp_filter_category.set("全部")
        self.sp_filter_category.grid(row=0, column=1, padx=3, pady=3)
        self.sp_filter_category.bind("<<ComboboxSelected>>", lambda e: self._refresh_spare_parts_tree())

        self.sp_filter_low = tk.BooleanVar(value=False)
        tk.Checkbutton(toolbar, text="仅显示低库存", variable=self.sp_filter_low,
                       font=("Microsoft YaHei", 10), bg="#f5f6fa",
                       command=self._refresh_spare_parts_tree).grid(row=0, column=2, padx=8, pady=3)

        tk.Button(toolbar, text="刷新", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=8, command=self._refresh_spare_parts_tree).grid(row=0, column=3, padx=3, pady=3)

        if self.current_user.role == Role.DISPATCHER:
            tk.Button(toolbar, text="新增备件", font=("Microsoft YaHei", 10, "bold"),
                      bg="#27ae60", fg="white", width=10,
                      command=self._on_sp_create).grid(row=0, column=4, padx=3, pady=3)
            tk.Button(toolbar, text="编辑选中", font=("Microsoft YaHei", 10),
                      bg="#f39c12", fg="white", width=10,
                      command=self._on_sp_edit).grid(row=0, column=5, padx=3, pady=3)
            tk.Button(toolbar, text="删除选中", font=("Microsoft YaHei", 10),
                      bg="#e74c3c", fg="white", width=10,
                      command=self._on_sp_delete).grid(row=0, column=6, padx=3, pady=3)
            tk.Button(toolbar, text="导入CSV", font=("Microsoft YaHei", 10),
                      bg="#9b59b6", fg="white", width=10,
                      command=self._on_sp_import_csv).grid(row=0, column=7, padx=3, pady=3)

        tk.Button(toolbar, text="导出CSV", font=("Microsoft YaHei", 10),
                  bg="#16a085", fg="white", width=10,
                  command=self._on_sp_export_csv).grid(row=0, column=8, padx=3, pady=3)
        tk.Button(toolbar, text="导出JSON", font=("Microsoft YaHei", 10),
                  bg="#16a085", fg="white", width=10,
                  command=self._on_sp_export_json).grid(row=0, column=9, padx=3, pady=3)

        tree_frame = tk.Frame(panel, bg="#f5f6fa")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        cols = ("part_id", "name", "category", "stock", "unit", "threshold", "status", "applicable", "desc")
        self.sp_parts_tree = ttk.Treeview(tree_frame, columns=cols, show="headings")
        for c, text, w in [
            ("part_id", "编号", 140), ("name", "名称", 140), ("category", "类别", 100),
            ("stock", "库存", 70), ("unit", "单位", 60), ("threshold", "低库阈值", 80),
            ("status", "状态", 80), ("applicable", "适用工单类别", 180), ("desc", "描述", 180),
        ]:
            self.sp_parts_tree.heading(c, text=text)
            self.sp_parts_tree.column(c, width=w, anchor="center")
        self.sp_parts_tree.column("name", anchor="w")
        self.sp_parts_tree.column("desc", anchor="w")
        self.sp_parts_tree.column("applicable", anchor="w")
        self.sp_parts_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sp_sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.sp_parts_tree.yview)
        sp_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.sp_parts_tree.configure(yscrollcommand=sp_sb.set)
        self.sp_parts_tree.tag_configure("low", background="#fdedec")
        self.sp_parts_tree.tag_configure("ok", background="#eafaf1")

        log_frame = tk.LabelFrame(panel, text="操作日志（按备件筛选）",
                                   font=("Microsoft YaHei", 11, "bold"), bg="#f5f6fa", fg="#2c3e50")
        log_frame.pack(fill=tk.BOTH, expand=False, padx=8, pady=6)
        log_cols = ("log_time", "action", "qty", "operator", "before", "after", "order", "note")
        self.sp_log_tree = ttk.Treeview(log_frame, columns=log_cols, show="headings", height=6)
        for c, text, w in [
            ("log_time", "时间", 160), ("action", "操作", 90), ("qty", "数量", 70),
            ("operator", "操作人", 80), ("before", "操作前", 70), ("after", "操作后", 70),
            ("order", "关联工单", 140), ("note", "备注", 280),
        ]:
            self.sp_log_tree.heading(c, text=text)
            self.sp_log_tree.column(c, width=w, anchor="center")
        self.sp_log_tree.column("note", anchor="w")
        self.sp_log_tree.pack(fill=tk.BOTH, expand=False, padx=8, pady=4)
        self.sp_parts_tree.bind("<<TreeviewSelect>>", lambda e: self._refresh_spare_parts_log())

    def _build_spare_parts_request_panel(self, parent):
        panel = tk.LabelFrame(parent, text="领用申请 & 审核", font=("Microsoft YaHei", 11, "bold"),
                               bg="#f5f6fa", fg="#2c3e50")
        panel.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        toolbar = tk.Frame(panel, bg="#f5f6fa")
        toolbar.pack(fill=tk.X, padx=8, pady=6)

        tk.Label(toolbar, text="状态:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=0, padx=3, pady=3)
        self.sp_req_filter_status = ttk.Combobox(
            toolbar,
            values=["全部", "待审核", "已审核", "已拒绝", "已退回"],
            state="readonly", width=10, font=("Microsoft YaHei", 10))
        self.sp_req_filter_status.set("全部")
        self.sp_req_filter_status.grid(row=0, column=1, padx=3, pady=3)
        self.sp_req_filter_status.bind("<<ComboboxSelected>>", lambda e: self._refresh_spare_parts_requests())

        tk.Label(toolbar, text="工单:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=2, padx=3, pady=3)
        self.sp_req_filter_order = tk.Entry(toolbar, width=14, font=("Microsoft YaHei", 10))
        self.sp_req_filter_order.grid(row=0, column=3, padx=3, pady=3)

        tk.Button(toolbar, text="查询", font=("Microsoft YaHei", 10), bg="#3498db", fg="white",
                  width=6, command=self._refresh_spare_parts_requests).grid(row=0, column=4, padx=3, pady=3)

        if self.current_user.role == Role.TECHNICIAN:
            tk.Button(toolbar, text="新建领用申请", font=("Microsoft YaHei", 10, "bold"),
                      bg="#2980b9", fg="white", width=14,
                      command=self._on_sp_request_create).grid(row=0, column=5, padx=8, pady=3)
            tk.Button(toolbar, text="退回选中", font=("Microsoft YaHei", 10),
                      bg="#8e44ad", fg="white", width=10,
                      command=self._on_sp_return).grid(row=0, column=6, padx=3, pady=3)

        if self.current_user.role == Role.DISPATCHER:
            tk.Button(toolbar, text="审核通过", font=("Microsoft YaHei", 10, "bold"),
                      bg="#27ae60", fg="white", width=10,
                      command=self._on_sp_approve).grid(row=0, column=5, padx=3, pady=3)
            tk.Button(toolbar, text="审核拒绝", font=("Microsoft YaHei", 10),
                      bg="#e74c3c", fg="white", width=10,
                      command=self._on_sp_reject).grid(row=0, column=6, padx=3, pady=3)
            tk.Button(toolbar, text="导出申请CSV", font=("Microsoft YaHei", 10),
                      bg="#16a085", fg="white", width=12,
                      command=self._on_sp_export_requests_csv).grid(row=0, column=7, padx=3, pady=3)
            tk.Button(toolbar, text="导出日志CSV", font=("Microsoft YaHei", 10),
                      bg="#16a085", fg="white", width=12,
                      command=self._on_sp_export_logs_csv).grid(row=0, column=8, padx=3, pady=3)

        tree_frame = tk.Frame(panel, bg="#f5f6fa")
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=4)

        req_cols = ("req_id", "order_id", "part_id", "part_name", "qty",
                     "applicant", "status", "reviewer", "reason", "created", "reviewed")
        self.sp_req_tree = ttk.Treeview(tree_frame, columns=req_cols, show="headings")
        for c, text, w in [
            ("req_id", "申请编号", 160), ("order_id", "工单", 140),
            ("part_id", "备件编号", 110), ("part_name", "备件名称", 120),
            ("qty", "数量", 60), ("applicant", "申请人", 80),
            ("status", "状态", 80), ("reviewer", "审核人", 80),
            ("reason", "原因/备注", 180), ("created", "申请时间", 140), ("reviewed", "审核/退回时间", 140),
        ]:
            self.sp_req_tree.heading(c, text=text)
            self.sp_req_tree.column(c, width=w, anchor="center")
        self.sp_req_tree.column("reason", anchor="w")
        self.sp_req_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        req_sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.sp_req_tree.yview)
        req_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.sp_req_tree.configure(yscrollcommand=req_sb.set)
        self.sp_req_tree.tag_configure("pending", background="#fef9e7")
        self.sp_req_tree.tag_configure("approved", background="#eafaf1")
        self.sp_req_tree.tag_configure("rejected", background="#fdedec")
        self.sp_req_tree.tag_configure("returned", background="#e8daef")

    def _refresh_spare_parts_tab(self):
        self._refresh_spare_parts_tree()
        self._refresh_spare_parts_requests()
        self._refresh_spare_parts_log()

    def _refresh_spare_parts_tree(self):
        for i in self.sp_parts_tree.get_children():
            self.sp_parts_tree.delete(i)
        cat = self.sp_filter_category.get()
        cat_filter = None if cat == "全部" else cat
        low_only = self.sp_filter_low.get()
        try:
            parts = self.store.get_spare_parts_by_filter(category=cat_filter, low_stock_only=low_only)
        except WorkOrderError:
            parts = []
        for p in parts:
            tag = "low" if p.is_low_stock else "ok"
            applicable = "|".join(p.applicable_categories) if p.applicable_categories else "全部"
            status_text = "低库存" if p.is_low_stock else "正常"
            self.sp_parts_tree.insert("", tk.END, iid=p.part_id, values=(
                p.part_id, p.name, p.category, p.stock, p.unit,
                p.low_stock_threshold, status_text, applicable, p.description,
            ), tags=(tag,))

    def _refresh_spare_parts_log(self):
        for i in self.sp_log_tree.get_children():
            self.sp_log_tree.delete(i)
        sel = self.sp_parts_tree.selection()
        part_id = sel[0] if sel else None
        try:
            logs = self.store.get_spare_part_audit_logs(part_id=part_id)
        except WorkOrderError:
            logs = []
        for l in logs[:50]:
            self.sp_log_tree.insert("", tk.END, values=(
                l.timestamp, l.action, l.quantity, l.operator_name,
                l.stock_before, l.stock_after, l.order_id or "", l.note,
            ))

    def _refresh_spare_parts_requests(self):
        for i in self.sp_req_tree.get_children():
            self.sp_req_tree.delete(i)
        status_text = self.sp_req_filter_status.get()
        status_map = {
            "待审核": SparePartRequestStatus.PENDING,
            "已审核": SparePartRequestStatus.APPROVED,
            "已拒绝": SparePartRequestStatus.REJECTED,
            "已退回": SparePartRequestStatus.RETURNED,
        }
        status_filter = status_map.get(status_text)
        order_filter = self.sp_req_filter_order.get().strip() or None
        try:
            reqs = self.store.get_spare_part_requests_by_filter(
                user=self.current_user,
                order_id=order_filter,
                status=status_filter,
            )
        except WorkOrderError:
            reqs = []
        for r in reqs:
            if r.status == SparePartRequestStatus.PENDING:
                tag = "pending"
            elif r.status == SparePartRequestStatus.APPROVED:
                tag = "approved"
            elif r.status == SparePartRequestStatus.REJECTED:
                tag = "rejected"
            else:
                tag = "returned"
            time_info = r.reviewed_at or r.returned_at or ""
            note = r.reason or r.review_note or r.return_note or ""
            self.sp_req_tree.insert("", tk.END, iid=r.request_id, values=(
                r.request_id, r.order_id, r.part_id, r.part_name, r.quantity,
                r.applicant_name, r.status.value, r.reviewer_name or "",
                note, r.created_at, time_info,
            ), tags=(tag,))

    def _on_sp_create(self):
        SparePartEditDialog(self.root, self.store, self.current_user, None, self._refresh_spare_parts_tab)

    def _on_sp_edit(self):
        sel = self.sp_parts_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要编辑的备件")
            return
        part = self.store.get_spare_part(sel[0])
        if not part:
            messagebox.showerror("错误", "备件不存在")
            return
        SparePartEditDialog(self.root, self.store, self.current_user, part, self._refresh_spare_parts_tab)

    def _on_sp_delete(self):
        sel = self.sp_parts_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要删除的备件")
            return
        if not messagebox.askyesno("确认", "确定删除选中的备件？存在待审核申请的备件无法删除。"):
            return
        try:
            deleted = self.store.delete_spare_part(sel[0], self.current_user)
            if deleted:
                messagebox.showinfo("成功", "备件已删除")
            else:
                messagebox.showwarning("提示", "备件不存在")
            self._refresh_spare_parts_tab()
        except WorkOrderError as e:
            messagebox.showerror("删除失败", str(e))

    def _on_sp_import_csv(self):
        path = filedialog.askopenfilename(title="选择备件CSV文件",
                                            filetypes=[("CSV文件", "*.csv")])
        if not path:
            return
        try:
            imported, errors = self.store.import_spare_parts_csv(path, self.current_user)
            if errors:
                messagebox.showerror("导入失败",
                    "检测到非法行，全部数据未导入:\n" + "\n".join(errors[:20]))
            else:
                messagebox.showinfo("成功", f"已成功导入/更新 {imported} 条备件档案")
            self._refresh_spare_parts_tab()
        except WorkOrderError as e:
            messagebox.showerror("导入失败", str(e))

    def _on_sp_export_csv(self):
        try:
            path = self.store.export_spare_parts_csv()
            messagebox.showinfo("导出成功", f"备件库存已导出到: {path}")
        except (ExportError, WorkOrderError) as e:
            messagebox.showerror("导出失败", str(e))

    def _on_sp_export_json(self):
        try:
            path = self.store.export_spare_parts_json()
            messagebox.showinfo("导出成功", f"备件库存已导出到: {path}")
        except (ExportError, WorkOrderError) as e:
            messagebox.showerror("导出失败", str(e))

    def _on_sp_export_requests_csv(self):
        try:
            path = self.store.export_spare_part_requests_csv()
            messagebox.showinfo("导出成功", f"领用申请已导出到: {path}")
        except (ExportError, WorkOrderError) as e:
            messagebox.showerror("导出失败", str(e))

    def _on_sp_export_logs_csv(self):
        try:
            path = self.store.export_spare_part_audit_logs_csv()
            messagebox.showinfo("导出成功", f"审核日志已导出到: {path}")
        except (ExportError, WorkOrderError) as e:
            messagebox.showerror("导出失败", str(e))

    def _on_sp_request_create(self):
        SparePartRequestDialog(self.root, self.store, self.current_user, self._refresh_spare_parts_tab)

    def _on_sp_approve(self):
        sel = self.sp_req_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要审核通过的申请")
            return
        note = simpledialog.askstring("审核备注", "请输入审核备注（可留空）:", parent=self.root) or ""
        failed = 0
        success = 0
        for req_id in sel:
            try:
                self.store.approve_spare_part_request(req_id, self.current_user, note)
                success += 1
            except WorkOrderError as e:
                failed += 1
                messagebox.showerror("审核失败", f"申请 {req_id} 审核被拦截:\n{str(e)}")
        if success:
            messagebox.showinfo("审核结果", f"成功审核 {success} 条申请，失败 {failed} 条")
        self._refresh_spare_parts_tab()

    def _on_sp_reject(self):
        sel = self.sp_req_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要拒绝的申请")
            return
        note = simpledialog.askstring("拒绝原因", "请输入拒绝原因（必填）:", parent=self.root)
        if not note or not note.strip():
            messagebox.showwarning("提示", "拒绝原因不能为空")
            return
        for req_id in sel:
            try:
                self.store.reject_spare_part_request(req_id, self.current_user, note)
            except WorkOrderError as e:
                messagebox.showerror("拒绝失败", f"申请 {req_id} 处理失败:\n{str(e)}")
        self._refresh_spare_parts_tab()

    def _on_sp_return(self):
        sel = self.sp_req_tree.selection()
        if not sel:
            messagebox.showwarning("提示", "请选择要退回的已审核申请")
            return
        note = simpledialog.askstring("退回备注", "请输入退回备注（可留空）:", parent=self.root) or ""
        for req_id in sel:
            try:
                self.store.return_spare_part(req_id, self.current_user, note)
            except WorkOrderError as e:
                messagebox.showerror("退回失败", f"申请 {req_id} 退回被拦截:\n{str(e)}")
        self._refresh_spare_parts_tab()

    def _refresh_all_tabs(self):
        try:
            self._refresh_orders()
        except Exception:
            pass
        try:
            self._refresh_history_order_list()
        except Exception:
            pass
        try:
            self._refresh_dispatch_orders()
        except Exception:
            pass
        try:
            self._refresh_schedule_tech_list()
        except Exception:
            pass
        try:
            self._refresh_tech_tab()
        except Exception:
            pass
        try:
            self._refresh_inspector_tab()
        except Exception:
            pass
        try:
            self._refresh_spare_parts_tab()
        except Exception:
            pass


class SparePartEditDialog(tk.Toplevel):
    def __init__(self, parent, store, dispatcher, part: Optional[SparePart], on_done):
        super().__init__(parent)
        self.store = store
        self.dispatcher = dispatcher
        self.part = part
        self.on_done = on_done
        self.title("编辑备件" if part else "新增备件")
        self.geometry("560x520")
        self.configure(bg="#f5f6fa")
        self.grab_set()
        self.transient(parent)
        self._build()

    def _build(self):
        form = tk.Frame(self, bg="#f5f6fa")
        form.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        fields = [
            ("名称:", "name", True),
            ("类别:", "category", True),
            ("当前库存:", "stock", True),
            ("低库存阈值:", "threshold", True),
            ("单位:", "unit", False),
        ]
        self.entries = {}
        defaults = {
            "name": self.part.name if self.part else "",
            "category": self.part.category if self.part else "",
            "stock": str(self.part.stock) if self.part else "0",
            "threshold": str(self.part.low_stock_threshold) if self.part else "0",
            "unit": self.part.unit if self.part else "个",
        }
        for i, (label, key, required) in enumerate(fields):
            tk.Label(form, text=label + ("*" if required else ""),
                     font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=i, column=0, sticky="e", padx=5, pady=6)
            e = tk.Entry(form, width=36, font=("Microsoft YaHei", 10))
            e.insert(0, defaults.get(key, ""))
            e.grid(row=i, column=1, sticky="w", padx=5, pady=6)
            self.entries[key] = e

        tk.Label(form, text="适用工单类别 (|分隔，留空=全部):", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=5, column=0, sticky="ne", padx=5, pady=6)
        self.applicable_text = tk.Text(form, width=36, height=3, font=("Microsoft YaHei", 10))
        if self.part and self.part.applicable_categories:
            self.applicable_text.insert("1.0", "|".join(self.part.applicable_categories))
        self.applicable_text.grid(row=5, column=1, sticky="w", padx=5, pady=6)

        tk.Label(form, text="描述:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=6, column=0, sticky="ne", padx=5, pady=6)
        self.desc_text = tk.Text(form, width=36, height=4, font=("Microsoft YaHei", 10))
        if self.part:
            self.desc_text.insert("1.0", self.part.description)
        self.desc_text.grid(row=6, column=1, sticky="w", padx=5, pady=6)

        btn_frame = tk.Frame(form, bg="#f5f6fa")
        btn_frame.grid(row=7, column=0, columnspan=2, pady=15)
        tk.Button(btn_frame, text="保存", font=("Microsoft YaHei", 11, "bold"),
                  bg="#27ae60", fg="white", width=12, command=self._on_save).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="取消", font=("Microsoft YaHei", 11),
                  bg="#7f8c8d", fg="white", width=12, command=self.destroy).pack(side=tk.LEFT, padx=8)

    def _on_save(self):
        name = self.entries["name"].get().strip()
        category = self.entries["category"].get().strip()
        try:
            stock = int(self.entries["stock"].get().strip())
        except ValueError:
            messagebox.showerror("错误", "库存必须是整数", parent=self)
            return
        try:
            threshold = int(self.entries["threshold"].get().strip())
        except ValueError:
            messagebox.showerror("错误", "低库存阈值必须是整数", parent=self)
            return
        unit = self.entries["unit"].get().strip() or "个"
        applicable_raw = self.applicable_text.get("1.0", tk.END).strip()
        applicable = [c.strip() for c in applicable_raw.split("|") if c.strip()] if applicable_raw else []
        description = self.desc_text.get("1.0", tk.END).strip()
        try:
            if self.part:
                self.store.update_spare_part(
                    part_id=self.part.part_id,
                    dispatcher=self.dispatcher,
                    name=name, category=category, stock=stock,
                    low_stock_threshold=threshold, applicable_categories=applicable,
                    unit=unit, description=description,
                )
            else:
                self.store.create_spare_part(
                    name=name, category=category, stock=stock,
                    low_stock_threshold=threshold, applicable_categories=applicable,
                    unit=unit, description=description, dispatcher=self.dispatcher,
                )
            messagebox.showinfo("成功", "备件已保存", parent=self)
            if self.on_done:
                self.on_done()
            self.destroy()
        except WorkOrderError as e:
            messagebox.showerror("保存失败", str(e), parent=self)


class SparePartRequestDialog(tk.Toplevel):
    def __init__(self, parent, store, technician, on_done):
        super().__init__(parent)
        self.store = store
        self.technician = technician
        self.on_done = on_done
        self.title("申请领用备件")
        self.geometry("520x420")
        self.configure(bg="#f5f6fa")
        self.grab_set()
        self.transient(parent)
        self._build()

    def _build(self):
        form = tk.Frame(self, bg="#f5f6fa")
        form.pack(fill=tk.BOTH, expand=True, padx=20, pady=15)

        my_orders = self.store.get_orders_by_filter(assignee_id=self.technician.user_id)
        active_orders = [o for o in my_orders if o.status != Status.COMPLETED]
        order_values = [f"{o.order_id} - {o.title} ({o.category})" for o in active_orders]
        self.order_map = {f"{o.order_id} - {o.title} ({o.category})": o for o in active_orders}

        tk.Label(form, text="关联工单*:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=0, column=0, sticky="e", padx=5, pady=8)
        self.order_combo = ttk.Combobox(form, values=order_values, state="readonly",
                                        width=40, font=("Microsoft YaHei", 10))
        self.order_combo.grid(row=0, column=1, sticky="w", padx=5, pady=8)
        self.order_combo.bind("<<ComboboxSelected>>", lambda e: self._refresh_parts())

        tk.Label(form, text="领用备件*:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=1, column=0, sticky="e", padx=5, pady=8)
        self.part_combo = ttk.Combobox(form, values=[], state="readonly",
                                        width=40, font=("Microsoft YaHei", 10))
        self.part_combo.grid(row=1, column=1, sticky="w", padx=5, pady=8)
        self.part_map = {}

        tk.Label(form, text="领用数量*:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=2, column=0, sticky="e", padx=5, pady=8)
        self.qty_entry = tk.Entry(form, width=40, font=("Microsoft YaHei", 10))
        self.qty_entry.insert(0, "1")
        self.qty_entry.grid(row=2, column=1, sticky="w", padx=5, pady=8)

        tk.Label(form, text="申请原因:", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa").grid(row=3, column=0, sticky="ne", padx=5, pady=8)
        self.reason_text = tk.Text(form, width=40, height=4, font=("Microsoft YaHei", 10))
        self.reason_text.grid(row=3, column=1, sticky="w", padx=5, pady=8)

        btn_frame = tk.Frame(form, bg="#f5f6fa")
        btn_frame.grid(row=4, column=0, columnspan=2, pady=20)
        tk.Button(btn_frame, text="提交申请", font=("Microsoft YaHei", 11, "bold"),
                  bg="#2980b9", fg="white", width=12, command=self._on_submit).pack(side=tk.LEFT, padx=8)
        tk.Button(btn_frame, text="取消", font=("Microsoft YaHei", 11),
                  bg="#7f8c8d", fg="white", width=12, command=self.destroy).pack(side=tk.LEFT, padx=8)

    def _refresh_parts(self):
        order_val = self.order_combo.get()
        order = self.order_map.get(order_val)
        if not order:
            self.part_combo["values"] = []
            self.part_map = {}
            return
        try:
            parts = self.store.get_spare_parts_by_filter(order_category=order.category)
        except WorkOrderError:
            parts = []
        display = [f"{p.part_id} - {p.name} (库存:{p.stock}{p.unit})" for p in parts]
        self.part_map = {f"{p.part_id} - {p.name} (库存:{p.stock}{p.unit})": p for p in parts}
        self.part_combo["values"] = display

    def _on_submit(self):
        order_val = self.order_combo.get()
        order = self.order_map.get(order_val)
        if not order:
            messagebox.showwarning("提示", "请选择关联工单", parent=self)
            return
        part_val = self.part_combo.get()
        part = self.part_map.get(part_val)
        if not part:
            messagebox.showwarning("提示", "请选择要领用的备件", parent=self)
            return
        try:
            qty = int(self.qty_entry.get().strip())
        except ValueError:
            messagebox.showerror("错误", "领用数量必须是正整数", parent=self)
            return
        reason = self.reason_text.get("1.0", tk.END).strip()
        try:
            self.store.create_spare_part_request(order.order_id, part.part_id, qty, self.technician, reason)
            messagebox.showinfo("成功", "申请已提交，等待调度员审核", parent=self)
            if self.on_done:
                self.on_done()
            self.destroy()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("提交失败", str(e), parent=self)


class CreateRescheduleDialog(tk.Toplevel):
    def __init__(self, parent, store, dispatcher, order):
        super().__init__(parent)
        self.store = store
        self.dispatcher = dispatcher
        self.order = order
        self.result = None
        self.title("发起上门改约")
        self.geometry("640x560")
        self.configure(bg="#f5f6fa")
        self.grab_set()
        self.transient(parent)
        self._slots: List[RescheduleCandidateSlot] = []
        self._build_ui()

    def _build_ui(self):
        info = tk.Frame(self, bg="#f5f6fa")
        info.pack(fill=tk.X, padx=15, pady=10)
        tk.Label(info, text=f"工单: {self.order.order_id}  {self.order.title}",
                 font=("Microsoft YaHei", 12, "bold"), bg="#f5f6fa").grid(row=0, column=0, sticky="w")
        cur = f"{self.order.scheduled_start or '(未排程)'} ~ {self.order.scheduled_end or ''}"
        tk.Label(info, text=f"当前排程: {cur.strip()}", font=("Microsoft YaHei", 10),
                 bg="#f5f6fa", fg="#555").grid(row=1, column=0, sticky="w", pady=3)
        tk.Label(info, text=f"维修员: {self.order.assignee_name or '未指派'}",
                 font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=2, column=0, sticky="w")

        tk.Label(self, text="改约原因（必填）:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15, pady=(8, 2))
        self.reason_text = tk.Text(self, height=3, font=("Microsoft YaHei", 10))
        self.reason_text.pack(fill=tk.X, padx=15, pady=2)

        slots_frame = tk.LabelFrame(self, text="候选时间窗（至少1个，格式 YYYY-MM-DD HH:MM）",
                                    font=("Microsoft YaHei", 10, "bold"), bg="#f5f6fa", fg="#2c3e50")
        slots_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=8)

        entry_frame = tk.Frame(slots_frame, bg="#f5f6fa")
        entry_frame.pack(fill=tk.X, padx=8, pady=6)
        tk.Label(entry_frame, text="开始:", bg="#f5f6fa", font=("Microsoft YaHei", 10)).grid(row=0, column=0, padx=2)
        self.slot_start = tk.Entry(entry_frame, width=18, font=("Microsoft YaHei", 10))
        self.slot_start.grid(row=0, column=1, padx=2)
        self.slot_start.insert(0, "2026-06-15 09:00")
        tk.Label(entry_frame, text="结束:", bg="#f5f6fa", font=("Microsoft YaHei", 10)).grid(row=0, column=2, padx=2)
        self.slot_end = tk.Entry(entry_frame, width=18, font=("Microsoft YaHei", 10))
        self.slot_end.grid(row=0, column=3, padx=2)
        self.slot_end.insert(0, "2026-06-15 11:00")
        tk.Button(entry_frame, text="添加", bg="#3498db", fg="white", width=8,
                  font=("Microsoft YaHei", 10), command=self._on_add_slot).grid(row=0, column=4, padx=6)
        tk.Button(entry_frame, text="删除选中", bg="#e74c3c", fg="white", width=10,
                  font=("Microsoft YaHei", 10), command=self._on_remove_slot).grid(row=0, column=5, padx=2)

        list_frame = tk.Frame(slots_frame, bg="#f5f6fa")
        list_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=6)
        self.slot_list = tk.Listbox(list_frame, font=("Microsoft YaHei", 10), height=6)
        self.slot_list.pack(fill=tk.BOTH, expand=True, side=tk.LEFT)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.slot_list.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.slot_list.configure(yscrollcommand=sb.set)

        tk.Label(self, text="备注:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15, pady=(4, 2))
        self.note_entry = tk.Entry(self, font=("Microsoft YaHei", 10))
        self.note_entry.pack(fill=tk.X, padx=15, pady=2)

        btn_frame = tk.Frame(self, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=15, pady=12)
        tk.Button(btn_frame, text="提交改约申请", bg="#16a085", fg="white", width=14,
                  font=("Microsoft YaHei", 10), command=self._on_submit).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="取消", bg="#7f8c8d", fg="white", width=10,
                  font=("Microsoft YaHei", 10), command=self.destroy).pack(side=tk.RIGHT, padx=5)

    def _on_add_slot(self):
        s = self.slot_start.get().strip()
        e = self.slot_end.get().strip()
        slot = RescheduleCandidateSlot(s, e)
        if not slot.is_valid():
            messagebox.showwarning("提示", "时间窗格式非法或结束时间不晚于开始时间", parent=self)
            return
        self._slots.append(slot)
        self.slot_list.insert(tk.END, f"{s} ~ {e}")

    def _on_remove_slot(self):
        sel = self.slot_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self.slot_list.delete(idx)
        del self._slots[idx]

    def _on_submit(self):
        reason = self.reason_text.get("1.0", tk.END).strip()
        note = self.note_entry.get().strip()
        if not reason:
            messagebox.showwarning("提示", "请填写改约原因", parent=self)
            return
        if not self._slots:
            messagebox.showwarning("提示", "请至少添加一个候选时间窗", parent=self)
            return
        try:
            req = self.store.create_reschedule_request(
                self.order.order_id, self.dispatcher, reason, list(self._slots), note
            )
            self.result = req
            messagebox.showinfo("成功", f"改约申请已提交: {req.reschedule_id}", parent=self)
            self.destroy()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("提交失败", str(e), parent=self)


class ConfirmRescheduleDialog(tk.Toplevel):
    def __init__(self, parent, store, confirmer, request: RescheduleRequest):
        super().__init__(parent)
        self.store = store
        self.confirmer = confirmer
        self.request = request
        self.result = None
        self.title("确认/拒绝改约申请")
        self.geometry("600x540")
        self.configure(bg="#f5f6fa")
        self.grab_set()
        self.transient(parent)
        self._build_ui()

    def _build_ui(self):
        info = tk.Frame(self, bg="#f5f6fa")
        info.pack(fill=tk.X, padx=15, pady=10)
        tk.Label(info, text=f"改约编号: {self.request.reschedule_id}",
                 font=("Microsoft YaHei", 11, "bold"), bg="#f5f6fa").grid(row=0, column=0, sticky="w")
        tk.Label(info, text=f"工单: {self.request.order_id}  {self.request.order_title}",
                 font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=1, column=0, sticky="w", pady=2)
        tk.Label(info, text=f"改约原因: {self.request.reason}",
                 font=("Microsoft YaHei", 10), bg="#f5f6fa").grid(row=2, column=0, sticky="w", pady=2)
        tk.Label(info, text=f"备注: {self.request.note or '(无)'}",
                 font=("Microsoft YaHei", 10), bg="#f5f6fa", fg="#555").grid(row=3, column=0, sticky="w", pady=2)

        tk.Label(self, text="请选择一个候选时间窗确认:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15, pady=(6, 2))

        self.slot_var = tk.StringVar()
        slots_frame = tk.Frame(self, bg="#f5f6fa")
        slots_frame.pack(fill=tk.X, padx=15, pady=4)
        for i, slot in enumerate(self.request.candidate_slots):
            text = f"{slot.start_time} ~ {slot.end_time}"
            rb = tk.Radiobutton(slots_frame, text=text, variable=self.slot_var,
                                value=f"{slot.start_time}|{slot.end_time}",
                                font=("Microsoft YaHei", 10), bg="#f5f6fa", anchor="w")
            rb.pack(fill=tk.X, padx=4, pady=2)
            if i == 0:
                rb.select()

        tk.Label(self, text="确认备注:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15, pady=(8, 2))
        self.note_entry = tk.Entry(self, font=("Microsoft YaHei", 10))
        self.note_entry.pack(fill=tk.X, padx=15, pady=2)

        tk.Label(self, text="若拒绝，请填写拒绝原因:", font=("Microsoft YaHei", 10, "bold"),
                 bg="#f5f6fa").pack(anchor="w", padx=15, pady=(8, 2))
        self.reject_text = tk.Text(self, height=3, font=("Microsoft YaHei", 10))
        self.reject_text.pack(fill=tk.X, padx=15, pady=2)

        btn_frame = tk.Frame(self, bg="#f5f6fa")
        btn_frame.pack(fill=tk.X, padx=15, pady=15)
        tk.Button(btn_frame, text="确认改约", bg="#27ae60", fg="white", width=12,
                  font=("Microsoft YaHei", 10), command=self._on_confirm).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="拒绝改约", bg="#e74c3c", fg="white", width=12,
                  font=("Microsoft YaHei", 10), command=self._on_reject).pack(side=tk.RIGHT, padx=5)
        tk.Button(btn_frame, text="取消", bg="#7f8c8d", fg="white", width=10,
                  font=("Microsoft YaHei", 10), command=self.destroy).pack(side=tk.RIGHT, padx=5)

    def _on_confirm(self):
        val = self.slot_var.get()
        if not val:
            messagebox.showwarning("提示", "请选择一个时间窗", parent=self)
            return
        s, e = val.split("|")
        slot = RescheduleCandidateSlot(s, e)
        note = self.note_entry.get().strip()
        try:
            req, log = self.store.confirm_reschedule_request(
                self.request.reschedule_id, self.confirmer, "confirm",
                selected_slot=slot, note=note
            )
            self.result = (req, log)
            messagebox.showinfo("成功", f"改约已确认，工单日程已更新为: {s} ~ {e}", parent=self)
            self.destroy()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("操作失败", str(e), parent=self)

    def _on_reject(self):
        reason = self.reject_text.get("1.0", tk.END).strip()
        if not reason:
            messagebox.showwarning("提示", "请填写拒绝原因", parent=self)
            return
        note = self.note_entry.get().strip()
        try:
            req, log = self.store.confirm_reschedule_request(
                self.request.reschedule_id, self.confirmer, "reject",
                reject_reason=reason, note=note
            )
            self.result = (req, log)
            messagebox.showinfo("成功", "改约已拒绝", parent=self)
            self.destroy()
        except (WorkOrderError, PermissionError) as e:
            messagebox.showerror("操作失败", str(e), parent=self)


def main():
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except tk.TclError:
        pass
    app = MaintenanceApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()