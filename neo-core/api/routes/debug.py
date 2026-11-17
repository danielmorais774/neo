"""Debug / Observability API routes — read-only access to MongoDB traces."""

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/debug", tags=["debug"])


def _get_obs():
    from observability.logger import get_obs
    return get_obs()


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    skip: int = Query(0, ge=0),
):
    """List debug sessions ordered by last activity."""
    obs = _get_obs()
    if not obs._available:
        return {"sessions": [], "total": 0, "mongo_available": False}

    cursor = obs._db.debug_sessions.find(
        {},
        {"_id": 0},
    ).sort("last_activity_at", -1).skip(skip).limit(limit)

    sessions = await cursor.to_list(length=limit)
    total = await obs._db.debug_sessions.count_documents({})
    return {"sessions": sessions, "total": total, "mongo_available": True}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single debug session with its message traces."""
    obs = _get_obs()
    if not obs._available:
        raise HTTPException(503, "MongoDB not available")

    session = await obs._db.debug_sessions.find_one(
        {"session_id": session_id}, {"_id": 0}
    )
    if not session:
        raise HTTPException(404, f"Session {session_id} not found")

    # Fetch traces for this session
    cursor = obs._db.message_traces.find(
        {"session_id": session_id},
        {"_id": 0},
    ).sort("started_at", 1)
    traces = await cursor.to_list(length=200)

    return {"session": session, "traces": traces}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str):
    """Get full detail of a single message trace."""
    obs = _get_obs()
    if not obs._available:
        raise HTTPException(503, "MongoDB not available")

    trace = await obs._db.message_traces.find_one({"_id": trace_id}, {"_id": 0})
    if not trace:
        raise HTTPException(404, f"Trace {trace_id} not found")

    trace["id"] = trace_id
    return trace


@router.get("/stats")
async def get_stats():
    """Global stats for the debug dashboard."""
    obs = _get_obs()
    if not obs._available:
        return {"mongo_available": False}

    total_sessions = await obs._db.debug_sessions.count_documents({})
    total_traces = await obs._db.message_traces.count_documents({})

    # Intent distribution
    pipeline = [
        {"$group": {"_id": "$intent_routing.intent", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    intent_cursor = obs._db.message_traces.aggregate(pipeline)
    intent_dist = {doc["_id"]: doc["count"] async for doc in intent_cursor}

    # Average LLM duration
    avg_pipeline = [
        {"$unwind": "$llm_calls"},
        {"$group": {"_id": None, "avg_ms": {"$avg": "$llm_calls.duration_ms"}}},
    ]
    avg_cursor = obs._db.message_traces.aggregate(avg_pipeline)
    avg_doc = await avg_cursor.to_list(length=1)
    avg_llm_ms = round(avg_doc[0]["avg_ms"], 1) if avg_doc else 0

    return {
        "mongo_available": True,
        "total_sessions": total_sessions,
        "total_traces": total_traces,
        "intent_distribution": intent_dist,
        "avg_llm_duration_ms": avg_llm_ms,
    }
