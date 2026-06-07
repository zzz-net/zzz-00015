import json
import os
import csv
import threading
import uuid
import shutil
from datetime import datetime
from typing import List, Dict, Optional, Tuple
from models import (
    WorkOrder,
    User,
    Role,
    Status,
    STATUS_FLOW,
    ROLE_PERMISSIONS,
    StatusHistory,
    AppConfig,
)


class WorkOrderError(Exception):
    pass


class PermissionError(WorkOrderError):
    pass


class StatusTransitionError(WorkOrderError):
    pass


class ConcurrentOperationError(WorkOrderError):
    pass


class ExportError(WorkOrderError):
    pass


class DataStore:
    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
        self.orders_file = os.path.join(data_dir, "work_orders.json")
        self.users_file = os.path.join(data_dir, "users.json")
        self.config_file = os.path.join(data_dir, "config.json")
        self._lock = threading.RLock()
        self._orders: Dict[str, WorkOrder] = {}
        self._users: Dict[str, User] = {}
        self._config: AppConfig = AppConfig()
        self._ensure_data_dir()
        self._load_all()

    def _ensure_data_dir(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)

    def _load_all(self):
        self._load_users()
        self._load_orders()
        self._load_config()
        if not self._users:
            self._init_default_users()
            self._save_users()

    def _load_users(self):
        if os.path.exists(self.users_file):
            try:
                with open(self.users_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._users = {u["user_id"]: User.from_dict(u) for u in data}
            except (json.JSONDecodeError, KeyError):
                self._users = {}

    def _save_users(self):
        with open(self.users_file, "w", encoding="utf-8") as f:
            json.dump([u.to_dict() for u in self._users.values()], f, ensure_ascii=False, indent=2)

    def _load_orders(self):
        if os.path.exists(self.orders_file):
            try:
                with open(self.orders_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._orders = {o["order_id"]: WorkOrder.from_dict(o) for o in data}
            except (json.JSONDecodeError, KeyError):
                self._orders = {}

    def _save_orders(self):
        with open(self.orders_file, "w", encoding="utf-8") as f:
            json.dump([o.to_dict() for o in self._orders.values()], f, ensure_ascii=False, indent=2)

    def _load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._config = AppConfig.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                self._config = AppConfig()

    def _save_config(self):
        with open(self.config_file, "w", encoding="utf-8") as f:
            json.dump(self._config.to_dict(), f, ensure_ascii=False, indent=2)

    def _init_default_users(self):
        default_users = [
            User("u001", "张调度", Role.DISPATCHER),
            User("u002", "李维修", Role.TECHNICIAN),
            User("u003", "王维修", Role.TECHNICIAN),
            User("u004", "赵验收", Role.INSPECTOR),
        ]
        for u in default_users:
            self._users[u.user_id] = u

    def _check_permission(self, user: User, action: str):
        if action not in ROLE_PERMISSIONS.get(user.role, []):
            raise PermissionError(f"用户【{user.name}】({user.role.value}) 无权执行此操作: {action}")

    def _check_transition(self, order: WorkOrder, target_status: Status):
        allowed = STATUS_FLOW.get(order.status, [])
        if target_status not in allowed:
            raise StatusTransitionError(
                f"工单状态不允许从【{order.status.value}】流转到【{target_status.value}】"
            )

    def _add_history(self, order: WorkOrder, status: Status, user: User, note: str = ""):
        order.history.append(
            StatusHistory(
                status,
                user.user_id,
                user.name,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                note,
            )
        )

    def get_user(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    def get_all_users(self) -> List[User]:
        return list(self._users.values())

    def get_users_by_role(self, role: Role) -> List[User]:
        return [u for u in self._users.values() if u.role == role]

    def get_order(self, order_id: str) -> Optional[WorkOrder]:
        return self._orders.get(order_id)

    def get_all_orders(self) -> List[WorkOrder]:
        return list(self._orders.values())

    def get_orders_by_filter(
        self,
        status: Optional[Status] = None,
        location: Optional[str] = None,
        category: Optional[str] = None,
        priority: Optional[str] = None,
        assignee_id: Optional[str] = None,
    ) -> List[WorkOrder]:
        result = list(self._orders.values())
        if status:
            result = [o for o in result if o.status == status]
        if location:
            result = [o for o in result if location in o.location]
        if category:
            result = [o for o in result if category in o.category]
        if priority:
            result = [o for o in result if o.priority == priority]
        if assignee_id:
            result = [o for o in result if o.assignee_id == assignee_id]
        result.sort(key=lambda o: o.created_at, reverse=True)
        return result

    def get_config(self) -> AppConfig:
        return self._config

    def set_export_dir(self, path: str):
        with self._lock:
            self._config.export_dir = path
            self._save_config()

    def create_order(
        self,
        title: str,
        description: str,
        location: str,
        category: str,
        priority: str,
        creator: User,
    ) -> WorkOrder:
        self._check_permission(creator, "create")
        with self._lock:
            order_id = "WO" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()
            order = WorkOrder(
                order_id=order_id,
                title=title,
                description=description,
                location=location,
                category=category,
                priority=priority,
                creator_id=creator.user_id,
                creator_name=creator.name,
            )
            self._orders[order_id] = order
            self._save_orders()
            return order

    def dispatch_order(self, order_id: str, assignee: User, dispatcher: User) -> WorkOrder:
        self._check_permission(dispatcher, "dispatch")
        if assignee.role != Role.TECHNICIAN:
            raise WorkOrderError(f"只能派工给维修员，【{assignee.name}】不是维修员")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            self._check_transition(order, Status.DISPATCHED)
            order.status = Status.DISPATCHED
            order.assignee_id = assignee.user_id
            order.assignee_name = assignee.name
            self._add_history(order, Status.DISPATCHED, dispatcher, f"派工给 {assignee.name}")
            self._save_orders()
            return order

    def accept_order(self, order_id: str, technician: User) -> WorkOrder:
        self._check_permission(technician, "accept")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            if order.status != Status.DISPATCHED:
                raise ConcurrentOperationError(
                    f"抢单失败！工单当前状态为【{order.status.value}】，已被他人处理"
                )
            if order.assignee_id and order.assignee_id != technician.user_id:
                raise ConcurrentOperationError(
                    f"抢单失败！该工单已被派给【{order.assignee_name}】，您不是指定维修员"
                )
            self._check_transition(order, Status.IN_PROGRESS)
            order.status = Status.IN_PROGRESS
            order.assignee_id = technician.user_id
            order.assignee_name = technician.name
            self._add_history(order, Status.IN_PROGRESS, technician, "接单处理")
            self._save_orders()
            return order

    def complete_order(self, order_id: str, technician: User) -> WorkOrder:
        self._check_permission(technician, "complete")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            if order.assignee_id != technician.user_id:
                raise PermissionError(f"您【{technician.name}】不是该工单的指定维修员，无法完工")
            self._check_transition(order, Status.PENDING_INSPECTION)
            order.status = Status.PENDING_INSPECTION
            self._add_history(order, Status.PENDING_INSPECTION, technician, "维修完工，申请验收")
            self._save_orders()
            return order

    def approve_order(self, order_id: str, inspector: User) -> WorkOrder:
        self._check_permission(inspector, "approve")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            self._check_transition(order, Status.COMPLETED)
            order.status = Status.COMPLETED
            self._add_history(order, Status.COMPLETED, inspector, "验收通过，工单完成")
            self._save_orders()
            return order

    def reject_order(self, order_id: str, inspector: User, reason: str) -> WorkOrder:
        self._check_permission(inspector, "reject")
        if not reason or not reason.strip():
            raise WorkOrderError("退回工单必须填写退回原因")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            self._check_transition(order, Status.IN_PROGRESS)
            order.status = Status.IN_PROGRESS
            self._add_history(order, Status.IN_PROGRESS, inspector, f"验收退回: {reason}")
            self._save_orders()
            return order

    def add_exception_note(self, order_id: str, note: str):
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                return
            order.add_exception_note(note)
            self._save_orders()

    def import_orders_csv(self, filepath: str, creator: User) -> Tuple[int, List[str]]:
        self._check_permission(creator, "import")
        if not os.path.exists(filepath):
            raise WorkOrderError(f"文件不存在: {filepath}")
        imported = 0
        errors = []
        with self._lock:
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader, start=2):
                        try:
                            title = (row.get("title") or row.get("标题") or "").strip()
                            description = (row.get("description") or row.get("描述") or "").strip()
                            location = (row.get("location") or row.get("位置") or "").strip()
                            category = (row.get("category") or row.get("类别") or "").strip()
                            priority = (row.get("priority") or row.get("优先级") or "中").strip()
                            if not title:
                                errors.append(f"第{i}行: 标题不能为空")
                                continue
                            self.create_order(title, description, location, category, priority, creator)
                            imported += 1
                        except Exception as e:
                            errors.append(f"第{i}行: {str(e)}")
            except UnicodeDecodeError:
                with open(filepath, "r", encoding="gbk") as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader, start=2):
                        try:
                            title = (row.get("title") or row.get("标题") or "").strip()
                            description = (row.get("description") or row.get("描述") or "").strip()
                            location = (row.get("location") or row.get("位置") or "").strip()
                            category = (row.get("category") or row.get("类别") or "").strip()
                            priority = (row.get("priority") or row.get("优先级") or "中").strip()
                            if not title:
                                errors.append(f"第{i}行: 标题不能为空")
                                continue
                            self.create_order(title, description, location, category, priority, creator)
                            imported += 1
                        except Exception as e:
                            errors.append(f"第{i}行: {str(e)}")
        return imported, errors

    def _test_writable(self, directory: str) -> bool:
        test_file = os.path.join(directory, f".write_test_{os.getpid()}_{uuid.uuid4().hex[:8]}.tmp")
        try:
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return True
        except OSError:
            try:
                if os.path.exists(test_file):
                    os.remove(test_file)
            except OSError:
                pass
            return False

    def _get_export_path(self, filename: str) -> str:
        export_dir = self._config.export_dir
        if not export_dir:
            export_dir = os.path.join(os.getcwd(), "exports")
        if not os.path.exists(export_dir):
            try:
                os.makedirs(export_dir)
            except OSError as e:
                raise ExportError(f"无法创建导出目录: {export_dir}. 错误: {str(e)}")
        if not os.path.isdir(export_dir):
            raise ExportError(f"导出路径不是有效目录: {export_dir}")
        if not self._test_writable(export_dir):
            raise ExportError(f"导出目录不可写: {export_dir}. 请检查目录权限或更换导出目录")
        return os.path.join(export_dir, filename)

    def export_orders_json(self, orders: Optional[List[WorkOrder]] = None) -> str:
        orders = orders or self.get_all_orders()
        filepath = self._get_export_path(f"work_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump([o.to_dict() for o in orders], f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_orders_csv(self, orders: Optional[List[WorkOrder]] = None) -> str:
        orders = orders or self.get_all_orders()
        filepath = self._get_export_path(f"work_orders_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "工单编号", "标题", "描述", "位置", "类别", "优先级",
                    "状态", "创建人", "创建时间", "维修员", "异常备注数", "历史记录数"
                ])
                for o in orders:
                    writer.writerow([
                        o.order_id, o.title, o.description, o.location, o.category,
                        o.priority, o.status.value, o.creator_name, o.created_at,
                        o.assignee_name or "未指派", str(len(o.exception_notes)), str(len(o.history))
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath
