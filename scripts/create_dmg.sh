#!/bin/bash
# Taey-Ed: Create distributable DMG
# Run AFTER sign_and_notarize.sh
#
# Usage:
#   ./scripts/create_dmg.sh
#
# Output: dist/Taey-Ed-7.0.0.dmg

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
APP_PATH="$PROJECT_DIR/dist/Taey-Ed.app"
VERSION="1.0.0"
DMG_NAME="Taey-Ed-${VERSION}"
DMG_PATH="$PROJECT_DIR/dist/${DMG_NAME}.dmg"
DMG_TEMP="$PROJECT_DIR/dist/dmg-staging"

# Verify app is signed
if ! codesign --verify "$APP_PATH" 2>/dev/null; then
    echo "WARNING: App is not signed. Run sign_and_notarize.sh first for distribution."
    echo "Continuing anyway for testing..."
fi

# ── Clean previous ──
rm -rf "$DMG_TEMP" "$DMG_PATH"
mkdir -p "$DMG_TEMP"

# ── Stage the DMG contents ──
echo "Staging DMG contents..."
cp -R "$APP_PATH" "$DMG_TEMP/"

# Create Applications symlink for drag-to-install
ln -s /Applications "$DMG_TEMP/Applications"

# ── Create DMG ──
echo "Creating DMG..."
hdiutil create -volname "$DMG_NAME" \
    -srcfolder "$DMG_TEMP" \
    -ov -format UDZO \
    "$DMG_PATH"

# ── Notarize the DMG too (if credentials available) ──
if xcrun notarytool history --keychain-profile "taey-ed-notary" &>/dev/null; then
    echo "Notarizing DMG..."
    xcrun notarytool submit "$DMG_PATH" \
        --keychain-profile "taey-ed-notary" \
        --wait
    xcrun stapler staple "$DMG_PATH"
    echo "DMG notarized and stapled."
else
    echo "Skipping DMG notarization (no stored credentials)."
fi

# ── Cleanup ──
rm -rf "$DMG_TEMP"

echo ""
echo "=== DMG Created ==="
echo "Path: $DMG_PATH"
echo "Size: $(du -h "$DMG_PATH" | cut -f1)"
echo ""
echo "Users install by:"
echo "  1. Download Taey-Ed-${VERSION}.dmg"
echo "  2. Drag Taey-Ed to Applications"
echo "  3. Create ~/.taey-ed/config.json with their API key + server URL"
echo "  4. Open Taey-Ed, grant Accessibility permission when prompted"
