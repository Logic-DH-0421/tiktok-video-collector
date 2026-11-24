class _Patch:
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
