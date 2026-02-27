# V19: Structural Pre-Classification Fix

## Problem

EXERCISE screens were matching as TRANSITION at Jaccard scores of 0.91+, causing exercises to be skipped entirely. The system would click "Next" instead of solving questions.

### Root Cause (Three-Level Problem)

Screen signature matching uses Jaccard similarity on "discriminative markers" — elements left after subtracting the common elements shared by ALL screens. The problem is that this only removes elements in the intersection of ALL signatures:

- **ALL**: Elements on every screen (browser chrome) — correctly removed
- **SOME**: Elements on most screens but not all (e.g., "Go to next item" button on 80% of screens) — **NOT removed**, inflates Jaccard
- **UNIQUE**: Actual differentiators (radio buttons, checkboxes) — drowned out by SOME-level noise

On Coursera, EXERCISE and TRANSITION screens share ~95% of their UI elements. After removing the ~252 elements shared by ALL screens, only ~40-50 markers remain. Most of those 40-50 are shared between EXERCISE and TRANSITION. The one `("AXRadioButton", "*")` entry that distinguishes them is 1 out of 50 = 2% weight in Jaccard — not enough to bring the score below the 0.70 threshold.

### Why V18 (Structural Penalty) Was Insufficient

The V18 fix added a 0.35 penalty to Jaccard scores when structural roles disagreed. This had problems:

1. The penalty value (0.35) was arbitrary and fragile
2. It didn't handle screens where both or neither had structural elements
3. It was a post-hoc adjustment to scores rather than addressing the root cause
4. As Gemini Deep Think noted: "a band-aid on a flawed model"

## Solution: Category-Constrained Matching (V19)

Instead of penalizing bad matches after Jaccard scoring, **prevent them from being compared in the first place**.

### How It Works

```
1. Incoming tree arrives at match_screen()
2. structural_classify(tree) runs FIRST:
   - Uses analyze_tree() from prompt_codex.py (already exists, battle-tested)
   - Returns: VIDEO, EXERCISE, NAVIGATION, TRANSITION, or UNCLASSIFIED
3. Category is passed to match_signature() as category_filter
4. Jaccard matching SKIPS all signatures that don't match the category
5. EXERCISE tree can only match against EXERCISE signatures
6. TRANSITION tree can only match against TRANSITION signatures
7. False positive between categories is impossible by construction
```

### Structural Classification Rules

| Structural Signal | Master Category | Priority |
|---|---|---|
| HAS_VIDEO (video player detected) | VIDEO | 1 (highest) |
| HAS_RADIO (3+ radio buttons) | EXERCISE | 2 |
| HAS_CHECKBOX (3+ checkboxes) | EXERCISE | 2 |
| HAS_TEXT_INPUT (text area/field) | EXERCISE | 2 |
| HAS_COMBOBOX (dropdown elements) | EXERCISE | 2 |
| HAS_MANY_LINKS (15+ links) | NAVIGATION | 3 |
| TRANSITION (buttons, no assessment) | TRANSITION | 4 |
| No definitive signal | UNCLASSIFIED | 5 |

UNCLASSIFIED screens match against all signatures (no filter), then fall through to Gemini classification. This is correct — ARTICLE screens often lack distinctive structural elements and need Gemini's visual analysis.

### Bonus: Skips Gemini Classification When Possible

When structural_classify() returns a definitive category and no signature match is found, Step 5 in next_action.py now uses the structural category directly instead of calling Gemini's classify_screen(). This saves one API call per new screen with clear structural signals.

## Files Changed

| File | Change | Lines |
|---|---|---|
| `spark/tasks/screen_signatures.py` | Added `structural_classify()`, removed V18 penalty code, added `category_filter` param to `match_signature()` | ~50 lines added, ~30 removed |
| `spark/tasks/match_screen.py` | Calls `structural_classify()`, passes category to `match_signature()`, includes category in return dict | ~20 lines changed |
| `spark/routes/next_action.py` | Step 4 logs structural category, Step 5 uses structural category to skip Gemini classification when possible | ~25 lines changed |
| `tests/test_structural_classify.py` | 10 unit tests covering all categories and the false positive scenario | New file, 253 lines |

## What Was NOT Changed

- **prompt_codex.py**: `analyze_tree()` already extracts the structural signals. No changes needed.
- **classify_screen.py**: Still used when structural_classify returns UNCLASSIFIED. No changes needed.
- **screen_type_util.py**: `get_master_category()` already handles category extraction from screen type strings. No changes needed.
- **Signature storage format**: No schema changes to the JSON files.
- **BT building**: No changes to how behavior trees are constructed.

## Test Results

```
V19 Structural Pre-Classification Tests
========================================

PASS: test_exercise_radio
PASS: test_exercise_checkbox
PASS: test_exercise_text_input
PASS: test_transition_no_structural
PASS: test_video_screen
PASS: test_navigation_many_links
PASS: test_unclassified_article (got TRANSITION)
PASS: test_false_positive_prevention
PASS: test_video_priority_over_exercise
PASS: test_post_answer_transition

Critical test: Raw Jaccard between EXERCISE and TRANSITION = 0.77
(above 0.70 threshold → would have been a false positive in V18).
V19 prevents this: category_filter=EXERCISE blocks TRANSITION matches.
```

## Relationship to Gemini/Claude Proposals

Both Gemini Deep Think and Spark Claude proposed replacing Jaccard matching entirely with deterministic structural rules. This solution takes the best parts of that idea while preserving the signature learning system that REQUIREMENTS.md specifies:

- **From proposals**: Use structural features as the primary classification signal
- **Preserved from current system**: Signature matching for within-category recognition, stored BT reuse for VIDEO/ARTICLE
- **Key insight**: Don't replace Jaccard — constrain it. Structural features determine the category, Jaccard handles fine-grained matching within categories.
