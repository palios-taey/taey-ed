#!/bin/bash
#═══════════════════════════════════════════════════════════════════════════════
# Taey-Ed: FULL REBUILD — Clean → Build → Sign → Launch → Notarize (bg)
#═══════════════════════════════════════════════════════════════════════════════
#
# THE ONE SCRIPT. Run this and only this for any rebuild.
#
# Usage:
#   ./scripts/full_rebuild.sh              # Full rebuild from scratch
#   ./scripts/full_rebuild.sh --resign     # Re-sign existing build (skip py2app)
#   ./scripts/full_rebuild.sh --no-launch  # Don't launch after notarization
#
# Prerequisites:
#   - "Developer ID Application" certificate installed in Keychain
#   - Notary credentials stored: xcrun notarytool store-credentials "taey-ed-notary"
#   - Python 3.12 at /opt/homebrew/bin/python3.12
#
# Known failure modes this script handles:
#   1. Bus error on `notarytool submit --wait` — separates submit from wait
#   2. Extended attributes blocking `rm -rf dist` — clears xattr first
#   3. Running Taey-Ed app blocks rebuild — kills it first
#   4. Homebrew dylibs have corrupted __LINKEDIT after py2app — replaces with fresh
#   5. python312.zip contains unsigned dylibs — extracts, signs, repacks
#   6. MacOS/python executable needs separate signing
#
#═══════════════════════════════════════════════════════════════════════════════
set -euo pipefail

# ── Parse arguments ──
RESIGN_ONLY=false
NO_LAUNCH=false
for arg in "$@"; do
    case "$arg" in
        --resign)    RESIGN_ONLY=true ;;
        --no-launch) NO_LAUNCH=true ;;
        *)           echo "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ── Path resolution (dynamic — works from any directory) ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$PROJECT_DIR/dist/Taey-Ed.app"
ENTITLEMENTS="$PROJECT_DIR/entitlements.plist"
ZIP_PATH="$PROJECT_DIR/dist/Taey-Ed.zip"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON="/opt/homebrew/bin/python3.12"
KEYCHAIN_PROFILE="taey-ed-notary"

echo "═══════════════════════════════════════════════════════════════"
echo "  Taey-Ed Full Rebuild"
echo "  Project: $PROJECT_DIR"
echo "  Mode:    $(if $RESIGN_ONLY; then echo 'RESIGN ONLY'; else echo 'FULL REBUILD'; fi)"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Preflight checks ──
echo "=== Preflight Checks ==="

# 1. Verify entitlements exist
if [ ! -f "$ENTITLEMENTS" ]; then
    echo "FATAL: entitlements.plist not found at $ENTITLEMENTS"
    exit 1
fi
echo "  [OK] entitlements.plist"

# 2. Find signing identity
IDENTITY=$(security find-identity -v -p codesigning | grep "Developer ID Application" | head -1 | awk -F'"' '{print $2}')
if [ -z "$IDENTITY" ]; then
    echo "FATAL: No 'Developer ID Application' certificate found in Keychain."
    echo "  Install at: https://developer.apple.com/account/resources/certificates/add"
    exit 1
fi
echo "  [OK] Identity: $IDENTITY"

# 3. Verify notary credentials
if ! xcrun notarytool history --keychain-profile "$KEYCHAIN_PROFILE" > /dev/null 2>&1; then
    echo "FATAL: Notary credentials not stored."
    echo "  Run: xcrun notarytool store-credentials \"$KEYCHAIN_PROFILE\""
    exit 1
fi
echo "  [OK] Notary credentials"

# 4. Verify Python 3.12
if [ ! -x "$PYTHON" ]; then
    echo "FATAL: Python 3.12 not found at $PYTHON"
    exit 1
fi
echo "  [OK] Python 3.12"

echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 0: Kill running Taey-Ed
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 0: Kill any running Taey-Ed ==="
if pgrep -xq "Taey-Ed"; then
    echo "  Sending quit signal..."
    osascript -e 'tell application "Taey-Ed" to quit' 2>/dev/null || true
    sleep 2
    # Force kill if still alive
    if pgrep -xq "Taey-Ed"; then
        echo "  Force killing..."
        pkill -9 -x "Taey-Ed" 2>/dev/null || true
        sleep 1
    fi
    echo "  Taey-Ed stopped."
else
    echo "  Not running."
fi
echo ""

