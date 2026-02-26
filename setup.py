"""
Taey-Ed V7 - py2app Setup

Build with:
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    ./venv/bin/python setup.py py2app

CRITICAL LEARNINGS (Phase 1):
1. Entry point MUST be at root level (run_ui.py), not inside package
2. argv_emulation MUST be False (True causes silent crash)
3. Use find_packages() for automatic package discovery
"""

from setuptools import setup, find_packages

APP = ["run_ui.py"]  # MUST be at root level for accessibility to work
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,  # MUST be False - True causes silent crash
    "packages": find_packages(),
    "plist": {
        "CFBundleName": "Taey-Ed",
        "CFBundleDisplayName": "Taey-Ed V7",
        "CFBundleVersion": "7.0.0",
        "CFBundleShortVersionString": "7.0.0",
        "CFBundleIdentifier": "com.paliostaey.taey-ed",
        "NSHighResolutionCapable": True,
        "NSAppleEventsUsageDescription": "Taey-Ed needs to control your Mac to automate educational platforms.",
        "NSAccessibilityUsageDescription": "Taey-Ed needs accessibility access to interact with educational platforms.",
        # API key and server URL now read from ~/.taey-ed/config.json
        # No secrets in the binary. See app/config.py.
    },
}

setup(
    name="Taey-Ed",
    version="7.0.0",
    packages=find_packages(),
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
