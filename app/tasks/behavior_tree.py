"""
Behavior tree - split into bt_core.py, bt_helpers.py, bt_handlers.py.
This file re-exports for backward compatibility.
"""
from app.tasks.bt_core import execute_tree, Status, Blackboard, ExecutionContext, tick_node, btlog
from app.tasks.bt_helpers import (
    _find_menu_subtree, _extract_menu_items, _find_preceding_label,
    _find_elements_by_role, _find_assessment_questions, _find_web_area,
)
from app.tasks.bt_handlers import register_all_handlers

__all__ = [
    "execute_tree",
    "Status",
    "Blackboard",
    "ExecutionContext",
    "tick_node",
    "btlog",
    "register_all_handlers",
    "_find_menu_subtree",
    "_extract_menu_items",
    "_find_preceding_label",
    "_find_elements_by_role",
    "_find_assessment_questions",
    "_find_web_area",
]
