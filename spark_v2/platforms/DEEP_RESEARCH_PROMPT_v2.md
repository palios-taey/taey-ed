# Deep Research Prompt v2 — Platform Discovery Template

**Used by**: Perplexity (Deep Research mode), invoked via Taeys-Hands MCP by `spark_v2/discovery/perplexity_driver.py`
**Triggered by**: First-touch of any platform with empty `knowledge.json` (Universal Layer §5)
**Output**: JSON conforming to `KNOWLEDGE_JSON_v2_SCHEMA.md`, written to `provisional_knowledge.json` (NOT directly to `knowledge.json` — Cosmos §3 quarantine)
**Date**: 2026-05-15

---

## PROMPT (copy everything below this line)

---

You are generating structured platform knowledge for **Taey-Ed**, an educational platform automation system. Taey-Ed uses a Mac app to navigate LMS (Learning Management System) platforms via accessibility APIs (AXButton, AXLink, AXRadioButton, AXComboBox, etc.) and **Claude Opus 4.7 via the CLI worker queue** to build behavior trees that complete coursework autonomously.

**YOUR DELIVERABLE IS A SINGLE JSON OBJECT. NOT A REPORT.**

Your output will be machine-consumed and validated against a strict schema. It will be JIT-injected into Claude Opus 4.7's prompt context during behavior-tree construction for this platform. Every field matters. Vague or missing information causes BT failures that route the system into a more expensive recovery loop.

**Your output is provisional knowledge.** It will not be trusted blindly. The system will JIT-inject your findings, attempt actions guided by them, and only merge your entries into permanent platform knowledge after Spark's Step 2 empirically validates that an action succeeded using each field. Confident-but-wrong values cost a validation cycle but cannot poison the permanent knowledge base.

### PLATFORM TO RESEARCH

- **Platform name**: {PLATFORM_NAME}
- **Platform URL**: {PLATFORM_URL}
- **Platform type**: {PLATFORM_TYPE}    (MOOC | LMS | tutorial | other)
- **Any preserved failure context**: {ANY_CONTEXT}

### YOUR TASK

Research this platform and output **ONE JSON object** conforming to the schema below. Fill every field for which you have evidence. For fields where you cannot determine a value confidently from public sources, set the field to `null` and add an entry to `research_confidence.unknown_fields` listing the field path and why it's unknown.

**Do not invent values to fill schema fields.** Null + acknowledged-unknown is far better than confident-incorrect. Cannot-Lie Provenance is a hard invariant of this system.

### WHAT TO INVESTIGATE (platform-agnostic investigation areas)

1. **Platform Architecture** — SPA or MPA? Framework (React, Vue, Angular, etc.)? Client-side routing? Iframe usage for video? Modal/portal libraries (React Aria, Headless UI, custom)? Content hierarchy abstraction (course → unit → lesson → activity, or other)?

2. **Completion Indicators** — How does this platform mark items as completed? In the rendered DOM AND in the macOS accessibility tree (`AXDescription`, `AXValue`, role state, ARIA attributes). Specific candidates to investigate: completion checkmarks (text prefix, CSS class, SVG icon, screen-reader-only span), progress bars (`role=progressbar`, `aria-valuenow`), strikethrough or faded styling, percentage indicators, "Completed" text labels. **Note any AXDescription string patterns observable to the accessibility tree.**

3. **Advancement Mechanics** — After content (video, article, exercise) completes, what UI elements appear to advance the user? Exact text labels (e.g. "Next", "Continue", "Up next: ...", "Done", "Submit"). Roles (AXLink vs AXButton). Position in the page (inline at bottom, fixed footer, modal). Does the platform auto-advance some content types?

4. **Navigation Model** — How is the course structure exposed? Sidebar tree? Breadcrumb trail? Grid of cards? Linear list? How are users expected to traverse — strictly linear, adaptive-recommended, free-choice? Are there adaptive systems that might recommend skipping content?

5. **Video Player Behavior** — Embedded YouTube iframe, native HTML5, Vimeo, custom player? How is video-completion signaled in the accessibility tree (e.g., a Pause button changes to a Replay button, an end-card appears, a progress indicator hits 100%)? Does the player support speed-up (1.5x, 2x) and does that affect completion tracking?

6. **Exercise/Assessment Types** — What kinds of assessments exist? For each: how is the question presented (heading element, plain text), how are options presented (radio buttons, checkboxes, dropdowns, text input, drag-and-drop, drawing canvas, code editor), what is the exact submit button text/role, how is wrong-answer state communicated, is there a retry mechanism?

7. **Article/Reading Behavior** — Is there a completion gate (read-to-bottom, click-complete button, time threshold)? Is there a transcript or sidebar for video content? How is content laid out in the AX tree?

8. **Timing & Loading States** — Typical SPA route-transition time (ms). Modal render delay. Video iframe initialization. Skeleton/loading screens that appear before real content. Any rate-limiting or bot-detection systems observable (CAPTCHA, Cloudflare challenges, request throttling).

9. **Destructive / Off-Topic Elements** — Buttons or links that, if clicked, would derail the autonomous flow: logout, account settings, donate, "Report a problem" overlays, modal dismissals that close the wrong thing, "Skip this lesson" buttons, course-chrome navigation arrows that take the user out of the current lesson path.

10. **Platform-Specific Widget Libraries** — Does this platform use a named widget library that has known accessibility/automation quirks? (Examples worth checking: Wonder Blocks, React Aria, Material UI, custom internal libraries.) For widgets identified, note observable AX role mappings (e.g., a dropdown might appear as AXComboBox + AXPopUpButton + nested AXMenuItems).

11. **Edge Cases & Free vs Paid** — Are there feature differences for free vs paid users? Modal overlays that gate certain content? Onboarding flows for new accounts that might interfere with autonomous navigation?

