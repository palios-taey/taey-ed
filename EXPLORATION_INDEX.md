# Taey-Ed Codebase Exploration - Complete Index

**Date**: February 26, 2026  
**Explorer**: Claude Code (CCM)  
**Status**: Complete - All execution flows, API endpoints, and failure points identified

---

## Documents Created

### 1. **TAEY_ED_EXECUTION_ANALYSIS.md** (14KB)
**Complete technical reference for understanding the system**

Contains:
- Full execution flow from button click to Spark API call
- Complete list of all 5 Spark API endpoints
- Detailed pipeline architecture (capture → ask → execute → report)
- Behavior tree execution model
- Element interaction strategies (click, type, find)
- Page change detection logic
- **10 specific failure points** where "not doing anything" happens
- Error handling analysis
- Critical issue: No validation of Spark response structure
- Key code locations for debugging
- Architecture diagram

**Use this when**: You need to understand how the system works end-to-end

---

### 2. **QUICK_TROUBLESHOOTING.md** (6KB)
**Practical diagnostic guide for debugging issues**

Contains:
- Step-by-step diagnosis procedure
- Log message → problem mapping table
- Configuration validation steps
- Spark connection testing commands
- Accessibility tree capture testing
- Common failure modes and fixes
- Minimal test suite for debugging
- Key files quick reference

**Use this when**: The app "doesn't do anything" and you need to find the problem quickly

---

### 3. **CURRENT_STATE.md** (26KB - Pre-existing)
**System flow and screen recognition documentation**

Contains:
- How the system works (overview)
- Screen types and recognition
- Detailed flow scenarios
- Phase documentation
- Current operational state

**Use this when**: You need context about what screens the app is supposed to handle

---

## Quick Navigation

### I need to understand how the system works
→ Read **TAEY_ED_EXECUTION_ANALYSIS.md** (sections 1-3)

### The app isn't working and I need to debug it
→ Follow **QUICK_TROUBLESHOOTING.md** (section "Step 1-6")

### I need to find where to add a new feature
→ See **TAEY_ED_EXECUTION_ANALYSIS.md** (section "Key Code Locations")

### I need to understand a specific failure
→ Read **TAEY_ED_EXECUTION_ANALYSIS.md** (section "Failure Points")

### I want to know what endpoints Spark provides
→ See **TAEY_ED_EXECUTION_ANALYSIS.md** (section "Spark API Endpoints")

---

## Key Findings Summary

### The System in 30 Seconds

```
User clicks button
    ↓
Background thread spawned
    ↓
capture_tree(app_name) → accessibility tree
    ↓
POST /next_action to Spark with tree
    ↓
Spark responds with "directive"
    ↓
Execute directive (click, type, wait, etc.)
    ↓
Report result back to Spark
    ↓
LOOP until Spark says "stop"
```

### All Spark API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| **/next_action** | POST | Main loop - screen → directive |
| /api/v1/extract_image | POST | VLM image analysis |
| /api/v1/embed | POST | Text embeddings |
| /api/v1/generate | POST | LLM generation |
| /chat/{platform}/history | GET | Chat history |

Default server: `http://10.0.0.68:5002`

### If "Not Doing Anything" - Check These First

1. **Spark is running**: `curl http://10.0.0.68:5002/health`
2. **App is running**: `ps aux | grep "Google Chrome"` (or app name)
3. **Config exists**: `cat ~/.taey-ed/config.json`
4. **Logs show errors**: `tail -f /tmp/behavior_tree_debug.log`
5. **Network is reachable**: `ping 10.0.0.68`

---

## Architecture Files

### Core Pipeline
- **app/pipeline.py** (lines 163-528) - Main execution loop
- **app/tasks/call_spark.py** (lines 27-51) - HTTP client (ONLY place APIs are called)

### UI & Threading
- **app/ui/window.py** (lines 549-721) - Button handlers and thread spawning
- **app/config.py** (lines 36-64) - Configuration loading (NO VALIDATION)

### Screen Capture
- **app/tasks/capture_tree.py** - Accessibility tree extraction
- **app/tasks/compute_tree_hash.py** - Change detection

