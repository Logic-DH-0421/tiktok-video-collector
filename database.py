from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from config_store import app_data_dir
from diagnostics import log
from feed_metrics import stored_metric


DB_PATH = app_data_dir() / "videos.db"


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL DEFAULT '',
                file_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                duration_seconds REAL NOT NULL DEFAULT 0,
                category TEXT NOT NULL DEFAULT '其他',
                downloaded_at TEXT NOT NULL,
                likes INTEGER NOT NULL DEFAULT 0,
                comments INTEGER NOT NULL DEFAULT 0,
                favorites INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT '已完成',
                progress INTEGER NOT NULL DEFAULT 100,
                status_message TEXT NOT NULL DEFAULT ''
            )
            """
        )
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(videos)")}
        for name, definition in {
            "status": "TEXT NOT NULL DEFAULT '已完成'",
            "progress": "INTEGER NOT NULL DEFAULT 100",
            "status_message": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_downloaded_at ON videos(downloaded_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_category ON videos(category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status)")
        recovered = conn.execute(
            "UPDATE videos SET status = '失败', progress = 0, status_message = '上次运行中断' WHERE status IN ('正在下载', '等待人工处理')"
        )
        count = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    log("INFO", "database", "database.initialized", "Database schema ready", schema_version=1, record_count=count)
    if recovered.rowcount:
        log("WARN", "database", "database.interrupted_recovery", "Prior in-progress records marked interrupted", record_count=recovered.rowcount)


def source_exists(url: str) -> bool:
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM videos WHERE source_url = ? AND status = '已完成'", (url,)).fetchone()
        if row is not None:return True
        key=extract_video_id(url)
        if key=='unknown':return False
        rows=conn.execute("SELECT source_url FROM videos WHERE status='已完成' AND source_url LIKE ?",
                          ('%/video/'+key+'%',)).fetchall()
        return any(extract_video_id(row['source_url'])==key for row in rows)


def add_candidate(data: dict[str, Any]) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO videos
            (source_url, title, file_path, size_bytes, duration_seconds, category,
             downloaded_at, likes, comments, favorites, status, progress, status_message)
            VALUES (?, ?, '', 0, ?, ?, ?, ?, ?, ?, '排队中', 0, '等待下载')
            ON CONFLICT(source_url) DO UPDATE SET
              title = excluded.title,
              duration_seconds = CASE WHEN videos.status = '已完成' THEN videos.duration_seconds ELSE excluded.duration_seconds END,
              likes = excluded.likes,
              comments = excluded.comments,
              favorites = excluded.favorites,
              status = CASE WHEN videos.status = '已完成' THEN videos.status ELSE '排队中' END,
              progress = CASE WHEN videos.status = '已完成' THEN videos.progress ELSE 0 END,
              status_message = CASE WHEN videos.status = '已完成' THEN videos.status_message ELSE '等待下载' END
            """,
            (
                data["source_url"], data.get("title", ""), float(data.get("duration_seconds", 0)),
                data.get("category", "其他"), now, stored_metric(data.get("likes", 0)),
                stored_metric(data.get("comments", 0)), stored_metric(data.get("favorites", 0)),
            ),
        )
        row = conn.execute("SELECT id FROM videos WHERE source_url = ?", (data["source_url"],)).fetchone()
        record_id = int(row["id"])
    log("INFO", "database", "candidate.queued", "Candidate persisted", record_id=record_id,
        video_id=extract_video_id(data["source_url"]), likes=data.get("likes"),
        comments=data.get("comments"), favorites=data.get("favorites"))
    return record_id


def extract_video_id(url: str) -> str:
    import re
    match = re.search(r"/video/(\d+)", url)
    return match.group(1) if match else "unknown"


def update_video_status(video_id: int, status: str, progress: int, message: str = "") -> None:
    with connect() as conn:
        old = conn.execute("SELECT status, progress FROM videos WHERE id = ?", (video_id,)).fetchone()
        updated = conn.execute(
            "UPDATE videos SET status = ?, progress = ?, status_message = ? WHERE id = ?",
            (status, max(0, min(100, int(progress))), message, video_id),
        )
    if old and old["status"] != status:
        log("INFO", "database", "video.status_changed", "Video status changed", record_id=video_id,
            previous_status=old["status"], new_status=status, progress=progress, result="updated")
    elif updated.rowcount == 0:
        log("WARN", "database", "video.status_missing", "Status update found no record", record_id=video_id)


