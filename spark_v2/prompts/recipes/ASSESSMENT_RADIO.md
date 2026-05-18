## ASSESSMENT_RADIO Screen

Multiple choice quiz with radio buttons. Extract question, get LLM answer, click answer, submit.

### Pattern
```json
{
  "screen_type": "QUIZ_MULTIPLE_CHOICE",
  "tree": {
    "type": "sequence",
    "children": [
      {"type": "action", "action": "extract_question", "store": "q_data"},
      {"type": "action", "action": "send_to_llm", "params": {
        "question": "$q_data.question_text", "question_type": "solve_choice",
        "options": "$q_data.options", "context": "$q_data.reference_texts"
      }, "store": "llm_result"},
      {"type": "action", "action": "find_and_click", "params": {
        "target": "$llm_result.answer", "role": "AXRadioButton",
        "strategy": "mouse_click", "match_mode": "exact", "post_delay": 1.0
      }},
      {"type": "action", "action": "find_and_click", "params": {
        "target": "SUBMIT_TEXT", "role": "AXButton",
        "strategy": "mouse_click", "match_mode": "exact", "post_delay": 3.0
      }},
      {"type": "action", "action": "store_qa", "params": {
        "question": "$q_data.question_text", "answer": "$llm_result.answer",
        "question_type": "solve_choice"
      }}
    ]
  },
  "extract": {
    "scope": "web_area",
    "question": {"role": "AXStaticText", "contains": "?"},
    "options": {"role": "AXRadioButton", "exclude_titles": ["Back", "Close", "Skip"]},
    "text": [{"role": "AXStaticText", "parent_role": "AXGroup"}]
  }
}
```

### Checklist
- [ ] Verify answer elements ARE AXRadioButton (check tree -- could be AXCheckBox, AXComboBox)
- [ ] Find the ACTUAL submit button text from the tree (Submit, Check, Check answer, etc.) -- replace SUBMIT_TEXT
- [ ] extract.options.role must match actual answer element role from the tree
- [ ] Add non-answer interactive elements to exclude_titles (UI toggles, mode switches)
- [ ] $q_data.reference_texts gives LLM full page context -- use it
