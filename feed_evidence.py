"""Bounded, value-free failure evidence and identity-checked share recovery."""
import asyncio
import re
from feed_metrics import valid_video_url


EVIDENCE_SCRIPT = r"""marker => {
  const root=[...document.querySelectorAll('[data-collector-card]')].find(n=>n.dataset.collectorCard===marker);
  const safeKey=k=>/^[A-Za-z_$][A-Za-z0-9_$]{0,47}$/.test(k) && !/cookie|token|secret|password|authorization/i.test(k);
  const keys=o=>o && typeof o==='object' && !Array.isArray(o) ? Object.keys(o).filter(safeKey).slice(0,24) : [];
  const result={schema:1,card_found:!!root,video_elements:document.querySelectorAll('video').length,
    article_elements:document.querySelectorAll('article').length,dom:[],components:[]};
  if(!root)return result;
  const nodes=[root,...root.querySelectorAll('*')].slice(0,160);
  const seen=new Set();
  for(const node of nodes) {
    if(result.dom.length<28)result.dom.push({tag:node.tagName.toLowerCase(),children:node.children.length,
      has_video_id:node.hasAttribute('data-video-id') || node.hasAttribute('data-item-id'),
      link_kind:node.hasAttribute('href') ? (/\/video\/\d+/.test(node.getAttribute('href'))?'video':'other'):'none'});
    for(const key of Object.keys(node).filter(k=>k.startsWith('__reactFiber$') || k.startsWith('__reactProps$'))) {
      let fiber=key.startsWith('__reactFiber$') ? node[key] : {memoizedProps:node[key]};
      for(let depth=0;fiber && depth<32;depth++,fiber=fiber.return) {
        if(seen.has(fiber))break;seen.add(fiber);
        if(fiber.stateNode?.nodeType===1 && !root.contains(fiber.stateNode))break;
        const props=fiber.memoizedProps;
        if(result.components.length>=28)break;
        const names=keys(props);
        result.components.push({depth,fields:names,objects:names.filter(k=>k!=='children' && k!=='style' && props[k] && typeof props[k]==='object' && !Array.isArray(props[k])).slice(0,8).map(k=>({field:k,fields:keys(props[k]),
          id_valid:/^[0-9]{15,22}$/.test(String(props[k].id||'')),has_video:!!props[k].video,
          author_type:typeof props[k].author,author_fields:keys(props[k].author),has_unique_name:!!props[k].author?.uniqueId}))});
      }
    }
  }
  return result;
}"""

SHARE_LINKS_SCRIPT = r"""() => {
  const visible=n=>{const r=n.getBoundingClientRect(),s=getComputedStyle(n);return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';};
  return [...document.querySelectorAll('input[readonly],textarea[readonly],[role="dialog"] a[href*="/video/"],[data-e2e*="share"] a[href*="/video/"]')]
    .filter(visible).map(n=>n.value || n.href || '').filter(v=>/^https:\/\/(www\.)?tiktok\.com\/@[^/]+\/video\/[0-9]+/.test(v));
}"""


async def recover_share_link(page, snapshot, inspect):
    """Only accept newly exposed share links after clicking this exact card.

    Never read/write the OS clipboard. Recheck the card identity after opening
    the popup; ambiguity, a changed video or absent controls is a safe failure.
    """
    marker = snapshot.get("card_marker")
    if not marker or not re.fullmatch(r"[A-Za-z0-9-]{1,64}",str(marker)) or not snapshot.get("identity"):
        return None, "no_card_identity"
    root = page.locator(f'[data-collector-card="{marker}"]')
    control = root.locator('[data-e2e="share-icon"], [data-e2e="share-button"], button[aria-label*="share" i], button[aria-label*="分享"]')
    if await control.count() == 0:
        return None, "no_card_share_control"
    before = set(await page.evaluate(SHARE_LINKS_SCRIPT))
    clicked = False
    try:
        await control.first.click(timeout=2000)
        clicked = True
        await asyncio.sleep(0.4)
        after = await inspect()
        if after.get("identity") != snapshot.get("identity") or after.get("card_marker") != marker:
            return None, "card_changed"
        links = set(await page.evaluate(SHARE_LINKS_SCRIPT)) - before
        urls = {value.split("?",1)[0].rstrip("/") for value in links}
        urls = {value for value in urls if valid_video_url(value)}
        video_id = str(snapshot.get("video_id") or "")
        if video_id:
            urls = {url for url in urls if url.rsplit("/",1)[-1] == video_id}
        if len(urls) != 1:
            return None, "ambiguous_or_missing_share_link"
        return next(iter(urls)), "card_share_dialog"
    finally:
        if clicked and not page.is_closed():
            await page.keyboard.press("Escape")