if ! $RESIGN_ONLY; then
    # ══════════════════════════════════════════════════════════════════════════
    # STEP 1: Clean build artifacts
    # ══════════════════════════════════════════════════════════════════════════
    echo "=== Step 1: Clean ==="
    for dir in "$PROJECT_DIR/dist" "$PROJECT_DIR/build"; do
        if [ -d "$dir" ]; then
            echo "  Removing $(basename "$dir")/..."
            # Try 1: xattr + rm (works for most cases)
            xattr -rc "$dir" 2>/dev/null || true
            rm -rf "$dir" 2>/dev/null || true
            # Try 2: find -delete (handles stubborn signed bundles)
            if [ -d "$dir" ]; then
                echo "  rm -rf failed, using find -delete..."
                find "$dir" -type f -delete 2>/dev/null || true
                find "$dir" -depth -type d -delete 2>/dev/null || true
            fi
            # Try 3: rename + background delete (nuclear option)
            if [ -d "$dir" ]; then
                echo "  find -delete failed, rename + async delete..."
                TRASH="/tmp/taey-ed-trash-$$"
                mv "$dir" "$TRASH" 2>/dev/null && rm -rf "$TRASH" &
            fi
            if [ -d "$dir" ]; then
                echo "  FATAL: Cannot remove $dir"
                exit 1
            fi
        fi
    done
    echo "  Clean complete."
    echo ""

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 2: Create venv and install dependencies
    # ══════════════════════════════════════════════════════════════════════════
    echo "=== Step 2: Create venv + install deps ==="
    rm -rf "$VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet 'setuptools==70.0.0'
    "$VENV_DIR/bin/pip" install --quiet py2app Pillow httpx \
        pyobjc-framework-Cocoa pyobjc-framework-Quartz \
        pyobjc-framework-ApplicationServices numpy
    echo "  Dependencies installed."
    echo ""

    # ══════════════════════════════════════════════════════════════════════════
    # STEP 3: Build py2app
    # ══════════════════════════════════════════════════════════════════════════
    echo "=== Step 3: Build py2app ==="
    cd "$PROJECT_DIR"
    "$VENV_DIR/bin/python" setup.py py2app 2>&1 | tail -3
    if [ ! -d "$APP_PATH" ]; then
        echo "FATAL: py2app build failed — $APP_PATH does not exist"
        exit 1
    fi
    echo "  Build complete."
    echo ""
else
    # RESIGN ONLY — verify existing build
    echo "=== RESIGN MODE: Verifying existing build ==="
    if [ ! -d "$APP_PATH" ]; then
        echo "FATAL: No existing build at $APP_PATH"
        echo "  Run without --resign for a full rebuild."
        exit 1
    fi
    echo "  Existing build found."
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 4: Fix Homebrew dylibs in Frameworks/
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 4: Fix Homebrew dylibs ==="
DYLIB_COUNT=0
for lib in "$APP_PATH"/Contents/Frameworks/*.dylib; do
    [ -f "$lib" ] || continue
    libname="$(basename "$lib")"
    brew_orig="/opt/homebrew/lib/$libname"
    if [ -f "$brew_orig" ]; then
        echo "  Replacing $libname with fresh Homebrew copy"
        cp "$brew_orig" "$lib"
        install_name_tool -id "@executable_path/../Frameworks/$libname" "$lib" 2>/dev/null || true
    fi
    codesign --remove-signature "$lib" 2>/dev/null || true
    DYLIB_COUNT=$((DYLIB_COUNT + 1))
done
echo "  Processed $DYLIB_COUNT framework dylibs."
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 5: Extract and sign dylibs inside python312.zip
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 5: Sign dylibs inside python312.zip ==="
ZIP_LIB="$APP_PATH/Contents/Resources/lib/python312.zip"
if [ -f "$ZIP_LIB" ]; then
    # Backup before modifying
    cp "$ZIP_LIB" "$ZIP_LIB.bak"

    TMPDIR_ZIP=$(mktemp -d)
    echo "  Extracting..."
    if ! unzip -q "$ZIP_LIB" -d "$TMPDIR_ZIP"; then
        echo "  FATAL: Failed to extract python312.zip"
        cp "$ZIP_LIB.bak" "$ZIP_LIB"
        rm -rf "$TMPDIR_ZIP"
        exit 1
    fi

    ZIP_SIGN_COUNT=0
    find "$TMPDIR_ZIP" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort | while read -r lib; do
        codesign --remove-signature "$lib" 2>/dev/null || true
        codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"
    done

    echo "  Repacking..."
    if ! (cd "$TMPDIR_ZIP" && zip -q -r "$ZIP_LIB" .); then
        echo "  FATAL: Failed to repack python312.zip — restoring backup"
        cp "$ZIP_LIB.bak" "$ZIP_LIB"
        rm -rf "$TMPDIR_ZIP"
        exit 1
    fi

    rm -rf "$TMPDIR_ZIP"
    rm -f "$ZIP_LIB.bak"
    echo "  python312.zip signed and repacked."
else
    echo "  No python312.zip found (skipping)."
fi
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 6: Sign all .so, .dylib, .bundle files
# ══════════════════════════════════════════════════════════════════════════════
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
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 7: Sign MacOS/python executable
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 7: Sign MacOS/python executable ==="
PYTHON_BIN="$APP_PATH/Contents/MacOS/python"
if [ -f "$PYTHON_BIN" ]; then
    codesign --remove-signature "$PYTHON_BIN" 2>/dev/null || true
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_BIN"
    echo "  Signed."
else
    echo "  No python executable found (skipping)."
fi
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 8: Sign Python framework
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 8: Sign Python framework ==="
PYTHON_FW="$APP_PATH/Contents/Frameworks/Python.framework"
if [ -d "$PYTHON_FW" ]; then
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW/Versions/3.12/Python"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW"
    echo "  Signed."
else
    echo "  No Python framework found (skipping)."
fi
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 9: Sign outer app bundle
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 9: Sign outer app bundle ==="
codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$APP_PATH"
echo "  Signed."
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 10: Verify signature
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 10: Verify signature ==="
if codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1; then
    echo "  Signature VALID."
else
    echo "FATAL: Signature verification failed."
    exit 1
fi
echo ""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 11: Launch app (before notarization — app is signed and usable now)
# Uses CLI launch to inherit terminal's Local Network permission on macOS
# Sequoia, avoiding the GUI-only NSLocalNetworkUsageDescription prompt.
# ══════════════════════════════════════════════════════════════════════════════
SUBMISSION_ID="(not-submitted)"
if ! $NO_LAUNCH; then
    echo "=== Step 11: Launch (CLI) ==="
    "$APP_PATH/Contents/MacOS/Taey-Ed" 2>/tmp/taey-ed-stderr.log &
    APP_PID=$!
    echo "  Launched as PID $APP_PID"
    sleep 5
    if kill -0 "$APP_PID" 2>/dev/null; then
        echo "  Taey-Ed is running (PID $APP_PID)."
        echo "  Stderr so far:"
        head -10 /tmp/taey-ed-stderr.log 2>/dev/null || true
    else
        echo "  WARNING: Taey-Ed may have crashed. Check /tmp/taey-ed-stderr.log"
    fi
    echo ""
fi

# ══════════════════════════════════════════════════════════════════════════════
# STEP 12: Notarize (background — does NOT block the script)
# Submits to Apple, then spawns a background watcher that polls for status,
# staples on acceptance, and cleans up the ZIP.
# ══════════════════════════════════════════════════════════════════════════════
echo "=== Step 12: Notarize (background) ==="
rm -f "$ZIP_PATH"
echo "  Creating ZIP..."
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

echo "  Submitting to Apple notary service..."
SUBMIT_OUTPUT=$(xcrun notarytool submit "$ZIP_PATH" \
    --keychain-profile "$KEYCHAIN_PROFILE" 2>&1) || true

SUBMISSION_ID=$(echo "$SUBMIT_OUTPUT" | grep "id:" | head -1 | awk '{print $2}')

if [ -z "$SUBMISSION_ID" ]; then
    echo "  WARNING: Failed to submit for notarization:"
    echo "  $SUBMIT_OUTPUT"
    echo "  App is running but NOT notarized. You can notarize manually later."
else
    echo "  Submission ID: $SUBMISSION_ID"
    echo "  Spawning background watcher (poll every 30s, auto-staple on accept)..."

    # Background watcher: poll → staple → cleanup
    (
        while true; do
            sleep 30
            STATUS=$(xcrun notarytool info "$SUBMISSION_ID" \
                --keychain-profile "$KEYCHAIN_PROFILE" 2>&1)
            if echo "$STATUS" | grep -q "status: Accepted"; then
                xcrun stapler staple "$APP_PATH" 2>/dev/null
                rm -f "$ZIP_PATH"
                echo "[notarize-bg] ACCEPTED and stapled ($SUBMISSION_ID)" >> /tmp/taey-ed-notarize.log
                exit 0
            elif echo "$STATUS" | grep -q "status: Invalid"; then
                echo "[notarize-bg] REJECTED ($SUBMISSION_ID)" >> /tmp/taey-ed-notarize.log
                echo "[notarize-bg] Check: xcrun notarytool log $SUBMISSION_ID --keychain-profile $KEYCHAIN_PROFILE" >> /tmp/taey-ed-notarize.log
                exit 1
            fi
        done
    ) &
    NOTARIZE_WATCHER_PID=$!
    echo "  Watcher PID: $NOTARIZE_WATCHER_PID"
    echo "  Check progress: xcrun notarytool info $SUBMISSION_ID --keychain-profile $KEYCHAIN_PROFILE"
    echo "  Log file: /tmp/taey-ed-notarize.log"
fi
echo ""

# ══════════════════════════════════════════════════════════════════════════════
echo "═══════════════════════════════════════════════════════════════"
echo "  DONE"
echo "  App: $APP_PATH"
echo "  Notarization ID: $SUBMISSION_ID"
if ! $NO_LAUNCH; then
    echo "  App PID: ${APP_PID:-unknown}"
fi
echo "  Notarization runs in background. Check /tmp/taey-ed-notarize.log"
echo "═══════════════════════════════════════════════════════════════"
