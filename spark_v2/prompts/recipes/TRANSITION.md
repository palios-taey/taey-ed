## TRANSITION Screen

Single navigation click. No content extraction, no question answering.

### Pattern
```json
{
  "screen_type": "DESCRIPTIVE_NAME",
  "tree": {
    "type": "sequence",
    "children": [
      {"type": "action", "action": "find_and_click", "params": {
        "target": "BUTTON_TEXT", "role": "AXButton",
        "strategy": "mouse_click", "match_mode": "exact", "post_delay": 3.0
      }}
    ]
  }
}
```

### Checklist
- [ ] Look at screenshot -- which button advances? (Resume, Next, Continue, Start, Up next, Let's go)
- [ ] Check tree for exact text and AX role (AXButton, AXLink, AXStaticText)
- [ ] Use match_mode "contains" only if text varies across instances
- [ ] post_delay 3.0+ for any click that changes the page
- [ ] No extract section needed
- [ ] screen_type should be reusable (e.g., "PRACTICE_START" not "Unit_3_Start")