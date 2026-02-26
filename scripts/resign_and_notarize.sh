#!/bin/bash
# Re-sign existing build, then notarize
# Used when py2app build is already done but signing needs fixing
set -euo pipefail

APP_PATH="/Users/user/taey-ed-v7/dist/Taey-Ed.app"
IDENTITY="Developer ID Application: Jesse LaRose (SQNKR6587Y)"
ENTITLEMENTS="/Users/user/taey-ed-v7/entitlements.plist"

echo "=== Step 1: Fix Homebrew dylibs in Frameworks/ ==="
for lib in "$APP_PATH"/Contents/Frameworks/*.dylib; do
    [ -f "$lib" ] || continue
    libname="$(basename "$lib")"
    brew_orig="/opt/homebrew/lib/$libname"
    if [ -f "$brew_orig" ]; then
        echo "  Replacing $libname with fresh Homebrew copy..."
        cp "$brew_orig" "$lib"
        install_name_tool -id "@executable_path/../Frameworks/$libname" "$lib" 2>/dev/null || true
    fi
    codesign --remove-signature "$lib" 2>/dev/null || true
done

echo ""
echo "=== Step 1.5: Extract and sign dylibs inside python312.zip ==="
ZIP_LIB="$APP_PATH/Contents/Resources/lib/python312.zip"
if [ -f "$ZIP_LIB" ]; then
    TMPDIR_ZIP=$(mktemp -d)
    echo "  Extracting python312.zip..."
    unzip -q "$ZIP_LIB" -d "$TMPDIR_ZIP"
    # Sign any dylibs found inside the zip
    find "$TMPDIR_ZIP" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort | while read -r lib; do
        codesign --remove-signature "$lib" 2>/dev/null || true
        echo "  Signing (in zip): $(basename "$lib")"
        codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"
    done
    # Repack the zip
    echo "  Repacking python312.zip..."
    (cd "$TMPDIR_ZIP" && zip -q -r "$ZIP_LIB" .)
    rm -rf "$TMPDIR_ZIP"
    echo "  python312.zip repacked with signed dylibs."
fi

echo ""
echo "=== Step 2: Sign all .so, .dylib, .bundle ==="
find "$APP_PATH" -type f \( -name "*.so" -o -name "*.dylib" -o -name "*.bundle" \) | sort | while read -r lib; do
    codesign --remove-signature "$lib" 2>/dev/null || true
    echo "  Signing: $(basename "$lib")"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$lib"
done

echo ""
echo "=== Step 2.5: Sign MacOS/python executable ==="
PYTHON_BIN="$APP_PATH/Contents/MacOS/python"
if [ -f "$PYTHON_BIN" ]; then
    codesign --remove-signature "$PYTHON_BIN" 2>/dev/null || true
    echo "  Signing: python (MacOS executable)"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_BIN"
fi

echo ""
echo "=== Step 3: Sign Python framework ==="
PYTHON_FW="$APP_PATH/Contents/Frameworks/Python.framework"
if [ -d "$PYTHON_FW" ]; then
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW/Versions/3.12/Python"
    codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$PYTHON_FW"
fi

echo ""
echo "=== Step 4: Sign outer app bundle ==="
codesign --force --sign "$IDENTITY" --entitlements "$ENTITLEMENTS" --options runtime --timestamp "$APP_PATH"

echo ""
echo "=== Step 5: Verify ==="
codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1
echo "Signature valid."

echo ""
echo "=== Step 6: Notarize ==="
ZIP_PATH="/Users/user/taey-ed-v7/dist/Taey-Ed.zip"
rm -f "$ZIP_PATH"
ditto -c -k --keepParent "$APP_PATH" "$ZIP_PATH"
echo "Submitting to Apple notary service..."
xcrun notarytool submit "$ZIP_PATH" --keychain-profile "taey-ed-notary" --wait

echo ""
echo "=== Step 7: Staple ==="
xcrun stapler staple "$APP_PATH"

echo ""
echo "=== DONE ==="
echo "Signed and notarized: $APP_PATH"
rm -f "$ZIP_PATH"
