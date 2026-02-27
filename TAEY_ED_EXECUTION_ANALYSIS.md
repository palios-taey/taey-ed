# Taey-Ed Codebase Analysis
## Complete Execution Flow & API Endpoints

**Date**: February 26, 2026
**Codebase Size**: ~2,200 Python files (built with py2app for macOS)
**Architecture**: Dumb executor model - Mac captures screen, Spark decides what to do

---

## EXECUTION FLOW: Button Click to Spark API Call

### Entry Point: UI Layer (`app/ui/window.py`)

When user clicks "Run One Screen" or "Run Continuous":

```
Button Click
    ↓
_on_run_one_screen() or _on_run_continuous()
    ├─ Spawns background thread
    ├─ Gets platform, app_name, course_id, platform_type from UI
    └─ Calls pipeline.run_one_screen() or pipeline.run_continuous()
```

### Pipeline Layer (`app/pipeline.py`)

The pipeline is a "directive loop" - it's the main execution engine:

1. **Capture Screen State**
   - `capture_tree(app_name)` → Returns accessibility tree (all UI elements)
   - `compute_tree_hash(tree)` → MD5 hash of tree for change detection

2. **Ask Spark What To Do**
   - Calls `call_spark("/next_action", payload)` ← MAIN API CALL
   - Payload includes: tree, screenshot, platform, course_id, session_id, last_result

3. **Receive Directive** (one of 6 types)
   - `execute_tree` → Behavior tree with instructions
   - `wait` → Sleep N seconds (consulting/page loading)
   - `need_screenshot` → Capture screenshot and send back
   - `consulting` → Consulting in progress, poll again
   - `user_input_needed` → Show chat panel, collect user text
   - `stop` → Pipeline done

4. **Execute Directive**
   - For `execute_tree`: Run behavior tree via `execute_tree()`
   - For others: Wait or poll

5. **Report Result**
   - Next iteration includes `last_result` with success/failure
   - If BT failed, includes `failed_bt` definition and `after_tree` for debugging

6. **Loop** (unless stop_event or max_screens reached)

---

## COMPLETE LIST OF SPARK API ENDPOINTS

The Mac app makes HTTP requests to Spark at port 5002 (configured in `app/config.py`).

### Primary Endpoints

| Endpoint | Method | Called From | Purpose |
|----------|--------|-------------|---------|
| **/next_action** | POST | `pipeline.py` (lines 102, 257) | **MAIN LOOP** - Send screen state, get directive |
| **/api/v1/extract_image** | POST | `handle_extraction.py` (lines 70, 103) | VLM image analysis for content extraction |
| **/api/v1/embed** | POST | `handle_extraction.py` (line 127) | Generate embeddings for extracted text |
| **/api/v1/generate** | POST | `bt_handlers.py` (lines 307, 407, 431) | LLM generation (prompts, questions, etc.) |
| **/chat/{platform}/history** | GET | `window.py` (line 492) | Load chat history on app startup |

### Default URL
```
http://10.0.0.68:5002  (can be overridden via ~/.taey-ed/config.json)
```

### Authentication
Optional X-API-Key header (if `api_key` configured in config):
```python
headers = {"X-API-Key": get_api_key()}  # from app/config.py
```

---

## EXECUTION FLOW DETAILED: Tree → Action → Result

### 1. Behavior Tree Execution (`app/tasks/bt_core.py` + `bt_handlers.py`)

When pipeline receives `execute_tree` directive:

```python
bt_result = execute_tree(
    tree_definition=tree_def,
    app_name=app_name,
    platform=platform,
    course_id=directive.get("course_id"),
    extract_config=directive.get("extract"),
)
```

The tree is a recursive structure of nodes:
- **sequence** - Run children in order, fail on first failure
- **fallback** - Run children in order, succeed on first success  
- **action** - Leaf node, calls registered handler
- **for_each** - Iterate list
- **conditional** - If/else on blackboard

### 2. Action Handlers Registered in `bt_handlers.py`

Each handler is a closure inside `register_all_handlers(ctx)`. Key handlers:

| Handler | Action | Error Handling |
|---------|--------|---------|
| **handle_click** | Click element by name/role or from blackboard | Returns None if element not found |
| **handle_find_and_click** | Find element with fallbacks, click | Logs error, returns None |
| **handle_find_and_type** | Find text field, type text | Returns None if field not found |
| **handle_find_all** | Find all matching elements | Returns enriched list |
| **handle_wait** | Sleep N seconds | Always succeeds |

### 3. Element Finding (`app/tasks/find_element.py`)

Finding uses macOS accessibility APIs:
```python
element = find_element(app_name, target_text, role=role, match_mode="exact")
```

Raises `RuntimeError` if app not found. If element not found, returns None (not exception).

### 4. Element Clicking (`app/tasks/click_element.py`)

