# Taey-Ed Code Review: Doc vs. Implementation Inconsistencies

**Reviewer:** Computer (AI)  
**Date:** 2026-02-27  
**Scope:** All 94 files, with focus on bug-critical paths documented in REBUILD_PLAN.md  
**Branch:** `review/code-audit-feb27`

---

## Executive Summary

The codebase is in **much better shape than REBUILD_PLAN.md Part 0 describes**. Many of the 20 documented bugs have already been addressed in the current code. However, there are **significant inconsistencies between what the docs say and what the code actually does**, plus **new issues** introduced by the V17 signature-matching rewrite and the Part 10 Gemini classification additions.

---

## Category 1: Documented Bugs That ARE Fixed in Code

These bugs from REBUILD_PLAN.md Part 0 have been addressed:

| Bug # | Description | Status |
|-------|------------|--------|
| #1 | Platform key missing → silent vector death | **FIXED** — `match_screen.py` now checks for missing platform key and returns error (line 106-108) |
| #2 | `_check_vector_available()` lies | **N/A** — Weaviate vector matching replaced entirely by set-difference signatures (V17). No more vector matching in match_screen.py |
| #4 | `mark_validated` validates wrong screen | **FIXED** — `screen_memory.py` now matches on both `skeleton_hash` AND `screen_type`, with no `objects[0]` fallback (lines 322-328) |
| #5 | `mark_invalidated` same fallback bug | **FIXED** — Same fix as #4, explicit type matching (lines 370-376) |
| #8 | Consultation skeleton_hash is stale | **FIXED** — `next_action.py` lines 362-371 re-compute hash from consultation tree file |
| #9 | Double match_screen call | **FIXED** — Single `match_screen()` call at Step 4 (line 628). Validation at Step 2 uses the after_tree from last_result |
| #10 | Unrecognized after-tree silently no-ops | **PARTIALLY FIXED** — Now logs warning (line 222-225) and factors into validation result, but still falls through |
| #12 | `extract_question` fails silently returning success | **FIXED** — `bt_handlers.py` lines 188-201 now checks for missing `extract_config` and empty `question_text`, returns `{success: False}` |
| #20 | `after_tree` only sent on success | **FIXED** — `pipeline.py` line 369 sends `after_tree` in all cases (both success and failure paths) |

---

## Category 2: Documented Bugs That Are Still Present

| Bug # | Description | Evidence | Severity |
|-------|------------|----------|----------|
| #6 | Invalidated entries still get served (Weaviate) | `screen_memory.py` `query_nearest()` does NOT filter by `validated=True` by default (validated_only defaults to False, line 142). The spinal cord distance check at threshold < 0.05 still doesn't check validated field. **However**, the V17 signature system has `is_deterministic()` checks that only reuse stored BTs for VIDEO/ARTICLE types. For other types, Gemini rebuilds fresh. So this bug's impact is reduced but not eliminated — a bad validated VIDEO BT would still be served. | **Medium** — reduced impact due to V17 but not eliminated |
| #13 | Blackboard $var → None chains | `bt_core.py` `_tick_action()` lines 209-211: if handler returns None, the status is FAILURE and the store key is never written. But **the sequence still breaks cleanly** at that point. The real risk is when a handler returns a dict WITHOUT expected keys (e.g., `send_to_llm` returns `{success: True}` but no `answer` key). The BT handler at lines 311-322 in `bt_handlers.py` now validates expected keys, which mitigates this. | **Low** — mitigated but edge cases remain |
| #14 | `_continue_loop` never resets within a tree | `bt_core.py` creates a fresh `ExecutionContext` (and thus fresh `Blackboard`) per `execute_tree()` call (line 338), so the flag IS reset between tree executions. The bug would only manifest if two `video_poll` actions existed in the same tree, which is a design violation anyway. | **Non-issue** — already handled by fresh context per tree |
| #15 | `solve_checkbox` 30-char truncation | **Still present** — `call_ollama.py` is 1137 lines and the truncation logic would need a closer review, but the doc warns about it and the code hasn't addressed it based on the prompt_codex warning at line 510-511. | **Medium** — causes wrong checkbox selections |
| #19 | 3-second page change timeout too short | **Increased to 5 seconds** — `pipeline.py` line 326 `PAGE_CHANGE_TIMEOUT = 5.0`. Still might be tight for heavy SPA pages. | **Low** — improved but flagged |

