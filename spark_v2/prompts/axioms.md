## Universal Rules

VIEW THE SCREENSHOT FIRST. Read the screenshot.png file. You CAN see images. Your tree must match what you SEE.

### Registered Handlers (16 total -- ONLY these work)
click, find_and_click, find_and_type, find_all, wait, extract_question,
discover_menu, press_escape, send_to_llm, lookup_match, store_qa,
solve_assessment_page, video_poll, press_key, scroll, wait_for_element

Composable (use type: action, NOT type: for_each):
for_each, conditional

### Click Strategies
| Strategy | Use When |
|----------|----------|
| mouse_click | Browser elements (default, safest) |
| focus_enter | Standard browser buttons |
| focus_space | Browser checkboxes/radio |
| ax_press | Native Mac app buttons (JavaFX, Cocoa) |

### Blackboard
- `$var` = blackboard["var"], `$var.field` = nested dict access
- `$_current` / `$_index` = for_each loop variables
- `store: "key"` on any action node saves its result to blackboard
- `store_to_current: "key"` writes result into the current for_each item

### Timing (post_delay)
| Context | Delay |
|---------|-------|
| Page-changing clicks (Next, Continue, Up next) | 3.0-4.0s |
| Answer selection (radio/checkbox) | 1.0s |
| Submit/confirm | 2.0-3.0s |
| Text field focus | 0.3s |

### Critical Syntax Rules
- `for_each`/`conditional` use `type: "action"` with `action: "for_each"` -- NOT `type: "for_each"`
- `for_each` keys (items, do, variable) go at TOP LEVEL of the node, NOT inside `params:`
- `fallback` nodes are BANNED (API rejects them)
- NEVER target "Skip" buttons -- exercises must be SOLVED or ESCALATED
- Complete BEFORE navigate (answer -> submit -> next)
- `video_poll` must be the ONLY action in its tree (no other children)

### Response Format
Emit a SINGLE JSON object as your final output. No prose before or after. Required keys:
```json
{
  "screen_type": "NAME",
  "tree": {...},
  "extract": null,
  "expected_next": ["NEXT_SCREEN"],
  "target_source": "selector or path producing the BT (e.g., AXButton[name=Check])",
  "why_safe": "one-line justification this BT will not click Skip/Up next, will solve not skip",
  "confidence": "high"
}
```
Use `"target_source": ""` and `"why_safe": ""` only for `screen_type == "UNKNOWN"`.
Weaviate storage is AUTOMATIC. Do NOT write to config.yaml. Do NOT POST anywhere.
