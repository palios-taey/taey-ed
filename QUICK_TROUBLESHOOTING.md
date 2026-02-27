# Taey-Ed Quick Troubleshooting Guide

## "App doesn't do anything" - Diagnosis

### Step 1: Check the logs (FIRST)

**In the UI window:**
- Bottom "Log:" panel shows real-time messages
- Look for ERROR or warning messages

**In terminal:**
```bash
tail -f /tmp/behavior_tree_debug.log    # Real-time BT execution
tail -f /tmp/taey-ed.log                # If app logs to file
```

### Step 2: Identify the problem from log messages

| Log Message | Problem | Fix |
|-------------|---------|-----|
| `"Pipeline error (retrying in 5s): Connection refused"` | Spark not running at 10.0.0.68:5002 | Start Spark API server |
| `"Pipeline error (retrying in 5s): Connection timeout"` | Network unreachable | Check network connectivity |
| `"Application 'Google Chrome' not found"` | App not running | Launch the app |
| `"element not found"` | UI element doesn't exist | Check screen state |
| `"execute_tree directive has no tree"` | Spark sent invalid response | Restart Spark Claude |
| `"Stale element"` | Element went off-screen | OK - should continue |
| **(Empty logs)** | UI thread blocked or crashed | Restart app |

### Step 3: Verify configuration

```bash
cat ~/.taey-ed/config.json
```

Should contain:
```json
{
  "spark_url": "http://10.0.0.68:5002"
}
```

If missing or wrong:
```bash
mkdir -p ~/.taey-ed
cat > ~/.taey-ed/config.json << 'CONFIG'
{
  "spark_url": "http://10.0.0.68:5002",
  "api_key": ""
}
CONFIG
```

### Step 4: Test Spark connection

```bash
# Quick health check
curl -s http://10.0.0.68:5002/health || echo "UNREACHABLE"

# If that works, try a real endpoint
curl -X POST http://10.0.0.68:5002/next_action \
  -H "Content-Type: application/json" \
  -d '{"platform":"khan_academy","tree":{"role":"root"}}'
```

If connection fails:
- Is Spark running? `pgrep -a "python.*5002"` on the Spark machine
- Is network reachable? `ping 10.0.0.68`
- Is firewall blocking? Check port 5002

### Step 5: Verify target app is running

```bash
# For browser platforms
ps aux | grep "Google Chrome"  # or "Firefox", etc.

# For native apps
ps aux | grep "Acellus"  # or app name
```

If app not running - launch it first.

### Step 6: Check accessibility tree capture

```bash
# Can we even see the app's UI?
python3 << 'PYTHON'
from app.tasks.capture_tree import capture_tree
try:
    tree = capture_tree("Google Chrome")  # Replace with your app name
    print("Tree captured successfully!")
    print(f"Root has {len(tree.get('children', []))} children")
except Exception as e:
    print(f"FAILED: {e}")
PYTHON
```

If this fails - app accessibility is broken. Check System Settings → Privacy → Accessibility.

---

## Common Failure Modes

### "Waiting and re-polling..." in logs

Pipeline is stuck in a waiting state. Spark is consulting or the page is loading.

**Normal if**: Consulting a VLM, page loading
**Problem if**: Stuck for more than a minute without progress

**Fix**: Click "Stop" button, check Spark logs on remote machine

### "Error: No module named 'ApplicationServices'"

You're running on non-Mac or venv is broken.

**Fix**:
```bash
cd /Users/user/taey-ed
python3 -m pip install pyobjc-framework-ApplicationServices
```

### "AXError -25211: Unable to perform action"

Accessibility permission missing or app not focused.

**Fix**:
```bash
open x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility
# Add Taey-Ed.app to the list
# Restart the app
```

### App launches but no windows

py2app issue or missing dependencies.

**Fix**:
```bash
cd /Users/user/taey-ed
python3 run_ui.py  # Run directly (not from .app)
```

---

## Quick Debug: Minimal Test

```bash
cd /Users/user/taey-ed

# Test 1: Can we load the config?
python3 -c "from app.config import get_spark_url; print(get_spark_url())"

# Test 2: Can we reach Spark?
python3 -c "
from app.tasks.call_spark import call_spark
try:
    result = call_spark('/health' if hasattr(__import__('httpx'), 'get') else '/next_action', {})
    print('Spark connection OK')
except Exception as e:
    print(f'Spark unreachable: {e}')
"

# Test 3: Can we capture a tree?
python3 -c "
from app.tasks.capture_tree import capture_tree
try:
    tree = capture_tree('Google Chrome')
    print(f'Tree captured: root has {len(tree.get(\"children\", []))} children')
except Exception as e:
    print(f'Tree capture failed: {e}')
"

# Test 4: Run one screen
python3 run_ui.py  # Then click "Run One Screen"
```

---

## Still Stuck?

Check these files in order:

1. **Spark API logs** (on Spark machine)
   ```bash
   ssh spark@10.0.0.68
   tail -f ~/taey-ed-api.log  # or wherever Spark logs
   ```

2. **Full traceback** (in taey-ed window)
   - Look for "Traceback" in logs
   - See exactly which line crashed

3. **Network capture** (if it's weird)
   ```bash
   tcpdump -i any -n 'port 5002'  # See if requests even leave
   ```

4. **Accessibility tree dump**
   ```bash
   cd /Users/user/taey-ed
   python3 -c "
   from app.tasks.capture_tree import capture_tree
   import json
   tree = capture_tree('Google Chrome')
   print(json.dumps(tree, indent=2))
   " | head -100  # First 100 lines
   ```

---

## Key Files

- **Main loop**: `/Users/user/taey-ed/app/pipeline.py` (lines 163-528)
- **API calls**: `/Users/user/taey-ed/app/tasks/call_spark.py` (ONLY place HTTP happens)
- **Config**: `/Users/user/taey-ed/app/config.py` (loading, no validation)
- **Tree capture**: `/Users/user/taey-ed/app/tasks/capture_tree.py`
- **Action execution**: `/Users/user/taey-ed/app/tasks/bt_handlers.py` (click, type, find, wait)

---

## Architecture in 30 seconds

1. User clicks "Run One Screen"
2. UI spawns background thread
3. Background thread calls `pipeline.run_one_screen()`
4. Pipeline captures screen tree
5. Pipeline sends tree to Spark via `POST /next_action`
6. Spark responds with a "directive" (either "execute_tree" or "wait", etc.)
7. Pipeline executes the directive
8. Pipeline reports result back to Spark
9. **Loop** until Spark says "stop"

**If "not doing anything"**: Problem is at step 5, 6, or 8 (network/Spark issue).

