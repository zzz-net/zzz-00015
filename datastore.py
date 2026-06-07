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
    REASSIGNABLE_STATUSES,
    ROLE_PERMISSIONS,
    StatusHistory,
    AppConfig,
    TimeSlot,
    ReassignmentLog,
    MatchResult,
    CATEGORY_SKILL_MAP,
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
            User("u002", "李维修", Role.TECHNICIAN, skills=["空调", "电路"]),
            User("u003", "王维修", Role.TECHNICIAN, skills=["水管", "电梯"]),
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

    # ----- Technician Schedule Management -----

    def get_technician_load(self, user_id: str) -> int:
        user = self._users.get(user_id)
        if not user or user.role != Role.TECHNICIAN:
            return 0
        active_statuses = [Status.DISPATCHED, Status.IN_PROGRESS, Status.PENDING_INSPECTION]
        return sum(
            1 for o in self._orders.values()
            if o.assignee_id == user_id and o.status in active_statuses
        )

    def get_technician_schedule(self, user_id: str) -> Optional[Dict]:
        user = self._users.get(user_id)
        if not user:
            return None
        return {
            "skills": list(user.skills),
            "max_parallel_orders": user.max_parallel_orders,
            "time_slots": [ts.to_dict() for ts in user.time_slots],
            "current_load": self.get_technician_load(user_id),
        }

    def set_technician_skills(
        self,
        user_id: str,
        skills: List[str],
        dispatcher: User,
    ) -> User:
        self._check_permission(dispatcher, "manage_schedule")
        with self._lock:
            user = self._users.get(user_id)
            if not user:
                raise WorkOrderError(f"用户不存在: {user_id}")
            if user.role != Role.TECHNICIAN:
                raise WorkOrderError(f"只能设置维修员技能，【{user.name}】不是维修员")
            seen = set()
            clean_skills = []
            for s in skills:
                s = s.strip()
                if not s:
                    continue
                if s in seen:
                    raise WorkOrderError(f"重复的技能标签: {s}")
                seen.add(s)
                clean_skills.append(s)
            user.skills = clean_skills
            self._save_users()
            return user

    def set_technician_max_parallel(
        self,
        user_id: str,
        max_parallel: int,
        dispatcher: User,
    ) -> User:
        self._check_permission(dispatcher, "manage_schedule")
        with self._lock:
            user = self._users.get(user_id)
            if not user:
                raise WorkOrderError(f"用户不存在: {user_id}")
            if user.role != Role.TECHNICIAN:
                raise WorkOrderError(f"只能设置维修员并行数，【{user.name}】不是维修员")
            if not isinstance(max_parallel, int) or max_parallel < 1:
                raise WorkOrderError("最大并行工单数必须是大于等于1的正整数")
            user.max_parallel_orders = max_parallel
            self._save_users()
            return user

    def set_technician_time_slots(
        self,
        user_id: str,
        time_slots: List[TimeSlot],
        dispatcher: User,
    ) -> User:
        self._check_permission(dispatcher, "manage_schedule")
        with self._lock:
            user = self._users.get(user_id)
            if not user:
                raise WorkOrderError(f"用户不存在: {user_id}")
            if user.role != Role.TECHNICIAN:
                raise WorkOrderError(f"只能设置维修员排班，【{user.name}】不是维修员")
            for ts in time_slots:
                if not ts.is_valid():
                    raise WorkOrderError(f"非法时段: 星期{ts.day_of_week} {ts.start_time}-{ts.end_time}")
            seen = set()
            for ts in time_slots:
                key = (ts.day_of_week, ts.start_time, ts.end_time)
                if key in seen:
                    raise WorkOrderError(f"重复的时段: {ts}")
                seen.add(key)
            user.time_slots = list(time_slots)
            self._save_users()
            return user

    def add_technician_time_slot(
        self,
        user_id: str,
        slot: TimeSlot,
        dispatcher: User,
    ) -> User:
        self._check_permission(dispatcher, "manage_schedule")
        if not slot.is_valid():
            raise WorkOrderError(f"非法时段: 星期{slot.day_of_week} {slot.start_time}-{slot.end_time}")
        with self._lock:
            user = self._users.get(user_id)
            if not user:
                raise WorkOrderError(f"用户不存在: {user_id}")
            if user.role != Role.TECHNICIAN:
                raise WorkOrderError(f"只能设置维修员排班，【{user.name}】不是维修员")
            for existing in user.time_slots:
                if (existing.day_of_week == slot.day_of_week and
                        existing.start_time == slot.start_time and
                        existing.end_time == slot.end_time):
                    raise WorkOrderError(f"时段已存在: {slot}")
            user.time_slots.append(slot)
            self._save_users()
            return user

    # ----- Match Scoring -----

    def calculate_match(
        self,
        order: WorkOrder,
        technician: User,
        at_time: Optional[datetime] = None,
    ) -> MatchResult:
        if technician.role != Role.TECHNICIAN:
            return MatchResult(False, False, False, 0, 0, ["不是维修员"])
        skill_match = technician.has_skill_for_category(order.category)
        available_now = technician.is_available_at(at_time)
        load = self.get_technician_load(technician.user_id)
        within_capacity = load < technician.max_parallel_orders
        warnings = []
        if not skill_match:
            required = CATEGORY_SKILL_MAP.get(order.category, "通用")
            warnings.append(f"缺少技能: {required}")
        if not available_now:
            warnings.append("当前不在可接单时段")
        if not within_capacity:
            warnings.append(f"已达负载上限 {load}/{technician.max_parallel_orders}")
        return MatchResult(
            skill_match, available_now, within_capacity,
            load, technician.max_parallel_orders, warnings,
        )

    def rank_technicians_for_order(
        self,
        order: WorkOrder,
    ) -> List[Tuple[User, MatchResult]]:
        techs = self.get_users_by_role(Role.TECHNICIAN)
        results = []
        for t in techs:
            results.append((t, self.calculate_match(order, t)))
        results.sort(key=lambda x: x[1].score, reverse=True)
        return results

    # ----- Core Order Operations -----

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

    # ----- Reassignment -----

    def can_reassign(self, order: WorkOrder, dispatcher: User) -> Tuple[bool, str]:
        if order.status == Status.COMPLETED:
            return False, "已完成工单不允许改派"
        if order.status not in REASSIGNABLE_STATUSES:
            return False, f"状态【{order.status.value}】不允许改派"
        info = REASSIGNABLE_STATUSES[order.status]
        return True, info["reason"]

    def reassign_order(
        self,
        order_id: str,
        new_assignee: User,
        dispatcher: User,
        reason: str,
        expected_version: Optional[int] = None,
    ) -> WorkOrder:
        self._check_permission(dispatcher, "reassign")
        if not reason or not reason.strip():
            raise WorkOrderError("改派必须填写原因")
        if new_assignee.role != Role.TECHNICIAN:
            raise WorkOrderError(f"只能改派给维修员，【{new_assignee.name}】不是维修员")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")

            allowed, msg = self.can_reassign(order, dispatcher)
            if not allowed:
                raise WorkOrderError(f"改派被拒绝: {msg}")

            if expected_version is not None and order.version != expected_version:
                raise ConcurrentOperationError(
                    f"并发冲突: 工单已被他人修改，请刷新后重试 (当前版本v{order.version}，期望v{expected_version})"
                )

            if order.assignee_id == new_assignee.user_id:
                raise WorkOrderError(f"新维修员与当前维修员相同，无需改派")

            from_uid = order.assignee_id or ""
            from_uname = order.assignee_name or "(未指派)"

            log = ReassignmentLog(
                order_id=order_id,
                from_user_id=from_uid,
                from_user_name=from_uname,
                to_user_id=new_assignee.user_id,
                to_user_name=new_assignee.name,
                reason=reason.strip(),
                dispatcher_id=dispatcher.user_id,
                dispatcher_name=dispatcher.name,
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

            order.add_reassignment_log(log)
            order.assignee_id = new_assignee.user_id
            order.assignee_name = new_assignee.name

            if order.status == Status.PENDING_DISPATCH:
                order.status = Status.DISPATCHED
                self._add_history(order, Status.DISPATCHED, dispatcher, f"改派给 {new_assignee.name}，原因: {reason.strip()}")
            elif order.status == Status.DISPATCHED:
                self._add_history(
                    order,
                    Status.DISPATCHED,
                    dispatcher,
                    f"从 {from_uname} 改派给 {new_assignee.name}，原因: {reason.strip()}",
                )
            else:
                old_status = order.status
                order.status = Status.DISPATCHED
                self._add_history(
                    order,
                    Status.DISPATCHED,
                    dispatcher,
                    f"从 {from_uname} 改派给 {new_assignee.name}，原因: {reason.strip()}（由{old_status.value}重置为已派单，新维修员需接单）",
                )

            order.bump_version()
            self._save_orders()
            return order

    def get_reassignment_logs(self, order_id: str) -> List[ReassignmentLog]:
        order = self._orders.get(order_id)
        if order:
            return list(order.reassignment_logs)
        return []

    # ----- Import -----

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

    def import_technicians_csv(self, filepath: str, dispatcher: User) -> Tuple[int, List[str]]:
        self._check_permission(dispatcher, "manage_schedule")
        if not os.path.exists(filepath):
            raise WorkOrderError(f"文件不存在: {filepath}")
        imported = 0
        errors = []
        temp_changes = []

        def parse_row(row, i):
            uid = (row.get("user_id") or row.get("用户ID") or "").strip()
            skills_raw = (row.get("skills") or row.get("技能") or "").strip()
            max_raw = (row.get("max_parallel") or row.get("最大并行") or "").strip()
            slots_raw = (row.get("time_slots") or row.get("排班") or "").strip()

            if not uid:
                errors.append(f"第{i}行: 用户ID不能为空")
                return None

            user = self._users.get(uid)
            if not user:
                errors.append(f"第{i}行: 用户不存在: {uid}")
                return None
            if user.role != Role.TECHNICIAN:
                errors.append(f"第{i}行: 【{user.name}】不是维修员")
                return None

            skills = []
            if skills_raw:
                seen = set()
                for s in skills_raw.split("|"):
                    s = s.strip()
                    if not s:
                        continue
                    if s in seen:
                        raise WorkOrderError(f"重复技能: {s}")
                    seen.add(s)
                    skills.append(s)

            max_parallel = user.max_parallel_orders
            if max_raw:
                try:
                    max_parallel = int(max_raw)
                    if max_parallel < 1:
                        raise ValueError()
                except ValueError:
                    raise WorkOrderError(f"最大并行数必须是正整数: {max_raw}")

            slots: List[TimeSlot] = []
            if slots_raw:
                seen_slots = set()
                for s in slots_raw.split("|"):
                    s = s.strip()
                    if not s:
                        continue
                    try:
                        day_str, time_str = s.split(" ", 1)
                        day_map = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
                        if day_str not in day_map:
                            raise WorkOrderError(f"无效星期: {day_str}")
                        dow = day_map[day_str]
                        start, end = time_str.split("-")
                        slot = TimeSlot(dow, start.strip(), end.strip())
                        if not slot.is_valid():
                            raise WorkOrderError(f"非法时段: {s}")
                        key = (slot.day_of_week, slot.start_time, slot.end_time)
                        if key in seen_slots:
                            raise WorkOrderError(f"重复时段: {s}")
                        seen_slots.add(key)
                        slots.append(slot)
                    except WorkOrderError:
                        raise
                    except Exception:
                        raise WorkOrderError(f"时段格式错误，应为'周一 09:00-18:00': {s}")

            return (uid, skills, max_parallel, slots)

        with self._lock:
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader, start=2):
                        try:
                            parsed = parse_row(row, i)
                            if parsed:
                                temp_changes.append(parsed)
                        except Exception as e:
                            errors.append(f"第{i}行: {str(e)}")
            except UnicodeDecodeError:
                with open(filepath, "r", encoding="gbk") as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader, start=2):
                        try:
                            parsed = parse_row(row, i)
                            if parsed:
                                temp_changes.append(parsed)
                        except Exception as e:
                            errors.append(f"第{i}行: {str(e)}")

            if errors:
                return 0, errors

            for uid, skills, max_parallel, slots in temp_changes:
                user = self._users[uid]
                user.skills = skills
                user.max_parallel_orders = max_parallel
                user.time_slots = slots
                imported += 1

            self._save_users()

        return imported, errors

    # ----- Export -----

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
            data = []
            for o in orders:
                d = o.to_dict()
                if o.assignee_id:
                    sched = self.get_technician_schedule(o.assignee_id)
                    if sched:
                        d["assignee_schedule"] = sched
                data.append(d)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
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
                    "状态", "创建人", "创建时间", "维修员",
                    "维修员技能", "维修员当前负载", "维修员最大并行",
                    "异常备注数", "历史记录数", "改派次数"
                ])
                for o in orders:
                    skills_str = ""
                    load_str = ""
                    max_str = ""
                    if o.assignee_id:
                        sched = self.get_technician_schedule(o.assignee_id)
                        if sched:
                            skills_str = ",".join(sched["skills"])
                            load_str = str(sched["current_load"])
                            max_str = str(sched["max_parallel_orders"])
                    writer.writerow([
                        o.order_id, o.title, o.description, o.location, o.category,
                        o.priority, o.status.value, o.creator_name, o.created_at,
                        o.assignee_name or "未指派",
                        skills_str, load_str, max_str,
                        str(len(o.exception_notes)), str(len(o.history)),
                        str(len(o.reassignment_logs)),
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def export_technicians_json(self) -> str:
        filepath = self._get_export_path(f"technicians_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            techs = self.get_users_by_role(Role.TECHNICIAN)
            data = []
            for t in techs:
                d = t.to_dict()
                d["current_load"] = self.get_technician_load(t.user_id)
                data.append(d)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_technicians_csv(self) -> str:
        filepath = self._get_export_path(f"technicians_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "用户ID", "姓名", "技能", "最大并行工单", "当前负载", "排班时段"
                ])
                days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                for t in self.get_users_by_role(Role.TECHNICIAN):
                    load = self.get_technician_load(t.user_id)
                    slots_str = "|".join(
                        f"{days[ts.day_of_week]} {ts.start_time}-{ts.end_time}"
                        for ts in t.time_slots
                    )
                    writer.writerow([
                        t.user_id, t.name,
                        ",".join(t.skills),
                        t.max_parallel_orders,
                        load,
                        slots_str,
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def export_reassignment_logs_json(self, orders: Optional[List[WorkOrder]] = None) -> str:
        orders = orders or self.get_all_orders()
        filepath = self._get_export_path(f"reassignments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            logs = []
            for o in orders:
                for r in o.reassignment_logs:
                    logs.append(r.to_dict())
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_reassignment_logs_csv(self, orders: Optional[List[WorkOrder]] = None) -> str:
        orders = orders or self.get_all_orders()
        filepath = self._get_export_path(f"reassignments_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "工单编号", "原维修员", "新维修员", "改派原因", "调度员", "时间"
                ])
                for o in orders:
                    for r in o.reassignment_logs:
                        writer.writerow([
                            r.order_id, r.from_user_name, r.to_user_name,
                            r.reason, r.dispatcher_name, r.timestamp,
                        ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath
