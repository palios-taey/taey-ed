#!/bin/bash
# Taey-Ed: Code Sign + Notarize for distribution
# Prerequisites:
#   - "Developer ID Application" certificate installed in Keychain
#   - App-specific password stored: xcrun notarytool store-credentials "taey-ed-notary"
#
# Usage:
#   ./scripts/sign_and_notarize.sh
#
# After running, dist/Taey-Ed.app is signed and notarized.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$PROJECT_DIR/dist/Taey-Ed.app"
ENTITLEMENTS="$PROJECT_DIR/entitlements.plist"
BUNDLE_ID="com.paliostaey.taey-ed"

# ── Find Developer ID certificate ──
IDENTITY=$(security find-identity -v -p codesigning | grep "Developer ID Application" | head -1 | awk -F'"' '{print $2}')

if [ -z "$IDENTITY" ]; then
    echo "ERROR: No 'Developer ID Application' certificate found."
    echo ""
    echo "You need to create one at:"
    echo "  https://developer.apple.com/account/resources/certificates/add"
    echo ""
    echo "Steps:"
    echo "  1. Open Keychain Access > Certificate Assistant > Request a Certificate from CA"
    echo "  2. Save the CSR to disk"
    echo "  3. Upload at the Apple Developer portal (Developer ID Application)"
    echo "  4. Download and double-click to install"
    exit 1
fi

echo "Using identity: $IDENTITY"

# ── Step 1: Build py2app ──
echo ""
echo "=== Step 1: Building py2app ==="
cd "$PROJECT_DIR"
./venv/bin/python setup.py py2app
echo "Build complete."

# ── Step 2: Sign all nested binaries (inner → outer) ──
echo ""
echo "=== Step 2: Code signing ==="

# Step 2a: Fix Homebrew dylibs in Frameworks/
# Homebrew dylibs can have corrupted __LINKEDIT after py2app copies them.
# Fix: replace with fresh copy, change install_name, strip signature, then sign.
echo "Fixing Homebrew dylibs in Frameworks/..."
for lib in "$APP_PATH"/Contents/Frameworks/*.dylib; do
    [ -f "$lib" ] || continue
    libname="$(basename "$lib")"

    # Try to find original in Homebrew
    brew_orig="/opt/homebrew/lib/$libname"
    if [ -f "$brew_orig" ]; then
        echo "  Replacing $libname with fresh Homebrew copy..."
        cp "$brew_orig" "$lib"
        install_name_tool -id "@executable_path/../Frameworks/$libname" "$lib" 2>/dev/null || true
    fi

    # Strip any existing/invalidated signature
    codesign --remove-signature "$lib" 2>/dev/null || true
done

# Step 2b: Sign ALL .so, .dylib, .bundle files
echo "Signing all .so, .dylib, .bundle files..."
find "$APP_PATH" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) -print0 | while IFS= read -r -d '' lib; do
    # Strip first (handles py2app-corrupted or Homebrew-signed files)
    codesign --remove-signature "$lib" 2>/dev/null || true
    echo "  Signing: $(basename "$lib")"
    if ! codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib" 2>&1; then
        echo "  ERROR: Failed to sign $(basename "$lib")"
        exit 1
    fi
done
if [ $? -ne 0 ]; then
    echo "FATAL: Inner binary signing failed. Aborting."
    exit 1
fi

# Step 2c: Sign the Python framework (must be after its inner binaries)
PYTHON_FW="$APP_PATH/Contents/Frameworks/Python.framework"
if [ -d "$PYTHON_FW" ]; then
    echo "  Signing Python framework executable..."
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW/Versions/3.12/Python"
    echo "  Signing Python framework bundle..."
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW"
fi

# Step 2d: Sign the main app bundle (outer, NO --deep since everything inside is already signed)
echo "  Signing app bundle..."
codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$APP_PATH"

echo "Signed. Verifying..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
echo "Signature valid."

# ── Step 3: Create ZIP for notarization ──
echo ""
echo "=== Step 3: Notarizing ==="
ZIP_PATH="$PROJECT_DIR/dist/Taey-Ed.zip"
rm -f "$ZIP_PATH"
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"

# Submit for notarization
# Uses stored credentials: xcrun notarytool store-credentials "taey-ed-notary"
echo "Submitting to Apple notary service..."
xcrun notarytool submit "$ZIP_PATH" \
    --keychain-profile "taey-ed-notary" \
    --wait

# ── Step 4: Staple the ticket ──
echo ""
echo "=== Step 4: Stapling notarization ticket ==="
xcrun stapler staple "$APP_PATH"

echo ""
echo "=== DONE ==="
echo "Signed and notarized app: $APP_PATH"
echo ""
echo "To create DMG: ./scripts/create_dmg.sh"

# Clean up zip
rm -f "$ZIP_PATH"
