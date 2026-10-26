"""Pure message-board text validation and response shaping."""
from core.message_board_contracts import MESSAGE_BOARD_CONTENT_MAX


def message_content(value):
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("留言内容不能为空")
    if len(text) > MESSAGE_BOARD_CONTENT_MAX:
        raise ValueError(f"留言不能超过 {MESSAGE_BOARD_CONTENT_MAX} 个字符")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ValueError("留言包含不支持的控制字符")
    return text


def post_row(row):
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "author_name": row["author_name"],
        "content": row["content"],
        "created_at": row["created_at"],
    }
