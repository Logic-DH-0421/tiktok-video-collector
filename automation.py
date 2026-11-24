from __future__ import annotations

import asyncio
import contextlib
import math
import os
import queue
import re
import shutil
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from classifier import categorized_path, classify_text
from config_store import AppConfig, app_data_dir, edge_executable
from database import add_candidate, add_video, query_pending_candidates, source_exists, update_video_status
from diagnostics import bind, finish as log_finish, log, new_id
from feed_metrics import CARD_SCRIPT, RecoveryGuard, api_items, normalize_api_item, snapshot_candidate, required_metrics, counter_value
from feed_evidence import EVIDENCE_SCRIPT, recover_share_link


TIKTOK_URL = "https://www.tiktok.com/"
SAVESORA_URL = "https://savesora.com/zh-cn/tiktok"


class ManualAttentionRequired(RuntimeError):
    """Raised only when the page visibly asks a human to pass a challenge."""


class RemoteUrlDownload:
    """Playwright-download compatible adapter for SaveSora direct links."""

    def __init__(self, url: str, headers: dict[str, str], operation_id: str | None = None, task_id: str | None = None):
        self.url = url
        self.headers = headers
        self.operation_id = operation_id
        self.task_id = task_id
        self.transfer_id = new_id()
        filename = Path(unquote(urlsplit(url).path)).name
        self.suggested_filename = filename if "." in filename else "tiktok_video.mp4"

    async def save_as(self, path: str) -> None:
        await asyncio.to_thread(self._stream_to_file, path)

    def _stream_to_file(self, path: str) -> None:
        began = time.monotonic()
        log("INFO", "network", "media_transfer.started", "Direct media transfer started", operation_id=self.transfer_id, parent_operation_id=self.operation_id, task_id=self.task_id, url=self.url)
        size = 0
        try:
            request = Request(self.url, headers=self.headers)
            with urlopen(request, timeout=120) as response, open(path, "wb") as output:
                content_type = str(response.headers.get("Content-Type", "")).lower()
                if "text/html" in content_type:
                    raise RuntimeError("SaveSora 返回了网页而不是视频文件")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    size += len(chunk)
            log_finish("media_transfer", "network", self.transfer_id, began, "success", "Direct media transfer completed", parent_operation_id=self.operation_id, task_id=self.task_id, size_bytes=size)
        except Exception as exc:
            log_finish("media_transfer", "network", self.transfer_id, began,
                       "timeout" if isinstance(exc, TimeoutError) else "failed", "Direct media transfer failed", parent_operation_id=self.operation_id, task_id=self.task_id, error=exc)
            raise


def compact_number(value: str | int | float | None) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").replace(" ", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kKmMwW万千]?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    suffix = match.group(2).lower()
    factor = {"k": 1_000, "m": 1_000_000, "w": 10_000, "万": 10_000, "千": 1_000}.get(suffix, 1)
    return int(number * factor)


