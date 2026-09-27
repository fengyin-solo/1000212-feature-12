"""并网调度业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "grid_connect"
REQUIRED_FIELDS = ["指令编号", "调度机构", "指令内容"]

# 生命周期：待签收 → 执行中 → 待复核 → 已关闭；超期是回执晚于截止时刻时的待复核形态
TERMINAL_STATUSES = ("已关闭", "已撤回")
STATUS_ORDER = ["待签收", "执行中", "待复核", "超期", "已关闭", "已撤回"]

# 当前状态决定可执行动作；不在列表里的动作一律拦下
STATE_ACTIONS = {
    "待签收": ["签收", "撤回"],
    "执行中": ["提交回执", "恢复"],
    "待复核": ["复核完成"],
    "超期": ["复核完成"],
    "已关闭": [],
    "已撤回": [],
}

# 目标状态固定的动作；提交回执的目标由截止时刻决定，单独处理
ACTION_TARGETS = {
    "签收": "执行中",
    "撤回": "已撤回",
    "恢复": "待签收",
    "复核完成": "已关闭",
}

DEADLINE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_deadline(value: Any) -> datetime | None:
    """解析执行截止时刻；缺值或格式不认识时视为没有截止约束。"""
    text = str(value or "").strip()
    for fmt in DEADLINE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


class GridConnectService:
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
        return [self._with_actions(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        return self._with_actions(entry) if entry is not None else None

    @staticmethod
    def _with_actions(row: dict[str, Any]) -> dict[str, Any]:
        """按当前状态挂上可执行动作，前端直接照着渲染。"""
        row["actions"] = STATE_ACTIONS.get(str(row.get("status") or ""), [])
        return row

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in ("下发时间", "执行截止", "执行人员", "反馈情况"):
            if values.get(field) is not None:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["指令状态"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return self._with_actions(entry), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"调度指令 {entry_id} 不存在或已归档"
        current = str(entry.get("status") or STATUS_ORDER[0])
        allowed = STATE_ACTIONS.get(current, [])
        if action not in allowed:
            known = any(action in actions for actions in STATE_ACTIONS.values())
            if known:
                return None, f"当前状态「{current}」不能{action}，可执行：{'、'.join(allowed) or '无'}"
            return None, f"动作「{action}」不属于并网调度可执行范围"
        if action == "提交回执":
            deadline = _parse_deadline(entry.get("执行截止"))
            overdue = deadline is not None and datetime.now() > deadline
            # 超过截止仍能提交，只是进入超期状态；原截止时刻保持不变
            target = "超期" if overdue else "待复核"
        else:
            target = ACTION_TARGETS[action]
        entry["status"] = target
        entry["指令状态"] = target
        entry["pending"] = target not in TERMINAL_STATUSES
        entry["abnormal"] = target == "超期"
        message = f"调度指令已{action}"
        if action == "提交回执" and target == "超期":
            message = "调度指令已提交回执，因超过执行截止进入超期状态"
        return self._with_actions(entry), message
