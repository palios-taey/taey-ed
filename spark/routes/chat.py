"""
Chat history endpoint — serves persistent chat messages to the Mac app.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/chat/{platform}/history")
def get_chat_history(platform: str, limit: int = 50):
    from spark.tasks.chat_store import get_history
    return {"messages": get_history(platform, limit)}
