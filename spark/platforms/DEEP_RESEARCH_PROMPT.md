# Deep Research Prompt: Platform Knowledge Generation
# Used by: Perplexity (Deep Research mode)
# Triggered by: Jesse or Spark when encountering a new platform
# Output: knowledge.json for spark/platforms/{platform}/

---

## PROMPT (copy everything below this line)

---

You are generating structured platform knowledge for **Taey-Ed**, an educational platform automation system. Taey-Ed uses a Mac app to navigate LMS (Learning Management System) platforms via accessibility APIs (AXButton, AXLink, AXRadioButton, etc.) and Gemini 2.5 Pro to build behavior trees that complete coursework.

**YOUR DELIVERABLE IS A SINGLE JSON OBJECT. NOT A REPORT.**

Your output will be machine-consumed — it feeds directly into Gemini's prompt context during BT (behavior tree) construction. Every field matters. Vague or missing information causes BT failures that cost real time and API credits.

### PLATFORM TO RESEARCH

**Platform name**: {PLATFORM_NAME}
**Platform URL**: {PLATFORM_URL}
**Platform type**: browser | native_app
**Known context** (if any): {ANY_CONTEXT_JESSE_PROVIDES}

### YOUR TASK

Research this platform and output **ONE `knowledge.json` object**. Use the exact schema below. Fill every field. If information is unavailable or uncertain, say so explicitly in the field value — never leave fields empty or omit them silently.

### WHAT TO RESEARCH (use these as investigation areas, NOT as output sections)

1. **Platform Architecture** — SPA? Framework? Client-side routing? Content hierarchy?
2. **Screen Types & Content** — For each content type: what elements are present, what buttons exist, how is completion tracked?
3. **Navigation Model** — How users move between content items, sidebar/breadcrumb, completion indicators
4. **Interactive Elements** — Assessment types, submit button names, correct/incorrect feedback, retry mechanisms
5. **Timing & Behavior** — Page transition speed, loading states, modal animations, rate limiting, bot detection
6. **Buttons & Safety** — For every button: safe/caution/never, what it does, could it cause harm?
7. **Edge Cases & Quirks** — Unusual behavior, known bugs, free vs paid differences, platform-specific terminology

### OUTPUT FORMAT

**Return ONLY a single JSON object.** Do not write a report, narrative, or analysis. Do not include multiple JSON blocks. One JSON object with a brief `research_confidence.notes` field for any important caveats.

If Deep Research mode requires you to produce a report before the JSON, keep the report as brief as possible (a few bullet points at most) and place the complete JSON at the very end as the final output. The JSON is the only thing that will be extracted — the report will be discarded.

**Do NOT wrap the JSON in markdown code fences** — this is consumed by Python `json.loads()`.

