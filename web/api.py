import logging
import json
import os
import re
from collections import Counter
from datetime import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

import config
from parser import get_connection, get_conversations, get_messages, search_messages, get_conversation_stats
from scheduler import get_sync_state, do_sync, setup_scheduler

logger = logging.getLogger(__name__)

app = FastAPI(title="钉钉聊天记录导出", version="1.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Scheduler (initialized lazily)
_scheduler = None


@app.on_event("startup")
async def startup():
    global _scheduler
    _scheduler = setup_scheduler(app)
    _scheduler.start()
    logger.info("Scheduler started")


@app.on_event("shutdown")
async def shutdown():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown()
        logger.info("Scheduler stopped")


# --- Static files ---

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse("<h1>DingTalk Exporter</h1><p>Frontend not found</p>")


@app.get("/export-viewer/{name}", response_class=HTMLResponse)
async def export_viewer(name: str):
    """Serve the export JSON viewer page."""
    html_path = os.path.join(STATIC_DIR, "export_viewer.html")
    if os.path.exists(html_path):
        return FileResponse(html_path)
    return HTMLResponse("<h1>Export Viewer</h1><p>Viewer not found</p>")


# Mount static files
if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# --- API Routes ---

_export_data_cache = {}

_ID_TITLE_RE = re.compile(r"^\d+(?::\d+)?$")


def _safe_join(base_dir, *parts):
    """Join paths while preventing traversal outside base_dir."""
    base_path = os.path.abspath(os.path.normpath(base_dir))
    full_path = os.path.abspath(os.path.normpath(os.path.join(base_path, *parts)))
    if full_path != base_path and not full_path.startswith(base_path + os.sep):
        raise HTTPException(status_code=403, detail="Access denied")
    return full_path


def _get_export_json_path(name):
    export_path = _safe_join(config.EXPORT_DIR, name)
    if os.path.isdir(export_path):
        return _safe_join(export_path, "export.json")
    if export_path.endswith(".json"):
        return export_path
    return _safe_join(export_path, "export.json")


def _load_export_data(name):
    json_path = _get_export_json_path(name)
    if not os.path.exists(json_path):
        raise HTTPException(status_code=404, detail="Export JSON not found")

    stat = os.stat(json_path)
    cache_key = (json_path, stat.st_mtime, stat.st_size)
    cached = _export_data_cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"Invalid export JSON: {e}") from e

    _export_data_cache.clear()
    _export_data_cache[cache_key] = data
    return data


def _infer_export_user_uid(conversations):
    counts = Counter()
    right_side_counts = Counter()
    for conv in conversations:
        if conv.get("type") != "single":
            continue
        cid = str(conv.get("conversation_id", ""))
        if ":" not in cid:
            continue
        parts = [part for part in cid.split(":") if part]
        for part in parts:
            if part:
                counts[part] += 1
        if len(parts) >= 2:
            right_side_counts[parts[-1]] += 1
    if counts:
        best_uid, best_count = counts.most_common(1)[0]
        tied = [uid for uid, count in counts.items() if count == best_count]
        if len(tied) == 1:
            return best_uid
        for uid, _ in right_side_counts.most_common():
            if uid in tied:
                return uid
        return best_uid
    return str(config.USER_UID or "")


def _is_identifier_title(title):
    return not title or bool(_ID_TITLE_RE.fullmatch(str(title).strip()))


def _single_chat_display_title(conv, export_user_uid):
    raw_title = str(conv.get("title", "") or "").strip()
    cid = str(conv.get("conversation_id", "") or "").strip()
    if conv.get("type") != "single" or not _is_identifier_title(raw_title):
        return raw_title or cid

    messages = conv.get("messages", [])
    sender_names = {}
    for msg in messages:
        sender_id = str(msg.get("sender_id", "") or "")
        sender_name = str(msg.get("sender_name", "") or "").strip()
        if sender_id and sender_name:
            sender_names[sender_id] = sender_name

    cid_parts = [part for part in cid.split(":") if part]
    other_uids = [part for part in cid_parts if part != export_user_uid]
    if not other_uids and cid_parts:
        other_uids = [cid_parts[0]]

    for uid in other_uids:
        if sender_names.get(uid):
            return sender_names[uid]

    for msg in reversed(messages):
        sender_id = str(msg.get("sender_id", "") or "")
        sender_name = str(msg.get("sender_name", "") or "").strip()
        if sender_name and sender_id != export_user_uid:
            return sender_name

    for msg in reversed(messages):
        sender_name = str(msg.get("sender_name", "") or "").strip()
        if sender_name:
            return sender_name

    return raw_title or cid


