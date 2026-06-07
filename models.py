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


ROLE_PERMISSIONS = {
    Role.DISPATCHER: ["create", "dispatch", "import", "export", "view_history"],
    Role.TECHNICIAN: ["accept", "complete", "view_history"],
    Role.INSPECTOR: ["approve", "reject", "view_history", "export"],
}


class User:
    def __init__(self, user_id: str, name: str, role: Role):
        self.user_id = user_id
        self.name = name
        self.role = role

    def to_dict(self) -> Dict:
        return {"user_id": self.user_id, "name": self.name, "role": self.role.value}

    @classmethod
    def from_dict(cls, data: Dict) -> "User":
        return cls(data["user_id"], data["name"], Role(data["role"]))


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
        order._lock = threading.Lock()
        return order

    def add_exception_note(self, note: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.exception_notes.append(f"[{timestamp}] {note}")


class AppConfig:
    def __init__(self, export_dir: str = ""):
        self.export_dir = export_dir

    def to_dict(self) -> Dict:
        return {"export_dir": self.export_dir}

    @classmethod
    def from_dict(cls, data: Dict) -> "AppConfig":
        return cls(data.get("export_dir", ""))
