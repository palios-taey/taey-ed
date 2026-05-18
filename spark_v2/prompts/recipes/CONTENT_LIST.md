## CONTENT_LIST Screen

List of items (courses, units, lessons). Must select first incomplete item DYNAMICALLY.
NEVER hardcode item names -- content varies per user/course.

### Pattern (MANDATORY)
```json
{
  "screen_type": "LIST_NAVIGATION",
  "tree": {
    "type": "sequence",
    "children": [
      {"type": "action", "action": "find_all", "params": {
        "role": "AXLink", "description_contains": "FILTER"
      }, "store": "nav_items"},
      {"type": "action", "action": "send_to_llm", "params": {
        "question_type": "navigate", "items": "$nav_items"
      }, "store": "nav_result"},
      {"type": "action", "action": "find_and_click", "params": {
        "target": "$nav_result.answer", "role": "AXLink",
        "strategy": "mouse_click", "match_mode": "exact", "post_delay": 3.0
      }}
    ]
  }
}
```

### Checklist
- [ ] Find common text pattern in list items for description_contains -- look at the tree for what the items share
- [ ] NEVER use literal text like "Unit 1" -- always use $nav_result.answer
- [ ] Check role: usually AXLink but could be AXButton -- verify in tree
- [ ] No extract section needed (navigation, not content)

### Distinction
If the screen has a sidebar list BUT the primary action is a single advance button, it's a TRANSITION, not CONTENT_LIST. Only use this pattern when the PRIMARY PURPOSE is selecting from a list.
