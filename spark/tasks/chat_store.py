"""
Chat message store — Redis sorted sets for persistent chat history.

Messages are stored per-platform with Unix timestamp as score.
Enables the Mac chat panel to load history on restart and
receive new messages via /next_action responses.
"""

import json
import time
import uuid

import redis

_redis = redis.Redis(host="192.168.100.10", port=6379, decode_responses=True)


def _key(platform: str) -> str:
    return f"taey:chat:{platform}:messages"


def store_message(platform: str, message: dict) -> str:
    """Store a chat message. Returns the message ID."""
    if "id" not in message:
        message["id"] = f"cm-{uuid.uuid4().hex[:8]}"
    if "timestamp" not in message:
        message["timestamp"] = time.time()
    _redis.zadd(_key(platform), {json.dumps(message): message["timestamp"]})
    return message["id"]


def get_history(platform: str, limit: int = 50) -> list[dict]:
    """Get recent chat messages, oldest first."""
    raw = _redis.zrange(_key(platform), -limit, -1)
    messages = []
    for item in raw:
        try:
            messages.append(json.loads(item))
        except json.JSONDecodeError:
            continue
    return messages


def build_status(text: str, **metadata) -> dict:
    """Build a system status message."""
    msg = {
        "id": f"cm-{uuid.uuid4().hex[:8]}",
        "sender": "system",
        "text": text,
        "timestamp": time.time(),
        "msg_type": "status",
    }
    msg.update(metadata)
    return msg


def build_question(text: str, **metadata) -> dict:
    """Build a system question message (expects user response)."""
    msg = {
        "id": f"cm-{uuid.uuid4().hex[:8]}",
        "sender": "system",
        "text": text,
        "timestamp": time.time(),
        "msg_type": "question",
    }
    msg.update(metadata)
    return msg


def build_user_message(text: str) -> dict:
    """Build a user message."""
    return {
        "id": f"cm-{uuid.uuid4().hex[:8]}",
        "sender": "user",
        "text": text,
        "timestamp": time.time(),
        "msg_type": "answer",
    }