```json
{
  "$schema": "taey-ed-platform-knowledge-v1",
  "platform": "{platform_key}",
  "platform_display": "{Platform Display Name}",
  "schema_version": "1.0",
  "last_researched": "{ISO 8601 timestamp}",
  "research_source": "perplexity_deep_research",
  "research_confidence": {
    "overall": "high|medium|low",
    "notes": "Any caveats about research completeness"
  },

  "global": {
    "platform_type": "browser|native_app",
    "platform_url": "{base URL}",
    "spa_framework": "{react|angular|vue|vanilla|unknown}",
    "default_click_strategy": "mouse_click|ax_press",
    "content_hierarchy": "{e.g., Course > Module > Lesson > Activity}",
    "timing": {
      "page_change": 0.0,
      "submit": 0.0,
      "portal_render": 0.0,
      "spa_navigation": 0.0,
      "modal_transition": 0.0,
      "notes": "Explain timing estimates"
    },
    "never_click": [
      {
        "text": "{button text}",
        "reason": "{why it's dangerous}"
      }
    ],
    "platform_quirks": [
      {
        "id": "{short_snake_case_id}",
        "description": "{Detailed description of the quirk}",
        "affects": ["{SCREEN_TYPE_1}", "{SCREEN_TYPE_2}"],
        "severity": "critical|important|minor"
      }
    ],
    "completion_model": {
      "{content_type}": "{How completion works for this content type}"
    },
    "bot_detection": {
      "known_measures": "{Description of any bot detection}",
      "recommended_delays": "{Any extra timing needed}"
    }
  },

  "screen_types": {
    "EXERCISE": {
      "description": "{What exercise screens look like on this platform}",
      "exists_on_platform": true,
      "tree_signals": ["{HAS_RADIO}", "{HAS_CHECKBOX}", "etc."],
      "subtypes": [
        {
          "name": "{e.g., multiple_choice}",
          "description": "{How it works}",
          "tree_signal": "{e.g., HAS_RADIO}",
          "interaction_pattern": "{e.g., click radio → click Submit}"
        }
      ],
      "handlers_needed": {
        "always": ["{handler1}", "{handler2}"],
        "conditional": {
          "{SIGNAL}": ["{handler}"]
        }
      },
      "question_types": {
        "{SIGNAL}": "{solve_choice|solve_checkbox|solve|solve_matching|solve_complex}"
      },
      "submit_button": {
        "text": "{exact button text}",
        "role": "{AXButton|AXLink}",
        "strategy": "mouse_click",
        "post_delay": 0.0,
        "notes": "{any variants or context}"
      },
      "extraction": {
        "text": [{"role": "AXStaticText", "parent_contains": "{container name if known}"}],
        "images": [{"source": "window", "purpose": "{what to describe}"}]
      },
      "expected_next": ["{SCREEN_TYPE}"],
      "wrong_answer_behavior": "{What happens when answer is wrong}"
    },

    "VIDEO": {
      "description": "{What video screens look like}",
      "exists_on_platform": true,
      "tree_signals": ["HAS_VIDEO"],
      "player_type": "{embedded YouTube|native|Vimeo|custom}",
      "handlers_needed": {
        "always": ["{handlers}"],
        "conditional": {}
      },
      "states": {
        "UNSTARTED": {
          "tree_signal": "{what to look for}",
          "action": "{what to do}",
          "expected_next": ["VIDEO"]
        },
        "PLAYING": {
          "tree_signal": "{what to look for}",
          "action": "video_poll ONLY",
          "expected_next": ["VIDEO"]
        },
        "COMPLETE": {
          "tree_signal": "{what to look for}",
          "action": "{what to do — click Next? Press Escape? Auto-advance?}",
          "expected_next": ["{SCREEN_TYPES}"]
        }
      },
      "extraction": {
        "text": [{"role": "AXStaticText", "parent_contains": "{transcript container if exists}"}],
        "images": [{"source": "window", "purpose": "Describe the video content being shown"}]
      },
      "speed_controls": "{available speeds, does 2x affect completion?}"
    },

    "ARTICLE": {
      "description": "{What article/reading screens look like}",
      "exists_on_platform": true,
      "tree_signals": [],
      "handlers_needed": {
        "always": ["{handlers}"],
        "conditional": {}
      },
      "completion_mechanism": "{scroll + mark complete | just open | read time threshold}",
      "completion_button": {
        "text": "{button text if any}",
        "role": "{role}",
        "notes": "{e.g., 'Mark as complete' appears after scrolling to bottom}"
      },
      "extraction": {
        "text": [{"role": "AXStaticText", "parent_contains": "{content container}"}],
        "images": [{"source": "window", "purpose": "Describe the article content"}]
      },
      "expected_next": ["{SCREEN_TYPES}"]
    },

    "NAVIGATION": {
      "description": "{What navigation/content list screens look like}",
      "exists_on_platform": true,
      "tree_signals": ["HAS_LINKS"],
      "handlers_needed": {
        "always": ["find_all", "send_to_llm", "find_and_click"]
      },
      "question_types": {"always": "navigate"},
      "extraction": null,
      "completion_indicators": {
        "done": ["{text/visual indicators for completed items}"],
        "not_done": ["{text/visual indicators for incomplete items}"],
        "in_progress": ["{text/visual indicators for in-progress items}"]
      },
      "navigation_model": "{sidebar|breadcrumb|grid|list}",
      "adaptive_routing_warning": "{Does the platform have auto-recommend that skips content? Details.}",
      "expected_next": ["VIDEO", "ARTICLE", "EXERCISE", "TRANSITION", "NAVIGATION"]
    },

    "TRANSITION": {
      "description": "{What transition/interstitial screens look like}",
      "exists_on_platform": true,
      "tree_signals": ["HAS_BUTTONS"],
      "handlers_needed": {
        "always": ["find_and_click"]
      },
      "extraction": null,
      "common_advance_buttons": ["{button texts}"],
      "expected_next": ["{SCREEN_TYPES}"]
    },

    "UNKNOWN": {
      "description": "Fallback for unrecognized screens",
      "exists_on_platform": true,
      "tree_signals": [],
      "handlers_needed": {
        "always": ["find_and_click", "scroll", "wait"]
      },
      "extraction": {
        "text": [{"role": "AXStaticText", "min_length": 40}],
        "images": [{"source": "window"}]
      },
      "expected_next": []
    }
  },

  "accessibility_tree_guide": {
    "focus_area": "AXWebArea subtree (skip AXMenuBar, AXToolbar, AXTabGroup)",
    "key_signals": {
      "{AXRole}": "{What it means on this platform}"
    },
    "completion_indicators_in_tree": {
      "{indicator_type}": "{How it appears in the accessibility tree}"
    },
    "platform_specific_roles": [
      {
        "role": "{AXRole}",
        "context": "{When/where this appears}",
        "meaning": "{What it indicates}"
      }
    ]
  },

  "button_reference": [
    {
      "text": "{exact button text}",
      "location": "{where it appears}",
      "safe": "yes|caution|never",
      "behavior": "{what clicking it does}",
      "screen_types": ["{where it appears}"]
    }
  ]
}
```