### Tree Execution
- **app/tasks/bt_core.py** (lines 125+) - Behavior tree engine
- **app/tasks/bt_handlers.py** - Action handlers (click, type, find, wait, etc.)

### Element Interaction
- **app/tasks/find_element.py** - Element lookup via accessibility APIs
- **app/tasks/click_element.py** - Element clicking (4 strategies)
- **app/tasks/type_text.py** - Text input

### Content Extraction
- **app/tasks/handle_extraction.py** - Content extraction pipeline
- **app/tasks/capture_macapptree.py** - Screenshot capture

---

## Debug Logs

### Behavior Tree Execution Log
```bash
tail -f /tmp/behavior_tree_debug.log
```
Shows real-time BT execution: which nodes ran, which actions succeeded/failed

### Application Log
Look in the UI window at the "Log:" panel - shows:
- Directive types received
- Screen transitions
- Errors with full tracebacks

---

## Error Handling Patterns

### Caught Exceptions
- Network errors → 5s retry
- App not found → 5s retry  
- Element not found → Handler returns None → BT fails
- Action crashes → Exception caught → BT logs error
- Stale element → 1s wait → recapture tree

### Silent Failures
- Missing config validation → 30s timeout on first API call
- Invalid Spark response → No validation, defaults to "stop"
- Chat history load fails → Warning logged, continues

---

## Testing Commands

```bash
cd /Users/user/taey-ed

# Test 1: Load config
python3 -c "from app.config import get_spark_url; print(get_spark_url())"

# Test 2: Reach Spark
curl http://10.0.0.68:5002/health

# Test 3: Capture tree
python3 -c "
from app.tasks.capture_tree import capture_tree
tree = capture_tree('Google Chrome')
print(f'Tree children: {len(tree.get(\"children\", []))}')
"

# Test 4: Run UI
python3 run_ui.py
```

---

## Code Statistics

- **Total files**: ~2,200 Python files (including py2app build artifacts)
- **Core app files**: ~30 files
- **Main loop**: 366 lines (pipeline.py)
- **Exception handlers**: Every critical function has try/except
- **Log statements**: 200+ across codebase

---

## Known Limitations

1. **No Spark response validation** - defaults to "stop" if directive key missing
2. **No config validation** - first error appears after 30s timeout
3. **No health check at startup** - problems discovered during execution
4. **Silent retry loops** - user sees "not doing anything" for 5-30 seconds
5. **No rollback on failure** - failed actions move to next screen (not previous)

---

## Next Steps for Debugging

### If app crashes immediately
1. Check System Settings → Privacy → Accessibility (add Taey-Ed.app)
2. Try running directly: `python3 run_ui.py`
3. Check dependencies: `python3 -m pip install -r requirements.txt`

### If button click does nothing
1. Check `/tmp/behavior_tree_debug.log` for errors
2. Verify Spark is reachable: `curl http://10.0.0.68:5002/health`
3. Verify app is running: `ps aux | grep AppName`
4. Check config: `cat ~/.taey-ed/config.json`

### If it runs but no actions happen
1. Check for "element not found" in logs
2. Verify screen state matches expectations
3. Check Spark response in logs (should show directive type)

---

## Questions Answered

✓ What happens when user clicks a button?
→ Background thread spawned, pipeline.run_one_screen() called

✓ What endpoints does the Mac app call on Spark?
→ 5 endpoints: /next_action (main), /api/v1/extract_image, /api/v1/embed, /api/v1/generate, /chat/*/history

✓ What's the execution flow from button to Spark call?
→ Capture tree → POST /next_action → get directive → execute → report → loop

✓ What error handling exists?
→ All exceptions caught, logged with traceback, 5s retry on network errors

✓ What are the failure points?
→ 10 identified: Spark unreachable, app not found, element not found, invalid response, no config, action crash, stale element, generic exception, missing extraction handler, chat history load fail

---

**For the complete technical deep-dive**: See TAEY_ED_EXECUTION_ANALYSIS.md  
**For step-by-step debugging**: See QUICK_TROUBLESHOOTING.md