def query_pending_candidates() -> list[sqlite3.Row]:
    with connect() as conn:
        return list(
            conn.execute(
                """SELECT * FROM videos
                   WHERE status IN ('排队中', '失败')
                   ORDER BY id ASC"""
            ).fetchall()
        )


def add_video(data: dict[str, Any]) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO videos
            (source_url, title, file_path, size_bytes, duration_seconds, category,
             downloaded_at, likes, comments, favorites, status, progress, status_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '已完成', 100, '下载完成')
            ON CONFLICT(source_url) DO UPDATE SET
              title = excluded.title, file_path = excluded.file_path,
              size_bytes = excluded.size_bytes, duration_seconds = excluded.duration_seconds,
              category = excluded.category, downloaded_at = excluded.downloaded_at,
              likes = excluded.likes, comments = excluded.comments, favorites = excluded.favorites,
              status = '已完成', progress = 100, status_message = '下载完成'
            """,
            (
                data["source_url"], data.get("title", ""), data["file_path"],
                int(data.get("size_bytes", 0)), float(data.get("duration_seconds", 0)),
                data.get("category", "其他"), data.get("downloaded_at", datetime.now().isoformat(timespec="seconds")),
                stored_metric(data.get("likes", 0)), stored_metric(data.get("comments", 0)), stored_metric(data.get("favorites", 0)),
            ),
        )
        row = conn.execute("SELECT id FROM videos WHERE source_url = ?", (data["source_url"],)).fetchone()
        record_id = int(row["id"])
    log("INFO", "database", "video.completed_persisted", "Completed video record committed", record_id=record_id,
        size_bytes=int(data.get("size_bytes", 0)), category=data.get("category", "其他"))
    return record_id


def query_videos(filters: dict[str, Any] | None = None) -> list[sqlite3.Row]:
    filters = filters or {}
    clauses: list[str] = []
    args: list[Any] = []
    if text := str(filters.get("title", "")).strip():
        clauses.append("title LIKE ?")
        args.append(f"%{text}%")
    if category := str(filters.get("category", "")).strip():
        if category != "全部":
            clauses.append("category = ?")
            args.append(category)
    if status := str(filters.get("status", "")).strip():
        if status != "全部":
            clauses.append("status = ?")
            args.append(status)
    for key, col, factor in [
        ("min_size", "size_bytes", 1024 * 1024), ("max_size", "size_bytes", 1024 * 1024),
        ("min_duration", "duration_seconds", 1), ("max_duration", "duration_seconds", 1),
    ]:
        val = filters.get(key)
        if val not in (None, ""):
            clauses.append(f"{col} {'>=' if key.startswith('min_') else '<='} ?")
            args.append(float(val) * factor)
    if date_from := str(filters.get("date_from", "")).strip():
        clauses.append("downloaded_at >= ?")
        args.append(date_from)
    if date_to := str(filters.get("date_to", "")).strip():
        clauses.append("downloaded_at <= ?")
        args.append(date_to + "T23:59:59")
    sql = "SELECT * FROM videos"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY downloaded_at DESC, id DESC"
    with connect() as conn:
        return list(conn.execute(sql, args).fetchall())


def summarize_videos() -> dict[str, int]:
    """Small read-only status summary for the UI footer."""
    with connect() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS count FROM videos GROUP BY status").fetchall()
    summary = {str(row["status"]): int(row["count"]) for row in rows}
    summary["total"] = sum(summary.values())
    return summary


def update_category(video_id: int, category: str, new_path: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE videos SET category = ?, file_path = ? WHERE id = ?", (category, new_path, video_id))


def delete_missing_records() -> int:
    rows = query_videos()
    missing = [int(row["id"]) for row in rows if row["status"] == "已完成" and not Path(row["file_path"]).exists()]
    if not missing:
        return 0
    with connect() as conn:
        conn.executemany("DELETE FROM videos WHERE id = ?", [(item,) for item in missing])
    return len(missing)


def delete_videos(video_ids: list[int]) -> int:
    if not video_ids:
        return 0
    with connect() as conn:
        before = conn.total_changes
        conn.executemany("DELETE FROM videos WHERE id = ?", [(int(item),) for item in video_ids])
        return conn.total_changes - before


def clear_videos() -> int:
    """Delete all list records only; downloaded files are intentionally untouched."""
    with connect() as conn:
        before = conn.total_changes
        conn.execute("DELETE FROM videos")
        return conn.total_changes - before
