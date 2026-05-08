#!/usr/bin/env python3
"""
Headless CLI launcher for Taey-Ed pipeline.

Usage:
    python3 run_cli.py PLATFORM [OPTIONS]

Examples:
    python3 run_cli.py coursera                    # Coursera, Chrome, unlimited
    python3 run_cli.py coursera --max-screens 5    # Stop after 5 screens
    python3 run_cli.py coursera --course myclass   # Specific course ID
    python3 run_cli.py khan_academy --app Safari   # Different browser

This runs the same pipeline as the GUI "Run Continuous" button but headless.
Logs go to stderr. Ctrl+C to stop gracefully.
"""
import argparse
import json
import logging
import signal
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

from app.pipeline import run_continuous


def main():
    parser = argparse.ArgumentParser(description='Taey-Ed headless automation')
    parser.add_argument('platform', nargs='?', default='coursera',
                        help='Platform key (default: coursera)')
    parser.add_argument('--app', default='Google Chrome',
                        help='macOS app name (default: Google Chrome)')
    parser.add_argument('--max-screens', type=int, default=0,
                        help='Stop after N screens (0=unlimited, default: 0)')
    parser.add_argument('--course', default='unknown',
                        help='Course ID (default: unknown)')
    parser.add_argument('--type', dest='platform_type', default='browser',
                        choices=['browser', 'app'],
                        help='Platform type (default: browser)')
    args = parser.parse_args()

    print(f'Taey-Ed CLI: platform={args.platform} app={args.app} '
          f'course={args.course} max_screens={args.max_screens}')
    print('Press Ctrl+C to stop')

    stop = threading.Event()

    # Graceful shutdown on SIGTERM (for kill from scripts)
    def _sigterm(signum, frame):
        print('\nSIGTERM received, stopping...')
        stop.set()
    signal.signal(signal.SIGTERM, _sigterm)

    def _screen_cb(completed, total):
        if total > 0:
            print(f'[progress] {completed}/{total} screens')
        else:
            print(f'[progress] {completed} screens')

    try:
        result = run_continuous(
            platform=args.platform,
            app_name=args.app,
            stop_event=stop,
            platform_type=args.platform_type,
            course_id=args.course,
            max_screens=args.max_screens,
            screen_callback=_screen_cb,
        )
        print(f'Result: {json.dumps(result, default=str)}')
    except KeyboardInterrupt:
        stop.set()
        print('\nStopped by user')


if __name__ == '__main__':
    main()
