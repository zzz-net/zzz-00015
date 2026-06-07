import json
import os
import threading
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Set
from enum import Enum


class Role(str, Enum):
    DISPATCHER = "dispatcher"
    TECHNICIAN = "technician"
    INSPECTOR = "inspector"


class Status(str, Enum):
    PENDING_DISPATCH = "待派单"
    DISPATCHED = "已派单"
    IN_PROGRESS = "处理中"
    PENDING_INSPECTION = "待验收"
    COMPLETED = "已完成"


STATUS_FLOW = {
    Status.PENDING_DISPATCH: [Status.DISPATCHED],
    Status.DISPATCHED: [Status.IN_PROGRESS],
    Status.IN_PROGRESS: [Status.PENDING_INSPECTION],
    Status.PENDING_INSPECTION: [Status.COMPLETED, Status.IN_PROGRESS],
    Status.COMPLETED: [],
}


REASSIGNABLE_STATUSES = {
    Status.PENDING_DISPATCH: {"reason": "待派单可直接改派", "note": "首次派单即生效"},
    Status.DISPATCHED: {"reason": "已派单但未接单可改派", "note": "维修员尚未接单"},
    Status.IN_PROGRESS: {"reason": "处理中改派需升级原因", "note": "需注明升级或请假原因"},
    Status.PENDING_INSPECTION: {"reason": "待验收改派仅允许管理员权限", "note": "仅限紧急或特殊情况"},
}


ROLE_PERMISSIONS = {
    Role.DISPATCHER: ["create", "dispatch", "import", "export", "view_history", "reassign", "manage_schedule", "manage_spare_parts", "review_spare_part_requests", "import_spare_parts", "export_spare_parts", "create_reschedule", "cancel_reschedule", "view_reschedules", "import_reschedules", "export_reschedules", "confirm_reschedule", "confirm_arrival", "view_arrivals"],
    Role.TECHNICIAN: ["accept", "complete", "view_history", "request_spare_parts", "view_own_spare_part_requests", "view_spare_parts_stock", "confirm_reschedule", "view_own_reschedules", "confirm_arrival", "view_own_arrivals"],
    Role.INSPECTOR: ["approve", "reject", "view_history", "export", "view_reschedules"],
}


CATEGORY_SKILL_MAP = {
    "空调维修": "空调",
    "电梯维修": "电梯",
    "电路维修": "电路",
    "照明维修": "电路",
    "水管维修": "水管",
    "门禁维修": "门禁",
    "办公设备": "办公设备",
    "网络维护": "网络",
    "其他": "通用",
}


