import json
import os
import threading
from datetime import datetime
from typing import List, Dict, Optional, Tuple
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
    Role.DISPATCHER: ["create", "dispatch", "import", "export", "view_history", "reassign", "manage_schedule"],
    Role.TECHNICIAN: ["accept", "complete", "view_history"],
    Role.INSPECTOR: ["approve", "reject", "view_history", "export"],
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
    ):
        self.order_id = order_id
        self.success = success
        self.skipped = skipped
        self.target_technician_id = target_technician_id
        self.target_technician_name = target_technician_name
        self.reason = reason
        self.error_message = error_message
        self.conflict_types = conflict_types or []

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
        }


class BatchReassignmentResult:
    def __init__(
        self,
        dispatcher_id: str,
        dispatcher_name: str,
        timestamp: Optional[str] = None,
        results: Optional[List[BatchItemResult]] = None,
    ):
        self.dispatcher_id = dispatcher_id
        self.dispatcher_name = dispatcher_name
        self.timestamp = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.results = results or []

    @property
    def success_count(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.success and not r.skipped)

    def to_dict(self) -> Dict:
        return {
            "dispatcher_id": self.dispatcher_id,
            "dispatcher_name": self.dispatcher_name,
            "timestamp": self.timestamp,
            "success_count": self.success_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "results": [r.to_dict() for r in self.results],
        }


class AppConfig:
    def __init__(self, export_dir: str = ""):
        self.export_dir = export_dir

    def to_dict(self) -> Dict:
        return {"export_dir": self.export_dir}

    @classmethod
    def from_dict(cls, data: Dict) -> "AppConfig":
        return cls(data.get("export_dir", ""))
