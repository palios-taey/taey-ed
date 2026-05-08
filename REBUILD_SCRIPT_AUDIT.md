# Taey-Ed Full Rebuild Script - Comprehensive Audit Report
**Date**: 2026-02-27
**File**: /Users/user/taey-ed/scripts/full_rebuild.sh
**Lines**: 390
**Status**: Multiple issues identified, some critical

---

## ISSUE #1: xattr/rm Problem (STEP 1, Lines 118-126)

**Severity**: MEDIUM (intermittent, workaround present but insufficient)

### Current Code
```bash
if [ -d "$PROJECT_DIR/dist" ]; then
    echo "  Clearing extended attributes on dist/..."
    xattr -rc "$PROJECT_DIR/dist" 2>/dev/null || true
    echo "  Removing dist/..."
    rm -rf "$PROJECT_DIR/dist"
fi
```

### The Problem
1. **xattr -rc** (recursive, clear) is called on the directory BEFORE deletion
2. **Extended attributes are on the app bundle itself** (Taey-Ed.app), not just its contents
3. **The `.DS_Store` file** in dist/ often has its own xattr that prevents full deletion
4. **Sporadic failures**: xattr doesn't guarantee all xattrs are cleared; some may still block rm -rf

### Why It Fails
- Code-signed app bundles in dist/ have extended attributes like `com.apple.quarantine`, `com.apple.security.cs` metadata
- Running `xattr -rc` at directory level doesn't always penetrate into nested bundles
- The app bundle's Info.plist and MacOS/python binary may have additional xattr that xattr -rc misses
- `.DS_Store` file with @ flag indicates extended attributes

### Root Cause
**Apple's codesign metadata is more persistent than xattr -rc can handle.** The attributes aren't just on files—they're on the code signature itself.

### Recommended Fixes

**Option A (Best - Atomic Deletion)**
```bash
# Use sudo with forced flag to bypass permission issues entirely
if [ -d "$PROJECT_DIR/dist" ]; then
    echo "  Removing dist/ (with permission override)..."
    sudo rm -rf "$PROJECT_DIR/dist"
    echo "  Clean complete."
fi
```
**Rationale**: Sudo bypasses all extended attribute checks. One atomic operation.

**Option B (Safest - Per-file xattr removal)**
```bash
if [ -d "$PROJECT_DIR/dist" ]; then
    echo "  Removing extended attributes on dist/..."
    # Force remove all extended attributes recursively
    find "$PROJECT_DIR/dist" -type f -exec xattr -c {} \; 2>/dev/null || true
    find "$PROJECT_DIR/dist" -type d -exec xattr -c {} \; 2>/dev/null || true
    
    # Additional nuclear option: change permissions before deletion
    chmod -R u+w "$PROJECT_DIR/dist" 2>/dev/null || true
    
    echo "  Removing dist/..."
    rm -rf "$PROJECT_DIR/dist" || {
        # Last resort: use find with -delete
        find "$PROJECT_DIR/dist" -type f -delete
        find "$PROJECT_DIR/dist" -type d -delete
    }
fi
```
**Rationale**: Explicitly removes xattr from ALL files AND directories, changes permissions, then uses find -delete as fallback.

**Option C (Nuclear - Direct Path)**
```bash
if [ -d "$PROJECT_DIR/dist" ]; then
    echo "  Removing dist/..."
    # Remove even if xattr/permissions are weird
    find "$PROJECT_DIR/dist" -name "*.app" -exec chmod -R u+w {} \;
    rm -rf "$PROJECT_DIR/dist" 2>/dev/null || {
        # If rm fails, use find -delete
        echo "  Using atomic find -delete (slower but guaranteed)..."
        find "$PROJECT_DIR/dist" -delete
    }
fi
```
**Rationale**: Handles permission issues first, then falls back to find -delete if needed.

---

## ISSUE #2: Notarization Blocking (STEP 11, Lines 299-350)

**Severity**: CRITICAL (blocks entire rebuild, 2-15+ minute wait)

### Current Code
```bash
echo "  Submitting to Apple notary service..."
SUBMIT_OUTPUT=$(xcrun notarytool submit "$ZIP_PATH" \
    --keychain-profile "$KEYCHAIN_PROFILE" 2>&1) || true

# ... extraction of SUBMISSION_ID ...

echo "  Waiting for Apple to process (this takes 2-15 minutes)..."

WAIT_OUTPUT=$(xcrun notarytool wait "$SUBMISSION_ID" \
    --keychain-profile "$KEYCHAIN_PROFILE" 2>&1)
WAIT_EXIT=$?
```

### The Problem
1. **`notarytool wait` is BLOCKING** - the script hangs for 2-15+ minutes
2. **No parallel processing** - build output could launch while notarization happens in background
3. **Bus error mentioned in comments (line 300)** but only for submit --wait, not for separate wait command
4. **Step 14 (Launch) happens AFTER stapling** - app could be running during earlier notarization
5. **User cannot interrupt gracefully** - no timeout mechanism, no background option