class AutomationWorker(threading.Thread):
    def __init__(self, config: AppConfig, events: queue.Queue[dict[str, Any]], operation_id: str | None = None):
        super().__init__(daemon=True)
        self.config = config
        self.events = events
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.checkpoint_event = threading.Event()
        self.skip_current_event = threading.Event()
        self.checkpoint_decision = False
        self.completed = 0
        self._batch_target = max(1, int(config.checkpoint_every))
        self._batch_accepted = 0
        self._batch_finished = 0
        self._batch_succeeded = 0
        self._batch_checkpoint_open = False
        self._restart_allowed = True
        # TikTok scanning and SaveSora downloading intentionally live in
        # separate persistent Edge contexts.  Sharing one context made a
        # SaveSora navigation contend with the scrolling TikTok renderer.
        self._browser_contexts: list[Any] = []
        self._manual_page = None
        self._download_active = False
        self._current_record_id = 0
        self._active_record_ids: set[int] = set()
        self._cancelled_record_ids: set[int] = set()
        self._skip_record_ids: set[int] = set()
        self.operation_id = operation_id or new_id()
        self.task_id = new_id()
        self._started_at = time.monotonic()
        self._terminal_outcome = "cancelled"
        self._admitted_urls: set[str] = set()
        self._api_metric_cache: dict[str, dict[str, Any]] = {}
        self._last_metric_snapshot: dict[str, Any] = {}
        self._feed_recovery = RecoveryGuard(time.monotonic())
        self._share_attempts: set[str] = set()
        self._identity_failure_count = 0
        self._metrics_logged: set[str] = set()

    def emit(self, kind: str, **data: Any) -> None:
        self.events.put({"kind": kind, **data})

    def stop(self) -> None:
        log("INFO", "automation", "task.stop_requested", "Stop requested", operation_id=self.operation_id, task_id=self.task_id)
        self._restart_allowed = False
        self.stop_event.set()
        self.pause_event.set()
        self.checkpoint_event.set()

    def pause(self) -> None:
        log("INFO", "automation", "task.paused", "Task paused by user", operation_id=self.operation_id, task_id=self.task_id)
        self.pause_event.clear()
        self.emit("status", text="已暂停")

    def resume(self) -> None:
        log("INFO", "automation", "task.resumed", "Task resumed by user", operation_id=self.operation_id, task_id=self.task_id)
        self.pause_event.set()
        self.emit("status", text="正在运行")

    def answer_checkpoint(self, continue_run: bool) -> None:
        log("INFO", "automation", "checkpoint.answered", "Batch checkpoint answered", operation_id=self.operation_id, task_id=self.task_id, continue_run=continue_run)
        self.checkpoint_decision = continue_run
        self.checkpoint_event.set()

    def skip_current(self) -> None:
        if self._active_record_ids:
            record_id = min(self._active_record_ids)
            self._skip_record_ids.add(record_id)
            self.emit("status", text="正在跳过一条正在下载的视频…")

    def cancel_records(self, record_ids: list[int]) -> None:
        self._cancelled_record_ids.update(int(item) for item in record_ids)
        self._skip_record_ids.update(int(item) for item in record_ids)

    def run(self) -> None:
        bind(self.operation_id, self.task_id)
        restart_count = 0
        log("INFO", "automation", "task.started", "Feed collector started", operation_id=self.operation_id, task_id=self.task_id)
        try:
            while self._restart_allowed:
                try:
                    asyncio.run(self._run())
                    self._terminal_outcome = "cancelled" if self.stop_event.is_set() else "success"
                    break
                except Exception as exc:
                    if self.stop_event.is_set() or not self._restart_allowed:
                        self._terminal_outcome = "cancelled"
                        log("INFO","automation","task.cancelled_during_io","Browser I/O ended after stop request",
                            operation_id=self.operation_id,task_id=self.task_id,error_type=type(exc).__name__)
                        break
                    restart_count += 1
                    log("WARN", "automation", "task.retry", "Collector failed; recovery scheduled", operation_id=self.operation_id, task_id=self.task_id, retry_count=restart_count, error=exc)
                    if not self._restart_allowed or restart_count >= 3:
                        self._terminal_outcome = "failed"
                        log("ERROR", "automation", "task.retry_exhausted", "Collector stopped after repeated failures", operation_id=self.operation_id, task_id=self.task_id, retry_count=restart_count, error=exc)
                        self.emit("error", text=f"自动化任务停止：{exc}")
                        break
                    delay=3*2**(restart_count-1)
                    self.emit("warning", text=f"后台采集异常，{delay} 秒后自动恢复（第 {restart_count} 次）：{exc}")
                    self.stop_event.clear()
                    self.pause_event.set()
                    self.stop_event.wait(delay)
        finally:
            log_finish("task", "automation", self.operation_id, self._started_at, self._terminal_outcome,
                       "Feed collector ended", task_id=self.task_id, completed=self.completed, retries=restart_count)
            self.emit("finished")

    async def _run(self) -> None:
        from playwright.async_api import async_playwright

        target = Path(self.config.download_dir)
        target.mkdir(parents=True, exist_ok=True)
        edge = edge_executable()
        if not edge:
            raise RuntimeError("未找到 Microsoft Edge。请安装 Edge 后重试。")

        self.emit("status", text="正在打开 TikTok 和 SaveSora…")
        log("INFO", "browser", "browser.launch_requested", "Launching Edge for feed collection", operation_id=self.operation_id, task_id=self.task_id,
            executable=Path(edge).name)
        self._write_diagnostic("启动新一轮采集", {"target": str(target)})
        profile_dir = app_data_dir() / "browser_profile_tiktok"
        async with async_playwright() as pw:
            scan_context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), executable_path=edge, headless=False,
                accept_downloads=True, viewport={"width": 1280, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=80,60",
                    "--window-size=1180,880",
                    "--start-minimized",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                ignore_default_args=["--no-sandbox"],
            )
            download_context = scan_context
            log("INFO", "browser", "browser.launch_completed", "Edge context opened", operation_id=self.operation_id, task_id=self.task_id)
            self._browser_contexts = [scan_context]
            pages = scan_context.pages
            tiktok = pages[0] if pages else await scan_context.new_page()
            savesora = await download_context.new_page()
            savesora.on("close", lambda: self._write_diagnostic("SaveSora 标签页已关闭", {"mode": "feed"}))
            recommend_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            tiktok.on(
                "response",
                lambda response: asyncio.create_task(
                    self._capture_recommend_response(response, recommend_queue)
                ),
            )
            # Two independent tabs share one minimized Edge window. Scanning and
            # downloading still run in separate async tasks.
            await self._set_browser_window_visible(tiktok, False)
            await self._set_browser_window_visible(savesora, False)
            self._write_diagnostic(
                "TikTok 与 SaveSora 已在同一 Edge 窗口使用独立标签页",
                {"profile": str(profile_dir), "tabs": 2},
            )
            await tiktok.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=90_000)
            await savesora.goto(SAVESORA_URL, wait_until="domcontentloaded", timeout=90_000)
            download_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            completed_lock = asyncio.Lock()
            # Feed collection is deliberately serial: do not advance while a
            # qualifying item's file is being prepared, transferred or verified.
            worker_count = max(1,min(4,int(self.config.max_concurrent_downloads)))
            savesora_pages = [savesora]
            for _ in range(worker_count - 1):
                page = await download_context.new_page()
                await self._set_browser_window_visible(page, False)
                savesora_pages.append(page)
            download_tasks = [
                asyncio.create_task(self._download_consumer(page, target, download_queue, completed_lock))
                for page in savesora_pages
            ]
            self._write_diagnostic("同一浏览器窗口的并行下载标签页已启动", {"workers": worker_count})
            recovered_backlog: deque[dict[str, Any]] = deque()
            for row in query_pending_candidates():
                item = {
                    "url": str(row["source_url"]), "title": str(row["title"]),
                    "likes": int(row["likes"]), "comments": int(row["comments"]),
                    "favorites": int(row["favorites"]), "duration": float(row["duration_seconds"]),
                    "record_id": int(row["id"]),
                }
                failures = self._qualification_failures(item)
                self._write_diagnostic(
                    "恢复队列视频指标记录",
                    {
                        "recordId": item["record_id"],
                        "url": item["url"],
                        "title": item["title"],
                        "metrics": {
                            "likes": item["likes"],
                            "comments": item["comments"],
                            "favorites": item["favorites"],
                            "durationSeconds": item["duration"],
                        },
                        "filters": {
                            "minLikes": self.config.min_likes,
                            "minComments": self.config.min_comments,
                            "minFavorites": self.config.min_favorites,
                            "minDuration": self.config.min_duration_seconds,
                            "maxDuration": self.config.max_duration_seconds,
                        },
                        "meetsFilters": not failures,
                        "reasons": failures,
                    },
                )
                if failures:
                    update_video_status(
                        int(item["record_id"]), "已跳过", 0,
                        "恢复时不符合当前筛选：" + "；".join(failures),
                    )
                    continue
                recovered_backlog.append(item)
            if recovered_backlog:
                self.emit("status", text=f"发现 {len(recovered_backlog)} 条未完成下载任务，将按本批次容量依次恢复")
            self.emit("status", text="正在静默扫描公开推荐视频")
            seen: set[str] = set()
            candidate_backlog: deque[dict[str, Any]] = deque()
            empty_rounds = 0
            refresh_rounds = 0
            metrics_missing_since: float | None = None
            try:
                while not self.stop_event.is_set():
                    await self._wait_if_paused()
                    # Existing rows and just-seen candidates are admitted only
                    # until this batch reaches its configured capacity.
                    while not self.stop_event.is_set() and self._batch_accepted < self._batch_target and recovered_backlog:
                        item = recovered_backlog.popleft()
                        self._admitted_urls.add(self._video_key(item["url"]))
                        update_video_status(int(item["record_id"]), "排队中", 0, "恢复到当前批次")
                        await download_queue.put(item)
                        self._batch_accepted += 1
                        await self._wait_for_serial_download(download_queue)
                    while not self.stop_event.is_set() and self._batch_accepted < self._batch_target and candidate_backlog:
                        item = candidate_backlog.popleft()
                        if self._video_key(item["url"]) in self._admitted_urls:
                            continue
                        if source_exists(item["url"]):
                            continue
                        failures = self._qualification_failures(item)
                        if failures:
                            self._write_diagnostic(
                                "暂存候选的结构化指标未通过筛选",
                                {"url": item["url"], "reasons": failures, "metrics": {
                                    "likes": item.get("likes"), "comments": item.get("comments"),
                                    "favorites": item.get("favorites"), "duration": item.get("duration"),
                                }},
                            )
                            continue
                        item["record_id"] = add_candidate({
                            "source_url": item["url"], "title": item.get("title", ""),
                            "duration_seconds": item.get("duration", 0),
                            "category": classify_text(item.get("title", "")),
                            "likes": item.get("likes", 0), "comments": item.get("comments", 0),
                            "favorites": item.get("favorites", 0),
                        })
                        self.emit("candidate", record_id=item["record_id"])
                        self._admitted_urls.add(self._video_key(item["url"]))
                        await download_queue.put(item)
                        self._batch_accepted += 1
                        await self._wait_for_serial_download(download_queue)
                    if self._batch_accepted >= self._batch_target:
                        self.emit("status", text=f"本批次队列已满（{self._batch_accepted}/{self._batch_target}），已暂停 TikTok 滚动，等待本批次下载完成")
                        await asyncio.sleep(0.3)
                        continue
                    candidates: list[dict[str, Any]] = []
                    while not recommend_queue.empty():
                        candidates.append(recommend_queue.get_nowait())
                    # API batches are independent of scrolling; an empty queue
                    # means only that no new batch is waiting right now.
                    if not candidates:
                        try:
                            dom_candidates = await self._extract_candidates(tiktok)
                            if dom_candidates:
                                candidates = dom_candidates
                        except Exception as exc:
                            if tiktok.is_closed():
                                log("ERROR","automation","feed.page_closed","TikTok tab closed; collection cannot continue",
                                    operation_id=self.operation_id,task_id=self.task_id,error=exc)
                                raise
                            self._last_metric_snapshot={"state":"inspection_error","reason":"inspection_error"}
                            log("WARN","automation","feed.card_read_failed","Current card extraction failed",
                                operation_id=self.operation_id,task_id=self.task_id,error=exc)
                    if candidates:
                        metrics_missing_since = None
                        self._identity_failure_count = 0
                    else:
                        snapshot=self._last_metric_snapshot
                        page_metrics_visible=bool(snapshot.get("metrics_visible"))
                        fingerprint=snapshot.get("fingerprint","")
                        if fingerprint != getattr(self,"_metric_wait_fingerprint",None):
                            metrics_missing_since=None
                            self._metric_wait_fingerprint=fingerprint
                        if metrics_missing_since is None:
                            metrics_missing_since = time.monotonic()
                        timeout_seconds = max(0.1, float(self.config.metric_timeout_seconds))
                        # Respect the configured timeout for both metadata and
                        # identity recovery; visible counters alone are not enough.
                        effective_timeout = timeout_seconds
                        elapsed = time.monotonic() - metrics_missing_since
                        if elapsed < effective_timeout:
                            state = {"unknown_duration":"当前视频时长未知，等待媒体元数据",
                                     "missing_required_metrics":"当前视频所需筛选指标缺失，等待结构化数据",
                                     "missing_permalink":"等待确认当前视频原链接",
                                     "identity_conflict":"当前视频身份冲突，等待稳定",
                                     "page_not_ready":"页面尚未出现可识别视频卡片"}.get(snapshot.get('reason'),"等待当前视频指标数据")
                            self.emit("status", text=f"{state}… {int(elapsed)}/{effective_timeout:g} 秒")
                            await asyncio.sleep(min(self.config.scroll_delay_seconds, effective_timeout - elapsed))
                            continue
                        status_text = (
                            ("当前视频时长仍未知，无法验证时长筛选，已切换下一条" if snapshot.get("reason")=="unknown_duration"
                             else "当前卡片指标可见，但视频身份无法确认")
                            if page_metrics_visible
                            else f"连续 {timeout_seconds:g} 秒未取得完整结构化指标，已切换下一条"
                        )
                        self.emit("status", text=status_text)
                        log("WARN","automation","feed.card_timeout",
                            "Current card could not be validated before timeout",operation_id=self.operation_id,
                            task_id=self.task_id,reason=snapshot.get("reason",snapshot.get("state","unknown")),
                            fingerprint=fingerprint,metrics_visible=page_metrics_visible,
                            link_count=snapshot.get("link_count",0),visible_cards=snapshot.get("visible_cards",0),
                            timeout_seconds=effective_timeout,api_cache_count=len(self._api_metric_cache),
                            identity_source=snapshot.get("identity_source","none"),
                            react_nodes=snapshot.get("react_nodes",0),react_depth=snapshot.get("react_depth",0),
                            card_tag=snapshot.get("card_tag","unknown"),
                            missing_required=snapshot.get("missing_required",[]),metric_sources=snapshot.get("metric_sources",{}),
                            api_silence_seconds=round(time.monotonic()-self._feed_recovery.last_api,1))
                        metrics_missing_since = None
                        if snapshot.get("reason")=="missing_permalink":
                            if await self._record_identity_failure(tiktok, snapshot):
                                continue  # Keep this exact card while paused/resumed.
                        if self._feed_recovery.timeout(time.monotonic(),page_metrics_visible,timeout_seconds):
                            self.emit("status", text="当前卡片持续无法读取且接口长时间无有效数据，正在刷新恢复")
                            log("WARN","automation","feed.recovery_reload","Stalled feed recovery reload",
                                operation_id=self.operation_id,task_id=self.task_id)
                            await tiktok.reload(wait_until="domcontentloaded", timeout=90_000)
                            await asyncio.sleep(3)
                        else:
                            await self._advance_tiktok(tiktok)
                            await asyncio.sleep(self.config.scroll_delay_seconds)
                        continue
                    fresh = [item for item in candidates if item["url"] not in seen]
                    if not fresh:
                        empty_rounds += 1
                        if empty_rounds in {1, 12}:
                            self._write_diagnostic(
                                f"第 {empty_rounds} 次未发现新视频",
                                await self._page_diagnostics(tiktok),
                            )
                    else:
                        empty_rounds = 0
                        refresh_rounds = 0
                        self._write_diagnostic(
                            "识别到视频",
                            {"count": len(fresh), "sample": fresh[:3]},
                        )
                    for index, item in enumerate(fresh):
                        if self._video_key(item["url"]) in self._admitted_urls:
                            continue
                        if self._batch_accepted >= self._batch_target:
                            candidate_backlog.extend(fresh[index:])
                            break
                        seen.add(item["url"])
                        from database import extract_video_id
                        log("INFO", "automation", "video.metrics", "Video metrics read", operation_id=self.operation_id,
                            task_id=self.task_id, video_id=extract_video_id(item["url"]),
                            likes=item.get("likes", 0), comments=item.get("comments", 0),
                            favorites=item.get("favorites", 0), duration_seconds=item.get("duration", 0),
                            metric_source=item.get("metric_source","recovered"),
                            metric_sources=item.get("metric_sources",{}),unknown_metrics=item.get("unknown_metrics",[]),
                            duration_known=bool(item.get("duration",0)))
                        if self.stop_event.is_set():
                            break
                        await self._wait_if_paused()
                        if source_exists(item["url"]):
                            log("INFO", "automation", "video.duplicate", "Already downloaded video skipped", video_id=extract_video_id(item["url"]))
                            continue
                        failures = self._qualification_failures(item)
                        if failures:
                            log("INFO", "automation", "video.rejected", "Video did not meet filters", video_id=extract_video_id(item["url"]), reasons=failures)
                            reason = "；".join(failures)
                            self.emit("status", text=f"跳过：{reason}")
                            self._write_diagnostic(
                                "结构化指标未通过筛选",
                                {"url": item["url"], "reasons": failures, "metrics": {
                                    "likes": item.get("likes"), "comments": item.get("comments"),
                                    "favorites": item.get("favorites"), "duration": item.get("duration"),
                                }},
                            )
                            continue
                        item["record_id"] = add_candidate({
                            "source_url": item["url"], "title": item.get("title", ""),
                            "duration_seconds": item.get("duration", 0),
                            "category": classify_text(item.get("title", "")),
                            "likes": item.get("likes", 0), "comments": item.get("comments", 0),
                            "favorites": item.get("favorites", 0),
                        })
                        log("INFO", "automation", "video.accepted", "Video accepted and queued", record_id=item["record_id"], video_id=extract_video_id(item["url"]))
                        self.emit("candidate", record_id=item["record_id"])
                        self._admitted_urls.add(self._video_key(item["url"]))
                        await download_queue.put(item)
                        self._batch_accepted += 1
                        self.emit(
                            "status",
                            text=f"已加入本批次（{self._batch_accepted}/{self._batch_target}），暂停滚动等待下载",
                        )
                        await self._wait_for_serial_download(download_queue)
                    if self.stop_event.is_set():
                        break
                    await self._advance_tiktok(tiktok)
                    await asyncio.sleep(self.config.scroll_delay_seconds)
                    if empty_rounds >= 12:
                        refresh_rounds += 1
                        self._write_diagnostic(
                            "推荐流暂时无新增，自动刷新恢复",
                            {"refreshRound": refresh_rounds, "queueSize": download_queue.qsize(), "downloadActive": self._download_active},
                        )
                        if refresh_rounds >= 4 and download_queue.empty() and not self._download_active:
                            await self._request_manual_attention(
                                tiktok,
                                "程序已自动刷新 TikTok 4 次，但仍未识别到新视频。请检查页面是否出现登录、验证码、Cookie 提示、地区限制或网络错误；完成必要处理后回到应用点击“继续”。",
                            )
                            refresh_rounds = 0
                        else:
                            self.emit("status", text=f"推荐流暂时无新增，正在自动刷新恢复（{refresh_rounds}/4）")
                            with contextlib.suppress(Exception):
                                await tiktok.reload(wait_until="domcontentloaded", timeout=90_000)
                            await asyncio.sleep(3)
                        empty_rounds = 0
            finally:
                self.stop_event.set()
                for task in download_tasks:
                    task.cancel()
                for task in download_tasks:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for browser_context in list(self._browser_contexts):
                    with contextlib.suppress(Exception):
                        await browser_context.close()
                log("INFO", "browser", "browser.closed", "Browser contexts closed", operation_id=self.operation_id, task_id=self.task_id)
                self._browser_contexts.clear()
                self._manual_page = None

    async def _wait_if_paused(self) -> None:
        while not self.pause_event.is_set() and not self.stop_event.is_set():
            await asyncio.sleep(0.2)
        if self._manual_page is not None and not self.stop_event.is_set():
            await self._set_browser_window_visible(self._manual_page, False)
            self._manual_page = None
            self.emit("status", text="人工处理完成，已恢复自动采集")

    async def _request_manual_attention(self, page, text: str) -> None:
        self.pause_event.clear()
        self._manual_page = page
        await self._set_browser_window_visible(page, True)
        self.emit("attention", text=text)
        await self._wait_if_paused()

    async def _set_browser_window_visible(self, page, visible: bool) -> None:
        """Keep Edge minimized normally; show it only for human intervention."""
        session = None
        try:
            session = await page.context.new_cdp_session(page)
            result = await session.send("Browser.getWindowForTarget")
            window_id = result["windowId"]
            bounds = (
                {"windowState": "normal", "left": 80, "top": 60, "width": 1180, "height": 880}
                if visible
                else {"windowState": "minimized"}
            )
            await session.send("Browser.setWindowBounds", {"windowId": window_id, "bounds": bounds})
            if visible:
                await page.bring_to_front()
        except Exception as exc:
            if visible:
                self.emit("warning", text=f"无法自动显示浏览器窗口，请从任务栏打开 Microsoft Edge：{exc}")
        finally:
            if session is not None:
                with contextlib.suppress(Exception):
                    await session.detach()

    @staticmethod
    def _video_key(url):
        match=re.search(r"/video/(\d{15,22})(?:[/?#]|$)",url)
        return match.group(1) if match else url

    def _qualifies(self, item: dict[str, Any]) -> bool:
        return not self._qualification_failures(item)

    def _qualification_failures(self, item: dict[str, Any]) -> list[str]:
        failures: list[str] = []
        raw_duration = item.get("duration")
        duration = float(raw_duration) if raw_duration is not None else 0.0
        duration_known = math.isfinite(duration) and duration > 0 and item.get("duration_known", True)
        for name,label in (("likes","点赞"),("comments","评论"),("favorites","收藏")):
            minimum=getattr(self.config,"min_"+name)
            value=counter_value(item.get(name))
            if minimum>0 and value is None:
                failures.append(f"{label}未知，无法验证当前筛选")
            elif value is not None and value<minimum:
                failures.append(f"{label} {value:,} < {minimum:,}")
        if not duration_known and (self.config.min_duration_seconds > 0 or self.config.max_duration_seconds > 0):
            failures.append("视频时长未知，无法验证当前时长筛选")
        elif duration < self.config.min_duration_seconds:
            failures.append(f"时长 {duration:g}秒 < {self.config.min_duration_seconds}秒")
        if duration_known and self.config.max_duration_seconds > 0 and duration > self.config.max_duration_seconds:
            failures.append(f"时长 {duration:g}秒 > {self.config.max_duration_seconds}秒")
        return failures

    async def _page_has_readable_metrics(self, page) -> bool:
        try:
            snapshot = await page.evaluate(CARD_SCRIPT)
            item=snapshot_candidate(snapshot,self._api_metric_cache,compact_number,required_metrics(self.config))
            self._last_metric_snapshot=snapshot
            return bool(item or snapshot.get("metrics_visible"))
        except Exception as exc:
            log("WARN","automation","feed.card_read_failed","Current card could not be inspected",
                operation_id=self.operation_id,task_id=self.task_id,error=exc)
            return False

    async def _advance_tiktok(self, page) -> None:
        # Targeted CDP input does not require activating the Edge window. Keeping
        # it off-screen also prevents the scanning and SaveSora tabs from fighting
        # for foreground focus while their tasks run concurrently.
        try:
            before=await page.evaluate(CARD_SCRIPT)
            await page.keyboard.press("ArrowDown")
            await asyncio.sleep(0.35)
            after=await page.evaluate(CARD_SCRIPT)
            # Do not send a second scroll when ArrowDown already changed cards.
            if before.get("identity")==after.get("identity"):
                await page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.9, 800))")
        except Exception as exc:
            log("WARN","automation","feed.advance_failed","Could not advance the video feed",
                operation_id=self.operation_id,task_id=self.task_id,error=exc)

    async def _wait_for_serial_download(self, pending) -> None:
        """Wait for the consumer's terminal state, not just queue dequeue.

        task_done is called after file verification and batch confirmation.
        Failed/skipped items also reach a terminal state and release a slot;
        their existing explicit warning remains visible before scanning resumes.
        """
        if self.config.max_concurrent_downloads>1:
            return
        log("INFO", "automation", "feed.download_wait_started", "Scrolling paused until download terminal state",
            operation_id=self.operation_id, task_id=self.task_id)
        while not self.stop_event.is_set():
            try:
                await asyncio.wait_for(pending.join(), timeout=0.25)
                log("INFO", "automation", "feed.download_wait_finished", "Download queue settled; scanning may resume",
                    operation_id=self.operation_id, task_id=self.task_id, outcome="settled")
                return
            except asyncio.TimeoutError:
                continue

    async def _extract_candidates(self, page) -> list[dict[str, Any]]:
        began=time.monotonic()
        snapshot=await page.evaluate(CARD_SCRIPT)
        item=snapshot_candidate(snapshot,self._api_metric_cache,compact_number,required_metrics(self.config))
        fingerprint=snapshot.get("fingerprint","")
        if (item is None and snapshot.get("reason")=="missing_permalink" and fingerprint
                and fingerprint not in self._share_attempts):
            if len(self._share_attempts)>=128:
                self._share_attempts.clear()
            self._share_attempts.add(fingerprint)
            try:
                url, reason=await recover_share_link(page,snapshot,lambda:page.evaluate(CARD_SCRIPT))
                log("INFO","automation","feed.share_recovery","Current-card share recovery attempted",
                    operation_id=self.operation_id,task_id=self.task_id,fingerprint=fingerprint,
                    outcome="verified" if url else "unavailable",reason=reason)
                if url:
                    snapshot.update(url=url,video_id=url.rsplit("/",1)[-1],identity_source=reason)
                    snapshot.pop("reason",None)
                    item=snapshot_candidate(snapshot,self._api_metric_cache,compact_number,required_metrics(self.config))
            except Exception as exc:
                log("WARN","automation","feed.share_recovery_failed","Share recovery failed safely",
                    operation_id=self.operation_id,task_id=self.task_id,fingerprint=fingerprint,error=exc)
        self._last_metric_snapshot=snapshot
        if item and not item.get("duration_known",True) and (
            self.config.min_duration_seconds>0 or self.config.max_duration_seconds>0):
            snapshot["reason"]="unknown_duration"
            return []
        if item and fingerprint not in self._metrics_logged:
            if len(self._metrics_logged)>=128:self._metrics_logged.clear()
            self._metrics_logged.add(fingerprint)
            log("INFO","automation","feed.metrics_resolved","Current video metrics resolved for active filters",
                operation_id=self.operation_id,task_id=self.task_id,fingerprint=fingerprint,
                video_id=item['url'].rsplit('/',1)[-1],metric_source=item['metric_source'],
                metric_sources=item.get('metric_sources',{}),unknown_metrics=item.get('unknown_metrics',[]),
                identity_source=snapshot.get('identity_source'),duration_ms=round((time.monotonic()-began)*1000,1))
        return [item] if item else []

    async def _record_identity_failure(self, page, snapshot) -> bool:
        self._identity_failure_count += 1
        count=self._identity_failure_count
        if count in {1,3}:
            try:
                evidence=await page.evaluate(EVIDENCE_SCRIPT,snapshot.get("card_marker",""))
                log("WARN","automation","feed.identity_evidence","Value-free current-card failure evidence",
                    operation_id=self.operation_id,task_id=self.task_id,
                    fingerprint=snapshot.get("fingerprint"),consecutive=count,evidence=evidence,
                    api_cache_count=len(self._api_metric_cache))
            except Exception as exc:
                log("WARN","automation","feed.evidence_failed","Could not capture failure evidence",
                    operation_id=self.operation_id,task_id=self.task_id,error=exc)
        if count<3:
            return False
        log("ERROR","automation","feed.identity_paused","Repeated identity failures; preserving current card",
            operation_id=self.operation_id,task_id=self.task_id,consecutive=count)
        await self._request_manual_attention(page,
            "连续 3 次无法确认当前视频原链接，已暂停滚动并保留现场。\n"
            "脱敏页面结构已记录到运行日志，可从左侧‘运行日志’导出。\n"
            "请勿手动下载或清空列表；可检查当前页面。点击应用‘继续采集’后重试。")
        self._identity_failure_count=0
        self._share_attempts.discard(snapshot.get("fingerprint",""))
        return True

    async def _capture_recommend_response(
        self,
        response,
        output: asyncio.Queue[dict[str, Any]],
    ) -> None:
        if "/api/" not in response.url or not any(name in response.url.split("?",1)[0]
                                                  for name in ("item_list","item/detail")):
            return
        try:
            endpoint=response.url.split("?",1)[0]
            if not response.ok:
                log("WARN","automation","feed.response_http_failed","TikTok metadata request returned an error",
                    operation_id=self.operation_id,task_id=self.task_id,url=endpoint,http_status=response.status)
                return
            data = await response.json()
            items = api_items(data)
            captured: list[dict[str, Any]] = []
            incomplete = 0
            for item in items:
                candidate=normalize_api_item(item,required_metrics(self.config),
                                             self.config.min_duration_seconds>0 or self.config.max_duration_seconds>0)
                if candidate is None:
                    incomplete += 1
                    continue
                self._api_metric_cache[str(item["id"])]=candidate
                while len(self._api_metric_cache)>512:
                    self._api_metric_cache.pop(next(iter(self._api_metric_cache)))
                self._feed_recovery.last_api=time.monotonic()
                self._feed_recovery.missing_rounds=0
                failures = self._qualification_failures(candidate)
                self._write_diagnostic(
                    "视频结构化指标记录",
                    {
                        "endpoint": response.url.split("?", 1)[0],
                        "url": candidate["url"],
                        "title": candidate["title"],
                        "metrics": {
                            "likes": candidate["likes"],
                            "comments": candidate["comments"],
                            "favorites": candidate["favorites"],
                            "durationSeconds": candidate["duration"],
                        },
                        "filters": {
                            "minLikes": self.config.min_likes,
                            "minComments": self.config.min_comments,
                            "minFavorites": self.config.min_favorites,
                            "minDurationSeconds": self.config.min_duration_seconds,
                            "maxDurationSeconds": self.config.max_duration_seconds,
                        },
                        "meetsFilters": not failures,
                        "reasons": failures,
                    },
                )
                output.put_nowait(candidate)
                captured.append(candidate)
            self._write_diagnostic(
                "捕获推荐流元数据",
                {
                    "endpoint": response.url.split("?", 1)[0],
                    "responseItems": len(items),
                    "usable": len(captured),
                    "incomplete": incomplete,
                    "sample": captured[:3],
                },
            )
            log("WARN" if incomplete or not captured else "DEBUG","automation","feed.response_summary",
                "TikTok metadata response processed",operation_id=self.operation_id,task_id=self.task_id,
                url=endpoint,http_status=response.status,response_items=len(items),usable=len(captured),incomplete=incomplete)
        except Exception as exc:
            log("WARN", "automation", "feed.response_parse_failed", "Recommendation response parse failed",
                operation_id=self.operation_id,task_id=self.task_id,error=exc)

    async def _download_consumer(self, savesora, target, pending, completed_lock):
            while not self.stop_event.is_set():
                await self._wait_if_paused()
                try:
                    item = await asyncio.wait_for(pending.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                await self._wait_if_paused()
                if self.stop_event.is_set():
                    pending.task_done()
                    return
                record_id = int(item.get("record_id", 0))
                self._active_record_ids.add(record_id)
                self._download_active = True
                self._current_record_id = record_id
                handed_to_saver = False
                try:
                    if record_id in self._cancelled_record_ids:
                        continue
                    if source_exists(item["url"]):
                        continue
                    if savesora.is_closed():
                        savesora = await self._browser_contexts[0].new_page()
                        self._write_diagnostic("SaveSora 标签页已自动重建", {"mode": "feed", "recordId": record_id})
                    self._set_download_progress(item, 5, "正在下载", "SaveSora 正在解析链接")
                    try:
                        download = await self._prepare_download_logged(savesora, item)
                    except Exception as exc:
                        message = str(exc)
                        if "Target page, context or browser has been closed" in message and self._browser_contexts:
                            try:
                                savesora = await self._browser_contexts[0].new_page()
                                self._write_diagnostic("SaveSora 标签页关闭后重试", {"recordId": record_id})
                                download = await self._prepare_download_logged(savesora, item)
                            except Exception as retry_exc:
                                message = str(retry_exc)
                            else:
                                handed_to_saver = True
                                await self._save_prepared_download(download, item, target, completed_lock)
                                continue
                        if "用户已手动跳过" in message:
                            self._set_download_progress(item, 0, "已跳过", "用户手动跳过")
                            self.emit("warning", text="已跳过当前视频，继续处理队列")
                            continue
                        if "文件保存超过 5 分钟" in message or not message.startswith("需要人工处理:"):
                            self._set_download_progress(item, 0, "失败", message)
                            self.emit("warning", text=f"下载解析失败，已释放名额并继续队列：{message}")
                            continue
                        self._set_download_progress(item, 35, "等待人工处理", message)
                        await self._request_manual_attention(
                            savesora,
                            "SaveSora 要求人工处理。请完成页面上的验证码或安全验证，然后回到应用点击‘继续’，程序会自动重试。\n\n"
                            f"详细信息：{message}",
                        )
                        if self.stop_event.is_set():
                            return
                        try:
                            download = await self._prepare_download_logged(savesora, item)
                        except Exception as retry_exc:
                            self._set_download_progress(item, 0, "失败", str(retry_exc))
                            self.emit("warning", text=f"人工处理后仍未下载成功，已跳过该视频：{retry_exc}")
                            continue
                    handed_to_saver = True
                    await self._save_prepared_download(download, item, target, completed_lock)
                finally:
                    if not handed_to_saver:
                        await self._mark_batch_terminal(False)
                        self._finish_active_record(record_id)
                    pending.task_done()

    async def _prepare_download_logged(self,page,item):
        operation=new_id();began=time.monotonic()
        log("INFO","automation","download_prepare.started","SaveSora preparation started",
            operation_id=operation,parent_operation_id=self.operation_id,task_id=self.task_id,record_id=item.get("record_id"))
        try:
            value=await self._prepare_savesora_download(page,item)
        except BaseException as exc:
            outcome="cancelled" if isinstance(exc,asyncio.CancelledError) or self.stop_event.is_set() else "timeout" if isinstance(exc,TimeoutError) else "failed"
            log_finish("download_prepare","automation",operation,began,outcome,"SaveSora preparation ended",
                parent_operation_id=self.operation_id,task_id=self.task_id,record_id=item.get("record_id"),error=exc)
            raise
        log_finish("download_prepare","automation",operation,began,"success","Download handle acquired; file not verified yet",
            parent_operation_id=self.operation_id,task_id=self.task_id,record_id=item.get("record_id"))
        return value

    async def _save_prepared_download(
        self,
        download,
        item: dict[str, Any],
        target: Path,
        completed_lock: asyncio.Lock,
        count_toward_batch: bool = True,
        event_kind: str = "manual_downloaded",
    ) -> bool:
        record_id = int(item.get("record_id", 0))
        succeeded = False
        download_op = new_id()
        began = time.monotonic()
        terminal_error = None
        save_task = None
        temp = None
        log("INFO", "automation", "download.started", "Prepared file transfer started", operation_id=download_op,
            parent_operation_id=self.operation_id, task_id=self.task_id, record_id=record_id)
        try:
            limit=int(self.config.max_file_size_mb*1024*1024)
            if limit and hasattr(download,"probe_size"):
                await download.probe_size()
            expected=getattr(download,"expected_size",None)
            if limit and expected and expected>limit:
                raise RuntimeError(f"文件大小 {expected/1048576:.1f}MB 超过设置上限 {self.config.max_file_size_mb:g}MB")
            suffix = Path(download.suggested_filename).suffix or ".mp4"
            temp = target / f".downloading_{uuid.uuid4().hex}{suffix}"
            save_task = asyncio.create_task(download.save_as(str(temp)))
            deadline = time.monotonic() + self.config.download_timeout_seconds
            last_size=0
            last_progress=time.monotonic()
            last_summary=last_progress
            while not save_task.done():
                if self.stop_event.is_set():
                    raise asyncio.CancelledError("用户停止任务")
                if self._is_skip_requested(item):
                    raise RuntimeError("用户已手动跳过")
                now=time.monotonic()
                size=getattr(download,"bytes_received",None)
                if size is None and temp.exists():size=temp.stat().st_size
                if size is not None:
                    if size>last_size:last_size=size;last_progress=now
                    if limit and size>limit:raise RuntimeError("下载大小超过设置上限")
                    if now-last_progress>=self.config.download_idle_timeout_seconds:
                        raise TimeoutError("文件传输长时间无进展")
                if now-last_summary>=30:
                    log("INFO","automation","download.progress_summary","Transfer still pending",
                        operation_id=download_op,task_id=self.task_id,record_id=record_id,
                        bytes_received=size,expected_size=expected,progress_observable=size is not None,
                        elapsed_seconds=round(now-began))
                    last_summary=now
                if now >= deadline:
                    raise TimeoutError("文件传输超过设置的总时限")
                await asyncio.sleep(0.25)
            await save_task
            if not temp.exists() or temp.stat().st_size == 0:
                raise RuntimeError("下载文件为空")
            if limit and temp.stat().st_size>limit:
                raise RuntimeError("已下载文件超过设置上限，不计为成功")
            self._set_download_progress(item, 90, "正在下载", "文件已保存，正在分类整理")
            category = classify_text(item.get("title", ""))
            duration = self._duration(temp)
            if not math.isfinite(duration) or duration<=0:
                raise RuntimeError("下载文件不是可验证的视频，未计为完成")
            if count_toward_batch and (duration<self.config.min_duration_seconds or
                    self.config.max_duration_seconds>0 and duration>self.config.max_duration_seconds):
                raise RuntimeError(f"实际媒体时长 {duration:.3f} 秒不符合时长范围，未计为完成")
            final_path = categorized_path(temp, category, item.get("title", ""), duration)
            temp.replace(final_path)
            record = {
                "source_url": item["url"], "title": item.get("title", ""), "file_path": str(final_path),
                "size_bytes": final_path.stat().st_size, "duration_seconds": duration, "category": category,
                "downloaded_at": datetime.now().isoformat(timespec="seconds"), "likes": item.get("likes", 0),
                "comments": item.get("comments", 0), "favorites": item.get("favorites", 0),
            }
            # Keep the completion count and confirmation boundary atomic.
            async with completed_lock:
                add_video(record)
                if count_toward_batch:
                    self.completed += 1
                    self._batch_succeeded += 1
                    self.emit(
                        "downloaded", record=record, completed=self.completed,
                        batch_completed=self._batch_succeeded, batch_target=self._batch_target,
                    )
                else:
                    self.emit(event_kind, record=record, record_id=record_id)
                succeeded = True
        except asyncio.CancelledError as exc:
            terminal_error=exc
            self._set_download_progress(item,0,"已跳过","任务已取消")
            raise
        except Exception as exc:
            terminal_error = exc
            if "用户已手动跳过" in str(exc):
                self._set_download_progress(item, 0, "已跳过", "用户手动跳过")
            elif isinstance(exc,TimeoutError):
                self._set_download_progress(item, 0, "失败", str(exc))
            else:
                self._set_download_progress(item, 0, "失败", str(exc))
                self.emit("warning", text=f"后台文件传输失败，已继续队列：{exc}")
        finally:
            if not succeeded:
                try:
                    if hasattr(download,"cancel"):
                        await asyncio.wait_for(download.cancel(),12)
                        log("INFO","automation","download.cancelled_verified","Owned download cancellation completed",
                            operation_id=download_op,task_id=self.task_id,record_id=record_id)
                except Exception as cancel_error:
                    self.stop_event.set()
                    log("ERROR","automation","download.cancel_failed","Cancellation uncertain; stop further admission",
                        operation_id=download_op,task_id=self.task_id,record_id=record_id,error=cancel_error)
                if save_task and not save_task.done():save_task.cancel()
                if save_task:
                    with contextlib.suppress(asyncio.CancelledError,Exception):await save_task
                if temp:
                    with contextlib.suppress(OSError):temp.unlink(missing_ok=True)
            outcome = "success" if succeeded else "cancelled" if isinstance(terminal_error,asyncio.CancelledError) or terminal_error and "用户已手动跳过" in str(terminal_error) else "timeout" if isinstance(terminal_error,TimeoutError) else "failed"
            log_finish("download", "automation", download_op, began, outcome, "File transfer ended",
                       task_id=self.task_id, record_id=record_id, parent_operation_id=self.operation_id,
                       error=terminal_error if outcome != "success" else None)
            if count_toward_batch:
                await self._mark_batch_terminal(succeeded)
            self._finish_active_record(record_id)
        return succeeded

    def _finish_active_record(self, record_id: int) -> None:
        self._active_record_ids.discard(record_id)
        self._skip_record_ids.discard(record_id)
        self._download_active = bool(self._active_record_ids)
        self._current_record_id = min(self._active_record_ids) if self._active_record_ids else 0

    async def _mark_batch_terminal(self, succeeded: bool) -> None:
        """Prompt only after the configured number of successful downloads."""
        self._batch_finished += 1
        if self._batch_finished == 1 or self._batch_finished % 5 == 0 or self._batch_succeeded >= self._batch_target:
            log("INFO", "automation", "batch.progress", "Batch progress summary", operation_id=self.operation_id,
                task_id=self.task_id, accepted=self._batch_accepted, finished=self._batch_finished,
                succeeded=self._batch_succeeded, target=self._batch_target)
        if not succeeded:
            # A failed/skipped download frees one slot. The scanner can then
            # admit a replacement, so a batch confirmation always represents
            # exactly the configured number of successful downloads.
            self._batch_accepted = max(self._batch_succeeded, self._batch_accepted - 1)
            self.emit(
                "status",
                text=f"本批次有任务未完成，已释放 1 个名额；当前成功 {self._batch_succeeded}/{self._batch_target}",
            )
            return
        if (
            self._batch_succeeded >= self._batch_target
            and not self._batch_checkpoint_open
        ):
            self._batch_checkpoint_open = True
            if not await self._checkpoint():
                self.stop_event.set()

    def _set_download_progress(self, item: dict[str, Any], progress: int, status: str, message: str) -> None:
        record_id = int(item.get("record_id", 0))
        if record_id:
            update_video_status(record_id, status, progress, message)
        self.emit(
            "download_progress", record_id=record_id, progress=progress, status=status,
            message=message, title=item.get("title", "") or item.get("url", ""),
        )
        if status in {"已完成", "失败", "已跳过", "等待人工处理"} or progress in {5, 65, 90}:
            log("ERROR" if status=="失败" else "WARN" if status=="等待人工处理" else "INFO", "automation", "download.stage",
                message, operation_id=self.operation_id, task_id=self.task_id, record_id=record_id,
                progress=progress, status=status)

    async def _page_diagnostics(self, page) -> dict[str, Any]:
        try:
            details = await page.evaluate(
                """
                () => ({
                  url: location.href,
                  title: document.title,
                  videoLinks: document.querySelectorAll('a[href*="/video/"]').length,
                  videos: document.querySelectorAll('video').length,
                  likeCounts: [...document.querySelectorAll('[data-e2e="like-count"]')].slice(0, 5).map(x => x.textContent?.trim() || ''),
                  commentCounts: [...document.querySelectorAll('[data-e2e="comment-count"]')].slice(0, 5).map(x => x.textContent?.trim() || ''),
                  bookmarkCounts: [...document.querySelectorAll('[data-e2e="favorite-count"], [data-e2e="bookmark-count"], [data-e2e="undefined-count"]')].slice(0, 5).map(x => x.textContent?.trim() || ''),
                  cards: document.querySelectorAll('[data-e2e="recommend-list-item-container"], [data-e2e="recommend-list-item"], article').length,
                  dataE2E: [...new Set([...document.querySelectorAll('[data-e2e]')].map(x => x.getAttribute('data-e2e')).filter(Boolean))].slice(0, 80)
                })
                """
            )
            return details or {}
        except Exception as exc:
            return {"diagnosticError": str(exc)}

    def _write_diagnostic(self, message: str, data: Any) -> None:
        # Compatibility entry point for existing call sites; one structured sink.
        details = data if isinstance(data, dict) else {"detail": data}
        details = {("legacy_error" if key == "error" else key): value for key, value in details.items()}
        level = "WARN" if any(word in message for word in ("异常", "超时", "失败", "关闭")) else "DEBUG"
        log(level, "automation", "automation.detail", message, operation_id=self.operation_id,
            task_id=self.task_id, **details)

    @staticmethod
    def _metric_from_labels(labels: str, words: list[str]) -> int:
        for word in words:
            patterns = [rf"{re.escape(word)}[^0-9]*([0-9.,]+\s*[kKmMwW万千]?)", rf"([0-9.,]+\s*[kKmMwW万千]?)\s*{re.escape(word)}"]
            for pattern in patterns:
                match = re.search(pattern, labels, re.IGNORECASE)
                if match:
                    return compact_number(match.group(1))
        return 0

    async def _prepare_savesora_download(self, page, item):
            from download_handles import NativeDownload, BytesDownload

            self._raise_if_skip_requested(item)
            await page.goto(SAVESORA_URL, wait_until="domcontentloaded", timeout=90000)
            box = page.get_by_placeholder("在此处粘贴您的 TikTok 视频链接")
            if await box.count() != 1:
                box = page.locator('input[type="text"],input[type="url"]').first
            await box.fill(item["url"])
            button = page.get_by_role("button", name="下载", exact=True)
            if await button.count() != 1:
                button = page.locator('button[type="submit"]').first
            await button.click()
            self._set_download_progress(item, 20, "正在下载", "已提交链接，等待 SaveSora")
            self._write_diagnostic("SaveSora 已提交链接", {"recordId": item.get("record_id"), "url": item["url"]})
            deadline = time.monotonic() + 60
            tried = set()
            selectors = (
                'a:has-text("直接下载链接")', 'button:has-text("直接下载链接")',
                'a:has-text("Direct download link")', 'button:has-text("Direct download link")',
                'a:has-text("下载视频")', 'button:has-text("下载视频")',
                'a:has-text("Download Video")', 'button:has-text("Download Video")',
                'a[href*=".mp4"]', 'a[download]',
            )
            while time.monotonic() < deadline and not self.stop_event.is_set():
                self._raise_if_skip_requested(item)
                body_text = (await page.locator("body").inner_text(timeout=3000)).lower()
                if "下载失败" in body_text or "download failed" in body_text or "检查您的链接并重试" in body_text:
                    self._write_diagnostic("SaveSora 拒绝链接", {"recordId": item.get("record_id"), "url": item["url"], "body": body_text[:700]})
                    raise RuntimeError("SaveSora 拒绝该链接")
                for selector in selectors:
                    candidates = page.locator(selector)
                    for index in range(min(await candidates.count(), 4)):
                        candidate = candidates.nth(index)
                        if not await candidate.is_visible():
                            continue
                        href = await candidate.evaluate("n=>n.href||''")
                        label = (await candidate.inner_text()).strip()
                        key = href or label or (selector,index)
                        if key in tried:
                            continue
                        tried.add(key)
                        self._write_diagnostic("SaveSora 下载候选", {"recordId": item.get("record_id"), "selector": selector, "label": label, "href": href[:500]})
                        try:
                            from download_events import receive_download
                            received=await receive_download(page,candidate,max_bytes=int(self.config.max_file_size_mb*1048576))
                            if "download" in received:
                                download=NativeDownload(received["download"],page.context)
                            else:
                                download=BytesDownload(received["data"])
                            self._write_diagnostic("SaveSora 已取得下载文件", {"recordId": item.get("record_id"), "selector": selector, "href": href[:500]})
                            self._set_download_progress(item, 65, "正在下载", "文件传输已开始")
                            return download
                        except Exception as exc:
                            self._write_diagnostic("SaveSora 下载候选失败", {"recordId": item.get("record_id"), "selector": selector, "error": str(exc)[:800]})
                            if "超过设置上限" in str(exc):raise
                await asyncio.sleep(1.5)
            details = await page.evaluate("() => ({url: location.href, text: document.body.innerText.slice(0,1500), links: [...document.querySelectorAll('a')].filter(x => /下载|download/i.test(x.innerText)).slice(0,8).map(x => ({text:x.innerText,href:x.href}))})")
            self._write_diagnostic("SaveSora 下载超时", {"recordId": item.get("record_id"), "page": details})
            raise RuntimeError("SaveSora 60 秒内未提供下载文件，已记录页面诊断")

    async def _remote_url_download(self, page, url: str) -> RemoteUrlDownload:
        cookies = await page.context.cookies(url)
        cookie_header = "; ".join(f"{item['name']}={item['value']}" for item in cookies)
        user_agent = await page.evaluate("navigator.userAgent")
        headers = {
            "User-Agent": str(user_agent),
            "Referer": page.url,
            "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.8",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header
        self._write_diagnostic("SaveSora 已提取直接下载地址", {"url": url})
        return RemoteUrlDownload(url, headers, self.operation_id, self.task_id)

    async def _click_for_download_or_media(self, page, candidate):
        loop = asyncio.get_running_loop()
        media_url: asyncio.Future[str] = loop.create_future()

        def on_response(response) -> None:
            try:
                content_type = str(response.headers.get("content-type", "")).lower()
                url = str(response.url)
                if not media_url.done() and (
                    content_type.startswith("video/") or ".mp4" in url.lower()
                ):
                    media_url.set_result(url)
            except Exception:
                pass

        page.on("response", on_response)
        download_task = asyncio.create_task(page.wait_for_event("download", timeout=12_000))
        try:
            await candidate.click(timeout=10_000, no_wait_after=True)
            done, _ = await asyncio.wait(
                {download_task, media_url}, timeout=12, return_when=asyncio.FIRST_COMPLETED,
            )
            if download_task in done:
                return download_task.result()
            if media_url in done:
                return await self._remote_url_download(page, media_url.result())
            current_url = str(page.url)
            if ".mp4" in current_url.lower():
                return await self._remote_url_download(page, current_url)
            raise RuntimeError("点击后未产生下载事件或媒体地址")
        finally:
            page.remove_listener("response", on_response)
            if not download_task.done():
                download_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await download_task

    async def _savesora_diagnostics(self, page) -> dict[str, Any]:
        try:
            return await page.evaluate(
                """
                () => ({
                  url: location.href,
                  title: document.title,
                  bodyText: (document.body?.innerText || '').slice(0, 4000),
                  buttons: [...document.querySelectorAll('button')].slice(0, 30).map(x => (x.innerText || '').trim()),
                  links: [...document.querySelectorAll('a')].slice(0, 50).map(x => ({text: (x.innerText || '').trim(), href: x.href}))
                })
                """
            )
        except Exception as exc:
            return {"url": str(getattr(page, "url", "")), "diagnosticError": str(exc)}

    def _is_skip_requested(self, item: dict[str, Any]) -> bool:
        return int(item.get("record_id", 0)) in self._skip_record_ids

    def _raise_if_skip_requested(self, item: dict[str, Any]) -> None:
        if self._is_skip_requested(item):
            raise RuntimeError("用户已手动跳过")

    @staticmethod
    def _duration(path: Path) -> float:
        try:
            from mutagen.mp4 import MP4
            return round(float(MP4(path).info.length), 2)
        except Exception:
            return 0.0

    async def _checkpoint(self) -> bool:
        # The TikTok scanner was already stopped when the batch filled. At this
        # point every queued/downloading item in that batch has reached a final
        # state, so request permission before admitting another batch.
        self.pause_event.clear()
        self.checkpoint_event.clear()
        log("INFO", "automation", "checkpoint.waiting", "Batch complete; waiting for user confirmation", operation_id=self.operation_id,
            task_id=self.task_id, completed=self._batch_succeeded, target=self._batch_target)
        self._write_diagnostic(
            "本批次成功下载完成，等待人工确认",
            {
                "totalCompleted": self.completed,
                "batchCompleted": self._batch_succeeded,
                "batchSize": self._batch_accepted,
                "target": self._batch_target,
            },
        )
        self.emit(
            "checkpoint", completed=self.completed, batch_completed=self._batch_succeeded,
            batch_size=self._batch_accepted, target=self._batch_target,
        )
        while not self.checkpoint_event.is_set() and not self.stop_event.is_set():
            await asyncio.sleep(0.2)
        should_continue = self.checkpoint_decision and not self.stop_event.is_set()
        if should_continue:
            self._batch_accepted = 0
            self._batch_finished = 0
            self._batch_succeeded = 0
            self._batch_checkpoint_open = False
            self.pause_event.set()
            self.emit("batch_started", target=self._batch_target)
        else:
            self._restart_allowed = False
        return should_continue


class AuthorDownloadWorker(AutomationWorker):
    """Collect and download all currently available public videos on an author page."""

    def __init__(self, config: AppConfig, events: queue.Queue[dict[str, Any]], url: str, operation_id: str | None = None):
        super().__init__(config, events, operation_id)
        self.url = url

    def run(self) -> None:
        bind(self.operation_id, self.task_id)
        log("INFO", "automation", "task.started", "Author download started", operation_id=self.operation_id, task_id=self.task_id, url=self.url)
        try:
            total, success = asyncio.run(self._run_author())
            self._terminal_outcome = "cancelled" if self.stop_event.is_set() else "success"
            log_finish("task", "automation", self.operation_id, self._started_at, self._terminal_outcome,
                       "Author download ended", task_id=self.task_id, total=total, success=success)
            self.emit("author_finished", total=total, success=success, url=self.url)
        except Exception as exc:
            log_finish("task", "automation", self.operation_id, self._started_at, "failed", "Author download failed", task_id=self.task_id, error=exc)
            self.emit("author_error", text=f"作者主页下载失败：{exc}", url=self.url)
            self.emit("author_finished", total=0, success=0, url=self.url)

    async def _run_author(self) -> tuple[int, int]:
        from playwright.async_api import async_playwright

        target = Path(self.config.download_dir)
        target.mkdir(parents=True, exist_ok=True)
        edge = edge_executable()
        if not edge:
            raise RuntimeError("未找到 Microsoft Edge，请安装 Edge 后重试。")
        log("INFO", "browser", "browser.launch_requested", "Launching Edge for author download", operation_id=self.operation_id, task_id=self.task_id, executable=Path(edge).name)
        self.emit("author_status", text="正在打开作者主页并读取视频列表…")
        self._write_diagnostic("收到作者主页下载请求", {"url": self.url, "target": str(target)})
        profile_dir = app_data_dir() / "browser_profile_author"
        async with async_playwright() as pw:
            scan_context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), executable_path=edge, headless=False,
                accept_downloads=True, viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled", "--start-minimized", "--no-first-run", "--no-default-browser-check"],
                ignore_default_args=["--no-sandbox"],
            )
            download_context = scan_context
            log("INFO", "browser", "browser.launch_completed", "Author Edge context opened", operation_id=self.operation_id, task_id=self.task_id)
            self._browser_contexts = [scan_context]
            scan_page = scan_context.pages[0] if scan_context.pages else await scan_context.new_page()
            await self._set_browser_window_visible(scan_page, False)
            await scan_page.goto(self.url, wait_until="domcontentloaded", timeout=90_000)
            links: dict[str, str] = {}
            stable_rounds = 0
            for _ in range(40):
                await self._wait_if_paused()
                found = await scan_page.evaluate(
                    """() => [...document.querySelectorAll("a[href*='/video/']")].map(a => ({url: a.href.split('?')[0], title: (a.innerText || a.getAttribute('aria-label') || '').trim()}))"""
                )
                before = len(links)
                for item in found or []:
                    url = str(item.get("url") or "")
                    if "/video/" not in url or url in links:
                        continue
                    links[url] = str(item.get("title") or "").strip()
                stable_rounds = stable_rounds + 1 if len(links) == before else 0
                if stable_rounds >= 4:
                    break
                await scan_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(max(1.0, float(self.config.scroll_delay_seconds)))
            items = []
            for url, title in links.items():
                if source_exists(url):
                    continue
                if not title:
                    title = await self._author_title(download_context, url)
                item = {"url": url, "title": title or "TikTok视频", "likes": 0, "comments": 0, "favorites": 0, "duration": 0.0}
                item["record_id"] = add_candidate({"source_url": url, "title": item["title"], "duration_seconds": 0, "likes": 0, "comments": 0, "favorites": 0})
                self.emit("candidate", record_id=item["record_id"])
                items.append(item)
            self._write_diagnostic("作者主页视频读取完成", {"url": self.url, "found": len(links), "queued": len(items)})
            self.emit("author_status", text=f"已读取 {len(links)} 条视频，开始并行下载 {len(items)} 条…")
            worker_count = max(1, min(4, int(self.config.max_concurrent_downloads)))
            pages = [await download_context.new_page()]
            await self._set_browser_window_visible(pages[0], False)
            for _ in range(worker_count - 1):
                page = await download_context.new_page()
                await self._set_browser_window_visible(page, False)
                pages.append(page)
            pending: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            for item in items:
                await pending.put(item)
            lock = asyncio.Lock()
            tasks = [asyncio.create_task(self._author_consumer(page, target, pending, lock)) for page in pages]
            await pending.join()
            self.stop_event.set()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            success = sum(1 for item in items if source_exists(item["url"]))
            return len(items), success

    async def _author_consumer(self, page, target: Path, pending: asyncio.Queue[dict[str, Any]], lock: asyncio.Lock) -> None:
        while not self.stop_event.is_set():
            try:
                item = await asyncio.wait_for(pending.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            try:
                record_id = int(item["record_id"])
                self._active_record_ids.add(record_id)
                self._download_active = True
                if page.is_closed():
                    page = await self._browser_contexts[0].new_page()
                    self._write_diagnostic("SaveSora 标签页已自动重建", {"mode": "author", "recordId": record_id})
                self._set_download_progress(item, 5, "正在下载", "作者主页视频：SaveSora 正在解析")
                download = await self._prepare_download_logged(page, item)
                await self._save_prepared_download(download, item, target, lock, count_toward_batch=False, event_kind="author_downloaded")
            except Exception as exc:
                self._set_download_progress(item, 0, "失败", str(exc))
                update_video_status(int(item.get("record_id", 0)), "失败", 0, str(exc))
                self._finish_active_record(int(item.get("record_id", 0)))
            finally:
                pending.task_done()

    @staticmethod
    async def _author_title(context, url: str) -> str:
        try:
            response = await context.request.get("https://www.tiktok.com/oembed", params={"url": url}, timeout=20_000)
            if response.ok:
                data = await response.json()
                return str(data.get("title", "")).strip()
        except Exception:
            pass
        return ""


class ManualDownloadWorker(AutomationWorker):
    """Download one user-supplied TikTok URL without running the feed scanner."""

    def __init__(self, config: AppConfig, events: queue.Queue[dict[str, Any]], url: str, operation_id: str | None = None):
        super().__init__(config, events, operation_id)
        self.url = url

    def run(self) -> None:
        bind(self.operation_id, self.task_id)
        log("INFO", "automation", "task.started", "Manual download started", operation_id=self.operation_id, task_id=self.task_id, url=self.url)
        try:
            success = asyncio.run(self._run_manual())
            outcome = "cancelled" if self.stop_event.is_set() else "success" if success else "failed"
            log_finish("task", "automation", self.operation_id, self._started_at, outcome, "Manual download ended", task_id=self.task_id)
            self.emit("manual_finished", success=success, url=self.url)
        except Exception as exc:
            log_finish("task", "automation", self.operation_id, self._started_at, "failed", "Manual download failed", task_id=self.task_id, error=exc)
            self.emit("manual_error", text=f"单链接下载失败：{exc}", url=self.url)
            self.emit("manual_finished", success=False, url=self.url)

    async def _run_manual(self) -> bool:
        from playwright.async_api import async_playwright

        target = Path(self.config.download_dir)
        target.mkdir(parents=True, exist_ok=True)
        edge = edge_executable()
        if not edge:
            raise RuntimeError("未找到 Microsoft Edge。请安装 Edge 后重试。")
        log("INFO", "browser", "browser.launch_requested", "Launching Edge for manual download", operation_id=self.operation_id, task_id=self.task_id, executable=Path(edge).name)
        if source_exists(self.url):
            raise RuntimeError("该视频已经下载完成，未重复下载")

        self._write_diagnostic("收到单链接下载请求", {"url": self.url, "target": str(target)})
        self.emit("manual_status", text="正在解析单独输入的视频链接…")
        profile_dir = app_data_dir() / "browser_profile_manual_download"
        async with async_playwright() as pw:
            context = await pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir), executable_path=edge, headless=False,
                accept_downloads=True, viewport={"width": 1280, "height": 900},
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-notifications",
                    "--no-first-run",
                    "--start-minimized",
                ],
            )
            self._browser_contexts = [context]
            page = context.pages[0] if context.pages else await context.new_page()
            await self._set_browser_window_visible(page, False)
            try:
                title = await self._manual_title(context, self.url)
                item = {
                    "url": self.url,
                    "title": title,
                    "likes": 0,
                    "comments": 0,
                    "favorites": 0,
                    "duration": 0.0,
                }
                item["record_id"] = add_candidate(
                    {
                        "source_url": self.url,
                        "title": title,
                        "duration_seconds": 0,
                        "likes": 0,
                        "comments": 0,
                        "favorites": 0,
                    }
                )
                self.emit("candidate", record_id=item["record_id"])
                self._active_record_ids.add(int(item["record_id"]))
                self._download_active = True
                self._current_record_id = int(item["record_id"])
                self._set_download_progress(item, 5, "正在下载", "单链接下载：SaveSora 正在解析")
                try:
                    download = await self._prepare_download_logged(page, item)
                except Exception as exc:
                    self._set_download_progress(item, 0, "失败", str(exc))
                    raise
                success = await self._save_prepared_download(
                    download, item, target, asyncio.Lock(), count_toward_batch=False,
                )
                self._write_diagnostic(
                    "单链接下载完成" if success else "单链接下载未完成",
                    {"url": self.url, "recordId": item["record_id"], "success": success},
                )
                return success
            finally:
                with contextlib.suppress(Exception):
                    await context.close()
                self._browser_contexts.clear()

    @staticmethod
    async def _manual_title(context, url: str) -> str:
        video_id_match = re.search(r"/video/(\d+)", url)
        fallback = f"TikTok_{video_id_match.group(1)}" if video_id_match else "TikTok视频"
        try:
            response = await context.request.get(
                "https://www.tiktok.com/oembed", params={"url": url}, timeout=30_000,
            )
            if response.ok:
                data = await response.json()
                return str(data.get("title", "")).strip() or fallback
        except Exception:
            pass
        return fallback