### OUTPUT FORMAT

**Return ONLY a single JSON object.** Do not write a narrative report. Do not include multiple JSON blocks. Do not wrap the JSON in markdown code fences — this is parsed by Python `json.loads()`.

If Deep Research mode forces a narrative section, keep it minimal (a few sentences at most) and place the complete JSON as the **final block** of your output. The post-processor will extract the trailing JSON object.

The JSON must conform to the schema in `KNOWLEDGE_JSON_v2_SCHEMA.md`. The key structural points repeated here:

```json
{
  "schema_version": "v2",
  "platform": {
    "name": "{platform_slug}",
    "display_name": "{Platform Display Name}",
    "url_pattern": "{domain or regex}",
    "platform_type": "{MOOC|LMS|tutorial|other}",
    "framework_hint": "{React|Vue|Angular|Vanilla|Unknown}",
    "discovered_at": "{ISO 8601 timestamp}",
    "discovery_source": "perplexity_deep_research_{event_id}"
  },
  "global": {
    "completion_indicators": [
      {
        "pattern_type": "axdescription_prefix|axdescription_suffix|role_state|css_class|progressbar_value|other",
        "pattern_value": "<observed pattern>",
        "applies_to": "<screen_master_category|any>",
        "provenance": { "source": "discovery", "event_id": "{event_id}", "timestamp": "{iso}", "validated_step2": false, "validated_step2_at": null, "notes": "<source URL or research-confidence note>" }
      }
    ],
    "advancement_link_patterns": [
      {
        "label_pattern": "<exact text>",
        "role": "AXLink|AXButton|other",
        "context": "post_video|post_article|post_exercise|nav|any",
        "provenance": { ... }
      }
    ],
    "video_completion_signal": {
      "tree_change_marker": "<description>",
      "concrete_indicator": "<observable element>",
      "provenance": { ... }
    } | null,
    "timing_characteristics": {
      "spa_transition_ms": <int|null>,
      "modal_render_ms": <int|null>,
      "video_iframe_poll_ms": <int|null>,
      "provenance": { ... }
    } | null
  },
  "screen_patterns": {
    "NAVIGATION": { ... } | null,
    "VIDEO": { ... } | null,
    "ARTICLE": { ... } | null,
    "EXERCISE": { ... } | null,
    "TRANSITION": { ... } | null
  },
  "never_clicks_platform": [
    {
      "label": "<exact text>",
      "role": "<AX role>",
      "reason": "discovered_destructive|discovered_off_topic|discovered_traps_to_other_section",
      "provenance": { ... }
    }
  ],
  "widget_classes": {},
  "cached_bts": {},
  "_meta": {
    "created_at": "{ISO}",
    "last_updated_at": "{ISO}",
    "discovery_event_id": "{event_id}",
    "schema_version": "v2",
    "validating_consults_total": 0,
    "discovery_consults_total": 1,
    "recovery_consults_total": 0,
    "user_assist_events_total": 0
  },
  "research_confidence": {
    "overall": "high|medium|low",
    "sources_count": <int>,
    "notes": "<brief summary of confidence>",
    "unknown_fields": [
      { "field_path": "global.timing_characteristics.modal_render_ms", "reason": "Not observable from public docs; will need empirical validation" }
    ]
  }
}
```

**Key shape rules**:
- `widget_classes` and `cached_bts` are populated empty `{}`. They get populated by the Failure-Recovery Loop and Self-Learning Mechanism — NOT by you.
- Every populated entry under `global`, `screen_patterns`, `never_clicks_platform` carries a `provenance` block with `source: "discovery"`, `validated_step2: false`, and the discovery event ID.
- All values you cannot confidently determine: set to `null`. Document in `research_confidence.unknown_fields`.

### CRITICAL RULES

1. **One JSON object only.** No multiple blocks. The trailing object is the one extracted.
2. **Provenance is required on every populated value.** No exceptions. Empty/null values do not need provenance.
3. **Exact text matters.** Button text "Submit Quiz" differs from "Submit" in AX-tree exact matching. Preserve case, punctuation, surrounding whitespace as observable.
4. **Never invent values to fill schema fields.** Null + acknowledged-unknown beats confident-incorrect.
5. **Investigate the accessibility tree specifically.** AX tree behavior often differs from DOM rendering. Where ARIA semantics conflict with visual presentation, the AX tree is what Taey-Ed sees.
6. **Cite sources where possible.** In `research_confidence.notes`, briefly mention how you verified key findings (platform documentation, accessibility audits, screen-reader testing, etc.).

---

## USAGE (for Spark, not sent to Perplexity)

The prompt above is substituted with platform parameters by `spark_v2/discovery/research_request.py`. The complete substituted prompt is sent to Perplexity in Deep Research mode via Taeys-Hands MCP.

Returned response is:
1. Parsed by `spark_v2/discovery/research_parser.py` — extracts the trailing JSON object
2. Validated against `KNOWLEDGE_JSON_v2_SCHEMA.md` structural rules
3. Stored to `spark_v2/platforms/{platform}/provisional_knowledge.json`
4. JIT-injected into the next consultation prompt for that platform
5. Promoted to `knowledge.json` only after Spark Step 2 empirically validates a successful action that consumed the provisional value

**Provisional values are never trusted blindly.** Even at Perplexity's "high" research confidence, the empirical Step 2 gate is the determinant of whether a value lands in permanent knowledge.

---

*Source: existing `spark/platforms/DEEP_RESEARCH_PROMPT.md` (structure reused). Updates: Gemini → Claude Opus 4.7; schema v1 → v2; output now provisional; explicit unknown-fields discipline; provenance required per value. Cosmos §3 (quarantine). UNIVERSAL_LAYER_v1.md §5 (Discovery Loop).*
