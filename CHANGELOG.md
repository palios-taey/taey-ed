# CHANGELOG — review/code-audit-feb27 branch

**Date**: February 27, 2026
**Author**: Computer (AI code review)
**Base**: main @ commit 01eb38e1
**Purpose**: Fix doc-code inconsistencies and critical signature matching bug

---

## Changes

### 1. CRITICAL: Signature matching structural hard constraints (screen_signatures.py)

**Problem**: EXERCISE screens were matching as TRANSITION at 0.91+ Jaccard scores,
causing the system to skip exercises entirely. Root cause: common element subtraction
only removes elements shared by ALL screen types, not pairs. After removing ~252
common elements, EXERCISE and TRANSITION still share ~95% of the remaining ~40-50
markers. The only real difference — structural roles like AXRadioButton, AXCheckBox —
was just one `("AXRadioButton", "*")` tuple among many.

**Fix**: Added structural hard constraints. New functions `_extract_structural_roles()`
and `_structural_mismatch()` check if the query tree has exercise-like structural
elements (radio buttons, checkboxes, text fields) that the known signature does NOT
have, or vice versa. When a mismatch is detected, a 0.35 penalty is applied to the
Jaccard score, dropping a 0.91 match to 0.56 — well below the 0.70 threshold.

**New constants**: `HARD_DIFFERENTIATOR_ROLES`, `STRUCTURAL_MISMATCH_PENALTY`

**Testing needed**: Run against Coursera with existing signatures. Verify:
- EXERCISE screens (with radio/checkbox) no longer match TRANSITION signatures
- TRANSITION screens no longer match EXERCISE signatures
- Same-type matching (EXERCISE → EXERCISE) is unaffected
- VIDEO and ARTICLE matching is unaffected (no structural roles)

### 2. prompt_codex.py — Fixed 5 incorrect documentation claims

**Changes**:
- **Cardinal Rule #1**: Changed from "FALLBACK NODES ARE BANNED" to "FALLBACK NODES ARE
  SUPPORTED" with guidance on usage. The code (classify_screen.py, bt_core.py) has
  always supported fallback nodes.
- **Role description**: Changed from "stored permanently in Weaviate" to accurate
  description of JSON signature storage with deterministic vs dynamic distinction.
- **Cardinal Rule #9**: Changed from "stored in Weaviate automatically" to accurate
  description of deterministic vs dynamic storage behavior.
- **Anti-patterns**: Changed "NEVER create fallback mechanisms" to "Use fallback nodes
  for OPTIONAL steps only". Changed "vector distance handles routing" to "signature
  matching handles routing".
- **Section 8 storage docs**: Changed from "WEAVIATE STORAGE" to "SIGNATURE STORAGE"
  with accurate description of JSON file storage at
  `/var/spark/taey-ed/signatures/{platform}.json`.

### 3. Renamed call_ollama.py → call_gemini.py

**Problem**: File was named `call_ollama.py` but uses zero Ollama models. All
generation is Gemini 2.5 Pro with Claude CLI fallback. Misleading filename.

**Fix**:
- Created `call_gemini.py` with updated docstring
- Converted `call_ollama.py` to a backward-compatible re-export shim
- Updated import in `spark/routes/compute.py`
- Updated test in `tests/test_step3.py`

### 4. Deprecated screen_memory.py

**Problem**: `screen_memory.py` (452 lines) is the old Weaviate-based screen memory
system. V17 replaced it with `screen_signatures.py` (JSON file-based). The main flow
(`next_action.py`) never imports from `screen_memory`. It's dead code creating
confusion about which module is active.

**Fix**: Added deprecation header with explanation. Did NOT delete the file because
`server.py` (ensure_schema at startup) and `validation.py` (embed_text) still import
from it. These are legacy calls that should be removed when Weaviate dependency is
fully cut.

### 5. Clean up empty BT storage for non-deterministic types (screen_signatures.py)

**Problem**: `learn_screen()` stored `behavior_tree or {}` which resulted in empty
`{}` dicts for non-deterministic types. Downstream code checked `.get("type")` on
the empty dict to detect this, but it was confusing.

**Fix**: Changed to `behavior_tree` (stores None instead of {}). All downstream
checks already handle None correctly since they check truthiness first.

---

## Files Changed

| File | Change |
|------|--------|
| `spark/tasks/screen_signatures.py` | Structural hard constraints, clean up empty BT storage |
| `spark/tasks/prompt_codex.py` | Fixed 5 incorrect doc claims |
| `spark/tasks/call_gemini.py` | NEW — renamed from call_ollama.py |
| `spark/tasks/call_ollama.py` | Converted to backward-compat re-export shim |
| `spark/tasks/screen_memory.py` | Added deprecation header |
| `spark/routes/compute.py` | Updated import call_ollama → call_gemini |
| `tests/test_step3.py` | Updated test for renamed module |

| `REVIEW_FINDINGS.md` | Initial review findings (from prior commit) |
| `CHANGELOG.md` | This file |

## How to Test

1. **Signature matching**: Clear Coursera signatures, run through a course that has
   both EXERCISE and TRANSITION screens. Verify exercises are solved, not skipped.
2. **Fallback nodes**: Verify ARTICLE and EXERCISE BT templates still execute correctly
   (they use fallback nodes which were always supported).
3. **Module rename**: `from spark.tasks.call_gemini import generate_answer` should work.
   Old `from spark.tasks.call_ollama import generate_answer` should also work via shim.
4. **Server startup**: `screen_memory.py` deprecation is header-only; `ensure_schema()`
   still works if Weaviate is running.