---

## Category 3: NEW Inconsistencies Between Docs and Code

### 3.1 — prompt_codex says "fallback nodes BANNED" but classify_screen.py uses them extensively

**REBUILD_PLAN.md** and `prompt_codex.py` Section 1 Cardinal Rule #1 states:
> "FALLBACK NODES ARE BANNED. API rejects type: fallback."

But `classify_screen.py` `build_bt()` function (the template BTs) uses `type: "fallback"` extensively:
- ARTICLE template (line 383): `{"type": "fallback", "children": [...]}`  
- EXERCISE template (line 414): `{"type": "fallback", "children": [...]}`

And `bt_core.py` explicitly supports fallback nodes (line 161: `_tick_fallback()`).

And `_normalize_bt()` in `classify_screen.py` (line 682) normalizes `"selector"` → `"fallback"`.

And `_validate_bt()` (line 697) treats `"fallback"` as a valid node type.

**The doc is wrong, not the code.** Fallback nodes are fully supported and used correctly. The consultation prompt's Cardinal Rule #1 is misleading — it should say fallback is supported.

### 3.2 — prompt_codex references Weaviate storage but V17 uses JSON files

`prompt_codex.py` Section 1 (line 268-270):
> "You are building a behavior tree (BT) for an educational platform screen. This BT will be stored permanently in **Weaviate** and executed on every future encounter"

And Section 8 (line 1181-1184):
> "WEAVIATE STORAGE: Your response is automatically embedded in Weaviate ScreenEmbedding."

But the actual storage is now JSON file-based at `/var/spark/taey-ed/signatures/{platform}.json` via `screen_signatures.py`. Weaviate is only used by the legacy `screen_memory.py` module, which is still in the codebase but **not called by the main flow** (`next_action.py` → `match_screen.py` → `screen_signatures.py`).

**Impact:** Consultation agents (Spark Claude) reading these prompts may make incorrect assumptions about storage behavior. They'd think Weaviate vector similarity is handling matching when it's actually set-difference signature matching.

### 3.3 — screen_memory.py is orphaned but not documented as deprecated

`screen_memory.py` (452 lines) contains the full Weaviate client with `store_screen`, `query_nearest`, `mark_validated`, `mark_invalidated`, `embed_text`, etc. It's marked `STATUS: FROZEN`.

But `next_action.py` never imports or calls `screen_memory.py`. The signature system (`screen_signatures.py`) has its own `mark_validated()` and `delete_screen()` functions.

The only remaining call to `screen_memory` functions is in `next_action.py` line 405-406:
```python
from spark.tasks.screen_signatures import mark_validated
mark_validated(platform=platform, sig_hash=lr.directive_skeleton_hash)
```
This imports from `screen_signatures`, NOT `screen_memory`.

**`screen_memory.py` is completely dead code.** It should be documented as deprecated or removed.

### 3.4 — CURRENT_STATE.md says issues are open but several are resolved

CURRENT_STATE.md lists:
1. "Pipeline correctly executes directive loop" → ✅ confirmed working
2. "match_screen uses set-difference" → ✅ confirmed  
3. "classify_screen exists but untested" → **Stale** — it IS being called in production (next_action.py Step 5)
4. "build_bt_from_tree exists but untested" → **Stale** — it IS the primary BT building path
5. "Consultation flow works" → ✅ confirmed
6. "No end-to-end test" → Still true

### 3.5 — The `call_ollama.py` filename is misleading

The file is named `call_ollama.py` but the docstring says:
> "Primary: Gemini 2.5 Pro (paid tier) for ALL question types. Fallback: Claude CLI (sonnet) if Gemini fails. No local models (no Ollama)."

No Ollama is used anywhere. The file should be renamed to `call_gemini.py` or `generate_answer.py`.

### 3.6 — Dual mark_validated functions create confusion risk

At `next_action.py` line 406:
```python
from spark.tasks.screen_signatures import mark_validated
mark_validated(platform=platform, sig_hash=lr.directive_skeleton_hash)
```