### Why This Is Bad
- Developer sits waiting for 15 minutes with nothing to do
- Could launch app during notarization processing (only depends on stapling, not waiting)
- If waiting fails, the app still runs fine (notarization is cosmetic for local use)
- Wastes developer productivity

### Recommended Fixes

**Option A (Best - Deferred Stapling with Async Notarization)**
```bash
# STEP 11: Notarize (background)
echo "=== Step 11: Notarize (async) ==="
rm -f "$ZIP_PATH"
echo "  Creating ZIP..."
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

echo "  Submitting to Apple (background)..."
SUBMIT_OUTPUT=$(xcrun notarytool submit "$ZIP_PATH" \
    --keychain-profile "$KEYCHAIN_PROFILE" 2>&1) || true

SUBMISSION_ID=$(echo "$SUBMIT_OUTPUT" | grep "id:" | head -1 | awk '{print $2}')

if [ -z "$SUBMISSION_ID" ]; then
    echo "FATAL: Failed to extract submission ID"
    exit 1
fi

echo "  Submission ID: $SUBMISSION_ID (check status later: xcrun notarytool info $SUBMISSION_ID --keychain-profile $KEYCHAIN_PROFILE)"

# Save submission ID to file for later reference
echo "$SUBMISSION_ID" > "$PROJECT_DIR/.notarization_pending"

# ══════════════════════════════════════════════════════════════════════════════
# STEP 12: Launch immediately (don't wait for notarization)
# ══════════════════════════════════════════════════════════════════════════════
if ! $NO_LAUNCH; then
    echo "=== Step 12: Launch (notarization in background) ==="
    open "$APP_PATH"
    sleep 2
    if pgrep -xq "Taey-Ed"; then
        echo "  Taey-Ed is running."
        echo ""
        echo "  NOTE: Notarization is processing in background (ID: $SUBMISSION_ID)"
        echo "  Check status later: xcrun notarytool info $SUBMISSION_ID --keychain-profile $KEYCHAIN_PROFILE"
    fi
else
    echo "=== Step 12: Launch skipped (--no-launch) ==="
fi

# Optional: Show what to do next
echo "=== Next: Wait for Notarization ==="
echo "  When ready, run:"
echo "    ./scripts/finalize_notarization.sh"
echo ""
```

**Option B (Interactive - Prompt to wait or defer)**
```bash
echo "=== Step 11: Notarize ==="
# ... submit code ...

if [ -z "$SUBMISSION_ID" ]; then
    echo "FATAL: Failed to extract submission ID"
    exit 1
fi

echo "  Submission ID: $SUBMISSION_ID"
echo ""
echo "  Option 1: Wait now (2-15 minutes)"
echo "  Option 2: Defer stapling + launch (run ./scripts/finalize_notarization.sh later)"
echo ""
read -p "Enter choice [1/2]: " CHOICE

if [ "$CHOICE" = "1" ]; then
    echo "  Waiting for Apple to process..."
    WAIT_OUTPUT=$(xcrun notarytool wait "$SUBMISSION_ID" --keychain-profile "$KEYCHAIN_PROFILE" 2>&1)
    if echo "$WAIT_OUTPUT" | grep -q "status: Accepted"; then
        echo "  ACCEPTED."
        echo "=== Step 12: Staple ==="
        xcrun stapler staple "$APP_PATH"
    else
        echo "FATAL: Notarization rejected or timed out"
        exit 1
    fi
else
    echo "  Deferred stapling. Run when ready:"
    echo "    ./scripts/finalize_notarization.sh $SUBMISSION_ID"
    exit 0
fi
```

**Option C (Timeout-based - Best compromise)**
```bash
echo "  Waiting for notarization (max 3 minutes, then background)..."

WAIT_OUTPUT=$(timeout 180 xcrun notarytool wait "$SUBMISSION_ID" \
    --keychain-profile "$KEYCHAIN_PROFILE" 2>&1) || {
    WAIT_EXIT=$?
    if [ $WAIT_EXIT -eq 124 ]; then
        # Timeout - notarization still pending
        echo "  Timeout waiting. Notarization continues in background."
        echo "  Check status: xcrun notarytool info $SUBMISSION_ID --keychain-profile $KEYCHAIN_PROFILE"
        echo "  Finalize when ready: ./scripts/finalize_notarization.sh $SUBMISSION_ID"
        
        # Don't staple yet - save ID for later
        echo "$SUBMISSION_ID" > "$PROJECT_DIR/.notarization_pending"
        
        # Skip Step 12-14 (staple + cleanup + launch)
        exit 0
    fi
}

# If we got here, notarization completed
if echo "$WAIT_OUTPUT" | grep -q "status: Accepted"; then
    echo "  ACCEPTED - proceeding to staple..."
else
    echo "FATAL: Notarization rejected"
    exit 1
fi
```

