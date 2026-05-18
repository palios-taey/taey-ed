## ASSESSMENT_MATCHING Screen

Dropdown matching: each popup/combobox maps to a correct menu item.
NEVER hardcode item names -- exercises change every time.

### Pattern
```json
{
  "screen_type": "QUIZ_MATCHING",
  "tree": {
    "type": "sequence",
    "children": [
      {"type": "action", "action": "find_all", "params": {"role": "DROPDOWN_ROLE"}, "store": "popups"},
      {"type": "action", "action": "for_each", "items": "$popups", "do": {
        "type": "action", "action": "discover_menu", "params": {"role": "AXMenuItem"},
        "store_to_current": "options"
      }},
      {"type": "action", "action": "send_to_llm", "params": {
        "question": "Match each item", "question_type": "solve_matching", "items": "$popups"
      }, "store": "llm_result"},
      {"type": "action", "action": "for_each", "items": "$popups", "do": {
        "type": "sequence", "children": [
          {"type": "action", "action": "find_and_click", "params": {
            "target": "$_current.label", "role": "DROPDOWN_ROLE",
            "strategy": "mouse_click", "match_mode": "contains"
          }},
          {"type": "action", "action": "wait", "params": {"seconds": 1.0}},
          {"type": "action", "action": "lookup_match", "params": {
            "matches": "$llm_result.matches", "key": "$_current.label"
          }, "store": "match_answer"},
          {"type": "action", "action": "find_and_click", "params": {
            "target": "$match_answer", "role": "AXMenuItem",
            "strategy": "mouse_click", "match_mode": "contains"
          }},
          {"type": "action", "action": "wait", "params": {"seconds": 0.5}}
        ]
      }},
      {"type": "action", "action": "find_and_click", "params": {
        "target": "SUBMIT_TEXT", "role": "AXButton",
        "strategy": "mouse_click", "match_mode": "exact", "post_delay": 3.0
      }}
    ]
  }
}
```

### Checklist
- [ ] Check tree for dropdown role: AXPopUpButton (native select) vs AXComboBox (React/custom) -- replace DROPDOWN_ROLE
- [ ] AXComboBox may open AXList elsewhere in tree (React Portal), not AXMenu as child
- [ ] Find the ACTUAL submit button text from the tree -- replace SUBMIT_TEXT
- [ ] for_each keys at TOP LEVEL, NOT in params