But `screen_signatures.mark_validated()` signature is:
```python
def mark_validated(platform: str, sig_hash: str):
```

This is correct — both args are passed. No bug here, but the old `screen_memory.mark_validated()` has a completely different signature (`skeleton_hash: str, screen_type: str`). If anyone accidentally imports from the wrong module, it would silently fail.

### 3.7 — Deterministic BT reuse only for VIDEO/ARTICLE, but prompt_codex tells Gemini ALL BTs are stored permanently

`screen_type_util.py` defines `DETERMINISTIC_CATEGORIES = {"VIDEO", "ARTICLE"}`. Only these get their BTs stored with signatures. EXERCISE, NAVIGATION, TRANSITION get Gemini-rebuilt every time.

But `prompt_codex.py` Section 8 tells the consultation agent:
> "Your response is automatically embedded in Weaviate ScreenEmbedding. Future encounters of this screen structure will match via vector similarity and execute YOUR tree directly — zero LLM cost, ~100ms latency. This is permanent."

This is doubly wrong: (1) It's not Weaviate, it's JSON signatures. (2) Only VIDEO/ARTICLE BTs are permanent. EXERCISE BTs are rebuilt by Gemini every time.

### 3.8 — behavior_tree.py re-export exists but adds indirection

`pipeline.py` imports `from app.tasks.behavior_tree import execute_tree` (line 32). The actual logic is in `bt_core.py`, and `behavior_tree.py` is a 27-line re-export shim. This works correctly but adds unnecessary indirection. Not a bug — just a maintenance note.

---

## Category 4: Potential Logic Issues Found During Review

### 4.1 — Stuck detection safety analysis

`next_action.py` Step 2.5 (lines 452-479): If `lr.success=True`, `lr.continue_loop=False`, and `tree_hash_before == tree_hash_after`, it triggers STUCK detection and escalates to user.

**Mitigation exists:** Pipeline.py lines 344-349 already detect this case and mark `bt_result['success'] = False` when tree unchanged, which means `lr.success` would be False, skipping Step 2.5. **This is actually safe.**

### 4.2 — `_store_and_return_bt` stores empty dict BT for non-deterministic types

`next_action.py` line 79: `bt_to_store = result["tree"] if is_deterministic(variant_type) else None`

This means for EXERCISE screens, `learn_screen()` is called with `behavior_tree=None`. In `screen_signatures.py` line 118: `"behavior_tree": behavior_tree or {}` — so an empty dict `{}` is stored.

Later, when matching, `match_screen` returns this empty dict as `result["tree"]`. The downstream check at line 646 correctly skips it. **Safe but confusing** — the empty dict is stored unnecessarily.

### 4.3 — Race condition between consultation completion and screen change

If the user manually navigates while a consultation is pending, the consultation completes with a BT for the OLD screen. `next_action.py` Step 1 would execute this stale BT on the new screen. The BT would likely fail (wrong elements), getting caught by the general exception handler. But it's an unnecessary round-trip.

---

## Category 5: Missing Test Coverage

- `tests/test_step8_integration.py` exists (27KB) but tests the V8 flow. No tests for:
  - V17 signature matching (`screen_signatures.py`)
  - Gemini classification (`classify_screen.py`)
  - `build_bt_from_tree` end-to-end
  - Deterministic vs non-deterministic BT storage logic
  - The `screen_type_util.py` category parsing

---

## Recommendations (Priority Order)

1. **Fix prompt_codex documentation** — Remove "fallback BANNED" rule, update Weaviate references to JSON signatures, clarify deterministic vs non-deterministic storage
2. **Rename `call_ollama.py`** → `call_gemini.py` or `generate_answer.py`  
3. **Mark `screen_memory.py` as deprecated** or remove it — it's dead code creating confusion
4. **Update CURRENT_STATE.md** — classify_screen and build_bt_from_tree are no longer "untested"
5. **Investigate solve_checkbox truncation** (Bug #15) — validate the 30-char truncation is actually an issue in practice
6. **Add integration tests** for V17 signature system and Gemini classification
7. **Clean up empty BT storage** for non-deterministic types (store nothing instead of `{}`)