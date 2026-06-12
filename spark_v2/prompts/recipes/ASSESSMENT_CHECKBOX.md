## ASSESSMENT_CHECKBOX Screen

Multi-select quiz with checkboxes. Uses for_each to click each selected option.

### Pattern
```json
{
  "screen_type": "QUIZ_CHECKBOX",
  "tree": {
    "type": "sequence",
    "children": [
      {"type": "action", "action": "extract_question", "store": "q_data"},
      {"type": "action", "action": "send_to_llm", "params": {
        "question": "$q_data.question_text", "question_type": "solve_checkbox",
        "options": "$q_data.options", "context": "$q_data.reference_texts"
      }, "store": "llm_result"},
      {"type": "action", "action": "for_each", "items": "$llm_result.selected", "do": {
        "type": "action", "action": "find_and_click", "params": {
          "target": "$_current", "role": "AXCheckBox",
          "strategy": "mouse_click", "match_mode": "exact", "post_delay": 0.5
        }
      }},
      {"type": "action", "action": "find_and_click", "params": {
        "target": "SUBMIT_TEXT", "role": "AXButton",
        "strategy": "mouse_click", "match_mode": "exact", "post_delay": 3.0
      }},
      {"type": "action", "action": "store_qa", "params": {
        "question": "$q_data.question_text", "answer": "$llm_result.selected",
        "question_type": "solve_checkbox"
      }}
    ]
  },
  "extract": {
    "scope": "web_area",
    "question": {"role": "AXStaticText", "contains": "?"},
    "options": {"role": "AXCheckBox", "exclude_titles": ["Back", "Close", "Skip"]},
    "text": [{"role": "AXStaticText", "parent_role": "AXGroup"}]
  }
}
```

### Checklist
- [ ] Find the ACTUAL submit button text from the tree -- replace SUBMIT_TEXT
- [ ] Add non-answer interactive elements to exclude_titles (UI toggles, mode switches -- check tree)
- [ ] for_each keys (items, do) at TOP LEVEL, NOT in params

### Key Differences from Radio
- question_type: "solve_checkbox" (returns `selected` list, not single `answer`)
- for_each iterates over `$llm_result.selected`
