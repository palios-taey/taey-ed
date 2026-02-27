"""
DEPRECATED: This file has been renamed to call_gemini.py (2026-02-27).

This shim re-exports everything for backward compatibility.
No Ollama models are used -- all generation is Gemini 2.5 Pro with Claude fallback.
"""
# Re-export everything from the renamed module
from spark.tasks.call_gemini import *  # noqa: F401, F403