class TimeSlot:
    def __init__(self, day_of_week: int, start_time: str, end_time: str):
        self.day_of_week = day_of_week
        self.start_time = start_time
        self.end_time = end_time

    def to_dict(self) -> Dict:
        return {
            "day_of_week": self.day_of_week,
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "TimeSlot":
        return cls(data["day_of_week"], data["start_time"], data["end_time"])

    def is_valid(self) -> bool:
        if self.day_of_week < 0 or self.day_of_week > 6:
            return False
        try:
            sh, smin = [int(x) for x in self.start_time.split(":")]
            eh, emin = [int(x) for x in self.end_time.split(":")]
            if sh < 0 or sh > 23 or smin < 0 or smin > 59:
                return False
            if eh < 0 or eh > 23 or emin < 0 or emin > 59:
                return False
            start_min = sh * 60 + smin
            end_min = eh * 60 + emin
            return start_min < end_min
        except (ValueError, AttributeError):
            return False

    def covers(self, dt: datetime) -> bool:
        if dt.weekday() != self.day_of_week:
            return False
        try:
            sh, smin = [int(x) for x in self.start_time.split(":")]
            eh, emin = [int(x) for x in self.end_time.split(":")]
            start_min = sh * 60 + smin
            end_min = eh * 60 + emin
            now_min = dt.hour * 60 + dt.minute
            return start_min <= now_min <= end_min
        except (ValueError, AttributeError):
            return False

    def __eq__(self, other):
        if not isinstance(other, TimeSlot):
            return False
        return (self.day_of_week == other.day_of_week and
                self.start_time == other.start_time and
                self.end_time == other.end_time)

    def __hash__(self):
        return hash((self.day_of_week, self.start_time, self.end_time))

    def __repr__(self):
        days = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        return f"{days[self.day_of_week]} {self.start_time}-{self.end_time}"


class ReassignmentLog:
    def __init__(
        self,
        order_id: str,
        from_user_id: str,
        from_user_name: str,
        to_user_id: str,
        to_user_name: str,
        reason: str,
        dispatcher_id: str,
        dispatcher_name: str,
        timestamp: str,
    ):
        self.order_id = order_id
        self.from_user_id = from_user_id
        self.from_user_name = from_user_name
        self.to_user_id = to_user_id
        self.to_user_name = to_user_name
        self.reason = reason
        self.dispatcher_id = dispatcher_id
        self.dispatcher_name = dispatcher_name
        self.timestamp = timestamp

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "from_user_id": self.from_user_id,
            "from_user_name": self.from_user_name,
            "to_user_id": self.to_user_id,
            "to_user_name": self.to_user_name,
            "reason": self.reason,
            "dispatcher_id": self.dispatcher_id,
            "dispatcher_name": self.dispatcher_name,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ReassignmentLog":
        return cls(
            data["order_id"],
            data["from_user_id"],
            data["from_user_name"],
            data["to_user_id"],
            data["to_user_name"],
            data["reason"],
            data["dispatcher_id"],
            data["dispatcher_name"],
            data["timestamp"],
        )


class MatchResult:
    def __init__(
        self,
        skill_match: bool,
        available_now: bool,
        within_capacity: bool,
        current_load: int,
        max_parallel: int,
        warnings: Optional[List[str]] = None,
    ):
        self.skill_match = skill_match
        self.available_now = available_now
        self.within_capacity = within_capacity
        self.current_load = current_load
        self.max_parallel = max_parallel
        self.warnings = warnings or []

    @property
    def score(self) -> int:
        s = 0
        if self.skill_match:
            s += 50
        if self.available_now:
            s += 30
        if self.within_capacity:
            s += 20
        return s

    @property
    def is_recommended(self) -> bool:
        return self.skill_match and self.available_now and self.within_capacity

    def to_dict(self) -> Dict:
        return {
            "skill_match": self.skill_match,
            "available_now": self.available_now,
            "within_capacity": self.within_capacity,
            "current_load": self.current_load,
            "max_parallel": self.max_parallel,
            "score": self.score,
            "is_recommended": self.is_recommended,
            "warnings": self.warnings,
        }

    def __repr__(self):
        parts = []
        if not self.skill_match:
            parts.append("技能不匹配")
        if not self.available_now:
            parts.append("非上班时间")
        if not self.within_capacity:
            parts.append(f"超载({self.current_load}/{self.max_parallel})")
        if not parts:
            parts.append("推荐")
        return f"匹配度{self.score}分: {', '.join(parts)}"


class User:
    def __init__(
        self,
        user_id: str,
        name: str,
        role: Role,
        skills: Optional[List[str]] = None,
        max_parallel_orders: int = 3,
        time_slots: Optional[List[TimeSlot]] = None,
    ):
        self.user_id = user_id
        self.name = name
        self.role = role
        self.skills = skills or []
        self.max_parallel_orders = max_parallel_orders
        self.time_slots = time_slots or []

    def to_dict(self) -> Dict:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "role": self.role.value,
            "skills": self.skills,
            "max_parallel_orders": self.max_parallel_orders,
            "time_slots": [ts.to_dict() for ts in self.time_slots],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "User":
        role = Role(data["role"])
        skills = data.get("skills", [])
        max_parallel_orders = data.get("max_parallel_orders", 3)
        time_slots = [TimeSlot.from_dict(ts) for ts in data.get("time_slots", [])]
        return cls(data["user_id"], data["name"], role, skills, max_parallel_orders, time_slots)

    def has_skill_for_category(self, category: str) -> bool:
        required = CATEGORY_SKILL_MAP.get(category, "通用")
        if required == "通用":
            return True
        return required in self.skills or "通用" in self.skills

    def is_available_at(self, dt: Optional[datetime] = None) -> bool:
        if not self.time_slots:
            return True
        dt = dt or datetime.now()
        return any(ts.covers(dt) for ts in self.time_slots)

    def add_skill(self, skill: str):
        skill = skill.strip()
        if skill and skill not in self.skills:
            self.skills.append(skill)

    def remove_skill(self, skill: str):
        if skill in self.skills:
            self.skills.remove(skill)

    def add_time_slot(self, slot: TimeSlot):
        if slot.is_valid() and slot not in self.time_slots:
            self.time_slots.append(slot)

    def clear_time_slots(self):
        self.time_slots = []


class StatusHistory:
    def __init__(self, status: Status, user_id: str, user_name: str, timestamp: str, note: str = ""):
        self.status = status
        self.user_id = user_id
        self.user_name = user_name
        self.timestamp = timestamp
        self.note = note

    def to_dict(self) -> Dict:
        return {
            "status": self.status.value,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "timestamp": self.timestamp,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "StatusHistory":
        return cls(
            Status(data["status"]),
            data["user_id"],
            data["user_name"],
            data["timestamp"],
            data.get("note", ""),
        )


class WorkOrder:
    def __init__(
        self,
        order_id: str,
        title: str,
        description: str,
        location: str,
        category: str,
        priority: str,
        creator_id: str,
        creator_name: str,
        created_at: Optional[str] = None,
        status: Status = Status.PENDING_DISPATCH,
        assignee_id: Optional[str] = None,
        assignee_name: Optional[str] = None,
        history: Optional[List[StatusHistory]] = None,
        exception_notes: Optional[List[str]] = None,
        reassignment_logs: Optional[List[ReassignmentLog]] = None,
        version: int = 0,
        scheduled_start: Optional[str] = None,
        scheduled_end: Optional[str] = None,
    ):
        self.order_id = order_id
        self.title = title
        self.description = description
        self.location = location
        self.category = category
        self.priority = priority
        self.creator_id = creator_id
        self.creator_name = creator_name
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.status = status
        self.assignee_id = assignee_id
        self.assignee_name = assignee_name
        self.history = history or []
        self.exception_notes = exception_notes or []
        self.reassignment_logs = reassignment_logs or []
        self.version = version
        self.scheduled_start = scheduled_start
        self.scheduled_end = scheduled_end
        self._lock = threading.Lock()

        if not self.history:
            self.history.append(
                StatusHistory(
                    self.status,
                    creator_id,
                    creator_name,
                    self.created_at,
                    "工单创建",
                )
            )

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "category": self.category,
            "priority": self.priority,
            "creator_id": self.creator_id,
            "creator_name": self.creator_name,
            "created_at": self.created_at,
            "status": self.status.value,
            "assignee_id": self.assignee_id,
            "assignee_name": self.assignee_name,
            "history": [h.to_dict() for h in self.history],
            "exception_notes": self.exception_notes,
            "reassignment_logs": [r.to_dict() for r in self.reassignment_logs],
            "version": self.version,
            "scheduled_start": self.scheduled_start,
            "scheduled_end": self.scheduled_end,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "WorkOrder":
        order = cls.__new__(cls)
        order.order_id = data["order_id"]
        order.title = data["title"]
        order.description = data["description"]
        order.location = data["location"]
        order.category = data["category"]
        order.priority = data["priority"]
        order.creator_id = data["creator_id"]
        order.creator_name = data["creator_name"]
        order.created_at = data["created_at"]
        order.status = Status(data["status"])
        order.assignee_id = data.get("assignee_id")
        order.assignee_name = data.get("assignee_name")
        order.history = [StatusHistory.from_dict(h) for h in data.get("history", [])]
        order.exception_notes = data.get("exception_notes", [])
        order.reassignment_logs = [ReassignmentLog.from_dict(r) for r in data.get("reassignment_logs", [])]
        order.version = data.get("version", 0)
        order.scheduled_start = data.get("scheduled_start")
        order.scheduled_end = data.get("scheduled_end")
        order._lock = threading.Lock()
        return order

    def add_exception_note(self, note: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.exception_notes.append(f"[{timestamp}] {note}")

    def add_reassignment_log(self, log: ReassignmentLog):
        self.reassignment_logs.append(log)

    def bump_version(self) -> int:
        self.version += 1
        return self.version


class ReassignmentDraft:
    def __init__(
        self,
        order_id: str,
        dispatcher_id: str,
        target_technician_id: str,
        reason: str,
        order_version: int,
        created_at: Optional[str] = None,
    ):
        self.order_id = order_id
        self.dispatcher_id = dispatcher_id
        self.target_technician_id = target_technician_id
        self.reason = reason
        self.order_version = order_version
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "dispatcher_id": self.dispatcher_id,
            "target_technician_id": self.target_technician_id,
            "reason": self.reason,
            "order_version": self.order_version,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ReassignmentDraft":
        return cls(
            data["order_id"],
            data["dispatcher_id"],
            data["target_technician_id"],
            data["reason"],
            data["order_version"],
            data.get("created_at"),
        )


class ConflictType(str, Enum):
    NONE = "none"
    VERSION_MISMATCH = "version_mismatch"
    STATUS_CHANGED = "status_changed"
    TECHNICIAN_REMOVED = "technician_removed"
    TECHNICIAN_ROLE_CHANGED = "technician_role_changed"
    TECHNICIAN_SKILLS_CHANGED = "technician_skills_changed"
    TECHNICIAN_SCHEDULE_CHANGED = "technician_schedule_changed"
    TECHNICIAN_CAPACITY_CHANGED = "technician_capacity_changed"
    ORDER_REMOVED = "order_removed"


class RevocationStatus(str, Enum):
    NOT_REVOKED = "not_revoked"
    REVOCABLE = "revocable"
    REVOKED = "revoked"
    NOT_REVOCABLE = "not_revocable"
    CONFLICT_SKIPPED = "conflict_skipped"


class RevocationConflictType(str, Enum):
    NONE = "none"
    ALREADY_REVOKED = "already_revoked"
    ORDER_REASSIGNED = "order_reassigned"
    ORDER_COMPLETED = "order_completed"
    TECHNICIAN_REMOVED = "technician_removed"
    PERMISSION_DENIED = "permission_denied"
    VERSION_MISMATCH = "version_mismatch"
    ORDER_NOT_FOUND = "order_not_found"


class RevocationRecord:
    def __init__(
        self,
        revocation_id: str,
        result_id: str,
        draft_id: Optional[str],
        order_id: str,
        operator_id: str,
        operator_name: str,
        reason: str,
        original_assignee_id: str,
        original_assignee_name: str,
        original_status: str,
        revoked_assignee_id: str,
        revoked_assignee_name: str,
        revoked_status: str,
        timestamp: Optional[str] = None,
        conflict_type: str = RevocationConflictType.NONE,
        conflict_message: Optional[str] = None,
        success: bool = False,
    ):
        self.revocation_id = revocation_id
        self.result_id = result_id
        self.draft_id = draft_id
        self.order_id = order_id
        self.operator_id = operator_id
        self.operator_name = operator_name
        self.reason = reason
        self.original_assignee_id = original_assignee_id
        self.original_assignee_name = original_assignee_name
        self.original_status = original_status
        self.revoked_assignee_id = revoked_assignee_id
        self.revoked_assignee_name = revoked_assignee_name
        self.revoked_status = revoked_status
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.conflict_type = conflict_type
        self.conflict_message = conflict_message
        self.success = success

    def to_dict(self) -> Dict:
        return {
            "revocation_id": self.revocation_id,
            "result_id": self.result_id,
            "draft_id": self.draft_id,
            "order_id": self.order_id,
            "operator_id": self.operator_id,
            "operator_name": self.operator_name,
            "reason": self.reason,
            "original_assignee_id": self.original_assignee_id,
            "original_assignee_name": self.original_assignee_name,
            "original_status": self.original_status,
            "revoked_assignee_id": self.revoked_assignee_id,
            "revoked_assignee_name": self.revoked_assignee_name,
            "revoked_status": self.revoked_status,
            "timestamp": self.timestamp,
            "conflict_type": self.conflict_type,
            "conflict_message": self.conflict_message,
            "success": self.success,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "RevocationRecord":
        return cls(
            revocation_id=data["revocation_id"],
            result_id=data["result_id"],
            draft_id=data.get("draft_id"),
            order_id=data["order_id"],
            operator_id=data["operator_id"],
            operator_name=data["operator_name"],
            reason=data["reason"],
            original_assignee_id=data["original_assignee_id"],
            original_assignee_name=data["original_assignee_name"],
            original_status=data["original_status"],
            revoked_assignee_id=data["revoked_assignee_id"],
            revoked_assignee_name=data["revoked_assignee_name"],
            revoked_status=data["revoked_status"],
            timestamp=data.get("timestamp"),
            conflict_type=data.get("conflict_type", RevocationConflictType.NONE),
            conflict_message=data.get("conflict_message"),
            success=data.get("success", False),
        )

    @property
    def status_label(self) -> str:
        if self.success:
            return "撤销成功"
        else:
            return f"撤销跳过: {self.conflict_message or self.conflict_type}"


class BatchDraftItem:
    def __init__(
        self,
        order_id: str,
        target_technician_id: str,
        reason: str,
        order_version: int,
        order_status: str,
        original_assignee_id: Optional[str] = None,
        recommended: bool = False,
        risk_warnings: Optional[List[str]] = None,
        match_score: Optional[int] = None,
        tech_skills_snapshot: Optional[List[str]] = None,
        tech_schedule_snapshot: Optional[List[Dict]] = None,
        tech_max_parallel_snapshot: Optional[int] = None,
    ):
        self.order_id = order_id
        self.target_technician_id = target_technician_id
        self.reason = reason
        self.order_version = order_version
        self.order_status = order_status
        self.original_assignee_id = original_assignee_id
        self.recommended = recommended
        self.risk_warnings = risk_warnings or []
        self.match_score = match_score
        self.tech_skills_snapshot = tech_skills_snapshot or []
        self.tech_schedule_snapshot = tech_schedule_snapshot or []
        self.tech_max_parallel_snapshot = tech_max_parallel_snapshot

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "target_technician_id": self.target_technician_id,
            "reason": self.reason,
            "order_version": self.order_version,
            "order_status": self.order_status,
            "original_assignee_id": self.original_assignee_id,
            "recommended": self.recommended,
            "risk_warnings": self.risk_warnings,
            "match_score": self.match_score,
            "tech_skills_snapshot": self.tech_skills_snapshot,
            "tech_schedule_snapshot": self.tech_schedule_snapshot,
            "tech_max_parallel_snapshot": self.tech_max_parallel_snapshot,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BatchDraftItem":
        return cls(
            data["order_id"],
            data["target_technician_id"],
            data["reason"],
            data["order_version"],
            data["order_status"],
            data.get("original_assignee_id"),
            data.get("recommended", False),
            data.get("risk_warnings", []),
            data.get("match_score"),
            data.get("tech_skills_snapshot", []),
            data.get("tech_schedule_snapshot", []),
            data.get("tech_max_parallel_snapshot"),
        )


class BatchReassignmentDraft:
    def __init__(
        self,
        draft_id: str,
        dispatcher_id: str,
        dispatcher_name: str,
        items: Optional[List[BatchDraftItem]] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        self.draft_id = draft_id
        self.dispatcher_id = dispatcher_id
        self.dispatcher_name = dispatcher_name
        self.items = items or []
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = updated_at or self.created_at

    def to_dict(self) -> Dict:
        return {
            "draft_id": self.draft_id,
            "dispatcher_id": self.dispatcher_id,
            "dispatcher_name": self.dispatcher_name,
            "items": [item.to_dict() for item in self.items],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BatchReassignmentDraft":
        return cls(
            data["draft_id"],
            data["dispatcher_id"],
            data["dispatcher_name"],
            [BatchDraftItem.from_dict(i) for i in data.get("items", [])],
            data.get("created_at"),
            data.get("updated_at"),
        )


class BatchItemResult:
    def __init__(
        self,
        order_id: str,
        success: bool,
        skipped: bool = False,
        target_technician_id: Optional[str] = None,
        target_technician_name: Optional[str] = None,
        reason: Optional[str] = None,
        error_message: Optional[str] = None,
        conflict_types: Optional[List[str]] = None,
        original_assignee_id: Optional[str] = None,
        original_assignee_name: Optional[str] = None,
        order_title: Optional[str] = None,
        permission_checked: bool = True,
        permission_passed: Optional[bool] = None,
        version_checked: bool = True,
        version_passed: Optional[bool] = None,
        skill_checked: bool = True,
        skill_passed: Optional[bool] = None,
        capacity_checked: bool = True,
        capacity_passed: Optional[bool] = None,
        schedule_checked: bool = True,
        schedule_passed: Optional[bool] = None,
        log_written: bool = False,
        log_write_error: Optional[str] = None,
        item_timestamp: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_name: Optional[str] = None,
        draft_id: Optional[str] = None,
        revoked: bool = False,
        revocation_status: str = RevocationStatus.NOT_REVOKED,
        revocation_id: Optional[str] = None,
        revocation_reason: Optional[str] = None,
        revocation_operator_id: Optional[str] = None,
        revocation_operator_name: Optional[str] = None,
        revocation_timestamp: Optional[str] = None,
        revocation_conflict_type: Optional[str] = None,
        revocation_conflict_message: Optional[str] = None,
        original_status_snapshot: Optional[str] = None,
    ):
        self.order_id = order_id
        self.success = success
        self.skipped = skipped
        self.target_technician_id = target_technician_id
        self.target_technician_name = target_technician_name
        self.reason = reason
        self.error_message = error_message
        self.conflict_types = conflict_types or []
        self.original_assignee_id = original_assignee_id
        self.original_assignee_name = original_assignee_name
        self.order_title = order_title
        self.permission_checked = permission_checked
        self.permission_passed = permission_passed
        self.version_checked = version_checked
        self.version_passed = version_passed
        self.skill_checked = skill_checked
        self.skill_passed = skill_passed
        self.capacity_checked = capacity_checked
        self.capacity_passed = capacity_passed
        self.schedule_checked = schedule_checked
        self.schedule_passed = schedule_passed
        self.log_written = log_written
        self.log_write_error = log_write_error
        self.item_timestamp = item_timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.operator_id = operator_id
        self.operator_name = operator_name
        self.draft_id = draft_id
        self.revoked = revoked
        self.revocation_status = revocation_status
        self.revocation_id = revocation_id
        self.revocation_reason = revocation_reason
        self.revocation_operator_id = revocation_operator_id
        self.revocation_operator_name = revocation_operator_name
        self.revocation_timestamp = revocation_timestamp
        self.revocation_conflict_type = revocation_conflict_type
        self.revocation_conflict_message = revocation_conflict_message
        self.original_status_snapshot = original_status_snapshot

    @property
    def status_label(self) -> str:
        if self.revoked:
            return "已撤销"
        if self.success:
            return "成功"
        elif self.skipped:
            return "跳过"
        else:
            return "失败"

    @property
    def revocation_status_label(self) -> str:
        labels = {
            RevocationStatus.NOT_REVOKED: "未撤销",
            RevocationStatus.REVOCABLE: "可撤销",
            RevocationStatus.REVOKED: "已撤销",
            RevocationStatus.NOT_REVOCABLE: "不可撤销",
            RevocationStatus.CONFLICT_SKIPPED: "冲突跳过",
        }
        return labels.get(self.revocation_status, self.revocation_status)

    @property
    def summary(self) -> str:
        parts = [f"工单 {self.order_id}"]
        if self.success:
            parts.append(f"改派成功至 {self.target_technician_name or self.target_technician_id or '?'}")
            if self.log_written:
                parts.append("日志已写入")
            else:
                parts.append(f"日志写入异常: {self.log_write_error or '未知原因'}")
        else:
            parts.append(self.status_label)
            if self.error_message:
                parts.append(f"原因: {self.error_message}")
            details = []
            if self.version_checked and self.version_passed is False:
                details.append("版本校验失败")
            if self.skill_checked and self.skill_passed is False:
                details.append("技能冲突")
            if self.capacity_checked and self.capacity_passed is False:
                details.append("容量超载")
            if self.schedule_checked and self.schedule_passed is False:
                details.append("排班冲突")
            if details:
                parts.append(f"[{', '.join(details)}]")
        return " - ".join(parts)

    def to_dict(self) -> Dict:
        return {
            "order_id": self.order_id,
            "success": self.success,
            "skipped": self.skipped,
            "target_technician_id": self.target_technician_id,
            "target_technician_name": self.target_technician_name,
            "reason": self.reason,
            "error_message": self.error_message,
            "conflict_types": self.conflict_types,
            "original_assignee_id": self.original_assignee_id,
            "original_assignee_name": self.original_assignee_name,
            "order_title": self.order_title,
            "permission_checked": self.permission_checked,
            "permission_passed": self.permission_passed,
            "version_checked": self.version_checked,
            "version_passed": self.version_passed,
            "skill_checked": self.skill_checked,
            "skill_passed": self.skill_passed,
            "capacity_checked": self.capacity_checked,
            "capacity_passed": self.capacity_passed,
            "schedule_checked": self.schedule_checked,
            "schedule_passed": self.schedule_passed,
            "log_written": self.log_written,
            "log_write_error": self.log_write_error,
            "item_timestamp": self.item_timestamp,
            "operator_id": self.operator_id,
            "operator_name": self.operator_name,
            "draft_id": self.draft_id,
            "status_label": self.status_label,
            "revoked": self.revoked,
            "revocation_status": self.revocation_status,
            "revocation_status_label": self.revocation_status_label,
            "revocation_id": self.revocation_id,
            "revocation_reason": self.revocation_reason,
            "revocation_operator_id": self.revocation_operator_id,
            "revocation_operator_name": self.revocation_operator_name,
            "revocation_timestamp": self.revocation_timestamp,
            "revocation_conflict_type": self.revocation_conflict_type,
            "revocation_conflict_message": self.revocation_conflict_message,
            "original_status_snapshot": self.original_status_snapshot,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BatchItemResult":
        return cls(
            order_id=data["order_id"],
            success=data["success"],
            skipped=data.get("skipped", False),
            target_technician_id=data.get("target_technician_id"),
            target_technician_name=data.get("target_technician_name"),
            reason=data.get("reason"),
            error_message=data.get("error_message"),
            conflict_types=data.get("conflict_types", []),
            original_assignee_id=data.get("original_assignee_id"),
            original_assignee_name=data.get("original_assignee_name"),
            order_title=data.get("order_title"),
            permission_checked=data.get("permission_checked", True),
            permission_passed=data.get("permission_passed"),
            version_checked=data.get("version_checked", True),
            version_passed=data.get("version_passed"),
            skill_checked=data.get("skill_checked", True),
            skill_passed=data.get("skill_passed"),
            capacity_checked=data.get("capacity_checked", True),
            capacity_passed=data.get("capacity_passed"),
            schedule_checked=data.get("schedule_checked", True),
            schedule_passed=data.get("schedule_passed"),
            log_written=data.get("log_written", False),
            log_write_error=data.get("log_write_error"),
            item_timestamp=data.get("item_timestamp"),
            operator_id=data.get("operator_id"),
            operator_name=data.get("operator_name"),
            draft_id=data.get("draft_id"),
            revoked=data.get("revoked", False),
            revocation_status=data.get("revocation_status", RevocationStatus.NOT_REVOKED),
            revocation_id=data.get("revocation_id"),
            revocation_reason=data.get("revocation_reason"),
            revocation_operator_id=data.get("revocation_operator_id"),
            revocation_operator_name=data.get("revocation_operator_name"),
            revocation_timestamp=data.get("revocation_timestamp"),
            revocation_conflict_type=data.get("revocation_conflict_type"),
            revocation_conflict_message=data.get("revocation_conflict_message"),
            original_status_snapshot=data.get("original_status_snapshot"),
        )


class BatchReassignmentResult:
    def __init__(
        self,
        dispatcher_id: str,
        dispatcher_name: str,
        timestamp: Optional[str] = None,
        results: Optional[List[BatchItemResult]] = None,
        result_id: Optional[str] = None,
        draft_id: Optional[str] = None,
        note: Optional[str] = None,
    ):
        import uuid as _uuid
        now = datetime.now()
        self.result_id = result_id or (
            "BRR" + now.strftime("%Y%m%d%H%M%S%f") + _uuid.uuid4().hex[:4].upper()
        )
        self.draft_id = draft_id
        self.dispatcher_id = dispatcher_id
        self.dispatcher_name = dispatcher_name
        self.timestamp = timestamp or now.strftime("%Y-%m-%d %H:%M:%S.%f")
        self.results = results or []
        self.note = note

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success and not r.revoked)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.success and not r.skipped)

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def revoked_count(self) -> int:
        return sum(1 for r in self.results if r.revoked)

    @property
    def revocable_count(self) -> int:
        return sum(
            1 for r in self.results
            if r.success and not r.revoked and r.revocation_status == RevocationStatus.REVOCABLE
        )

    @property
    def not_revocable_count(self) -> int:
        return sum(
            1 for r in self.results
            if r.success and not r.revoked and r.revocation_status == RevocationStatus.NOT_REVOCABLE
        )

    @property
    def revocation_conflict_skipped_count(self) -> int:
        return sum(
            1 for r in self.results
            if r.revocation_status == RevocationStatus.CONFLICT_SKIPPED
        )

    @property
    def all_conflict_types(self) -> Set[str]:
        seen = set()
        for r in self.results:
            for ct in r.conflict_types:
                seen.add(ct)
        return seen

    @property
    def all_revocation_conflict_types(self) -> Set[str]:
        seen = set()
        for r in self.results:
            if r.revocation_conflict_type:
                seen.add(r.revocation_conflict_type)
        return seen

    def filter_results(
        self,
        status: Optional[str] = None,
        conflict_type: Optional[str] = None,
        revocation_status: Optional[str] = None,
    ) -> List[BatchItemResult]:
        filtered = []
        for r in self.results:
            if status and status != "all":
                if status == "success" and not (r.success and not r.revoked):
                    continue
                if status == "skipped" and not r.skipped:
                    continue
                if status == "failed" and (r.success or r.skipped):
                    continue
                if status == "revoked" and not r.revoked:
                    continue
            if conflict_type and conflict_type != "all" and conflict_type not in r.conflict_types:
                continue
            if revocation_status and revocation_status != "all":
                if revocation_status == "revoked" and not r.revoked:
                    continue
                if revocation_status == "revocable" and not (r.success and not r.revoked and r.revocation_status == RevocationStatus.REVOCABLE):
                    continue
                if revocation_status == "not_revocable" and not (r.success and not r.revoked and r.revocation_status == RevocationStatus.NOT_REVOCABLE):
                    continue
                if revocation_status == "conflict_skipped" and r.revocation_status != RevocationStatus.CONFLICT_SKIPPED:
                    continue
            filtered.append(r)
        return filtered

    def to_dict(self) -> Dict:
        return {
            "result_id": self.result_id,
            "draft_id": self.draft_id,
            "dispatcher_id": self.dispatcher_id,
            "dispatcher_name": self.dispatcher_name,
            "timestamp": self.timestamp,
            "success_count": self.success_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "total_count": self.total_count,
            "revoked_count": self.revoked_count,
            "revocable_count": self.revocable_count,
            "not_revocable_count": self.not_revocable_count,
            "revocation_conflict_skipped_count": self.revocation_conflict_skipped_count,
            "all_conflict_types": sorted(self.all_conflict_types),
            "all_revocation_conflict_types": sorted(self.all_revocation_conflict_types),
            "note": self.note,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "BatchReassignmentResult":
        results = [BatchItemResult.from_dict(r) for r in data.get("results", [])]
        return cls(
            dispatcher_id=data["dispatcher_id"],
            dispatcher_name=data["dispatcher_name"],
            timestamp=data.get("timestamp"),
            results=results,
            result_id=data.get("result_id"),
            draft_id=data.get("draft_id"),
            note=data.get("note"),
        )


class AppConfig:
    def __init__(self, export_dir: str = ""):
        self.export_dir = export_dir

    def to_dict(self) -> Dict:
        return {"export_dir": self.export_dir}

    @classmethod
    def from_dict(cls, data: Dict) -> "AppConfig":
        return cls(data.get("export_dir", ""))


class SparePartRequestStatus(str, Enum):
    PENDING = "待审核"
    APPROVED = "已审核"
    REJECTED = "已拒绝"
    RETURNED = "已退回"


class SparePart:
    def __init__(
        self,
        part_id: str,
        name: str,
        category: str,
        stock: int,
        low_stock_threshold: int,
        applicable_categories: Optional[List[str]] = None,
        unit: str = "个",
        description: str = "",
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
        version: int = 0,
    ):
        self.part_id = part_id
        self.name = name
        self.category = category
        self.stock = stock
        self.low_stock_threshold = low_stock_threshold
        self.applicable_categories = applicable_categories or []
        self.unit = unit
        self.description = description
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.updated_at = updated_at or self.created_at
        self.version = version
        self._lock = threading.Lock()

    def to_dict(self) -> Dict:
        return {
            "part_id": self.part_id,
            "name": self.name,
            "category": self.category,
            "stock": self.stock,
            "low_stock_threshold": self.low_stock_threshold,
            "applicable_categories": self.applicable_categories,
            "unit": self.unit,
            "description": self.description,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SparePart":
        part = cls.__new__(cls)
        part.part_id = data["part_id"]
        part.name = data["name"]
        part.category = data["category"]
        part.stock = data["stock"]
        part.low_stock_threshold = data["low_stock_threshold"]
        part.applicable_categories = data.get("applicable_categories", [])
        part.unit = data.get("unit", "个")
        part.description = data.get("description", "")
        part.created_at = data.get("created_at")
        part.updated_at = data.get("updated_at")
        part.version = data.get("version", 0)
        part._lock = threading.Lock()
        return part

    def bump_version(self) -> int:
        self.version += 1
        self.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return self.version

    @property
    def is_low_stock(self) -> bool:
        return self.stock <= self.low_stock_threshold

    def is_applicable_for_order_category(self, order_category: str) -> bool:
        if not self.applicable_categories:
            return True
        return order_category in self.applicable_categories


class SparePartRequest:
    def __init__(
        self,
        request_id: str,
        order_id: str,
        part_id: str,
        part_name: str,
        quantity: int,
        applicant_id: str,
        applicant_name: str,
        reason: str = "",
        status: SparePartRequestStatus = SparePartRequestStatus.PENDING,
        reviewer_id: Optional[str] = None,
        reviewer_name: Optional[str] = None,
        review_note: str = "",
        created_at: Optional[str] = None,
        reviewed_at: Optional[str] = None,
        returned_at: Optional[str] = None,
        return_note: str = "",
        version: int = 0,
    ):
        self.request_id = request_id
        self.order_id = order_id
        self.part_id = part_id
        self.part_name = part_name
        self.quantity = quantity
        self.applicant_id = applicant_id
        self.applicant_name = applicant_name
        self.reason = reason
        self.status = status
        self.reviewer_id = reviewer_id
        self.reviewer_name = reviewer_name
        self.review_note = review_note
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.reviewed_at = reviewed_at
        self.returned_at = returned_at
        self.return_note = return_note
        self.version = version
        self._lock = threading.Lock()

    def to_dict(self) -> Dict:
        return {
            "request_id": self.request_id,
            "order_id": self.order_id,
            "part_id": self.part_id,
            "part_name": self.part_name,
            "quantity": self.quantity,
            "applicant_id": self.applicant_id,
            "applicant_name": self.applicant_name,
            "reason": self.reason,
            "status": self.status.value,
            "reviewer_id": self.reviewer_id,
            "reviewer_name": self.reviewer_name,
            "review_note": self.review_note,
            "created_at": self.created_at,
            "reviewed_at": self.reviewed_at,
            "returned_at": self.returned_at,
            "return_note": self.return_note,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SparePartRequest":
        req = cls.__new__(cls)
        req.request_id = data["request_id"]
        req.order_id = data["order_id"]
        req.part_id = data["part_id"]
        req.part_name = data["part_name"]
        req.quantity = data["quantity"]
        req.applicant_id = data["applicant_id"]
        req.applicant_name = data["applicant_name"]
        req.reason = data.get("reason", "")
        req.status = SparePartRequestStatus(data["status"])
        req.reviewer_id = data.get("reviewer_id")
        req.reviewer_name = data.get("reviewer_name")
        req.review_note = data.get("review_note", "")
        req.created_at = data.get("created_at")
        req.reviewed_at = data.get("reviewed_at")
        req.returned_at = data.get("returned_at")
        req.return_note = data.get("return_note", "")
        req.version = data.get("version", 0)
        req._lock = threading.Lock()
        return req

    def bump_version(self) -> int:
        self.version += 1
        return self.version


class SparePartAuditLog:
    def __init__(
        self,
        log_id: str,
        part_id: str,
        part_name: str,
        action: str,
        quantity: int,
        operator_id: str,
        operator_name: str,
        order_id: Optional[str] = None,
        request_id: Optional[str] = None,
        note: str = "",
        timestamp: Optional[str] = None,
        stock_before: int = 0,
        stock_after: int = 0,
    ):
        self.log_id = log_id
        self.part_id = part_id
        self.part_name = part_name
        self.action = action
        self.quantity = quantity
        self.operator_id = operator_id
        self.operator_name = operator_name
        self.order_id = order_id
        self.request_id = request_id
        self.note = note
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.stock_before = stock_before
        self.stock_after = stock_after

    def to_dict(self) -> Dict:
        return {
            "log_id": self.log_id,
            "part_id": self.part_id,
            "part_name": self.part_name,
            "action": self.action,
            "quantity": self.quantity,
            "operator_id": self.operator_id,
            "operator_name": self.operator_name,
            "order_id": self.order_id,
            "request_id": self.request_id,
            "note": self.note,
            "timestamp": self.timestamp,
            "stock_before": self.stock_before,
            "stock_after": self.stock_after,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SparePartAuditLog":
        return cls(
            log_id=data["log_id"],
            part_id=data["part_id"],
            part_name=data["part_name"],
            action=data["action"],
            quantity=data["quantity"],
            operator_id=data["operator_id"],
            operator_name=data["operator_name"],
            order_id=data.get("order_id"),
            request_id=data.get("request_id"),
            note=data.get("note", ""),
            timestamp=data.get("timestamp"),
            stock_before=data.get("stock_before", 0),
            stock_after=data.get("stock_after", 0),
        )


class RescheduleStatus(str, Enum):
    PENDING = "待确认"
    CONFIRMED = "已确认"
    REJECTED = "已拒绝"
    CANCELLED = "已取消"
    EXPIRED = "已过期"


RESCHEDULE_STATUS_FLOW = {
    RescheduleStatus.PENDING: [RescheduleStatus.CONFIRMED, RescheduleStatus.REJECTED, RescheduleStatus.CANCELLED, RescheduleStatus.EXPIRED],
    RescheduleStatus.CONFIRMED: [],
    RescheduleStatus.REJECTED: [],
    RescheduleStatus.CANCELLED: [],
    RescheduleStatus.EXPIRED: [],
}

RESCHEDULE_DECISIONS = ("confirm", "reject")

RESCHEDULEABLE_ORDER_STATUSES = {
    Status.PENDING_DISPATCH,
    Status.DISPATCHED,
    Status.IN_PROGRESS,
    Status.PENDING_INSPECTION,
}

ARRIVAL_CONFIRMABLE_STATUSES = {
    Status.DISPATCHED,
    Status.IN_PROGRESS,
    Status.PENDING_INSPECTION,
}


class RescheduleRuleViolation:
    NO_PERMISSION = "权限不足"
    ORDER_NOT_FOUND = "工单不存在"
    ORDER_COMPLETED = "已完成工单禁止操作"
    ORDER_NOT_DISPATCHED = "未派单的工单不能操作"
    ORDER_STATUS_NOT_ALLOWED = "工单当前状态不支持此操作"
    PENDING_EXISTS = "该工单已有待确认的改约申请，请先处理"
    NOT_PENDING = "只能处理待确认状态的改约申请"
    NOT_CREATOR = "只有发起人可以撤销此改约申请"
    NOT_ASSIGNED_OR_DISPATCHER = "只有工单指定维修员或调度员可以操作"
    INVALID_SLOT = "非法时间窗"
    EMPTY_SLOTS = "至少需要提供一个候选时间窗"
    EMPTY_REASON = "原因不能为空"
    EMPTY_REJECT_REASON = "拒绝改约必须填写原因"
    SLOT_NOT_IN_CANDIDATES = "选择的时间窗不在候选列表中"
    NO_SELECTED_SLOT = "确认改约必须选择一个时间窗"
    INVALID_DECISION = "非法决策值"
    ALREADY_PROCESSED = "改约申请已被处理，重复确认不覆盖原有结果"
    SCHEDULE_CONFLICT = "时间窗冲突"
    REQUEST_NOT_FOUND = "改约申请不存在"


class RescheduleCandidateSlot:
    def __init__(self, start_time: str, end_time: str):
        self.start_time = start_time
        self.end_time = end_time

    def to_dict(self) -> Dict:
        return {
            "start_time": self.start_time,
            "end_time": self.end_time,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "RescheduleCandidateSlot":
        return cls(
            data["start_time"],
            data["end_time"],
        )

    def is_valid(self) -> bool:
        try:
            s = datetime.strptime(self.start_time, "%Y-%m-%d %H:%M")
            e = datetime.strptime(self.end_time, "%Y-%m-%d %H:%M")
            return s < e
        except (ValueError, TypeError):
            return False

    @property
    def start_dt(self) -> Optional[datetime]:
        try:
            return datetime.strptime(self.start_time, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return None

    @property
    def end_dt(self) -> Optional[datetime]:
        try:
            return datetime.strptime(self.end_time, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            return None

    def overlaps_with(self, other: "RescheduleCandidateSlot") -> bool:
        s1, e1 = self.start_dt, self.end_dt
        s2, e2 = other.start_dt, other.end_dt
        if not all([s1, e1, s2, e2]):
            return False
        return s1 < e2 and s2 < e1

    def __repr__(self):
        return f"{self.start_time} ~ {self.end_time}"


class RescheduleRequest:
    def __init__(
        self,
        reschedule_id: str,
        order_id: str,
        order_title: str,
        dispatcher_id: str,
        dispatcher_name: str,
        reason: str,
        candidate_slots: List[RescheduleCandidateSlot],
        note: str = "",
        status: RescheduleStatus = RescheduleStatus.PENDING,
        created_at: Optional[str] = None,
        version: int = 0,
        original_scheduled_start: Optional[str] = None,
        original_scheduled_end: Optional[str] = None,
    ):
        self.reschedule_id = reschedule_id
        self.order_id = order_id
        self.order_title = order_title
        self.dispatcher_id = dispatcher_id
        self.dispatcher_name = dispatcher_name
        self.reason = reason
        self.candidate_slots = candidate_slots
        self.note = note
        self.status = status
        self.created_at = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        self.version = version
        self.original_scheduled_start = original_scheduled_start
        self.original_scheduled_end = original_scheduled_end

    def to_dict(self) -> Dict:
        return {
            "reschedule_id": self.reschedule_id,
            "order_id": self.order_id,
            "order_title": self.order_title,
            "dispatcher_id": self.dispatcher_id,
            "dispatcher_name": self.dispatcher_name,
            "reason": self.reason,
            "candidate_slots": [s.to_dict() for s in self.candidate_slots],
            "note": self.note,
            "status": self.status.value,
            "created_at": self.created_at,
            "version": self.version,
            "original_scheduled_start": self.original_scheduled_start,
            "original_scheduled_end": self.original_scheduled_end,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "RescheduleRequest":
        return cls(
            reschedule_id=data["reschedule_id"],
            order_id=data["order_id"],
            order_title=data.get("order_title", ""),
            dispatcher_id=data["dispatcher_id"],
            dispatcher_name=data["dispatcher_name"],
            reason=data["reason"],
            candidate_slots=[RescheduleCandidateSlot.from_dict(s) for s in data.get("candidate_slots", [])],
            note=data.get("note", ""),
            status=RescheduleStatus(data.get("status", RescheduleStatus.PENDING.value)),
            created_at=data.get("created_at"),
            version=data.get("version", 0),
            original_scheduled_start=data.get("original_scheduled_start"),
            original_scheduled_end=data.get("original_scheduled_end"),
        )

    @property
    def status_label(self) -> str:
        return self.status.value

    def bump_version(self) -> int:
        self.version += 1
        return self.version

    def can_transition_to(self, new_status: RescheduleStatus) -> bool:
        return new_status in RESCHEDULE_STATUS_FLOW.get(self.status, [])

    def has_candidate_slot(self, slot: RescheduleCandidateSlot) -> bool:
        return any(
            s.start_time == slot.start_time and s.end_time == slot.end_time
            for s in self.candidate_slots
        )

    def candidate_slots_text(self) -> str:
        return "; ".join(str(s) for s in self.candidate_slots)

    def summary_text(self) -> str:
        lines = [
            f"改约编号: {self.reschedule_id}",
            f"工单: {self.order_id}  {self.order_title}",
            f"创建人: {self.dispatcher_name}    创建时间: {self.created_at}",
            f"原因: {self.reason}",
        ]
        if self.note:
            lines.append(f"备注: {self.note}")
        lines.append(
            f"原排程: {self.original_scheduled_start or '(无)'} ~ {self.original_scheduled_end or '(无)'}"
        )
        lines.append(f"当前状态: {self.status.value}    版本: v{self.version}")
        lines.append("候选时间窗:")
        for i, slot in enumerate(self.candidate_slots):
            lines.append(f"  [{i+1}] {slot.start_time} ~ {slot.end_time}")
        return "\n".join(lines)


class RescheduleConfirmLog:
    def __init__(
        self,
        log_id: str,
        reschedule_id: str,
        order_id: str,
        confirmer_id: str,
        confirmer_name: str,
        confirmer_role: str,
        decision: str,
        selected_slot_start: Optional[str] = None,
        selected_slot_end: Optional[str] = None,
        reject_reason: str = "",
        note: str = "",
        timestamp: Optional[str] = None,
    ):
        self.log_id = log_id
        self.reschedule_id = reschedule_id
        self.order_id = order_id
        self.confirmer_id = confirmer_id
        self.confirmer_name = confirmer_name
        self.confirmer_role = confirmer_role
        self.decision = decision
        self.selected_slot_start = selected_slot_start
        self.selected_slot_end = selected_slot_end
        self.reject_reason = reject_reason
        self.note = note
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    def to_dict(self) -> Dict:
        return {
            "log_id": self.log_id,
            "reschedule_id": self.reschedule_id,
            "order_id": self.order_id,
            "confirmer_id": self.confirmer_id,
            "confirmer_name": self.confirmer_name,
            "confirmer_role": self.confirmer_role,
            "decision": self.decision,
            "selected_slot_start": self.selected_slot_start,
            "selected_slot_end": self.selected_slot_end,
            "reject_reason": self.reject_reason,
            "note": self.note,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "RescheduleConfirmLog":
        return cls(
            log_id=data["log_id"],
            reschedule_id=data["reschedule_id"],
            order_id=data["order_id"],
            confirmer_id=data["confirmer_id"],
            confirmer_name=data["confirmer_name"],
            confirmer_role=data["confirmer_role"],
            decision=data["decision"],
            selected_slot_start=data.get("selected_slot_start"),
            selected_slot_end=data.get("selected_slot_end"),
            reject_reason=data.get("reject_reason", ""),
            note=data.get("note", ""),
            timestamp=data.get("timestamp"),
        )

    @property
    def decision_label(self) -> str:
        labels = {
            "confirm": "确认改约",
            "reject": "拒绝改约",
        }
        return labels.get(self.decision, self.decision)

    @property
    def confirmed_at(self) -> str:
        return self.timestamp

    @property
    def selected_slot_text(self) -> str:
        if self.selected_slot_start and self.selected_slot_end:
            return f"{self.selected_slot_start} ~ {self.selected_slot_end}"
        return ""

    def row_values(self) -> Tuple:
        return (
            self.log_id,
            self.confirmer_name,
            self.decision_label,
            self.selected_slot_text,
            self.reject_reason or "",
            self.confirmed_at,
        )


class ArrivalConfirmation:
    def __init__(
        self,
        arrival_id: str,
        order_id: str,
        order_title: str,
        confirmer_id: str,
        confirmer_name: str,
        confirmer_role: str,
        scheduled_start: Optional[str] = None,
        scheduled_end: Optional[str] = None,
        actual_arrival_time: Optional[str] = None,
        note: str = "",
        status: str = "confirmed",
        timestamp: Optional[str] = None,
    ):
        self.arrival_id = arrival_id
        self.order_id = order_id
        self.order_title = order_title
        self.confirmer_id = confirmer_id
        self.confirmer_name = confirmer_name
        self.confirmer_role = confirmer_role
        self.scheduled_start = scheduled_start
        self.scheduled_end = scheduled_end
        self.actual_arrival_time = actual_arrival_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.note = note
        self.status = status
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")

    def to_dict(self) -> Dict:
        return {
            "arrival_id": self.arrival_id,
            "order_id": self.order_id,
            "order_title": self.order_title,
            "confirmer_id": self.confirmer_id,
            "confirmer_name": self.confirmer_name,
            "confirmer_role": self.confirmer_role,
            "scheduled_start": self.scheduled_start,
            "scheduled_end": self.scheduled_end,
            "actual_arrival_time": self.actual_arrival_time,
            "note": self.note,
            "status": self.status,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ArrivalConfirmation":
        return cls(
            arrival_id=data["arrival_id"],
            order_id=data["order_id"],
            order_title=data.get("order_title", ""),
            confirmer_id=data["confirmer_id"],
            confirmer_name=data["confirmer_name"],
            confirmer_role=data["confirmer_role"],
            scheduled_start=data.get("scheduled_start"),
            scheduled_end=data.get("scheduled_end"),
            actual_arrival_time=data.get("actual_arrival_time"),
            note=data.get("note", ""),
            status=data.get("status", "confirmed"),
            timestamp=data.get("timestamp"),
        )

    @property
    def status_label(self) -> str:
        labels = {
            "confirmed": "已到场",
            "cancelled": "已取消",
        }
        return labels.get(self.status, self.status)

    @property
    def confirmed_at(self) -> str:
        return self.actual_arrival_time or self.timestamp

    def row_values(self) -> Tuple:
        return (
            self.arrival_id,
            self.order_id,
            self.confirmer_name,
            self.note or "",
            self.confirmed_at,
        )
