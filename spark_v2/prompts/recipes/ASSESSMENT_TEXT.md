## ASSESSMENT_TEXT Screen

Free-text answer: type response into text area or text field.

### Pattern
```json
{
  "screen_type": "QUIZ_TEXT_INPUT",
  "tree": {
    "type": "sequence",
    "children": [
      {"type": "action", "action": "extract_question", "store": "q_data"},
      {"type": "action", "action": "send_to_llm", "params": {
        "question": "$q_data.question_text", "question_type": "solve",
        "context": "$q_data.reference_texts"
      }, "store": "llm_result"},
      {"type": "action", "action": "find_and_type", "params": {
        "target": "", "role": "AXTextArea",
        "text": "$llm_result.answer", "focus_strategy": "mouse_click"
      }},
      {"type": "action", "action": "find_and_click", "params": {
        "target": "SUBMIT_TEXT", "role": "AXButton",
        "strategy": "mouse_click", "match_mode": "contains", "post_delay": 3.0
      }},
      {"type": "action", "action": "store_qa", "params": {
        "question": "$q_data.question_text", "answer": "$llm_result.answer",
        "question_type": "solve"
      }}
    ]
  },
  "extract": {
    "scope": "web_area",
    "question": {"role": "AXStaticText", "contains": "?"},
    "text": [{"role": "AXStaticText", "parent_role": "AXGroup"}]
  }
}
```

### Checklist
- [ ] Check if target role is AXTextArea or AXTextField (varies by platform)
- [ ] Find the ACTUAL submit button text from the tree -- replace SUBMIT_TEXT
- [ ] If multiple text fields, use target text to disambiguate
- [ ] focus_strategy: "mouse_click" is safest for browsers