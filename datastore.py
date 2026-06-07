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
    SparePart,
    SparePartRequest,
    SparePartRequestStatus,
    SparePartAuditLog,
    RescheduleStatus,
    RescheduleCandidateSlot,
    RescheduleRequest,
    RescheduleConfirmLog,
    ArrivalConfirmation,
    RESCHEDULE_STATUS_FLOW,
    RESCHEDULE_DECISIONS,
    RESCHEDULEABLE_ORDER_STATUSES,
    ARRIVAL_CONFIRMABLE_STATUSES,
    RescheduleRuleViolation,
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
        self.spare_parts_file = os.path.join(data_dir, "spare_parts.json")
        self.spare_part_requests_file = os.path.join(data_dir, "spare_part_requests.json")
        self.spare_part_audit_logs_file = os.path.join(data_dir, "spare_part_audit_logs.json")
        self.reschedule_requests_file = os.path.join(data_dir, "reschedule_requests.json")
        self.reschedule_confirm_logs_file = os.path.join(data_dir, "reschedule_confirm_logs.json")
        self.arrival_confirmations_file = os.path.join(data_dir, "arrival_confirmations.json")
        self._lock = threading.RLock()
        self._orders: Dict[str, WorkOrder] = {}
        self._users: Dict[str, User] = {}
        self._config: AppConfig = AppConfig()
        self._reassignment_drafts: Dict[str, ReassignmentDraft] = {}
        self._batch_reassignment_drafts: Dict[str, BatchReassignmentDraft] = {}
        self._batch_reassignment_results: Dict[str, BatchReassignmentResult] = {}
        self._revocation_records: Dict[str, RevocationRecord] = {}
        self._spare_parts: Dict[str, SparePart] = {}
        self._spare_part_requests: Dict[str, SparePartRequest] = {}
        self._spare_part_audit_logs: Dict[str, SparePartAuditLog] = {}
        self._reschedule_requests: Dict[str, RescheduleRequest] = {}
        self._reschedule_confirm_logs: Dict[str, RescheduleConfirmLog] = {}
        self._arrival_confirmations: Dict[str, ArrivalConfirmation] = {}
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
        self._load_spare_parts()
        self._load_spare_part_requests()
        self._load_spare_part_audit_logs()
        self._load_reschedule_requests()
        self._load_reschedule_confirm_logs()
        self._load_arrival_confirmations()
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

    def _is_assigned_technician_or_dispatcher(self, order: WorkOrder, user: User) -> bool:
        is_assigned_tech = (
            user.role == Role.TECHNICIAN and
            order.assignee_id == user.user_id
        )
        is_dispatcher = user.role == Role.DISPATCHER
        return is_assigned_tech or is_dispatcher

    def _has_pending_reschedule(self, order_id: str) -> bool:
        return any(
            r.order_id == order_id and r.status == RescheduleStatus.PENDING
            for r in self._reschedule_requests.values()
        )

    # ----- Unified Permission Pre-checks (can_xxx) -----

    def can_create_reschedule(self, order_id: str, user: User) -> Tuple[bool, str]:
        try:
            self._check_permission(user, "create_reschedule")
        except PermissionError as e:
            return False, str(e)
        order = self._orders.get(order_id)
        if not order:
            return False, f"工单不存在: {order_id}"
        if order.status == Status.COMPLETED:
            return False, RescheduleRuleViolation.ORDER_COMPLETED
        if order.status not in RESCHEDULEABLE_ORDER_STATUSES:
            return False, f"工单当前状态【{order.status.value}】不支持改约"
        if not order.assignee_id:
            return False, RescheduleRuleViolation.ORDER_NOT_DISPATCHED
        if self._has_pending_reschedule(order_id):
            return False, RescheduleRuleViolation.PENDING_EXISTS
        return True, ""

    def can_cancel_reschedule(self, reschedule_id: str, user: User) -> Tuple[bool, str]:
        try:
            self._check_permission(user, "cancel_reschedule")
        except PermissionError as e:
            return False, str(e)
        request = self._reschedule_requests.get(reschedule_id)
        if not request:
            return False, f"改约申请不存在: {reschedule_id}"
        if request.status != RescheduleStatus.PENDING:
            return False, f"只能撤销待确认状态的改约申请，当前状态: {request.status_label}"
        if request.dispatcher_id != user.user_id:
            return False, f"只有发起人【{request.dispatcher_name}】可以撤销此改约申请"
        return True, ""

    def can_confirm_reschedule(self, reschedule_id: str, user: User) -> Tuple[bool, str]:
        request = self._reschedule_requests.get(reschedule_id)
        if not request:
            return False, f"改约申请不存在: {reschedule_id}"
        if request.status != RescheduleStatus.PENDING:
            return False, f"改约申请已被处理，当前状态: {request.status_label}"
        order = self._orders.get(request.order_id)
        if not order:
            return False, f"关联工单不存在: {request.order_id}"
        if not self._is_assigned_technician_or_dispatcher(order, user):
            return False, f"只有工单指定维修员或调度员可以确认此改约申请，您【{user.name}】无权操作"
        try:
            self._check_permission(user, "confirm_reschedule")
        except PermissionError as e:
            return False, str(e)
        return True, ""

    def can_confirm_arrival(self, order_id: str, user: User) -> Tuple[bool, str]:
        try:
            self._check_permission(user, "confirm_arrival")
        except PermissionError as e:
            return False, str(e)
        order = self._orders.get(order_id)
        if not order:
            return False, f"工单不存在: {order_id}"
        if order.status == Status.COMPLETED:
            return False, "工单已完成，无需到场确认"
        if order.status not in ARRIVAL_CONFIRMABLE_STATUSES:
            return False, f"工单当前状态【{order.status.value}】不支持到场确认"
        if not self._is_assigned_technician_or_dispatcher(order, user):
            return False, f"只有工单指定维修员或调度员可以到场确认，您【{user.name}】无权操作"
        return True, ""

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

    # ----- Spare Parts Persistence -----

    def _load_spare_parts(self):
        if os.path.exists(self.spare_parts_file):
            try:
                with open(self.spare_parts_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._spare_parts = {
                        p["part_id"]: SparePart.from_dict(p) for p in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._spare_parts = {}

    def _save_spare_parts(self):
        with open(self.spare_parts_file, "w", encoding="utf-8") as f:
            json.dump(
                [p.to_dict() for p in self._spare_parts.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _load_spare_part_requests(self):
        if os.path.exists(self.spare_part_requests_file):
            try:
                with open(self.spare_part_requests_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._spare_part_requests = {
                        r["request_id"]: SparePartRequest.from_dict(r) for r in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._spare_part_requests = {}

    def _save_spare_part_requests(self):
        with open(self.spare_part_requests_file, "w", encoding="utf-8") as f:
            json.dump(
                [r.to_dict() for r in self._spare_part_requests.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _load_spare_part_audit_logs(self):
        if os.path.exists(self.spare_part_audit_logs_file):
            try:
                with open(self.spare_part_audit_logs_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._spare_part_audit_logs = {
                        l["log_id"]: SparePartAuditLog.from_dict(l) for l in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._spare_part_audit_logs = {}

    def _save_spare_part_audit_logs(self):
        with open(self.spare_part_audit_logs_file, "w", encoding="utf-8") as f:
            json.dump(
                [l.to_dict() for l in self._spare_part_audit_logs.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _add_spare_part_audit_log(
        self,
        part: SparePart,
        action: str,
        quantity: int,
        operator: User,
        stock_before: int,
        stock_after: int,
        order_id: Optional[str] = None,
        request_id: Optional[str] = None,
        note: str = "",
    ):
        log_id = "SPL" + datetime.now().strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex[:4].upper()
        log = SparePartAuditLog(
            log_id=log_id,
            part_id=part.part_id,
            part_name=part.name,
            action=action,
            quantity=quantity,
            operator_id=operator.user_id,
            operator_name=operator.name,
            order_id=order_id,
            request_id=request_id,
            note=note,
            stock_before=stock_before,
            stock_after=stock_after,
        )
        self._spare_part_audit_logs[log_id] = log
        self._save_spare_part_audit_logs()
        return log

    # ----- Spare Parts CRUD -----

    def create_spare_part(
        self,
        name: str,
        category: str,
        stock: int,
        low_stock_threshold: int,
        applicable_categories: Optional[List[str]] = None,
        unit: str = "个",
        description: str = "",
        dispatcher: Optional[User] = None,
    ) -> SparePart:
        if dispatcher is not None:
            self._check_permission(dispatcher, "manage_spare_parts")
        if not name or not name.strip():
            raise WorkOrderError("备件名称不能为空")
        if not category or not category.strip():
            raise WorkOrderError("备件类别不能为空")
        if not isinstance(stock, int) or stock < 0:
            raise WorkOrderError("库存数量必须是非负整数")
        if not isinstance(low_stock_threshold, int) or low_stock_threshold < 0:
            raise WorkOrderError("低库存阈值必须是非负整数")
        with self._lock:
            part_id = "SP" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()
            part = SparePart(
                part_id=part_id,
                name=name.strip(),
                category=category.strip(),
                stock=stock,
                low_stock_threshold=low_stock_threshold,
                applicable_categories=applicable_categories or [],
                unit=unit or "个",
                description=description,
            )
            self._spare_parts[part_id] = part
            self._save_spare_parts()
            if dispatcher is not None:
                self._add_spare_part_audit_log(
                    part, "创建", stock, dispatcher, 0, stock, note=f"创建备件档案，阈值={low_stock_threshold}"
                )
            return part

    def update_spare_part(
        self,
        part_id: str,
        dispatcher: User,
        name: Optional[str] = None,
        category: Optional[str] = None,
        stock: Optional[int] = None,
        low_stock_threshold: Optional[int] = None,
        applicable_categories: Optional[List[str]] = None,
        unit: Optional[str] = None,
        description: Optional[str] = None,
    ) -> SparePart:
        self._check_permission(dispatcher, "manage_spare_parts")
        with self._lock:
            part = self._spare_parts.get(part_id)
            if not part:
                raise WorkOrderError(f"备件不存在: {part_id}")
            stock_before = part.stock
            if name is not None:
                if not name.strip():
                    raise WorkOrderError("备件名称不能为空")
                part.name = name.strip()
            if category is not None:
                if not category.strip():
                    raise WorkOrderError("备件类别不能为空")
                part.category = category.strip()
            if stock is not None:
                if not isinstance(stock, int) or stock < 0:
                    raise WorkOrderError("库存数量必须是非负整数")
                part.stock = stock
            if low_stock_threshold is not None:
                if not isinstance(low_stock_threshold, int) or low_stock_threshold < 0:
                    raise WorkOrderError("低库存阈值必须是非负整数")
                part.low_stock_threshold = low_stock_threshold
            if applicable_categories is not None:
                part.applicable_categories = list(applicable_categories)
            if unit is not None:
                part.unit = unit or "个"
            if description is not None:
                part.description = description
            part.bump_version()
            self._save_spare_parts()
            if stock is not None and stock != stock_before:
                diff = stock - stock_before
                action = "库存调整" if diff >= 0 else "库存扣减"
                self._add_spare_part_audit_log(
                    part, action, abs(diff), dispatcher, stock_before, stock, note=f"手动调整库存: {stock_before} -> {stock}"
                )
            return part

    def delete_spare_part(self, part_id: str, dispatcher: User) -> bool:
        self._check_permission(dispatcher, "manage_spare_parts")
        with self._lock:
            part = self._spare_parts.get(part_id)
            if not part:
                return False
            pending_requests = [
                r for r in self._spare_part_requests.values()
                if r.part_id == part_id and r.status == SparePartRequestStatus.PENDING
            ]
            if pending_requests:
                raise WorkOrderError(f"该备件存在 {len(pending_requests)} 条待审核申请，无法删除")
            del self._spare_parts[part_id]
            self._save_spare_parts()
            return True

    def get_spare_part(self, part_id: str) -> Optional[SparePart]:
        return self._spare_parts.get(part_id)

    def get_all_spare_parts(self) -> List[SparePart]:
        return list(self._spare_parts.values())

    def get_spare_parts_by_filter(
        self,
        category: Optional[str] = None,
        low_stock_only: bool = False,
        order_category: Optional[str] = None,
    ) -> List[SparePart]:
        result = list(self._spare_parts.values())
        if category:
            result = [p for p in result if category in p.category]
        if low_stock_only:
            result = [p for p in result if p.is_low_stock]
        if order_category:
            result = [p for p in result if p.is_applicable_for_order_category(order_category)]
        result.sort(key=lambda p: p.updated_at, reverse=True)
        return result

    # ----- Spare Part Requests -----

    def create_spare_part_request(
        self,
        order_id: str,
        part_id: str,
        quantity: int,
        technician: User,
        reason: str = "",
    ) -> SparePartRequest:
        self._check_permission(technician, "request_spare_parts")
        if not isinstance(quantity, int) or quantity < 1:
            raise WorkOrderError("领用数量必须是大于0的正整数")
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {order_id}")
            if order.status == Status.COMPLETED:
                raise WorkOrderError(f"工单【{order_id}】已完成，不能申请领用备件")
            if order.assignee_id != technician.user_id:
                raise PermissionError(
                    f"您【{technician.name}】不是工单【{order_id}】的指定维修员，无法申请备件"
                )
            part = self._spare_parts.get(part_id)
            if not part:
                raise WorkOrderError(f"备件不存在: {part_id}")
            if not part.is_applicable_for_order_category(order.category):
                applicable = ", ".join(part.applicable_categories) if part.applicable_categories else "无限制"
                raise WorkOrderError(
                    f"备件【{part.name}】不适用于工单类别【{order.category}】，"
                    f"该备件适用类别: {applicable}"
                )
            request_id = "SPR" + datetime.now().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4].upper()
            request = SparePartRequest(
                request_id=request_id,
                order_id=order_id,
                part_id=part_id,
                part_name=part.name,
                quantity=quantity,
                applicant_id=technician.user_id,
                applicant_name=technician.name,
                reason=reason,
            )
            self._spare_part_requests[request_id] = request
            self._save_spare_part_requests()
            return request

    def get_spare_part_request(self, request_id: str) -> Optional[SparePartRequest]:
        return self._spare_part_requests.get(request_id)

    def get_spare_part_requests_by_filter(
        self,
        user: Optional[User] = None,
        order_id: Optional[str] = None,
        part_id: Optional[str] = None,
        status: Optional[SparePartRequestStatus] = None,
    ) -> List[SparePartRequest]:
        result = list(self._spare_part_requests.values())
        if user is not None and user.role == Role.TECHNICIAN:
            result = [r for r in result if r.applicant_id == user.user_id]
        if order_id:
            result = [r for r in result if r.order_id == order_id]
        if part_id:
            result = [r for r in result if r.part_id == part_id]
        if status:
            result = [r for r in result if r.status == status]
        result.sort(key=lambda r: r.created_at, reverse=True)
        return result

    def approve_spare_part_request(
        self,
        request_id: str,
        dispatcher: User,
        note: str = "",
    ) -> SparePartRequest:
        self._check_permission(dispatcher, "review_spare_part_requests")
        with self._lock:
            request = self._spare_part_requests.get(request_id)
            if not request:
                raise WorkOrderError(f"申请不存在: {request_id}")
            if request.status != SparePartRequestStatus.PENDING:
                raise WorkOrderError(
                    f"申请【{request_id}】当前状态为【{request.status.value}】，"
                    f"只有【待审核】状态可以审核"
                )
            order = self._orders.get(request.order_id)
            if not order:
                raise WorkOrderError(f"工单不存在: {request.order_id}")
            if order.status == Status.COMPLETED:
                raise WorkOrderError(
                    f"关联工单【{request.order_id}】已完成，审核被拦截"
                )
            part = self._spare_parts.get(request.part_id)
            if not part:
                raise WorkOrderError(f"备件不存在: {request.part_id}")
            if not part.is_applicable_for_order_category(order.category):
                applicable = ", ".join(part.applicable_categories) if part.applicable_categories else "无限制"
                raise WorkOrderError(
                    f"备件类别不匹配：备件【{part.name}】适用类别({applicable})"
                    f"不包含工单类别【{order.category}】，审核被拦截"
                )
            if part.stock < request.quantity:
                raise WorkOrderError(
                    f"库存不足：备件【{part.name}】当前库存{part.stock}{part.unit}，"
                    f"申请{request.quantity}{part.unit}，审核被拦截"
                )
            stock_before = part.stock
            part.stock -= request.quantity
            part.bump_version()
            request.status = SparePartRequestStatus.APPROVED
            request.reviewer_id = dispatcher.user_id
            request.reviewer_name = dispatcher.name
            request.review_note = note
            request.reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            request.bump_version()
            self._add_history(
                order, order.status, dispatcher,
                f"备件领用审核通过: {part.name} x{request.quantity}{part.unit}，"
                f"申请人: {request.applicant_name}，备注: {note or '无'}"
            )
            order.bump_version()
            self._save_spare_parts()
            self._save_spare_part_requests()
            self._save_orders()
            self._add_spare_part_audit_log(
                part, "审核领用", request.quantity, dispatcher,
                stock_before, part.stock,
                order_id=order.order_id, request_id=request.request_id,
                note=note or f"工单{order.order_id}领用审核通过"
            )
            return request

    def reject_spare_part_request(
        self,
        request_id: str,
        dispatcher: User,
        note: str = "",
    ) -> SparePartRequest:
        self._check_permission(dispatcher, "review_spare_part_requests")
        if not note or not note.strip():
            raise WorkOrderError("拒绝申请必须填写原因")
        with self._lock:
            request = self._spare_part_requests.get(request_id)
            if not request:
                raise WorkOrderError(f"申请不存在: {request_id}")
            if request.status != SparePartRequestStatus.PENDING:
                raise WorkOrderError(
                    f"申请【{request_id}】当前状态为【{request.status.value}】，"
                    f"只有【待审核】状态可以拒绝"
                )
            request.status = SparePartRequestStatus.REJECTED
            request.reviewer_id = dispatcher.user_id
            request.reviewer_name = dispatcher.name
            request.review_note = note.strip()
            request.reviewed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            request.bump_version()
            self._save_spare_part_requests()
            return request

    def return_spare_part(
        self,
        request_id: str,
        user: User,
        note: str = "",
    ) -> SparePartRequest:
        with self._lock:
            request = self._spare_part_requests.get(request_id)
            if not request:
                raise WorkOrderError(f"申请不存在: {request_id}")
            if request.status != SparePartRequestStatus.APPROVED:
                raise WorkOrderError(
                    f"申请【{request_id}】当前状态为【{request.status.value}】，"
                    f"只有【已审核】状态可以退回"
                )
            if request.applicant_id != user.user_id and user.role != Role.DISPATCHER:
                raise PermissionError(
                    f"只有申请人【{request.applicant_name}】或调度员可以执行退回操作"
                )
            part = self._spare_parts.get(request.part_id)
            if not part:
                raise WorkOrderError(f"备件不存在: {request.part_id}")
            stock_before = part.stock
            part.stock += request.quantity
            part.bump_version()
            request.status = SparePartRequestStatus.RETURNED
            request.returned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            request.return_note = note
            request.bump_version()
            self._save_spare_parts()
            self._save_spare_part_requests()
            self._add_spare_part_audit_log(
                part, "退回", request.quantity, user,
                stock_before, part.stock,
                order_id=request.order_id, request_id=request.request_id,
                note=note or f"退回备件: {part.name} x{request.quantity}{part.unit}"
            )
            return request

    # ----- Spare Parts Import/Export -----

    def import_spare_parts_csv(
        self,
        filepath: str,
        dispatcher: User,
    ) -> Tuple[int, List[str]]:
        self._check_permission(dispatcher, "import_spare_parts")
        if not os.path.exists(filepath):
            raise WorkOrderError(f"文件不存在: {filepath}")
        imported = 0
        errors = []
        temp_parts: List[Dict] = []

        def parse_row(row, i):
            name = (row.get("name") or row.get("名称") or "").strip()
            category = (row.get("category") or row.get("类别") or "").strip()
            stock_raw = (row.get("stock") or row.get("库存") or "0").strip()
            threshold_raw = (row.get("low_stock_threshold") or row.get("低库存阈值") or "0").strip()
            applicable_raw = (row.get("applicable_categories") or row.get("适用类别") or "").strip()
            unit = (row.get("unit") or row.get("单位") or "个").strip()
            description = (row.get("description") or row.get("描述") or "").strip()
            part_id = (row.get("part_id") or row.get("备件编号") or "").strip()

            if not name:
                errors.append(f"第{i}行: 名称不能为空")
                return None
            if not category:
                errors.append(f"第{i}行: 类别不能为空")
                return None
            try:
                stock = int(stock_raw)
                if stock < 0:
                    raise ValueError()
            except ValueError:
                errors.append(f"第{i}行: 库存必须是非负整数: {stock_raw}")
                return None
            try:
                threshold = int(threshold_raw)
                if threshold < 0:
                    raise ValueError()
            except ValueError:
                errors.append(f"第{i}行: 低库存阈值必须是非负整数: {threshold_raw}")
                return None
            applicable: List[str] = []
            if applicable_raw:
                applicable = [c.strip() for c in applicable_raw.split("|") if c.strip()]
            return {
                "part_id": part_id or None,
                "name": name,
                "category": category,
                "stock": stock,
                "low_stock_threshold": threshold,
                "applicable_categories": applicable,
                "unit": unit or "个",
                "description": description,
            }

        with self._lock:
            try:
                with open(filepath, "r", encoding="utf-8-sig") as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader, start=2):
                        parsed = parse_row(row, i)
                        if parsed:
                            temp_parts.append(parsed)
            except UnicodeDecodeError:
                with open(filepath, "r", encoding="gbk") as f:
                    reader = csv.DictReader(f)
                    for i, row in enumerate(reader, start=2):
                        parsed = parse_row(row, i)
                        if parsed:
                            temp_parts.append(parsed)
            if errors:
                return 0, errors

            for pd in temp_parts:
                try:
                    if pd["part_id"] and pd["part_id"] in self._spare_parts:
                        self.update_spare_part(
                            part_id=pd["part_id"],
                            dispatcher=dispatcher,
                            name=pd["name"],
                            category=pd["category"],
                            stock=pd["stock"],
                            low_stock_threshold=pd["low_stock_threshold"],
                            applicable_categories=pd["applicable_categories"],
                            unit=pd["unit"],
                            description=pd["description"],
                        )
                    else:
                        self.create_spare_part(
                            name=pd["name"],
                            category=pd["category"],
                            stock=pd["stock"],
                            low_stock_threshold=pd["low_stock_threshold"],
                            applicable_categories=pd["applicable_categories"],
                            unit=pd["unit"],
                            description=pd["description"],
                            dispatcher=dispatcher,
                        )
                    imported += 1
                except Exception as e:
                    errors.append(f"处理数据时出错: {str(e)}")
                    return 0, errors

        return imported, errors

    def export_spare_parts_json(self) -> str:
        filepath = self._get_export_path(f"spare_parts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(
                    [p.to_dict() for p in self._spare_parts.values()],
                    f, ensure_ascii=False, indent=2,
                )
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_spare_parts_csv(self) -> str:
        filepath = self._get_export_path(f"spare_parts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "备件编号", "名称", "类别", "当前库存", "单位",
                    "低库存阈值", "库存状态", "适用维修类别", "描述",
                    "创建时间", "更新时间", "版本",
                ])
                for p in self._spare_parts.values():
                    writer.writerow([
                        p.part_id, p.name, p.category, p.stock, p.unit,
                        p.low_stock_threshold,
                        "低库存" if p.is_low_stock else "正常",
                        "|".join(p.applicable_categories) if p.applicable_categories else "全部",
                        p.description,
                        p.created_at, p.updated_at, p.version,
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def export_spare_part_requests_json(self) -> str:
        filepath = self._get_export_path(f"spare_part_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(
                    [r.to_dict() for r in self._spare_part_requests.values()],
                    f, ensure_ascii=False, indent=2,
                )
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_spare_part_requests_csv(self) -> str:
        filepath = self._get_export_path(f"spare_part_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "申请编号", "工单编号", "备件编号", "备件名称",
                    "申请数量", "申请人ID", "申请人姓名", "申请原因",
                    "状态", "审核人ID", "审核人姓名", "审核备注",
                    "申请时间", "审核时间", "退回时间", "退回备注", "版本",
                ])
                for r in self._spare_part_requests.values():
                    writer.writerow([
                        r.request_id, r.order_id, r.part_id, r.part_name,
                        r.quantity, r.applicant_id, r.applicant_name, r.reason,
                        r.status.value,
                        r.reviewer_id or "", r.reviewer_name or "", r.review_note,
                        r.created_at, r.reviewed_at or "",
                        r.returned_at or "", r.return_note, r.version,
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def export_spare_part_audit_logs_json(self) -> str:
        filepath = self._get_export_path(f"spare_part_audit_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(
                    [l.to_dict() for l in self._spare_part_audit_logs.values()],
                    f, ensure_ascii=False, indent=2,
                )
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_spare_part_audit_logs_csv(self) -> str:
        filepath = self._get_export_path(f"spare_part_audit_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "日志编号", "备件编号", "备件名称", "操作类型",
                    "操作数量", "操作人ID", "操作人姓名",
                    "关联工单", "关联申请", "备注",
                    "操作前库存", "操作后库存", "操作时间",
                ])
                for l in self._spare_part_audit_logs.values():
                    writer.writerow([
                        l.log_id, l.part_id, l.part_name, l.action,
                        l.quantity, l.operator_id, l.operator_name,
                        l.order_id or "", l.request_id or "", l.note,
                        l.stock_before, l.stock_after, l.timestamp,
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def get_spare_part_audit_logs(
        self,
        order_id: Optional[str] = None,
        part_id: Optional[str] = None,
    ) -> List[SparePartAuditLog]:
        result = list(self._spare_part_audit_logs.values())
        if order_id:
            result = [l for l in result if l.order_id == order_id]
        if part_id:
            result = [l for l in result if l.part_id == part_id]
        result.sort(key=lambda l: l.timestamp, reverse=True)
        return result

    # ----- Reschedule & Arrival Confirmation: Load/Save -----

    def _load_reschedule_requests(self):
        if os.path.exists(self.reschedule_requests_file):
            try:
                with open(self.reschedule_requests_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._reschedule_requests = {
                        r["reschedule_id"]: RescheduleRequest.from_dict(r) for r in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._reschedule_requests = {}

    def _save_reschedule_requests(self):
        with open(self.reschedule_requests_file, "w", encoding="utf-8") as f:
            json.dump(
                [r.to_dict() for r in self._reschedule_requests.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _load_reschedule_confirm_logs(self):
        if os.path.exists(self.reschedule_confirm_logs_file):
            try:
                with open(self.reschedule_confirm_logs_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._reschedule_confirm_logs = {
                        l["log_id"]: RescheduleConfirmLog.from_dict(l) for l in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._reschedule_confirm_logs = {}

    def _save_reschedule_confirm_logs(self):
        with open(self.reschedule_confirm_logs_file, "w", encoding="utf-8") as f:
            json.dump(
                [l.to_dict() for l in self._reschedule_confirm_logs.values()],
                f, ensure_ascii=False, indent=2,
            )

    def _load_arrival_confirmations(self):
        if os.path.exists(self.arrival_confirmations_file):
            try:
                with open(self.arrival_confirmations_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._arrival_confirmations = {
                        a["arrival_id"]: ArrivalConfirmation.from_dict(a) for a in data
                    }
            except (json.JSONDecodeError, KeyError):
                self._arrival_confirmations = {}

    def _save_arrival_confirmations(self):
        with open(self.arrival_confirmations_file, "w", encoding="utf-8") as f:
            json.dump(
                [a.to_dict() for a in self._arrival_confirmations.values()],
                f, ensure_ascii=False, indent=2,
            )

    # ----- Reschedule: Core Operations -----

    def _check_technician_schedule_conflict(
        self,
        technician_id: str,
        new_slot: RescheduleCandidateSlot,
        exclude_order_id: Optional[str] = None,
    ) -> Optional[WorkOrder]:
        for order in self._orders.values():
            if exclude_order_id and order.order_id == exclude_order_id:
                continue
            if order.assignee_id != technician_id:
                continue
            if order.status in (Status.COMPLETED,):
                continue
            if not (order.scheduled_start and order.scheduled_end):
                continue
            order_slot = RescheduleCandidateSlot(order.scheduled_start, order.scheduled_end)
            if order_slot.overlaps_with(new_slot):
                return order
        return None

    def _validate_candidate_slots(self, candidate_slots: List[RescheduleCandidateSlot]) -> None:
        if not candidate_slots:
            raise WorkOrderError(RescheduleRuleViolation.EMPTY_SLOTS)
        for slot in candidate_slots:
            if not slot.is_valid():
                raise WorkOrderError(f"{RescheduleRuleViolation.INVALID_SLOT}: {slot}")

    def _check_all_slots_no_conflict(
        self,
        technician_id: str,
        candidate_slots: List[RescheduleCandidateSlot],
        exclude_order_id: str,
    ) -> None:
        for slot in candidate_slots:
            conflict_order = self._check_technician_schedule_conflict(
                technician_id, slot, exclude_order_id=exclude_order_id
            )
            if conflict_order:
                raise WorkOrderError(
                    f"{RescheduleRuleViolation.SCHEDULE_CONFLICT}：候选时间 {slot} 与工单 "
                    f"{conflict_order.order_id} ({conflict_order.title}) 的已排程时间重叠"
                )

    def _create_reschedule_confirm_log(
        self,
        reschedule_id: str,
        order_id: str,
        confirmer: User,
        decision: str,
        selected_slot: Optional[RescheduleCandidateSlot],
        reject_reason: str,
        note: str,
    ) -> RescheduleConfirmLog:
        log_id = "RCL" + datetime.now().strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex[:4].upper()
        return RescheduleConfirmLog(
            log_id=log_id,
            reschedule_id=reschedule_id,
            order_id=order_id,
            confirmer_id=confirmer.user_id,
            confirmer_name=confirmer.name,
            confirmer_role=confirmer.role.value,
            decision=decision,
            selected_slot_start=selected_slot.start_time if (decision == "confirm" and selected_slot) else None,
            selected_slot_end=selected_slot.end_time if (decision == "confirm" and selected_slot) else None,
            reject_reason=reject_reason.strip() if decision == "reject" else "",
            note=note,
        )

    def _raise_from_check(self, ok: bool, msg: str):
        if not ok:
            if "无权" in msg or "权限" in msg:
                raise PermissionError(msg)
            raise WorkOrderError(msg)

    def create_reschedule_request(
        self,
        order_id: str,
        dispatcher: User,
        reason: str,
        candidate_slots: List[RescheduleCandidateSlot],
        note: str = "",
    ) -> RescheduleRequest:
        if not reason or not reason.strip():
            raise WorkOrderError(RescheduleRuleViolation.EMPTY_REASON)
        self._validate_candidate_slots(candidate_slots)

        ok, msg = self.can_create_reschedule(order_id, dispatcher)
        self._raise_from_check(ok, msg)

        with self._lock:
            order = self._orders.get(order_id)
            self._check_all_slots_no_conflict(order.assignee_id, candidate_slots, order_id)

            reschedule_id = "RS" + datetime.now().strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex[:4].upper()
            request = RescheduleRequest(
                reschedule_id=reschedule_id,
                order_id=order_id,
                order_title=order.title,
                dispatcher_id=dispatcher.user_id,
                dispatcher_name=dispatcher.name,
                reason=reason.strip(),
                candidate_slots=candidate_slots,
                note=note,
                original_scheduled_start=order.scheduled_start,
                original_scheduled_end=order.scheduled_end,
            )
            self._reschedule_requests[reschedule_id] = request
            self._save_reschedule_requests()

            self._add_history(
                order, order.status, dispatcher,
                f"发起改约申请[{reschedule_id}]，原因: {reason.strip()}，候选时间: "
                + request.candidate_slots_text()
            )
            order.bump_version()
            self._save_orders()
            return request

    def cancel_reschedule_request(
        self,
        reschedule_id: str,
        dispatcher: User,
    ) -> RescheduleRequest:
        ok, msg = self.can_cancel_reschedule(reschedule_id, dispatcher)
        self._raise_from_check(ok, msg)

        with self._lock:
            request = self._reschedule_requests.get(reschedule_id)
            if not request.can_transition_to(RescheduleStatus.CANCELLED):
                raise WorkOrderError(f"状态流转不允许从【{request.status_label}】到【{RescheduleStatus.CANCELLED.value}】")

            request.status = RescheduleStatus.CANCELLED
            request.bump_version()
            self._save_reschedule_requests()

            order = self._orders.get(request.order_id)
            if order:
                self._add_history(
                    order, order.status, dispatcher,
                    f"撤销改约申请[{reschedule_id}]"
                )
                order.bump_version()
                self._save_orders()
            return request

    def confirm_reschedule_request(
        self,
        reschedule_id: str,
        confirmer: User,
        decision: str,
        selected_slot: Optional[RescheduleCandidateSlot] = None,
        reject_reason: str = "",
        note: str = "",
    ) -> Tuple[RescheduleRequest, RescheduleConfirmLog]:
        if decision not in RESCHEDULE_DECISIONS:
            raise WorkOrderError(f"{RescheduleRuleViolation.INVALID_DECISION}: {decision}")

        ok, msg = self.can_confirm_reschedule(reschedule_id, confirmer)
        self._raise_from_check(ok, msg)

        with self._lock:
            request = self._reschedule_requests.get(reschedule_id)
            order = self._orders.get(request.order_id)

            if decision == "confirm":
                if not selected_slot:
                    raise WorkOrderError(RescheduleRuleViolation.NO_SELECTED_SLOT)
                if not request.has_candidate_slot(selected_slot):
                    raise WorkOrderError(RescheduleRuleViolation.SLOT_NOT_IN_CANDIDATES)
                if order.status == Status.COMPLETED:
                    raise WorkOrderError(RescheduleRuleViolation.ORDER_COMPLETED)
                if order.assignee_id:
                    conflict_order = self._check_technician_schedule_conflict(
                        order.assignee_id, selected_slot, exclude_order_id=order.order_id
                    )
                    if conflict_order:
                        raise WorkOrderError(
                            f"{RescheduleRuleViolation.SCHEDULE_CONFLICT}：选中时间 {selected_slot} 与工单 "
                            f"{conflict_order.order_id} ({conflict_order.title}) 的已排程重叠"
                        )

                if not request.can_transition_to(RescheduleStatus.CONFIRMED):
                    raise WorkOrderError(f"状态流转不允许从【{request.status_label}】到【{RescheduleStatus.CONFIRMED.value}】")
                order.scheduled_start = selected_slot.start_time
                order.scheduled_end = selected_slot.end_time
                request.status = RescheduleStatus.CONFIRMED

                self._add_history(
                    order, order.status, confirmer,
                    f"确认改约[{reschedule_id}]，新时间: {selected_slot}"
                )
            else:
                if not reject_reason or not reject_reason.strip():
                    raise WorkOrderError(RescheduleRuleViolation.EMPTY_REJECT_REASON)
                if not request.can_transition_to(RescheduleStatus.REJECTED):
                    raise WorkOrderError(f"状态流转不允许从【{request.status_label}】到【{RescheduleStatus.REJECTED.value}】")
                request.status = RescheduleStatus.REJECTED
                self._add_history(
                    order, order.status, confirmer,
                    f"拒绝改约[{reschedule_id}]，原因: {reject_reason.strip()}"
                )

            request.bump_version()
            order.bump_version()

            log = self._create_reschedule_confirm_log(
                reschedule_id, request.order_id, confirmer, decision,
                selected_slot, reject_reason, note
            )
            self._reschedule_confirm_logs[log.log_id] = log
            self._save_reschedule_requests()
            self._save_reschedule_confirm_logs()
            self._save_orders()
            return request, log

    # ----- Reschedule: Queries -----

    def get_reschedule_request(self, reschedule_id: str) -> Optional[RescheduleRequest]:
        return self._reschedule_requests.get(reschedule_id)

    def get_reschedule_requests(
        self,
        order_id: Optional[str] = None,
        user_id: Optional[str] = None,
        status: Optional[RescheduleStatus] = None,
        viewer: Optional[User] = None,
    ) -> List[RescheduleRequest]:
        if viewer:
            if viewer.role == Role.TECHNICIAN:
                self._check_permission(viewer, "view_own_reschedules")
            else:
                self._check_permission(viewer, "view_reschedules")
        result = list(self._reschedule_requests.values())
        if viewer and viewer.role == Role.TECHNICIAN:
            result = [r for r in result if self._orders.get(r.order_id) and
                      self._orders.get(r.order_id).assignee_id == viewer.user_id]
        if order_id:
            result = [r for r in result if r.order_id == order_id]
        if user_id:
            result = [r for r in result if r.dispatcher_id == user_id]
        if status:
            result = [r for r in result if r.status == status]
        result.sort(key=lambda r: r.created_at, reverse=True)
        return result

    def get_reschedule_confirm_logs(
        self,
        reschedule_id: Optional[str] = None,
        order_id: Optional[str] = None,
    ) -> List[RescheduleConfirmLog]:
        result = list(self._reschedule_confirm_logs.values())
        if reschedule_id:
            result = [l for l in result if l.reschedule_id == reschedule_id]
        if order_id:
            result = [l for l in result if l.order_id == order_id]
        result.sort(key=lambda l: l.timestamp)
        return result

    # ----- Arrival Confirmation -----

    def _create_arrival_confirmation(
        self,
        order_id: str,
        order_title: str,
        confirmer: User,
        scheduled_start: Optional[str],
        scheduled_end: Optional[str],
        note: str,
    ) -> ArrivalConfirmation:
        arrival_id = "ARR" + datetime.now().strftime("%Y%m%d%H%M%S%f") + uuid.uuid4().hex[:4].upper()
        return ArrivalConfirmation(
            arrival_id=arrival_id,
            order_id=order_id,
            order_title=order_title,
            confirmer_id=confirmer.user_id,
            confirmer_name=confirmer.name,
            confirmer_role=confirmer.role.value,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            note=note,
        )

    def confirm_arrival(
        self,
        order_id: str,
        confirmer: User,
        note: str = "",
    ) -> ArrivalConfirmation:
        ok, msg = self.can_confirm_arrival(order_id, confirmer)
        self._raise_from_check(ok, msg)

        with self._lock:
            order = self._orders.get(order_id)
            arrival = self._create_arrival_confirmation(
                order_id, order.title, confirmer,
                order.scheduled_start, order.scheduled_end, note
            )
            self._arrival_confirmations[arrival.arrival_id] = arrival
            self._save_arrival_confirmations()

            self._add_history(
                order, order.status, confirmer,
                f"到场确认[{arrival.arrival_id}]，备注: {note or '无'}"
            )
            order.bump_version()
            self._save_orders()
            return arrival

    def get_arrival_confirmations(
        self,
        order_id: Optional[str] = None,
        confirmer_id: Optional[str] = None,
        viewer: Optional[User] = None,
    ) -> List[ArrivalConfirmation]:
        if viewer:
            if viewer.role == Role.TECHNICIAN:
                self._check_permission(viewer, "view_own_arrivals")
            else:
                self._check_permission(viewer, "view_arrivals")
        result = list(self._arrival_confirmations.values())
        if viewer and viewer.role == Role.TECHNICIAN:
            result = [a for a in result if a.confirmer_id == viewer.user_id]
        if order_id:
            result = [a for a in result if a.order_id == order_id]
        if confirmer_id:
            result = [a for a in result if a.confirmer_id == confirmer_id]
        result.sort(key=lambda a: a.timestamp, reverse=True)
        return result

    # ----- View Data Builders (for GUI) -----

    def build_reschedule_row(self, r: RescheduleRequest) -> Tuple:
        return (
            r.reschedule_id, r.order_id, r.order_title, r.reason,
            r.status.value, r.dispatcher_name, r.created_at,
        )

    def get_reschedule_requests_for_view(
        self,
        order_id: Optional[str] = None,
        status: Optional[RescheduleStatus] = None,
        viewer: Optional[User] = None,
    ) -> List[Tuple]:
        reqs = self.get_reschedule_requests(
            order_id=order_id, status=status, viewer=viewer
        )
        return [self.build_reschedule_row(r) for r in reqs]

    def get_order_visible_status(self, order_id: str, viewer: User) -> Dict:
        order = self._orders.get(order_id)
        if not order:
            raise WorkOrderError(f"工单不存在: {order_id}")
        if viewer.role == Role.TECHNICIAN and order.assignee_id != viewer.user_id:
            raise PermissionError(f"您【{viewer.name}】不是该工单的维修员")

        reschedules = self.get_reschedule_requests(order_id=order_id)
        arrivals = self.get_arrival_confirmations(order_id=order_id)
        pending_reschedule = next(
            (r for r in reschedules if r.status == RescheduleStatus.PENDING), None
        )
        latest_confirmed = next(
            (r for r in reschedules if r.status == RescheduleStatus.CONFIRMED), None
        )
        latest_arrival = arrivals[0] if arrivals else None

        can_create_rs, _ = self.can_create_reschedule(order_id, viewer)
        can_confirm_arrival, _ = self.can_confirm_arrival(order_id, viewer)

        return {
            "order_id": order.order_id,
            "title": order.title,
            "status": order.status.value,
            "scheduled_start": order.scheduled_start,
            "scheduled_end": order.scheduled_end,
            "pending_reschedule": pending_reschedule.to_dict() if pending_reschedule else None,
            "latest_confirmed_reschedule": latest_confirmed.to_dict() if latest_confirmed else None,
            "latest_arrival": latest_arrival.to_dict() if latest_arrival else None,
            "reschedule_count": len(reschedules),
            "arrival_count": len(arrivals),
            "can_create_reschedule": can_create_rs,
            "can_confirm_arrival": can_confirm_arrival,
        }

    # ----- Reschedule: Import/Export -----

    def export_reschedule_requests_json(
        self,
        requests: Optional[List[RescheduleRequest]] = None,
    ) -> str:
        filepath = self._get_export_path(f"reschedule_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        data = requests or list(self._reschedule_requests.values())
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump([r.to_dict() for r in data], f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_reschedule_requests_csv(
        self,
        requests: Optional[List[RescheduleRequest]] = None,
    ) -> str:
        filepath = self._get_export_path(f"reschedule_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        data = requests or list(self._reschedule_requests.values())
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "改约编号", "工单编号", "工单标题", "调度员ID", "调度员姓名",
                    "改约原因", "候选时间窗", "备注", "状态", "创建时间", "版本",
                    "原排程开始", "原排程结束",
                ])
                for r in data:
                    writer.writerow([
                        r.reschedule_id, r.order_id, r.order_title,
                        r.dispatcher_id, r.dispatcher_name,
                        r.reason,
                        r.candidate_slots_text(),
                        r.note, r.status_label, r.created_at, r.version,
                        r.original_scheduled_start or "", r.original_scheduled_end or "",
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def export_reschedule_confirm_logs_json(
        self,
        logs: Optional[List[RescheduleConfirmLog]] = None,
    ) -> str:
        filepath = self._get_export_path(f"reschedule_confirm_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        data = logs or list(self._reschedule_confirm_logs.values())
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump([l.to_dict() for l in data], f, ensure_ascii=False, indent=2)
        except OSError as e:
            raise ExportError(f"写入JSON文件失败: {str(e)}")
        return filepath

    def export_reschedule_confirm_logs_csv(
        self,
        logs: Optional[List[RescheduleConfirmLog]] = None,
    ) -> str:
        filepath = self._get_export_path(f"reschedule_confirm_logs_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        data = logs or list(self._reschedule_confirm_logs.values())
        try:
            with open(filepath, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "日志编号", "改约编号", "工单编号",
                    "确认人ID", "确认人姓名", "确认人角色",
                    "决策", "选中开始时间", "选中结束时间",
                    "拒绝原因", "备注", "时间戳",
                ])
                for l in data:
                    writer.writerow([
                        l.log_id, l.reschedule_id, l.order_id,
                        l.confirmer_id, l.confirmer_name, l.confirmer_role,
                        l.decision_label, l.selected_slot_start or "", l.selected_slot_end or "",
                        l.reject_reason, l.note, l.timestamp,
                    ])
        except OSError as e:
            raise ExportError(f"写入CSV文件失败: {str(e)}")
        return filepath

    def import_reschedule_requests_csv(
        self,
        filepath: str,
        dispatcher: User,
    ) -> Tuple[int, int, List[str]]:
        self._check_permission(dispatcher, "import_reschedules")
        imported = 0
        skipped = 0
        errors: List[str] = []
        try:
            with open(filepath, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for i, row in enumerate(reader, start=2):
                    try:
                        order_id = (row.get("工单编号") or "").strip()
                        reason = (row.get("改约原因") or "").strip()
                        slots_raw = (row.get("候选时间窗") or "").strip()
                        note = (row.get("备注") or "").strip()
                        if not order_id:
                            raise WorkOrderError("缺少工单编号")
                        if not reason:
                            raise WorkOrderError("缺少改约原因")
                        if not slots_raw:
                            raise WorkOrderError("缺少候选时间窗")
                        slots: List[RescheduleCandidateSlot] = []
                        for part in slots_raw.split(";"):
                            part = part.strip()
                            if not part:
                                continue
                            if "~" in part:
                                s, e = [x.strip() for x in part.split("~", 1)]
                            elif "-" in part and part.count(" ") >= 3:
                                parts = part.split()
                                s = " ".join(parts[:2])
                                e = " ".join(parts[3:5]) if len(parts) >= 5 else ""
                            else:
                                raise WorkOrderError(f"无法解析时间窗: {part}")
                            slot = RescheduleCandidateSlot(s, e)
                            if not slot.is_valid():
                                raise WorkOrderError(f"非法时间窗: {part}")
                            slots.append(slot)
                        if not slots:
                            raise WorkOrderError("未解析出有效候选时间窗")
                        self.create_reschedule_request(order_id, dispatcher, reason, slots, note)
                        imported += 1
                    except Exception as e:
                        skipped += 1
                        errors.append(f"第{i}行跳过: {str(e)}")
        except (OSError, csv.Error) as e:
            raise WorkOrderError(f"读取CSV文件失败: {str(e)}")
        return imported, skipped, errors
