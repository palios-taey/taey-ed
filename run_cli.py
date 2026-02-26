#!/usr/bin/env python3
"""Headless CLI launcher for Taey-Ed pipeline testing."""
import logging
import sys
import threading

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S'
)

from app.pipeline import run_continuous

platform = sys.argv[1] if len(sys.argv) > 1 else 'khan_academy'
app_name = sys.argv[2] if len(sys.argv) > 2 else 'Google Chrome'
max_screens = int(sys.argv[3]) if len(sys.argv) > 3 else 10

print(f'Starting: platform={platform}, app={app_name}, max_screens={max_screens}')
print('Press Ctrl+C to stop')

stop = threading.Event()
try:
    result = run_continuous(
        platform=platform,
        app_name=app_name,
        stop_event=stop,
        platform_type='browser',
        course_id='hs-chemistry',
        max_screens=max_screens,
    )
    print(f'Result: {result}')
except KeyboardInterrupt:
    stop.set()
    print('Stopped by user')