### Recommended: Create Helper Script

**File**: `scripts/finalize_notarization.sh`
```bash
#!/bin/bash
# Finalize notarization after async background processing

SUBMISSION_ID="${1:-$(cat .notarization_pending 2>/dev/null)}"
if [ -z "$SUBMISSION_ID" ]; then
    echo "USAGE: finalize_notarization.sh <submission-id>"
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_PATH="$PROJECT_DIR/dist/Taey-Ed.app"
KEYCHAIN_PROFILE="taey-ed-notary"

echo "Checking notarization status for $SUBMISSION_ID..."

INFO=$(xcrun notarytool info "$SUBMISSION_ID" --keychain-profile "$KEYCHAIN_PROFILE" 2>&1)

if echo "$INFO" | grep -q "status: Accepted"; then
    echo "ACCEPTED! Stapling..."
    xcrun stapler staple "$APP_PATH"
    rm -f "$PROJECT_DIR/.notarization_pending"
    echo "Done!"
elif echo "$INFO" | grep -q "status: In Progress"; then
    echo "Still processing... Try again in a minute."
    exit 1
else
    echo "REJECTED or ERROR. Full info:"
    echo "$INFO"
    exit 1
fi
```

### CRITICAL FINDING: Pending Submission Blocking Current Build

**From notarytool history (2026-02-27 16:20:02):**
- Submission ID: `29a21bfa-f886-4be3-ae44-f13bc46ad610`
- Status: **In Progress** (submitted 27 minutes ago)
- Name: Taey-Ed.zip

**Impact**: If you run full_rebuild.sh again RIGHT NOW, it will:
1. Build new app
2. Create new ZIP
3. Submit new notarization (second one)
4. First one is still pending - could cause confusion or rate limiting

**Recommendation BEFORE next rebuild**: 
```bash
# Option A: Wait for current one
xcrun notarytool info 29a21bfa-f886-4be3-ae44-f13bc46ad610 --keychain-profile taey-ed-notary

# Once Accepted, staple it:
xcrun stapler staple /Users/user/taey-ed/dist/Taey-Ed.app

# Then proceed with new rebuild
```

---

## ISSUE #3: Variable Scope Bug in Step 5 (Lines 207-210)

**Severity**: MEDIUM (silent failure of dylib signing inside zip)

### Current Code
```bash
ZIP_SIGN_COUNT=0
find "$TMPDIR_ZIP" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort | while read -r lib; do
    codesign --remove-signature "$lib" 2>/dev/null || true
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"
done
```

### The Problem
1. **`while read` creates a SUBSHELL** in bash when piped
2. **Variable modifications inside the while loop don't persist outside it**
3. **`ZIP_SIGN_COUNT=$((ZIP_SIGN_COUNT + 1))` would be lost** if used
4. **No count output after processing** - silent processing, no feedback

### Impact
- If dylib signing fails inside the zip, you won't know
- No count of how many dylibs were signed
- Makes debugging harder if signature verification fails later

### Recommended Fix
```bash
echo "  Signing dylibs inside python312.zip..."
# Use process substitution to avoid subshell scope issues
while IFS= read -r lib; do
    libname="$(basename "$lib")"
    codesign --remove-signature "$lib" 2>/dev/null || true
    if codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"; then
        echo "    ✓ $libname"
    else
        echo "    ✗ $libname (FAILED)"
        exit 1
    fi
done < <(find "$TMPDIR_ZIP" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort)
```

**Key improvements**:
- Uses `< <(...)` syntax (process substitution) instead of pipe to avoid subshell
- Shows success/failure feedback for each file
- Exits immediately on signing failure instead of silently continuing

---

## ISSUE #4: Step 6 Sign Count Not Reported (Lines 232-247)

**Severity**: LOW (cosmetic, but inconsistent feedback)

### Current Code
```bash
echo "=== Step 6: Sign all inner binaries ==="
SIGN_COUNT=0
SIGN_FAIL=0
find "$APP_PATH" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort | while read -r lib; do
    codesign --remove-signature "$lib" 2>/dev/null || true
    if codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib" 2>&1; then
        : # success
    else
        echo "  FAILED: $(basename "$lib")"
        exit 1
    fi
done
if [ $? -ne 0 ]; then
    echo "FATAL: Inner binary signing failed."
    exit 1
fi
echo "  All inner binaries signed."
```

### The Problem
1. **`SIGN_COUNT` and `SIGN_FAIL` are initialized but NEVER USED**
2. **The while loop subshell issue means they can't be incremented anyway**
3. **No feedback on how many binaries were actually signed**
4. **Inconsistent with Step 5 which at least prints progress**