### CRITICAL RULES

1. **ONE JSON OBJECT ONLY.** Do not split into multiple JSON blocks.
2. **Every screen type must be present** even if `exists_on_platform` is false. Set false and leave other fields minimal.
3. **Button text must be exact** — "Submit Quiz" is different from "Submit" in accessibility tree matching.
4. **Container names matter** — if you can determine the CSS class or ARIA label of content containers, include them in `parent_contains` fields.
5. **Timing estimates should be conservative** — better to wait 3s than fail at 1s.
6. **If the platform has content types not covered by the 6 screen categories**, describe them in `platform_quirks`.

---

## USAGE INSTRUCTIONS (for Spark Claude, not sent to Perplexity)

To use this prompt:
1. Replace `{PLATFORM_NAME}`, `{PLATFORM_URL}`, and `{ANY_CONTEXT_JESSE_PROVIDES}`
2. Send to Perplexity in Deep Research mode
3. Deep Research may force a report alongside the JSON — **ignore the report**
4. Extract ONLY the JSON block from the response (look for `{"$schema": "taey-ed-platform-knowledge-v1"`)
5. Save to `spark/platforms/{platform_key}/knowledge.json`
6. Create empty `learned/` directory: `spark/platforms/{platform_key}/learned/`
7. Restart the API so the knowledge gate passes

### EXTRACTION NOTES

Perplexity Deep Research mode may generate a narrative report before or around the JSON.
- Use the **Download** button (not Copy — Copy returns summary only)
- The downloaded markdown will contain the JSON embedded in the report
- Search for `"$schema": "taey-ed-platform-knowledge-v1"` to find the JSON start
- Extract from `{` to the matching closing `}` — that's your knowledge.json
- If Perplexity split the JSON into multiple blocks, concatenate them (this is a known issue)
- Validate with `python3 -c "import json; json.load(open('knowledge.json'))"` before deploying
