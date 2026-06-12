# Per-Screen-Type Definitions (Jesse's intended architecture, 2026-06-12)

One YAML per (platform, screen type). Each is the COMPLETE, CURRENT, ONE-PAGE
truth for operating that screen type. The worker's build context = core rules
+ THE ONE matching YAML + the screen session. Nothing else.

knowledge.json shrinks back to its intended role: platform identity, global
never_click/timing, and the classification guide used ONLY for UNKNOWN screens.

Learning = EDIT THE YAML IN PLACE (supersede, never layer). A type's YAML can
never contain two conflicting instructions — that defect class is structurally
impossible here and was the root of repeated worker failures.

Schema per file:
  screen_type:        canonical name (EXERCISE_DROPDOWN, VIDEO, ...)
  classify:           how to recognize it from the CURRENT answer widgets
  recipe:             the canonical BT skeleton — THE program; the worker fills
                      question-specific values only, never redesigns
  contracts:          answer formats (verbatim option names, strict JSON, ...)
  actuation:          strategies + timing (mouse_click, press_hold 0.25, settle 2s)
  verification:       how to confirm effect (staging values, screenshots, verdicts)
  completion:         indicators that mean done/advance
  failure_modes:      known traps, each with its counter
