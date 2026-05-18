## Khan Academy Warnings

- NEVER click "Explore" or "Courses" -- these are site navigation, NOT course actions
- NEVER click "Get started" on course overview -- it skips videos/articles. Use navigate pattern instead.
- Exercise submit button is "Check" (NOT "Submit")
- After correct answer: look for "Next question" or arrow button
- Sidebar shows completion: checkmark = done, empty circle = not started
- AXComboBox on KA opens AXList via React Portal at TOP of AXWebArea, not as child of trigger
- post_delay 3.0+ on ALL page-changing clicks (React SPA takes 1-3s to load)
- "Show summary" after last exercise question needs post_delay 4.0
- "Let's go" and "Start" buttons are common TRANSITION targets on practice screens