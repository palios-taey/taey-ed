#!/bin/bash
# Re-sign already-built app (skip py2app build)
set -euo pipefail

APP_PATH="/Users/user/taey-ed-v7/dist/Taey-Ed.app"
IDENTITY="Developer ID Application: Jesse LaRose (SQNKR6587Y)"
ENTITLEMENTS="/Users/user/taey-ed-v7/entitlements.plist"

echo "=== Signing all .so, .dylib, .bundle files ==="
find "$APP_PATH" \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | while read -r lib; do
    echo "  Signing: $(basename "$lib")"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib" 2>&1 || true
done

echo ""
echo "=== Signing Python framework ==="
PYTHON_FW="$APP_PATH/Contents/Frameworks/Python.framework"
if [ -d "$PYTHON_FW" ]; then
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW/Versions/3.12/Python"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW"
fi

echo ""
echo "=== Signing app bundle ==="
codesign --force --deep --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$APP_PATH"

echo ""
echo "=== Verifying ==="
codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1
echo ""
echo "SUCCESS: Signature valid"
