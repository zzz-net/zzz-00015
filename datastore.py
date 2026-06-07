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
    ReassignmentDraft,
    MatchResult,
    CATEGORY_SKILL_MAP,
    BatchReassignmentDraft,
    BatchDraftItem,
    BatchReassignmentResult,
    BatchItemResult,
    ConflictType,
    RevocationRecord,
    RevocationStatus,
    RevocationConflictType,
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
        self.drafts_file = os.path.join(data_dir, "reassignment_drafts.json")
        self.batch_drafts_file = os.path.join(data_dir, "batch_reassignment_drafts.json")
        self.batch_results_file = os.path.join(data_dir, "batch_reassignment_results.json")
        self.revocation_records_file = os.path.join(data_dir, "revocation_records.json")
        self._lock = threading.RLock()
        self._orders: Dict[str, WorkOrder] = {}
        self._users: Dict[str, User] = {}
        self._config: AppConfig = AppConfig()
        self._reassignment_drafts: Dict[str, ReassignmentDraft] = {}
        self._batch_reassignment_drafts: Dict[str, BatchReassignmentDraft] = {}
        self._batch_reassignment_results: Dict[str, BatchReassignmentResult] = {}
        self._revocation_records: Dict[str, RevocationRecord] = {}
        self._ensure_data_dir()
        self._load_all()

    def _ensure_data_dir(self):
        if not os.path.exists(self.data_dir):
            os.makedirs(self.data_dir)

    def _load_all(self):
        self._load_users()
        self._load_orders()
        self._load_config()
        self._load_reassignment_drafts()
        self._load_batch_reassignment_drafts()
        self._load_batch_reassignment_results()
        self._load_revocation_records()
        self._sync_revocation_statuses()
        if not self._users:
            self._init_default_users()
            self._save_users()

    def _load_revocation_records(self):
        if os.path.exists(self.revocation_records_file):
            try:
                with open(self.revocation_records_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._revocation_records = {
                        r["revocation_id"]: RevocationRecord.from_dict(r) for r in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._revocation_records = {}

    def _save_revocation_records(self):
        with open(self.revocation_records_file, "w", encoding="utf-8") as f:
            json.dump(
                [r.to_dict() for r in self._revocation_records.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _sync_revocation_statuses(self):
        for result in self._batch_reassignment_results.values():
            for item in result.results:
                if item.revoked and item.revocation_id:
                    rev = self._revocation_records.get(item.revocation_id)
                    if rev and not rev.success:
                        item.revoked = False
                        item.revocation_status = RevocationStatus.CONFLICT_SKIPPED
                elif item.success and not item.revoked:
                    item.revocation_status = self._evaluate_revocability(item)

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

    def _load_reassignment_drafts(self):
        if os.path.exists(self.drafts_file):
            try:
                with open(self.drafts_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._reassignment_drafts = {d["order_id"]: ReassignmentDraft.from_dict(d) for d in data}
            except (json.JSONDecodeError, KeyError):
                self._reassignment_drafts = {}

    def _save_reassignment_drafts(self):
        with open(self.drafts_file, "w", encoding="utf-8") as f:
            json.dump([d.to_dict() for d in self._reassignment_drafts.values()], f, ensure_ascii=False, indent=2)

    def _load_batch_reassignment_drafts(self):
        if os.path.exists(self.batch_drafts_file):
            try:
                with open(self.batch_drafts_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._batch_reassignment_drafts = {
                        d["draft_id"]: BatchReassignmentDraft.from_dict(d) for d in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._batch_reassignment_drafts = {}

    def _save_batch_reassignment_drafts(self):
        with open(self.batch_drafts_file, "w", encoding="utf-8") as f:
            json.dump(
                [d.to_dict() for d in self._batch_reassignment_drafts.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _load_batch_reassignment_results(self):
        if os.path.exists(self.batch_results_file):
            try:
                with open(self.batch_results_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._batch_reassignment_results = {
                        r["result_id"]: BatchReassignmentResult.from_dict(r) for r in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._batch_reassignment_results = {}

    def _save_batch_reassignment_results(self):
        with open(self.batch_results_file, "w", encoding="utf-8") as f:
            json.dump(
                [r.to_dict() for r in self._batch_reassignment_results.values()],
                f, ensure_ascii=False, indent=2,
            )

    def save_batch_result(self, result: BatchReassignmentResult) -> BatchReassignmentResult:
        with self._lock:
            self._batch_reassignment_results[result.result_id] = result
            self._save_batch_reassignment_results()
            return result

    def get_batch_result(self, result_id: str) -> Optional[BatchReassignmentResult]:
        return self._batch_reassignment_results.get(result_id)

    def get_batch_results_by_dispatcher(
        self, dispatcher: User
    ) -> List[BatchReassignmentResult]:
        results = [
            r for r in self._batch_reassignment_results.values()
            if r.dispatcher_id == dispatcher.user_id
        ]
        results.sort(key=lambda r: (r.timestamp, r.result_id), reverse=True)
        return results

    def get_latest_batch_result(
        self, dispatcher: Optional[User] = None
    ) -> Optional[BatchReassignmentResult]:
        results = list(self._batch_reassignment_results.values())
        if dispatcher is not None:
            results = [r for r in results if r.dispatcher_id == dispatcher.user_id]
        if not results:
            return None
        results.sort(key=lambda r: (r.timestamp, r.result_id), reverse=True)
        return results[0]

    def _evaluate_revocability(self, item: BatchItemResult) -> str:
        if item.revoked:
            return RevocationStatus.REVOKED
        if not item.success:
            return RevocationStatus.NOT_REVOCABLE
        order = self._orders.get(item.order_id)
        if not order:
            return RevocationStatus.NOT_REVOCABLE
        if order.status == Status.COMPLETED:
            return RevocationStatus.NOT_REVOCABLE
        if order.assignee_id != item.target_technician_id:
            return RevocationStatus.NOT_REVOCABLE
        original_tech = self._users.get(item.original_assignee_id) if item.original_assignee_id else None
        if original_tech is None:
            return RevocationStatus.NOT_REVOCABLE
        if original_tech.role != Role.TECHNICIAN:
            return RevocationStatus.NOT_REVOCABLE
        return RevocationStatus.REVOCABLE

    def _find_original_status_for_revocation(
        self, order: WorkOrder, item: BatchItemResult
    ) -> Status:
        if item.original_status_snapshot:
            try:
                return Status(item.original_status_snapshot)
            except ValueError:
                pass
        target_log = None
        for log in reversed(order.reassignment_logs):
            if (log.to_user_id == item.target_technician_id and
                    log.dispatcher_id == item.operator_id):
                target_log = log
                break
        if target_log:
            for hist in reversed(order.history):
                if hist.timestamp <= target_log.timestamp:
                    try:
                        return Status(hist.status)
                    except ValueError:
                        continue
        return Status.DISPATCHED

    def _check_order_reassigned_after_batch(
        self, order: WorkOrder, item: BatchItemResult
    ) -> bool:
        if order.assignee_id != item.target_technician_id:
            return True
        batch_ts = item.item_timestamp
        for log in order.reassignment_logs:
            if log.to_user_id == item.target_technician_id and log.timestamp >= batch_ts:
                for later_log in order.reassignment_logs:
                    if later_log.timestamp > log.timestamp:
                        return True
                break
        return False

    def can_revoke_item(self, item: BatchItemResult, dispatcher: User) -> Tuple[bool, Optional[str], Optional[str]]:
        if "reassign" not in ROLE_PERMISSIONS.get(dispatcher.role, []):
            return False, RevocationConflictType.PERMISSION_DENIED, f"用户【{dispatcher.name}】无权撤销改派"
        if item.revoked:
            return False, RevocationConflictType.ALREADY_REVOKED, "该条目已被撤销"
        if not item.success:
            return False, None, "非成功改派条目不可撤销"
        order = self._orders.get(item.order_id)
        if not order:
            return False, RevocationConflictType.ORDER_NOT_FOUND, f"工单不存在: {item.order_id}"
        if order.status == Status.COMPLETED:
            return False, RevocationConflictType.ORDER_COMPLETED, "工单已完成，不可撤销"
        if self._check_order_reassigned_after_batch(order, item):
            return False, RevocationConflictType.ORDER_REASSIGNED, "工单已被他人再次改派"
        if order.assignee_id != item.target_technician_id:
            return False, RevocationConflictType.ORDER_REASSIGNED, "工单当前维修员与改派目标不一致"
        original_tech = self._users.get(item.original_assignee_id) if item.original_assignee_id else None
        if original_tech is None:
            return False, RevocationConflictType.TECHNICIAN_REMOVED, "原维修员不存在"
        if original_tech.role != Role.TECHNICIAN:
            return False, RevocationConflictType.TECHNICIAN_REMOVED, "原用户已不是维修员"
        return True, None, None

    def revoke_batch_items(
        self,
        result: BatchReassignmentResult,
        order_ids: List[str],
        dispatcher: User,
        reason: str,
    ) -> Dict:
        self._check_permission(dispatcher, "reassign")
        if not reason or not reason.strip():
            raise WorkOrderError("撤销必须填写原因")

        revocation_success = 0
        revocation_skipped = 0
        revocation_failed = 0
        revocation_records: List[RevocationRecord] = []

        with self._lock:
            result_in_store = self._batch_reassignment_results.get(result.result_id)
            if result_in_store is None:
                raise WorkOrderError(f"批量改派结果不存在: {result.result_id}")
            result = result_in_store

            order_id_set = set(order_ids)
            items_to_revoke = [r for r in result.results if r.order_id in order_id_set]

            for item in items_to_revoke:
                can_revoke, conflict_type, conflict_msg = self.can_revoke_item(item, dispatcher)

                revocation_id = "REV" + datetime.now().strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex[:4].upper()
                now_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

                if not can_revoke:
                    if conflict_type:
                        if conflict_type != RevocationConflictType.ALREADY_REVOKED:
                            item.revocation_status = RevocationStatus.CONFLICT_SKIPPED
                            item.revocation_conflict_type = conflict_type
                            item.revocation_conflict_message = conflict_msg
                            item.revocation_id = revocation_id
                        revocation_skipped += 1
                    else:
                        revocation_failed += 1
                    _order = self._orders.get(item.order_id)
                    _current_status = _order.status.value if _order else "unknown"
                    _orig_status = item.original_status_snapshot or "unknown"
                    rec = RevocationRecord(
                        revocation_id=revocation_id,
                        result_id=result.result_id,
                        draft_id=result.draft_id,
                        order_id=item.order_id,
                        operator_id=dispatcher.user_id,
                        operator_name=dispatcher.name,
                        reason=reason.strip(),
                        original_assignee_id=item.original_assignee_id,
                        original_assignee_name=item.original_assignee_name,
                        original_status=_orig_status,
                        revoked_assignee_id=item.target_technician_id,
                        revoked_assignee_name=item.target_technician_name,
                        revoked_status=_current_status,
                        timestamp=now_ts,
                        conflict_type=conflict_type,
                        conflict_message=conflict_msg,
                        success=False,
                    )
                    self._revocation_records[revocation_id] = rec
                    revocation_records.append(rec)
                    continue

                try:
                    order = self._orders[item.order_id]
                    original_tech = self._users[item.original_assignee_id]
                    original_status = self._find_original_status_for_revocation(order, item)

                    revert_log = ReassignmentLog(
                        order_id=order.order_id,
                        from_user_id=order.assignee_id or "",
                        from_user_name=order.assignee_name or "(未指派)",
                        to_user_id=original_tech.user_id,
                        to_user_name=original_tech.name,
                        reason=f"撤销改派: {reason.strip()}",
                        dispatcher_id=dispatcher.user_id,
                        dispatcher_name=dispatcher.name,
                        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    )
                    order.add_reassignment_log(revert_log)
                    order.assignee_id = original_tech.user_id
                    order.assignee_name = original_tech.name

                    old_status = order.status
                    if original_status in REASSIGNABLE_STATUSES or original_status == Status.DISPATCHED:
                        order.status = original_status
                    else:
                        order.status = Status.DISPATCHED

                    self._add_history(
                        order,
                        order.status,
                        dispatcher,
                        f"撤销改派，从 {item.target_technician_name} 恢复给 {original_tech.name}，原因: {reason.strip()}（由{old_status.value}恢复为{order.status.value}）",
                    )
                    order.bump_version()
                    self._save_orders()

                    item.revoked = True
                    item.revocation_status = RevocationStatus.REVOKED
                    item.revocation_id = revocation_id
                    item.revocation_reason = reason.strip()
                    item.revocation_operator_id = dispatcher.user_id
                    item.revocation_operator_name = dispatcher.name
                    item.revocation_timestamp = now_ts
                    item.revocation_conflict_type = None
                    item.revocation_conflict_message = None
                    item.original_status_snapshot = original_status.value

                    rec = RevocationRecord(
                        revocation_id=revocation_id,
                        result_id=result.result_id,
                        draft_id=result.draft_id,
                        order_id=item.order_id,
                        operator_id=dispatcher.user_id,
                        operator_name=dispatcher.name,
                        reason=reason.strip(),
                        original_assignee_id=original_tech.user_id,
                        original_assignee_name=original_tech.name,
                        original_status=original_status.value,
                        revoked_assignee_id=item.target_technician_id,
                        revoked_assignee_name=item.target_technician_name,
                        revoked_status=old_status.value,
                        timestamp=now_ts,
                        success=True,
                    )
                    self._revocation_records[revocation_id] = rec
                    revocation_records.append(rec)
                    revocation_success += 1

                except Exception as e:
                    revocation_failed += 1
                    err_msg = str(e)
                    item.revocation_status = RevocationStatus.CONFLICT_SKIPPED
                    item.revocation_conflict_type = RevocationConflictType.ORDER_REASSIGNED
                    item.revocation_conflict_message = f"撤销异常: {err_msg}"
                    _order_exc = self._orders.get(item.order_id)
                    _current_status_exc = _order_exc.status.value if _order_exc else "unknown"
                    _orig_status_exc = item.original_status_snapshot or "unknown"
                    rec = RevocationRecord(
                        revocation_id=revocation_id,
                        result_id=result.result_id,
                        draft_id=result.draft_id,
                        order_id=item.order_id,
                        operator_id=dispatcher.user_id,
                        operator_name=dispatcher.name,
                        reason=reason.strip(),
                        original_assignee_id=item.original_assignee_id,
                        original_assignee_name=item.original_assignee_name,
                        original_status=_orig_status_exc,
                        revoked_assignee_id=item.target_technician_id,
                        revoked_assignee_name=item.target_technician_name,
                        revoked_status=_current_status_exc,
                        timestamp=now_ts,
                        conflict_type=RevocationConflictType.ORDER_REASSIGNED,
                        conflict_message=f"撤销异常: {err_msg}",
                        success=False,
                    )
                    self._revocation_records[revocation_id] = rec
                    item.revocation_id = revocation_id
                    revocation_records.append(rec)

            self._save_revocation_records()
            self._save_batch_reassignment_results()

            for r in result.results:
                if r.success and not r.revoked and r.revocation_status != RevocationStatus.CONFLICT_SKIPPED:
                    r.revocation_status = self._evaluate_revocability(r)

        return {
            "success": revocation_success,
            "skipped": revocation_skipped,
            "failed": revocation_failed,
            "total": len(items_to_revoke),
            "records": revocation_records,
        }

    def get_revocation_records_by_result(self, result_id: str) -> List[RevocationRecord]:
        return sorted(
            [r for r in self._revocation_records.values() if r.result_id == result_id],
            key=lambda r: r.timestamp,
        )

    def get_revocation_records_by_order(self, order_id: str) -> List[RevocationRecord]:
        return sorted(
            [r for r in self._revocation_records.values() if r.order_id == order_id],
            key=lambda r: r.timestamp,
        )

    def get_all_revocation_records(self) -> List[RevocationRecord]:
        return sorted(
            list(self._revocation_records.values()),
            key=lambda r: r.timestamp,
        )

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

            if order_id in self._reassignment_drafts:
                del self._reassignment_drafts[order_id]
                self._save_reassignment_drafts()

            return order

    def get_reassignment_logs(self, order_id: str) -> List[ReassignmentLog]:
        order = self._orders.get(order_id)
        if order:
            return list(order.reassignment_logs)
        return []

    # ----- Reassignment Drafts -----

    def save_reassignment_draft(
        self,
        order_id: str,
        dispatcher: User,
        target_technician: User,
        reason: str,
    ) -> ReassignmentDraft:
        self._check_permission(dispatcher, "reassign")
        if target_technician.role != Role.TECHNICIAN:
            raise WorkOrderError(f"只能改派给维修员，【{target_technician.name}】不是维修员")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            draft = ReassignmentDraft(
                order_id=order_id,
                dispatcher_id=dispatcher.user_id,
                target_technician_id=target_technician.user_id,
                reason=reason.strip(),
                order_version=order.version,
            )
            self._reassignment_drafts[order_id] = draft
            self._save_reassignment_drafts()
            return draft

    def get_reassignment_draft(self, order_id: str, dispatcher: Optional[User] = None) -> Optional[ReassignmentDraft]:
        draft = self._reassignment_drafts.get(order_id)
        if draft is None:
            return None
        if dispatcher is not None and draft.dispatcher_id != dispatcher.user_id:
            return None
        return draft

    def delete_reassignment_draft(self, order_id: str, dispatcher: Optional[User] = None) -> bool:
        with self._lock:
            draft = self._reassignment_drafts.get(order_id)
            if draft is None:
                return False
            if dispatcher is not None and draft.dispatcher_id != dispatcher.user_id:
                return False
            del self._reassignment_drafts[order_id]
            self._save_reassignment_drafts()
            return True

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

    # ----- Batch Reassignment -----

    def generate_batch_recommendations(
        self,
        order_ids: List[str],
        dispatcher: User,
    ) -> List[BatchDraftItem]:
        self._check_permission(dispatcher, "reassign")
        items = []
        for oid in order_ids:
            order = self._orders.get(oid)
            if not order:
                continue
            allowed, _ = self.can_reassign(order, dispatcher)
            if not allowed:
                continue
            ranked = self.rank_technicians_for_order(order)
            best_tech, best_match = ranked[0] if ranked else (None, None)
            if best_tech and best_match:
                default_reason = REASSIGNABLE_STATUSES.get(order.status, {}).get("reason", "批量改派")
                item = BatchDraftItem(
                    order_id=order.order_id,
                    target_technician_id=best_tech.user_id,
                    reason=default_reason,
                    order_version=order.version,
                    order_status=order.status.value,
                    original_assignee_id=order.assignee_id,
                    recommended=best_match.is_recommended,
                    risk_warnings=list(best_match.warnings),
                    match_score=best_match.score,
                    tech_skills_snapshot=list(best_tech.skills),
                    tech_schedule_snapshot=[ts.to_dict() for ts in best_tech.time_slots],
                    tech_max_parallel_snapshot=best_tech.max_parallel_orders,
                )
            else:
                item = BatchDraftItem(
                    order_id=order.order_id,
                    target_technician_id="",
                    reason="无匹配维修员",
                    order_version=order.version,
                    order_status=order.status.value,
                    original_assignee_id=order.assignee_id,
                    recommended=False,
                    risk_warnings=["无可用维修员"],
                    match_score=0,
                    tech_skills_snapshot=[],
                    tech_schedule_snapshot=[],
                    tech_max_parallel_snapshot=None,
                )
            items.append(item)
        return items

    def detect_batch_conflicts(
        self,
        draft: BatchReassignmentDraft,
    ) -> Dict[str, List[ConflictType]]:
        conflicts: Dict[str, List[ConflictType]] = {}
        for item in draft.items:
            item_conflicts: List[ConflictType] = []
            order = self._orders.get(item.order_id)
            if order is None:
                item_conflicts.append(ConflictType.ORDER_REMOVED)
                conflicts[item.order_id] = item_conflicts
                continue
            if order.version != item.order_version:
                item_conflicts.append(ConflictType.VERSION_MISMATCH)
            if order.status.value != item.order_status:
                item_conflicts.append(ConflictType.STATUS_CHANGED)
            tech = self._users.get(item.target_technician_id)
            if tech is None:
                item_conflicts.append(ConflictType.TECHNICIAN_REMOVED)
            else:
                if tech.role != Role.TECHNICIAN:
                    item_conflicts.append(ConflictType.TECHNICIAN_ROLE_CHANGED)
                else:
                    if sorted(tech.skills) != sorted(item.tech_skills_snapshot):
                        item_conflicts.append(ConflictType.TECHNICIAN_SKILLS_CHANGED)
                    if tech.max_parallel_orders != item.tech_max_parallel_snapshot:
                        item_conflicts.append(ConflictType.TECHNICIAN_CAPACITY_CHANGED)
                    current_sched_dicts = sorted(
                        [ts.to_dict() for ts in tech.time_slots],
                        key=lambda d: (d.get("day", ""), d.get("start", ""), d.get("end", "")),
                    )
                    snapshot_sched_dicts = sorted(
                        item.tech_schedule_snapshot,
                        key=lambda d: (d.get("day", ""), d.get("start", ""), d.get("end", "")),
                    )
                    if current_sched_dicts != snapshot_sched_dicts:
                        item_conflicts.append(ConflictType.TECHNICIAN_SCHEDULE_CHANGED)
            if item_conflicts:
                conflicts[item.order_id] = item_conflicts
        return conflicts

    def save_batch_reassignment_draft(
        self,
        dispatcher: User,
        items: List[BatchDraftItem],
        draft_id: Optional[str] = None,
    ) -> BatchReassignmentDraft:
        self._check_permission(dispatcher, "reassign")
        with self._lock:
            if draft_id is None:
                draft_id = "BRD" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()
            draft = BatchReassignmentDraft(
                draft_id=draft_id,
                dispatcher_id=dispatcher.user_id,
                dispatcher_name=dispatcher.name,
                items=list(items),
                updated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if draft_id in self._batch_reassignment_drafts:
                draft.created_at = self._batch_reassignment_drafts[draft_id].created_at
            self._batch_reassignment_drafts[draft_id] = draft
            self._save_batch_reassignment_drafts()
            return draft

    def get_batch_reassignment_draft(
        self,
        draft_id: str,
        dispatcher: Optional[User] = None,
    ) -> Optional[BatchReassignmentDraft]:
        draft = self._batch_reassignment_drafts.get(draft_id)
        if draft is None:
            return None
        if dispatcher is not None and draft.dispatcher_id != dispatcher.user_id:
            return None
        return draft

    def get_batch_drafts_by_dispatcher(
        self,
        dispatcher: User,
    ) -> List[BatchReassignmentDraft]:
        return [
            d for d in self._batch_reassignment_drafts.values()
            if d.dispatcher_id == dispatcher.user_id
        ]

    def delete_batch_reassignment_draft(
        self,
        draft_id: str,
        dispatcher: Optional[User] = None,
    ) -> bool:
        with self._lock:
            draft = self._batch_reassignment_drafts.get(draft_id)
            if draft is None:
                return False
            if dispatcher is not None and draft.dispatcher_id != dispatcher.user_id:
                return False
            del self._batch_reassignment_drafts[draft_id]
            self._save_batch_reassignment_drafts()
            return True

    def execute_batch_reassignment(
        self,
        draft: BatchReassignmentDraft,
        dispatcher: User,
    ) -> BatchReassignmentResult:
        self._check_permission(dispatcher, "reassign")
        result = BatchReassignmentResult(
            dispatcher_id=dispatcher.user_id,
            dispatcher_name=dispatcher.name,
            draft_id=draft.draft_id if draft.draft_id and not draft.draft_id.startswith("_") else None,
        )
        with self._lock:
            for item in draft.items:
                tech = self._users.get(item.target_technician_id)
                conflict_types: List[str] = []
                order = self._orders.get(item.order_id)

                common_fields = dict(
                    order_id=item.order_id,
                    draft_id=result.draft_id,
                    operator_id=dispatcher.user_id,
                    operator_name=dispatcher.name,
                    reason=item.reason,
                    target_technician_id=tech.user_id if tech else item.target_technician_id,
                    target_technician_name=tech.name if tech else None,
                    order_title=order.title if order else None,
                    original_assignee_id=(
                        order.assignee_id if order else item.original_assignee_id
                    ),
                    original_assignee_name=(
                        order.assignee_name if order else None
                    ),
                )

                if order is None:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message="工单不存在或已被删除",
                        conflict_types=[ConflictType.ORDER_REMOVED.value],
                        permission_checked=False,
                        version_checked=False,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                if tech is None:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message="目标维修员不存在",
                        conflict_types=[ConflictType.TECHNICIAN_REMOVED.value],
                        permission_checked=False,
                        version_checked=False,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                version_ok = order.version == item.order_version
                if not version_ok:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message=f"版本冲突：草稿v{item.order_version} vs 当前v{order.version}",
                        conflict_types=[ConflictType.VERSION_MISMATCH.value],
                        version_passed=False,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                status_ok = order.status.value == item.order_status
                if not status_ok:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message=f"状态变更：草稿【{item.order_status}】 vs 当前【{order.status.value}】",
                        conflict_types=[ConflictType.STATUS_CHANGED.value],
                        version_passed=True,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                if tech.role != Role.TECHNICIAN:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message=f"目标用户【{tech.name}】已不是维修员",
                        conflict_types=[ConflictType.TECHNICIAN_ROLE_CHANGED.value],
                        version_passed=True,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                allowed, msg = self.can_reassign(order, dispatcher)
                if not allowed:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message=msg,
                        version_passed=True,
                        permission_passed=False,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                if order.assignee_id == tech.user_id:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message="新维修员与当前维修员相同，无需改派",
                        version_passed=True,
                        permission_passed=True,
                        skill_checked=False,
                        capacity_checked=False,
                        schedule_checked=False,
                        **common_fields,
                    ))
                    continue

                current_match = self.calculate_match(order, tech)
                realtime_conflicts: List[str] = []
                skip_reasons: List[str] = []
                skill_ok = current_match.skill_match
                capacity_ok = current_match.within_capacity
                schedule_ok = current_match.available_now

                if not skill_ok:
                    realtime_conflicts.append(ConflictType.TECHNICIAN_SKILLS_CHANGED.value)
                    required = CATEGORY_SKILL_MAP.get(order.category, "通用")
                    skip_reasons.append(f"目标维修员【{tech.name}】缺少所需技能: {required}")
                if not capacity_ok:
                    realtime_conflicts.append(ConflictType.TECHNICIAN_CAPACITY_CHANGED.value)
                    skip_reasons.append(
                        f"目标维修员【{tech.name}】已达负载上限 {current_match.current_load}/{current_match.max_parallel}"
                    )
                if not schedule_ok:
                    realtime_conflicts.append(ConflictType.TECHNICIAN_SCHEDULE_CHANGED.value)

                if skip_reasons:
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message="；".join(skip_reasons),
                        conflict_types=realtime_conflicts,
                        version_passed=True,
                        permission_passed=True,
                        skill_passed=skill_ok,
                        capacity_passed=capacity_ok,
                        schedule_passed=schedule_ok,
                        **common_fields,
                    ))
                    continue

                conflict_types.extend(realtime_conflicts)

                log_written = False
                log_write_error = None
                try:
                    logs_before = len(order.reassignment_logs)
                    self.reassign_order(
                        order_id=item.order_id,
                        new_assignee=tech,
                        dispatcher=dispatcher,
                        reason=item.reason,
                        expected_version=item.order_version,
                    )
                    fresh_order = self._orders.get(item.order_id)
                    logs_after = len(fresh_order.reassignment_logs) if fresh_order else logs_before
                    log_written = logs_after > logs_before
                    if not log_written:
                        log_write_error = "改派执行成功但改派日志未增加"
                    result.results.append(BatchItemResult(
                        success=True,
                        conflict_types=conflict_types,
                        version_passed=True,
                        permission_passed=True,
                        skill_passed=skill_ok,
                        capacity_passed=capacity_ok,
                        schedule_passed=schedule_ok,
                        log_written=log_written,
                        log_write_error=log_write_error,
                        revocation_status=RevocationStatus.REVOCABLE,
                        original_status_snapshot=order.status.value,
                        **common_fields,
                    ))
                except (PermissionError, ConcurrentOperationError, WorkOrderError) as e:
                    err_msg = str(e)
                    is_perm_err = isinstance(e, PermissionError)
                    is_ver_err = isinstance(e, ConcurrentOperationError) or "版本" in err_msg or "并发" in err_msg
                    result.results.append(BatchItemResult(
                        success=False,
                        skipped=True,
                        error_message=err_msg,
                        conflict_types=conflict_types,
                        version_passed=(not is_ver_err),
                        permission_passed=(not is_perm_err),
                        skill_passed=skill_ok,
                        capacity_passed=capacity_ok,
                        schedule_passed=schedule_ok,
                        log_written=False,
                        log_write_error=f"改派异常: {err_msg}",
                        **common_fields,
                    ))

            if result.success_count > 0:
                success_order_ids = {r.order_id for r in result.results if r.success}
                remaining_items = [it for it in draft.items if it.order_id not in success_order_ids]
                if remaining_items:
                    draft.items = remaining_items
                    draft.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    self._batch_reassignment_drafts[draft.draft_id] = draft
                else:
                    if draft.draft_id in self._batch_reassignment_drafts:
                        del self._batch_reassignment_drafts[draft.draft_id]
                self._save_batch_reassignment_drafts()

            self.save_batch_result(result)
            return result

    def export_batch_result_json(self, result: BatchReassignmentResult) -> str:
        filepath = self._get_export_path(f"batch_reassignment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_batch_result_csv(self, result: BatchReassignmentResult) -> str:
        filepath = self._get_export_path(f"batch_reassignment_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "结果编号", "草稿编号", "工单编号", "工单标题",
                    "原维修员ID", "原维修员姓名", "新维修员ID", "新维修员姓名",
                    "提交人ID", "提交人姓名", "处理时间",
                    "执行结果", "权限校验", "版本校验", "技能校验", "容量校验", "排班校验",
                    "日志已写入", "日志写入异常",
                    "冲突类型", "改派原因", "错误/跳过原因",
                    "成功标记", "跳过标记",
                    "撤销状态", "是否已撤销", "撤销记录ID", "撤销原因",
                    "撤销操作人ID", "撤销操作人姓名", "撤销时间",
                    "撤销冲突类型", "撤销冲突描述", "原始状态快照",
                ])
                for r in result.results:
                    order = self._orders.get(r.order_id)
                    orig_assignee_id = r.original_assignee_id or (order.assignee_id if order else "")
                    orig_assignee_name = r.original_assignee_name
                    if not orig_assignee_name and order:
                        orig_assignee_name = order.assignee_name or "(未指派)"
                        for log in reversed(order.reassignment_logs):
                            if log.to_user_id == r.target_technician_id:
                                orig_assignee_name = log.from_user_name
                                orig_assignee_id = log.from_user_id
                                break
                    status_label = r.status_label
                    writer.writerow([
                        result.result_id,
                        result.draft_id or "",
                        r.order_id,
                        r.order_title or (order.title if order else ""),
                        orig_assignee_id or "",
                        orig_assignee_name or "",
                        r.target_technician_id or "",
                        r.target_technician_name or "",
                        r.operator_id or result.dispatcher_id,
                        r.operator_name or result.dispatcher_name,
                        r.item_timestamp or "",
                        status_label,
                        ("通过" if r.permission_passed else "不通过") if r.permission_checked and r.permission_passed is not None else ("未检查" if not r.permission_checked else ""),
                        ("通过" if r.version_passed else "不通过") if r.version_checked and r.version_passed is not None else ("未检查" if not r.version_checked else ""),
                        ("通过" if r.skill_passed else "不通过") if r.skill_checked and r.skill_passed is not None else ("未检查" if not r.skill_checked else ""),
                        ("通过" if r.capacity_passed else "不通过") if r.capacity_checked and r.capacity_passed is not None else ("未检查" if not r.capacity_checked else ""),
                        ("通过" if r.schedule_passed else "不通过") if r.schedule_checked and r.schedule_passed is not None else ("未检查" if not r.schedule_checked else ""),
                        "是" if r.log_written else "否",
                        r.log_write_error or "",
                        ",".join(r.conflict_types) if r.conflict_types else "",
                        r.reason or "",
                        r.error_message or "",
                        "是" if r.success else "否",
                        "是" if r.skipped else "否",
                        r.revocation_status_label,
                        "是" if r.revoked else "否",
                        r.revocation_id or "",
                        r.revocation_reason or "",
                        r.revocation_operator_id or "",
                        r.revocation_operator_name or "",
                        r.revocation_timestamp or "",
                        r.revocation_conflict_type or "",
                        r.revocation_conflict_message or "",
                        r.original_status_snapshot or "",
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath
