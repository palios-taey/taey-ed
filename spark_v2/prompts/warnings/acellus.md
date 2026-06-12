## Acellus Warnings

- Acellus uses JavaFX — ax_press strategy works for buttons, but mouse_click is safest
- Course tiles on dashboard are clickable — no separate "Start" button
- Video lessons auto-play after clicking course tile — expect VIDEO state immediately
- Practice problems appear AFTER each video lesson (video -> practice -> video -> practice)
- "Assignment Complete" / "Goal Accomplished" screens are TRANSITION — click "Move On" or "Continue"
- Submit button on practice problems may be unlabeled — check tree for AXButton near answer area
- Menu (hamburger icon) opens lesson list — avoid clicking it during normal flow
- Progress bar at top shows remaining problems — NOT a clickable element
- post_delay 3.0+ for all navigation clicks (JavaFX transitions are slower than React)