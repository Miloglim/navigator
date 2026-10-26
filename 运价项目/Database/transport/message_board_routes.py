"""HTTP adapters for the authenticated user message board."""
import uuid

from core.contracts import ok
from core.message_board_contracts import (MESSAGE_BOARD_CREATE_ROUTE, MESSAGE_BOARD_PAGE_MAX,
    MESSAGE_BOARD_PAGE_SIZE, MESSAGE_BOARD_ROUTE, MESSAGE_BOARD_TABLE)
from services.message_board import message_content, post_row


def build_message_board_routes(connect, now, log):
    def listing(query):
        limit = max(1, min(int(query.get("limit") or MESSAGE_BOARD_PAGE_SIZE), MESSAGE_BOARD_PAGE_MAX))
        offset = max(0, int(query.get("offset") or 0))
        conn = connect()
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM {MESSAGE_BOARD_TABLE}").fetchone()[0]
            rows = [post_row(dict(row)) for row in conn.execute(
                f"SELECT * FROM {MESSAGE_BOARD_TABLE} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (limit, offset))]
            return ok({"rows": rows, "total": total, "limit": limit, "offset": offset})
        finally:
            conn.close()

    def create(body):
        actor_id = str(body.get("_actor_id") or "").strip()
        author_name = str(body.get("_actor_display") or body.get("_actor") or "").strip()
        if not actor_id or not author_name:
            raise PermissionError("登录身份无效")
        content = message_content(body.get("content"))
        post_id, stamp = "msg_" + uuid.uuid4().hex, now()
        conn = connect()
        try:
            conn.execute(
                f"INSERT INTO {MESSAGE_BOARD_TABLE}(id,user_id,author_name,content,created_at) VALUES(?,?,?,?,?)",
                (post_id, actor_id, author_name, content, stamp))
            conn.commit()
            log("INFO", f"留言发布 {post_id} by {actor_id}")
            return ok({"post": {"id": post_id, "user_id": actor_id, "author_name": author_name,
                                  "content": content, "created_at": stamp}})
        finally:
            conn.close()

    return {MESSAGE_BOARD_ROUTE: listing}, {MESSAGE_BOARD_CREATE_ROUTE: create}
