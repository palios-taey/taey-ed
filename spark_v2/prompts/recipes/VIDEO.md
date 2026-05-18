## VIDEO Screen

Three distinct states. Check completion indicators (sidebar, progress bar) to distinguish.

### VIDEO_PLAYING (in progress, NOT marked complete)
```json
{
  "screen_type": "VIDEO_PLAYING",
  "tree": {"type": "action", "action": "video_poll"}
}
```
CRITICAL: video_poll is the ONLY action. No children. No extract section.
No params needed (30s interval is hardcoded). Returns continue_loop=True.

### VIDEO_UNSTARTED (Play button visible, lesson NOT complete)
```json
{
  "screen_type": "VIDEO_UNSTARTED",
  "tree": {"type": "sequence", "children": [
    {"type": "action", "action": "find_and_click", "params": {
      "target": "Play", "role": "AXButton", "strategy": "mouse_click", "post_delay": 2.0
    }}
  ]}
}
```

### VIDEO_COMPLETE (video visible BUT lesson IS marked complete)
This is a TRANSITION. Find the advance button from the tree.
```json
{
  "screen_type": "VIDEO_COMPLETE",
  "tree": {"type": "sequence", "children": [
    {"type": "action", "action": "find_and_click", "params": {
      "target": "ADVANCE_TEXT", "role": "AXLink", "strategy": "mouse_click",
      "match_mode": "contains", "post_delay": 3.0
    }}
  ]}
}
```

### How to Distinguish
Check for completion indicators (checkmarks, "completed" text, progress bars).
Completion status overrides video player visibility.
Find the actual advance button text from the tree -- replace ADVANCE_TEXT.