def _message_timestamp(msg):
    created_at = msg.get("created_at", 0)
    if isinstance(created_at, (int, float)) and created_at:
        return created_at
    created_at_str = msg.get("created_at_str", "")
    if created_at_str:
        try:
            return int(datetime.strptime(created_at_str, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
        except (TypeError, ValueError):
            return 0
    return 0


def _summarize_conversation(conv, export_user_uid=None):
    messages = conv.get("messages", [])
    last_message = max(messages, key=_message_timestamp) if messages else {}
    title = conv.get("title", "") or conv.get("conversation_id", "")
    display_title = _single_chat_display_title(conv, export_user_uid or "")
    last_time = _message_timestamp(last_message)
    return {
        "conversation_id": conv.get("conversation_id", ""),
        "title": title,
        "display_title": display_title,
        "type": conv.get("type", ""),
        "member_count": conv.get("member_count", 0),
        "message_count": len(messages),
        "last_time": last_time,
        "last_time_str": last_message.get("created_at_str", ""),
        "last_sender": last_message.get("sender_name", ""),
        "last_content": (
            last_message.get("content")
            or last_message.get("text")
            or last_message.get("content_type_name")
            or ""
        )[:160],
    }

@app.get("/api/config")
async def api_config():
    """Return public configuration for the frontend."""
    return {"user_uid": config.USER_UID}


@app.get("/api/export-viewer/{name}")
async def api_export_viewer_data(name: str):
    """Return a previously exported export.json for browser viewing."""
    return _load_export_data(name)


@app.get("/api/export-viewer/{name}/summary")
async def api_export_viewer_summary(name: str):
    """Return export metadata and conversation summaries without message bodies."""
    data = _load_export_data(name)
    conversations = data.get("conversations", [])
    export_user_uid = _infer_export_user_uid(conversations)
    summaries = [
        _summarize_conversation(c, export_user_uid) for c in conversations
    ]
    summaries.sort(key=lambda c: c.get("last_time", 0), reverse=True)
    return {
        "export_time": data.get("export_time", ""),
        "export_type": data.get("export_type", ""),
        "total_conversations": len(conversations),
        "total_messages": sum(len(c.get("messages", [])) for c in conversations),
        "conversations": summaries,
    }


@app.get("/api/export-viewer/{name}/messages")
async def api_export_viewer_messages(
    name: str,
    cid: str = Query(..., min_length=1),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """Return paginated messages for one exported conversation."""
    data = _load_export_data(name)
    export_user_uid = _infer_export_user_uid(data.get("conversations", []))
    for conv in data.get("conversations", []):
        if str(conv.get("conversation_id", "")) == cid:
            messages = conv.get("messages", [])
            page = messages[offset:offset + limit]
            return {
                "conversation": _summarize_conversation(conv, export_user_uid),
                "total": len(messages),
                "limit": limit,
                "offset": offset,
                "messages": page,
            }
    raise HTTPException(status_code=404, detail="Conversation not found")


@app.get("/api/export-viewer/{name}/files/{path:path}")
async def api_export_viewer_file(name: str, path: str):
    """Serve files referenced by an export directory."""
    export_dir = _safe_join(config.EXPORT_DIR, name)
    if not os.path.isdir(export_dir):
        raise HTTPException(status_code=404, detail="Export directory not found")
    full_path = _safe_join(export_dir, path)
    if not os.path.exists(full_path) or not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(full_path)


@app.get("/api/conversations")
async def api_conversations(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    keyword: str = Query(None),
):
    conn = get_connection()
    try:
        result = get_conversations(conn, limit=limit, offset=offset, keyword=keyword)
        return result
    finally:
        conn.close()


@app.get("/api/conversations/{cid}/messages")
async def api_messages(
    cid: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    since: int = Query(None, description="Since timestamp (ms)"),
    until: int = Query(None, description="Until timestamp (ms)"),
):
    conn = get_connection()
    try:
        result = get_messages(conn, cid, limit=limit, offset=offset, since_time=since, until_time=until)
        return result
    finally:
        conn.close()


@app.get("/api/search")
async def api_search(
    q: str = Query(..., min_length=1),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    conn = get_connection()
    try:
        results = search_messages(conn, q, limit=limit, offset=offset)
        return {"query": q, "total": len(results), "messages": results}
    finally:
        conn.close()


@app.get("/api/stats")
async def api_stats():
    conn = get_connection()
    try:
        stats = get_conversation_stats(conn)
        return stats
    finally:
        conn.close()


@app.get("/api/sync/status")
async def api_sync_status():
    state = get_sync_state()
    return state


@app.post("/api/sync/trigger")
async def api_sync_trigger(full: bool = Query(False)):
    state = get_sync_state()
    if state.get("is_syncing"):
        raise HTTPException(status_code=409, detail="Sync already in progress")

    # Run sync in background thread to avoid blocking
    import threading
    thread = threading.Thread(target=do_sync, kwargs={"full": full}, daemon=True)
    thread.start()

    return {"status": "started", "full": full}


@app.post("/api/export/selected")
async def api_export_selected(body: dict):
    """Export only the selected conversation IDs as JSON."""
    cids = body.get("cids", [])
    since_time = body.get("since_time")  # optional ms timestamp
    if not cids:
        raise HTTPException(status_code=400, detail="No conversations selected")

    import threading

    thread = threading.Thread(
        target=_do_export_selected, args=(cids, since_time), daemon=True
    )
    thread.start()
    return {"status": "started", "selected_count": len(cids)}


def _do_export_selected(cids, since_time=None):
    """Run the selected export in a background thread."""
    import scheduler as sched
    sched._sync_state["is_syncing"] = True
    sched._sync_state["last_error"] = None
    try:
        from exporter import export_by_cids
        path = export_by_cids(cids, since_time=since_time)
        sched._sync_state["last_export_path"] = path
        sched._sync_state["sync_count"] += 1
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Selected export failed: {e}", exc_info=True)
        sched._sync_state["last_error"] = str(e)
    finally:
        sched._sync_state["is_syncing"] = False


@app.get("/api/attachments/{path:path}")
async def api_attachment(path: str):
    """Serve attachment files from the DingTalk data directory."""
    # Security: only allow access to specific subdirectories
    allowed_dirs = ["ImageFiles", "AudioFiles", "VideoFiles", "resource_cache"]
    parts = path.replace("\\", "/").split("/")
    if parts[0] not in allowed_dirs:
        raise HTTPException(status_code=403, detail="Access denied")

    full_path = os.path.join(config.DINGTALK_DATA_DIR, path)
    full_path = os.path.normpath(full_path)

    # Security: ensure the path doesn't escape the data directory
    if not full_path.startswith(os.path.normpath(config.DINGTALK_DATA_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(full_path)


@app.get("/api/local-file")
async def api_local_file(path: str = Query(..., min_length=1)):
    """Serve a local file from an absolute path (for downloaded attachments)."""
    try:
        full_path = os.path.normpath(path)

        # Security: only allow local drive paths, no UNC
        if full_path.startswith("\\\\"):
            raise HTTPException(status_code=403, detail="UNC paths not allowed")

        # Only allow common document/file extensions
        ext = os.path.splitext(full_path)[1].lower()
        allowed_exts = {
            ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".pdf",
            ".txt", ".csv", ".json", ".xml", ".html", ".htm",
            ".zip", ".rar", ".7z", ".gz", ".tar",
            ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
            ".mp4", ".mp3", ".wav", ".avi", ".m4a",
        }
        if ext not in allowed_exts:
            raise HTTPException(status_code=403, detail=f"File type '{ext}' not allowed")

        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="File not found")

        filename = os.path.basename(full_path)
        return FileResponse(full_path, filename=filename)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"local-file error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/exports/{name}/download")
async def api_export_download(name: str):
    """Download an export as a zip file."""
    import zipfile
    import tempfile
    import io as _io

    export_path = os.path.join(config.EXPORT_DIR, name)
    export_path = os.path.normpath(export_path)
    if not export_path.startswith(os.path.normpath(config.EXPORT_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(export_path):
        raise HTTPException(status_code=404, detail="Export not found")

    # If it's a directory, zip it
    if os.path.isdir(export_path):
        zip_filename = f"{name}.zip"
        # Create zip in memory
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(export_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, config.EXPORT_DIR)
                    zf.write(file_path, arcname)
        buf.seek(0)

        from fastapi.responses import StreamingResponse
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={zip_filename}"},
        )

    # If it's a single file (legacy JSON export)
    return FileResponse(
        export_path,
        media_type="application/json",
        filename=os.path.basename(export_path),
    )


@app.get("/api/exports/{filename}")
async def api_export_file(filename: str):
    """Download an exported JSON file (legacy single-file exports)."""
    filepath = os.path.join(config.EXPORT_DIR, filename)
    filepath = os.path.normpath(filepath)
    if not filepath.startswith(os.path.normpath(config.EXPORT_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(filepath, media_type="application/json", filename=filename)


@app.get("/api/exports")
async def api_list_exports():
    """List all exports (directories and legacy JSON files)."""
    exports = []
    if os.path.isdir(config.EXPORT_DIR):
        for f in sorted(os.listdir(config.EXPORT_DIR), reverse=True):
            fp = os.path.join(config.EXPORT_DIR, f)
            if f == "latest.json":
                continue
            if os.path.isdir(fp):
                # Directory-type export (new format)
                json_path = os.path.join(fp, "export.json")
                if os.path.exists(json_path):
                    exports.append({
                        "filename": f,
                        "type": "directory",
                        "size": os.path.getsize(json_path),
                        "modified": os.path.getmtime(fp),
                        "download_url": f"/api/exports/{f}/download",
                        "view_url": f"/export-viewer/{f}",
                    })
            elif f.endswith(".json"):
                # Legacy single-file export
                exports.append({
                    "filename": f,
                    "type": "file",
                    "size": os.path.getsize(fp),
                    "modified": os.path.getmtime(fp),
                    "download_url": f"/api/exports/{f}",
                    "view_url": f"/export-viewer/{f}",
                })
    return {"exports": exports}
