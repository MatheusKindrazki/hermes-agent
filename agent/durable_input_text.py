"""Normalize admitted input text without mutating conversation history."""
from typing import Any

def _turn_text(user_message: Any) -> str:
    """Flatten a provider-shaped message into the text that was actually said."""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = [
            str(block.get("text") or "")
            for block in user_message
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part)
    if isinstance(user_message, dict):
        return str(user_message.get("text") or user_message.get("content") or "")
    return ""
