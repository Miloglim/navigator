"""Message board contracts shared by storage, services and HTTP transport."""
from typing import TypedDict

MESSAGE_BOARD_ROUTE = "/api/message-board"
MESSAGE_BOARD_CREATE_ROUTE = "/api/message-board/create"
MESSAGE_BOARD_TABLE = "message_board_posts"
MESSAGE_BOARD_PAGE_SIZE = 30
MESSAGE_BOARD_PAGE_MAX = 100
MESSAGE_BOARD_CONTENT_MAX = 1000

MESSAGE_BOARD_DDL = """CREATE TABLE IF NOT EXISTS message_board_posts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL)"""


class MessageBoardPostInput(TypedDict):
    content: str


class MessageBoardPost(TypedDict):
    id: str
    user_id: str
    author_name: str
    content: str
    created_at: str