### Recommended Fix
```bash
echo "=== Step 6: Sign all inner binaries ==="
SIGNED=0
while IFS= read -r lib; do
    libname="$(basename "$lib")"
    codesign --remove-signature "$lib" 2>/dev/null || true
    if codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib" 2>&1; then
        SIGNED=$((SIGNED + 1))
    else
        echo "  FAILED: $libname"
        exit 1
    fi
done < <(find "$APP_PATH" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort)
echo "  Signed $SIGNED inner binaries."
```

---

## ISSUE #5: No Verification That App is Code-Signed (Before Notarization)

**Severity**: MEDIUM (silent failure during notarization)

### Current Code
**Step 10 (line 289-295)** verifies signature, but...

```bash
if codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1; then
    echo "  Signature VALID."
else
    echo "FATAL: Signature verification failed."
    exit 1
fi
```

### The Problem
1. This verification uses `--strict` which is overly strict for Apple
2. If codesign verification PASSES but notarization FAILS, no diagnostic info
3. No check for timestamp authority requirement (needed for notarization)
4. No verification that entitlements are actually embedded in the signature

### Recommended Enhancement
```bash
echo "=== Step 10: Verify signature ==="
if ! codesign --verify --deep --verbose=2 "$APP_PATH" 2>&1; then
    echo "FATAL: Signature verification failed."
    exit 1
fi

# Additional checks for notarization readiness
echo "  Checking notarization requirements..."

# Verify timestamp authority
if codesign --display --verbose=2 "$APP_PATH" 2>&1 | grep -q "Timestamp="; then
    echo "  [OK] Timestamp authority present"
else
    echo "  WARNING: No timestamp authority (needed for notarization)"
fi

# Verify entitlements are embedded
if codesign --display --entitlements :- "$APP_PATH" 2>&1 | grep -q "apple.security"; then
    echo "  [OK] Entitlements embedded"
else
    echo "  WARNING: Entitlements may not be properly embedded"
fi

echo "  Signature VALID."
```

---

## ISSUE #6: setup.py Mismatch Risk (Minor)

**Severity**: LOW (but could cause silent build failures)

### Current setup.py (Lines 1-44)
```python
APP = ["run_ui.py"]
OPTIONS = {
    "argv_emulation": False,
    "packages": find_packages(),
    ...
}
```

### Potential Issues
1. **No explicit packages list** - relies on `find_packages()` auto-discovery
2. If `app/` subpackage structure changes, build could silently miss modules
3. No explicit `data_files` even though app may depend on config files

### Recommended Enhancement
```python
from setuptools import setup, find_packages
import os

APP = ["run_ui.py"]
DATA_FILES = [
    # Add any data files the app needs
    ('app/assets', [f for f in os.listdir('app/assets') if os.path.isfile(os.path.join('app/assets', f))])
    if os.path.exists('app/assets') else [],
]
OPTIONS = {
    "argv_emulation": False,
    "packages": find_packages(),
    "include_packages": {"app": "app"},  # Explicit include
    ...
}

setup(
    ...
    data_files=DATA_FILES,
    ...
)
```

---

## SUMMARY TABLE

| # | Issue | Line(s) | Severity | Impact | Fix |
|---|-------|---------|----------|--------|-----|
| 1 | xattr/rm fails on code-signed bundles | 118-126 | MEDIUM | Intermittent build failure | Use sudo rm or find -delete fallback |
| 2 | notarytool wait blocks for 15+ min | 299-350 | CRITICAL | Wastes developer time, no parallelism | Background + deferred stapling |
| 3 | While-read subshell in Step 5 | 207-210 | MEDIUM | Silent dylib signing failures | Use process substitution |
| 4 | Sign count variables unused in Step 6 | 232-247 | LOW | No feedback on signing count | Increment and print actual counts |
| 5 | No notarization readiness check | After 289 | MEDIUM | Silent notarization failures | Add timestamp/entitlement verification |
| 6 | setup.py relies on auto-discovery | setup.py | LOW | Could miss modules if structure changes | Make packages explicit |

---

## RECOMMENDED IMMEDIATE ACTIONS

1. **RIGHT NOW**: Check if previous notarization is done
   ```bash
   xcrun notarytool info 29a21bfa-f886-4be3-ae44-f13bc46ad610 --keychain-profile taey-ed-notary
   ```
   If Accepted, staple it:
   ```bash
   xcrun stapler staple /Users/user/taey-ed/dist/Taey-Ed.app
   ```

2. **Before next rebuild**: Apply the xattr fix (Option B is safest)

3. **For developer experience**: Implement deferred notarization (create finalize_notarization.sh helper)

4. **Code quality**: Fix subprocess scope issues in Steps 5 & 6

5. **Testing**: Run codesign verification test before notarization submission

