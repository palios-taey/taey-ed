# Per-Screen-Type Definitions (Jesse's intended architecture, 2026-06-12)

One YAML per (platform, screen type). Each is the COMPLETE, CURRENT, ONE-PAGE
truth for operating that screen type. The worker's build context = core rules
+ THE ONE matching YAML + the screen session. Nothing else.

knowledge.json shrinks back to its intended role: platform identity, global
never_click/timing, and the classification guide used ONLY for UNKNOWN screens.

Learning = EDIT THE YAML IN PLACE (supersede, never layer). A type's YAML can
never contain two conflicting instructions — that defect class is structurally
impossible here and was the root of repeated worker failures.

## CATEGORY -> SUBTYPE (2026-06-13, Jesse-confirmed; converged 3-design synthesis)

A MASTER CATEGORY (NAVIGATION, ARTICLE, VIDEO, EXERCISE, TRANSITION) is NOT a
screen — it is a routing namespace. Real screens are SUBTYPES. No bare master
category may be a `screen_type`, a classify output, or a recipe/BT key for the
split masters (NAV/ARTICLE/VIDEO; EXERCISE_* are already subtyped). The
classifier and assembler REJECT a bare master and resolve to a subtype or UNKNOWN.

  File naming:   MASTER__SUBTYPE.yaml  (double underscore)
                 e.g. NAVIGATION__COURSE_DASHBOARD.yaml, VIDEO__PLAYING.yaml
  Category file: MASTER.yaml is OPTIONAL and holds ONLY shared routing context
                 (shared classify hints, shared completion indicators, the
                 subtype registry) — it is NOT executable, never serves a recipe.

DETERMINISM (Jesse): ARTICLE + VIDEO subtypes are FULLY automated — fixed BT,
no validation. NAVIGATION subtypes share one fixed BT (structure constant; the
send_to_llm node adapts per screen at Mac-execution time). EXERCISE subtypes
are NOT auto-trusted — they need the 3-success promotion (combined exercises
false-flag), so they carry a recipe the worker realizes once, not a fixed BT.

Schema per file:
  screen_type:        FULL canonical name "MASTER__SUBTYPE" (never bare master)
  master_category:    NAVIGATION | ARTICLE | VIDEO | EXERCISE | TRANSITION
  subtype:            the subtype slug (course_dashboard, playing, dropdown, ...)
  deterministic:      true  -> a fixed BT is stored and replayed VERBATIM every
                              encounter (no worker rebuild = no variance); the
                              worker is invoked only on first discovery / after
                              demotion. ARTICLE/VIDEO/NAVIGATION subtypes.
                      false -> recipe is realized by the worker; EXERCISE
                              subtypes (3-success promotion before trust).
  fixed_behavior_tree: (deterministic only) the exact BT JSON replayed to the
                      Mac. Structure is constant; intelligence lives in its
                      send_to_llm / extract_question nodes evaluated at Mac
                      execution time, so a fixed BT stays screen-adaptive.
                      store/store_to_current MUST be at the NODE level, not in
                      params (Mac engine reads node_def.get('store')).
  classify:           how to recognize THIS subtype from the CURRENT tree+screenshot
  recipe:             (non-deterministic only) the canonical BT skeleton the
                      worker fills — question-specific values only, never redesigns
  contracts:          answer formats (verbatim option names, strict JSON, ...)
  actuation:          strategies + timing (mouse_click, press_hold 0.25, settle 2s)
  verification:       how to confirm effect (staging values, screenshots, verdicts)
  completion:         indicators that mean done/advance
  failure_modes:      known traps, each with its counter