Four click strategies:
- **ax_press** - AXUIElementPerformAction (native apps)
- **focus_space** - Focus + Space key (checkboxes/radio)
- **focus_enter** - Focus + Enter key (buttons/links)
- **mouse_click** - Move mouse + click (browser custom components) ← Most reliable

Raises `StaleElementError` if element gone (off-screen/overlay).

### 5. Page Change Detection (`pipeline.py` lines 321-350)

After successful action:
- Waits up to 5 seconds for page hash to change
- If hash same after 5s, reports action as failed
- Captures `after_tree` for Spark diagnostics

### 6. Result Reporting

```python
last_result = {
    "directive_id": directive_id,
    "success": bt_result.get("success"),
    "action": bt_result.get("action"),
    "screen": screen,
    "tree_hash_before": before_hash,
    "tree_hash_after": after_hash,
    "after_tree": _strip_tree_for_validation(after_tree),  # For Spark
    "failed_bt": tree_def,  # If failed
    "bt_debug_tail": last_20_lines_of_debug_log,
}
```

Next iteration sends this as `last_result` in payload.

---

## FAILURE POINTS: WHERE "NOT DOING ANYTHING" HAPPENS

### 1. **Config Not Found** (Silent)
```python
# In call_spark() at config.py
get_spark_url()  # Returns "http://10.0.0.68:5002" by default
get_api_key()    # Returns "" (empty) if not configured
```
**Problem**: If `api_key` is required by Spark but app sends empty → 401 Unauthorized, then what?
- `call_spark()` does `response.raise_for_status()` → raises exception
- Exception caught in pipeline at line 513 → logs error, sleeps 5s, continues
- **Result**: Silent retry loop, looks like "not doing anything"

### 2. **Spark Unreachable** (Timeout)
```python
TIMEOUT = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
```
- Connect timeout: 30 seconds
- If Spark at 10.0.0.68:5002 is down → waits 30s, then raises
- Exception caught at line 513 → sleeps 5s, retries
- **Result**: "Not doing anything" for 30s+

### 3. **Application Not Found**
```python
# In capture_tree() at capture_tree.py line 47
if not target_app:
    raise RuntimeError(f"Application '{app_name}' not found")
```
- Exception raised at line 513 → sleeps 5s, retries
- **Result**: Forever loop, never actually runs

### 4. **Tree Structure Mismatch**
```python
# In pipeline.py line 280
tree_def = directive.get("tree")
if not tree_def:
    logger.error(f"execute_tree directive has no tree")
    last_result = {..., "success": False, ...}
```
- Spark sends directive without tree
- Pipeline logs error, reports failure, continues
- Spark may retry or escalate
- **Result**: Looks like it ran but got nothing

### 5. **Element Not Found During Execution**
```python
# In bt_handlers.py line 56
if element is None:
    btlog(f"click: element not found")
    return None  # Handler returns None (not error)
```
- Handler returns None = failure
- Behavior tree sees failure → stops sequence
- BT result: `{"success": False, "action": "click failed"}`
- **Result**: Action fails, moves to next screen (or escalates)

### 6. **No Error Handling in Action Handler**
```python
# In bt_handlers.py handle_find_and_type (line 113)
def handle_find_and_type(ctx, params):
    target = params.get("target", "")
    text = params.get("text", "")
    # ...
    type_text(element, text)  # No try/except!
    time.sleep(0.5)
    return {"success": True}
```
- If `type_text()` crashes → exception propagates
- Caught in bt_core.py `_tick_action()` (line 160-165)
- Handler error logged, returns failure
- **Result**: Action fails, sequence stops

### 7. **Stale Element Error Loop**
```python
# In pipeline.py line 508-511
except StaleElementError as e:
    logger.warning(f"Stale element: {e} — recapturing")
    time.sleep(1.0)
    continue
```
- Element goes stale (off-screen, overlay)
- Caught, waits 1s, recaptures tree
- **Result**: Brief pause, continues (OK behavior)

### 8. **Generic Exception → Silent Retry**
```python
# In pipeline.py line 513-519
except Exception as e:
    logger.error(f"Pipeline error (retrying in 5s): {e}", exc_info=True)
    last_result = None
    time.sleep(5.0)
    continue
```
- ANY exception (JSON decode, network, etc.) caught
- Logged with traceback
- Sleeps 5s, retries
- **Result**: "Not doing anything" for 5s+

### 9. **Missing Extraction Handler**
```python
# In handle_extraction.py line 70
vlm_result = call_spark("/api/v1/extract_image", {...})
```
- If endpoint doesn't exist on Spark → 404
- `raise_for_status()` raises exception
- Caught in pipeline → sleeps 5s, retries
- **Result**: Extraction fails silently, continues

