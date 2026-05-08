#!/bin/bash
# Fix PIL dylibs inside python312.zip, sign main executable, and re-notarize
set -euo pipefail

APP_PATH="/Users/user/taey-ed/dist/Taey-Ed.app"
IDENTITY="Developer ID Application: Jesse LaRose (SQNKR6587Y)"
ENTITLEMENTS="/Users/user/taey-ed/entitlements.plist"
ZIP_FILE="$APP_PATH/Contents/Resources/lib/python312.zip"

echo "=== Step 1: Extract python312.zip and sign PIL dylibs ==="
TMPDIR_ZIP=$(mktemp -d)
cd "$TMPDIR_ZIP"
unzip -q "$ZIP_FILE"

# Sign all dylibs inside the extracted zip
find . -type f \( -name "*.dylib" -o -name "*.so" \) | sort | while read -r lib; do
    codesign --remove-signature "$lib" 2>/dev/null || true
    echo "  Signing (zip): $(basename "$lib")"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"
done

# Re-create the zip
rm -f "$ZIP_FILE"
zip -q -r "$ZIP_FILE" .
cd /
rm -rf "$TMPDIR_ZIP"
echo "python312.zip rebuilt with signed dylibs."

echo ""
echo "=== Step 2: Sign main executable ==="
MAIN_EXE="$APP_PATH/Contents/MacOS/python"
codesign --remove-signature "$MAIN_EXE" 2>/dev/null || true
codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$MAIN_EXE"
echo "Main executable signed."

echo ""
echo "=== Step 3: Fix Homebrew Frameworks dylibs ==="
for lib in "$APP_PATH"/Contents/Frameworks/*.dylib; do
    [ -f "$lib" ] || continue
    libname="$(basename "$lib")"
    brew_orig="/opt/homebrew/lib/$libname"
    if [ -f "$brew_orig" ]; then
        cp "$brew_orig" "$lib"
        install_name_tool -id "@executable_path/../Frameworks/$libname" "$lib" 2>/dev/null || true
    fi
    codesign --remove-signature "$lib" 2>/dev/null || true
done

echo ""
echo "=== Step 4: Sign ALL inner binaries ==="
find "$APP_PATH" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort | while read -r lib; do
    codesign --remove-signature "$lib" 2>/dev/null || true
    echo "  Signing: $(basename "$lib")"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"
done

echo ""
echo "=== Step 5: Sign Python framework ==="
PYTHON_FW="$APP_PATH/Contents/Frameworks/Python.framework"
if [ -d "$PYTHON_FW" ]; then
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW/Versions/3.12/Python"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW"
fi

echo ""
echo "=== Step 6: Sign outer app ==="
codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$APP_PATH"

echo ""
echo "=== Step 7: Verify ==="
codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1
echo "Signature valid."

echo ""
echo "=== Step 8: Notarize ==="
NOTARIZE_ZIP="/Users/user/taey-ed/dist/Taey-Ed.zip"
rm -f "$NOTARIZE_ZIP"
ditto -c -k --keepParent "$APP_PATH" "$NOTARIZE_ZIP"
echo "Submitting to Apple..."
xcrun notarytool submit "$NOTARIZE_ZIP" --keychain-profile "taey-ed-notary" --wait

echo ""
echo "=== Step 9: Staple ==="
xcrun stapler staple "$APP_PATH"

echo ""
echo "=== DONE ==="
rm -f "$NOTARIZE_ZIP"
echo "Notarized: $APP_PATH"
