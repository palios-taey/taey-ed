#!/usr/bin/env python3
"""
Taey-Ed V7 - Application Entry Point

CRITICAL: This file MUST be at project root for py2app accessibility to work.
Entry points inside packages (e.g., app/main.py) fail with AX error -25211.
"""

import logging
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

from app.ui.window import TaeyEdWindow


def main():
    """Main entry point."""
    window = TaeyEdWindow()
    window.run()


if __name__ == "__main__":
    main()
