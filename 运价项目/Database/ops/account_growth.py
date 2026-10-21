"""Session-bound, append-only account experience persistence."""
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from core.contracts import ok
from core.growth_contracts import (GROWTH_APPROVAL_EVENT, GROWTH_APPROVAL_POINTS,
                                   GROWTH_CORRECTION_EVENT, GROWTH_CORRECTION_POINTS,
                                   GROWTH_DAILY_ACTIVITY_POINTS, GROWTH_DAILY_CAP,
                                   GROWTH_GOVERNANCE_POINTS,
                                   GROWTH_HISTORY_SIZE,
                                   GROWTH_LEGACY_SEARCH_EVENT, GROWTH_REVIEW_EVENT,
                                   GROWTH_RESET_EVENT,
                                   GROWTH_RULE_VERSION, GROWTH_SEARCH_EVENT,
                                   GROWTH_SEARCH_POINTS)
from services.account_growth import (GROWTH_XP_PER_CYCLE, award_feedback,
                                     map_legacy_xp, pagination, summary)

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _today():
    return datetime.now(BUSINESS_TIMEZONE).date().isoformat()


def _read(connection, user_id, day, limit, offset):
    totals = connection.execute(
        "SELECT COALESCE(SUM(CASE WHEN rule_version=1 THEN points ELSE 0 END),0) AS legacy_xp,"
        "COALESCE(SUM(CASE WHEN rule_version>=2 THEN CASE WHEN event_type=? THEN -points ELSE points END ELSE 0 END),0) AS current_xp "
        "FROM account_experience_events WHERE user_id=?", (GROWTH_RESET_EVENT,user_id)).fetchone()
    total = map_legacy_xp(totals["legacy_xp"]) + int(totals["current_xp"])
    daily_earned = int(connection.execute(
        "SELECT COALESCE(SUM(points),0) FROM account_experience_events "
        "WHERE user_id=? AND rule_version>=2 AND event_type<>? AND created_at LIKE ?",
        (user_id, GROWTH_RESET_EVENT, day + "%")).fetchone()[0])
    awarded = daily_earned >= GROWTH_DAILY_CAP or bool(connection.execute(
        "SELECT 1 FROM account_experience_events WHERE user_id=? AND event_type IN (?,?) AND subject_key=?",
        (user_id, GROWTH_LEGACY_SEARCH_EVENT, GROWTH_SEARCH_EVENT, day)).fetchone())
    count = connection.execute("SELECT COUNT(*) FROM account_experience_events WHERE user_id=?", (user_id,)).fetchone()[0]
    events = [dict(row) for row in connection.execute(
        "SELECT event_id,event_type,subject_key,points,created_at FROM account_experience_events "
        "WHERE user_id=? ORDER BY created_at DESC,event_id DESC LIMIT ? OFFSET ?", (user_id, limit, offset))]
    for event in events:
        if event['event_type'] == GROWTH_RESET_EVENT:
            event['points'] = -int(event['points'])
    return {"summary": summary(total, awarded), "history": events, "total": count,
            "today_xp": daily_earned,
            "limit": limit, "offset": offset, "rule": {
                "version": GROWTH_RULE_VERSION,
                "daily_search_points": GROWTH_SEARCH_POINTS,
                "daily_activity_points": GROWTH_DAILY_ACTIVITY_POINTS,
                "approval_points": GROWTH_APPROVAL_POINTS,
                "correction_points": GROWTH_CORRECTION_POINTS,
                "governance_points": GROWTH_GOVERNANCE_POINTS,
                "daily_cap": GROWTH_DAILY_CAP,
                "cycle_xp": GROWTH_XP_PER_CYCLE}}


def _record(connection, user_id, event_type, subject_key, points, stamp, daily_cap=GROWTH_DAILY_CAP):
    if not user_id or not subject_key or points <= 0:
        return {"awarded": False, "points": 0}
    if connection.execute(
            "SELECT 1 FROM account_experience_events WHERE user_id=? AND event_type=? AND subject_key=?",
            (user_id, event_type, subject_key)).fetchone():
        return {"awarded": False, "points": 0}
    day = stamp[:10]
    earned = int(connection.execute(
        "SELECT COALESCE(SUM(points),0) FROM account_experience_events "
        "WHERE user_id=? AND rule_version>=2 AND event_type<>? AND created_at LIKE ?",
        (user_id, GROWTH_RESET_EVENT, day + "%")).fetchone()[0])
    granted = int(points) if daily_cap is None else min(int(points), max(0, int(daily_cap) - earned))
    if granted <= 0:
        return {"awarded": False, "points": 0}
    inserted = connection.execute(
        "INSERT INTO account_experience_events(event_id,user_id,event_type,subject_key,points,rule_version,created_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,event_type,subject_key) DO NOTHING",
        ("xp_" + uuid.uuid4().hex, user_id, event_type, subject_key,
         granted, GROWTH_RULE_VERSION, stamp))
    return {"awarded": inserted.rowcount == 1,
            "points": granted if inserted.rowcount == 1 else 0}


def award_business_activity(connection, user_id, activity_event, subjects=(), now=None, activity_daily_cap=GROWTH_DAILY_CAP):
    """Record verified server-side work without letting XP failure alter business data."""
    stamp = now or datetime.now(BUSINESS_TIMEZONE).isoformat(timespec="seconds")
    day = stamp[:10]
    engine = getattr(connection, "engine", "sqlite")
    if engine == "postgres":
        connection.execute("SELECT user_id FROM operators WHERE user_id=? FOR UPDATE", (user_id,)).fetchone()
    elif not getattr(connection, "in_transaction", False):
        connection.execute("BEGIN IMMEDIATE")
    result = _record(connection, user_id, activity_event, day,
                     GROWTH_DAILY_ACTIVITY_POINTS, stamp, activity_daily_cap)
    awarded = result["points"]
    for event_type, subject_key, points in subjects:
        cap = None if event_type in (GROWTH_APPROVAL_EVENT, GROWTH_CORRECTION_EVENT) else GROWTH_DAILY_CAP
        awarded += _record(connection, user_id, event_type, str(subject_key), points, stamp, cap)["points"]
    connection.commit()
    state = _read(connection, user_id, day, 1, 0)
    return award_feedback(state["summary"], awarded, state["today_xp"],
                          GROWTH_DAILY_CAP)


def read(connect, session, query, today=_today):
    limit, offset = pagination(query)
    connection = connect()
    try:
        return ok(_read(connection, session["user_id"], today(), limit, offset))
    finally:
        connection.close()


def award_search(connect, session, body, today=_today):
    if body:
        raise ValueError("growth award accepts no client fields")
    day = today()
    connection = connect()
    try:
        user_id = session["user_id"]
        legacy_awarded = connection.execute(
            "SELECT 1 FROM account_experience_events WHERE user_id=? AND event_type=? AND subject_key=?",
            (user_id, GROWTH_LEGACY_SEARCH_EVENT, day)).fetchone()
        current_stamp = datetime.now(BUSINESS_TIMEZONE).isoformat(timespec="seconds")
        business_stamp = day + current_stamp[10:]
        award = {"awarded": False, "points": 0} if legacy_awarded else _record(
            connection, user_id, GROWTH_SEARCH_EVENT, day, GROWTH_SEARCH_POINTS,
            business_stamp)
        connection.commit()
        result = _read(connection, user_id, day, GROWTH_HISTORY_SIZE, 0)
        result["awarded"] = award["awarded"]
        result["awarded_points"] = award["points"]
        result["growth_award"] = award_feedback(
            result["summary"], award["points"], result["today_xp"],
            GROWTH_DAILY_CAP)
        return ok(result)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
