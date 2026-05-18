## UNKNOWN Screen

The classifier could not determine the archetype. Classify manually.

### Steps
1. VIEW the screenshot (Read screenshot.png -- you CAN see images)
2. READ tree.json -- focus on interactive elements in AXWebArea
3. Classify:
   - Radio buttons? -> Use ASSESSMENT_RADIO pattern
   - Checkboxes? -> Use ASSESSMENT_CHECKBOX pattern
   - Text input + question? -> Use ASSESSMENT_TEXT pattern
   - Dropdowns (2+)? -> Use ASSESSMENT_MATCHING pattern
   - Content list with many links? -> Use CONTENT_LIST pattern (find_all -> navigate -> click)
   - Single button/link to advance? -> Use TRANSITION pattern
   - Video player? -> Use VIDEO pattern
4. If genuinely unrecognizable, research via Perplexity

### Common Misclassifications
- Post-answer feedback (radios visible but NOT clickable) -> TRANSITION (click Next/Continue)
- Completed content with sidebar -> TRANSITION (click "Up next"), NOT CONTENT_LIST
- Loading/processing screen -> TRANSITION with wait action
- Confirmation modal -> TRANSITION (click Confirm/Submit)