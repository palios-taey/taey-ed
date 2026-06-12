# UNKNOWN Classification Guide — khan_academy
Used ONLY when no signature matches: the classifier reads the CURRENT capture
against this guide and outputs one canonical screen_type (one YAML filename)
or UNKNOWN. Classify by the CURRENT ANSWER WIDGETS, never page history — the
same skeleton hash hosts different question types across visits.

## Where to look
AXWebArea subtree only (skip menubar/toolbar/tab strip — Chrome chrome).
Question content renders inside perseus-renderer / framework-perseus groups;
sidebar = AXLinks for lesson items; YouTube player = nested AXWebArea.
Chrome popups ("View progress", "Share with...", "Sort by:", tab-strip
radio buttons) are NEVER answer widgets.

## Decision order (first match wins)
1. Answer widgets present (question area) -> EXERCISE_*:
   - "(Choice X)"-prefixed AXCheckBox rows
       question says "Select all that apply"/"Choose N answers"
                                  -> EXERCISE_MULTIPLE_SELECT
       otherwise (single answer)  -> EXERCISE_MULTIPLE_CHOICE
       NOTE: Khan single-answer choices are AXCheckBox, NOT AXRadioButton —
       tree AXRadioButtons are browser chrome (live-proven 2026-06-11).
   - AXComboBox "Select an answer" (one per blank/row)
                                  -> EXERCISE_DROPDOWN
   - AXTextField/AXTextArea answer box (image/graph beside it is CONTEXT,
     chrome comboboxes are not answers)
                                  -> EXERCISE_TEXT_INPUT
   - grid of identical "Your answer:" cells and/or per-cell selectors
                                  -> EXERCISE_TABLE_INPUT
   - AXButtons named "Point N at X comma Y" (zero-size handles) + plot/drag
     instruction                  -> EXERCISE_GRAPH_POINTS
   - image with clickable dots, each opening a small selector; "Click each
     dot" wording                 -> EXERCISE_LABEL_IMAGE
   - draggable value-bank -> slot pairing (AXTable/AXRow/AXCell or empty-name
     AXGroup cards; clicking opens NO menu items)
                                  -> EXERCISE_MATCHER
   - draggable single-column list / category buckets; "rank/order/arrange"
     wording                      -> EXERCISE_SORTER
   - mixed/ambiguous widgets, or one hash known to alternate types
                                  -> bare EXERCISE (forces per-question
                                     classification at build time)
2. Embedded player (YouTube group / Play-Pause-Replay) -> VIDEO
3. article-renderer text content, no Check button, "Up next" link -> ARTICLE
4. Buttons/links, NO answer widgets, NO player: intro ("Let's go"/"Start
   quiz"), summary card (mastery points + "Up next"), review state
   ("(Choice X, Correct)" markers), interstitial modal -> TRANSITION
5. 15+ AXLinks content list (course/unit/lesson pages) -> NAVIGATION
6. None of the above (settings, Khanmigo chat, donation, search, profile,
   teacher dashboard) -> UNKNOWN: extract text (AXStaticText min_length 40)
   + window screenshot, escalate for mapping. Never improvise actuation.

## State signals (read AFTER type, for the session)
- correct:   green answer area / "Correct!" / ding
- wrong:     "Not quite" / "Incorrect" / "Sorry, try again" / red area —
             verdict-first: this is WRONG even if the tree moved (R3 full stop)
- complete:  sidebar "completed <Type> <title>" AXLink / checkmark AXImage;
             video also: "Replay Video" present or seek >= 99.5
- danger:    "Skip", "Up next" mid-content, "Video on YouTube", "Report a
             problem", "Try again" on summary cards — never click during
             classification
