"""并网调度业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "grid_connect"
REQUIRED_FIELDS = ["指令编号", "调度机构", "指令内容"]
OPTIONAL_FIELDS = ["下发时间", "执行截止", "执行人员", "反馈情况"]

# 生命周期主线：待签收 → 执行中 → 待复核 → 已关闭；已撤回是签收前的分支终态。
STATUS_ORDER = ["待签收", "执行中", "待复核", "已关闭"]
WITHDRAWN = "已撤回"
TERMINAL_STATUSES = ["已关闭", WITHDRAWN]

# 当前状态决定可执行动作：签收前可撤回，执行中可恢复，回执提交后待复核，复核完成才关闭。
ACTION_RULES: dict[str, dict[str, Any]] = {
    "签收指令": {"sources": ["待签收"], "target": "执行中"},
    "撤回指令": {"sources": ["待签收"], "target": WITHDRAWN},
    "恢复指令": {"sources": ["执行中"], "target": "待签收"},
    "提交回执": {"sources": ["执行中"], "target": "待复核"},
    "复核完成": {"sources": ["待复核"], "target": "已关闭"},
}


def _parse_deadline(raw: Any) -> datetime | None:
    """执行截止可能是日期或日期时间；解析不了就当没有截止时刻。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _display_status(entry: dict[str, Any]) -> str:
    """列表里展示的指令状态：超期且未终结时带上超期标记。"""
    status = str(entry.get("status") or STATUS_ORDER[0])
    if entry.get("overdue") and status not in TERMINAL_STATUSES:
        return f"{status}（超期）"
    return status


class GridConnectService:
    def _serialize(self, entry: dict[str, Any]) -> dict[str, Any]:
        """输出前同步展示状态，并按当前状态给出可执行动作清单。"""
        data = dict(entry)
        data["指令状态"] = _display_status(entry)
        data["available_actions"] = [
            action for action, rule in ACTION_RULES.items() if entry.get("status") in rule["sources"]
        ]
        return data

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("指令编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._serialize(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._serialize(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry.update({field: values[field] for field in OPTIONAL_FIELDS if values.get(field) is not None})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["overdue"] = False
        rows.append(entry)
        return self._serialize(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度指令 {entry_id} 不存在或已归档"
        rule = ACTION_RULES.get(action)
        if rule is None:
            return None, f"动作「{action}」不属于并网调度可执行范围"
        if entry.get("status") not in rule["sources"]:
            return None, f"当前状态「{_display_status(entry)}」不允许执行「{action}」"
        target = rule["target"]
        entry["status"] = target
        entry["pending"] = target not in TERMINAL_STATUSES
        message = f"调度指令已{action}"
        if action == "提交回执":
            deadline = _parse_deadline(entry.get("执行截止"))
            if deadline is not None and datetime.now() > deadline:
                # 超过截止时间仍能提交，但进入超期状态，原截止时刻不变。
                entry["overdue"] = True
                entry["abnormal"] = True
                message = "调度指令已提交回执（已超期，原执行截止时刻不变）"
        return self._serialize(entry), message