### 10. **Chat History Load Fails**
```python
# In window.py line 491-499
def _load_chat_history(self):
    try:
        result = call_spark(f"/chat/{platform}/history", method="GET")
        # ...
    except Exception as e:
        self.logger.warning(f"Could not load chat history: {e}")
```
- Runs on background thread at startup
- Exception logged as warning (non-fatal)
- App continues
- **Result**: Empty chat panel, but app runs

---

## ERROR HANDLING SUMMARY

| Scenario | Handling | Visibility |
|----------|----------|------------|
| **Spark unreachable** | Exception → 5s retry | ✓ Logged |
| **App not found** | Exception → 5s retry | ✓ Logged |
| **Element not found** | Handler returns None | ✓ Logged |
| **Action crashes** | Exception caught → BT fails | ✓ Logged |
| **JSON parse error** | Exception → 5s retry | ✓ Logged (with traceback) |
| **API returns 404** | `raise_for_status()` → exception | ✓ Logged |
| **API returns 401** | `raise_for_status()` → exception | ✓ Logged |
| **No tree in directive** | Logged, result marked failure | ✓ Logged |

### Critical Issue: No Validation of Spark Response

```python
# pipeline.py line 257
directive = call_spark("/next_action", payload)
dtype = directive.get("directive", "stop")  # Defaults to "stop" if missing!
```

If Spark returns invalid JSON or missing `directive` key:
- `.get("directive", "stop")` returns "stop"
- Pipeline stops with reason "server_stop"
- **Problem**: No validation that response is actually valid

---

## CONFIG VALIDATION AT STARTUP

**No config validation!** The app loads config once:

```python
# app/config.py
def _load_config() -> dict:
    config = dict(_DEFAULTS)
    # Layer 2: User config file
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, "r") as f:
            user_config = json.load(f)
        config.update({k: v for k, v in user_config.items() if v})
    # Layer 1: Environment variables
    env_url = os.environ.get("TAEY_ED_SPARK_URL")
    if env_url:
        config["spark_url"] = env_url
    return config
```

Problems:
1. No check if `spark_url` is valid URL
2. No check if server is reachable
3. No check if `api_key` is required
4. No validation at startup - only at first API call (30s+ timeout)

---

## QUICK DIAGNOSTIC: FINDING THE PROBLEM

If app "doesn't do anything":

1. **Check logs** (bottom of window or `/tmp/behavior_tree_debug.log`)
   ```
   "Pipeline error (retrying in 5s): ..." → Network/API problem
   "Application 'X' not found" → App not running
   "element not found" → Element matching failed
   ```

2. **Check config**
   ```bash
   cat ~/.taey-ed/config.json  # Should have spark_url
   ```

3. **Test Spark connection**
   ```bash
   curl http://10.0.0.68:5002/next_action -X OPTIONS  # Quick test
   ```

4. **Check accessible app**
   ```bash
   # Verify app is actually running
   ps aux | grep "Google Chrome"  # or "Acellus", etc.
   ```

5. **Enable debug logs**
   ```bash
   tail -f /tmp/behavior_tree_debug.log  # Real-time BT execution
   ```

---

## KEY CODE LOCATIONS FOR DEBUGGING

| File | Purpose | Lines |
|------|---------|-------|
| `app/pipeline.py` | Main loop, directive handling | 163-528 |
| `app/ui/window.py` | UI + thread spawning | 549-595 (one screen), 658-721 (continuous) |
| `app/tasks/call_spark.py` | HTTP client (ONLY place API is called) | 27-51 |
| `app/config.py` | Config loading (NO VALIDATION) | 36-64 |
| `app/tasks/bt_core.py` | Behavior tree execution | 125-200+ |
| `app/tasks/bt_handlers.py` | All action implementations | 20+ handlers |
| `app/tasks/capture_tree.py` | Accessibility tree capture | 25-118 |
| `app/tasks/click_element.py` | Element interaction | 48-200+ |

---

## ARCHITECTURE SUMMARY

```
┌─────────────────────────────────────────┐
│         TaeyEdWindow (UI Thread)         │
│  Buttons → spawn background thread      │
└──────────────────┬──────────────────────┘
                   │
       ┌───────────┴───────────┐
       ▼                       ▼
  run_one_screen()    run_continuous()
       │                       │
       └───────────┬───────────┘
                   ▼
           pipeline.py (Main Loop)
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
capture_tree  call_spark    execute_tree
    │           /next_action      │
    │              │        ┌─────┴─────┐
    │              │        ▼           ▼
    │              │    bt_core    bt_handlers
    │              │       │        (click, type,
    │              │       │         find, wait)
    │              ▼       ▼
    └─────────────────────────────┐
                   │
         last_result sent back
         to Spark in next call
```

**Key insight**: Mac is a "dumb executor" - ALL logic lives in Spark's responses. The only way things fail is:
1. Can't reach Spark
2. Can't find app/elements (access layer problem)
3. Spark sends invalid directive
4. Action crashes

