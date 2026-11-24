"""Current-card extraction and conservative, identity-bound metric recovery."""
from __future__ import annotations

import hashlib
import math
import re
from urllib.parse import urlsplit


CARD_SCRIPT = r"""() => {
  const visible = el => {
    if (!el) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity) !== 0
      && r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < innerHeight
      && r.right > 0 && r.left < innerWidth;
  };
  const selectors = ['[data-e2e="like-count"], strong[data-e2e*="like"], button[aria-label*="like" i] strong',
    '[data-e2e="comment-count"], strong[data-e2e*="comment"], button[aria-label*="comment" i] strong',
    '[data-e2e="favorite-count"], [data-e2e="bookmark-count"], [data-e2e="undefined-count"], button[aria-label*="favorite" i] strong'];
  const hasStats = root => selectors.some(s => [...root.querySelectorAll(s)].some(visible));
  const findCard = element => {
    let node = element, fallback = null;
    for (let depth = 0; node && depth < 14; depth++, node = node.parentElement) {
      const videos = node.querySelectorAll('video');
      if (videos.length > 1) break; // Never merge neighbouring video cards.
      if (node === document.body || node === document.documentElement) break;
      // The smallest stats wrapper often omits the sibling permalink.
      // Keep the widest single-video wrapper, never the whole document.
      if (videos.length === 1 && hasStats(node)) fallback = node;
      if (node.matches('[data-e2e="recommend-list-item-container"], [data-e2e="recommend-list-item"], article')
          && (videos.length || hasStats(node))) return node;
    }
    return fallback;
  };
  const roots = [];
  const add = root => {if(root && root.querySelectorAll('video').length<=1 && visible(root) && !roots.includes(root)) roots.push(root);};
  for (const video of document.querySelectorAll('video')) if (visible(video)) add(findCard(video));
  for (const node of document.querySelectorAll('[data-e2e="recommend-list-item-container"], [data-e2e="recommend-list-item"], article'))
    if (hasStats(node) || node.querySelector('video,canvas')) add(node);
  const score = root => {
    const el = [...root.querySelectorAll('video')].find(visible) || root;
    const r = el.getBoundingClientRect();
    return Math.max(0, Math.min(r.bottom,innerHeight)-Math.max(r.top,0))
      * Math.max(0,Math.min(r.right,innerWidth)-Math.max(r.left,0));
  };
  roots.sort((a,b)=>score(b)-score(a));
  const root = roots[0];
  if (!root) return {state:'no_visible_card', counters:[], identity:''};
  if(!root.dataset.collectorCard) root.dataset.collectorCard=crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2);
  const text = selector => [...root.querySelectorAll(selector)].find(visible)?.textContent?.trim() ?? null;
  const counters = selectors.map(text);
  const video = [...root.querySelectorAll('video')].find(visible);
  const title = root.querySelector('[data-e2e="browse-video-desc"], [data-e2e="video-desc"], h1, h2')?.textContent?.trim() || '';
  let id = '';
  let boundItem = null;
  let identitySource = '', reactNodes = 0, reactDepth = 0;
  const inspected = new Set();
  const inspectProps = (props, source) => {
    if (!props || typeof props !== 'object' || inspected.has(props)) return;
    inspected.add(props);reactNodes++;
    const values=[props,props.item,props.itemInfo,props.itemData,props.data,props.videoData];
    // Known detail wrappers only; never recursively search children or arrays.
    for (const value of [...values]) if(value && !Array.isArray(value)) {
      values.push(value.itemStruct, value.itemInfo?.itemStruct);
    }
    for (const item of values) {
      if (item && !Array.isArray(item) && /^[0-9]{15,22}$/.test(String(item.id||''))
          && item.video) {
        if (boundItem && String(boundItem.id)!==String(item.id)) return false;
        boundItem=item;identitySource=source;
      }
    }
    return true;
  };
  // Host DOM props often contain only src/style. Video data belongs to a
  // function component higher in the Fiber return chain. Stay inside the
  // selected card's host boundary, and never inspect siblings/feed arrays.
  const visitedFibers=new Set();
  // Canvas-backed players may have a zero-sized video. The item provider is
  // below the article host, so inspect descendants of this card as well.
  // Never descend into another semantic card, or inspect sibling fibers.
  const cardNodes=[video,root,...root.querySelectorAll('*')].filter(Boolean).slice(0,162);
  for (const node of new Set(cardNodes)) {
    const semantic=node.closest('article,[data-e2e="recommend-list-item-container"],[data-e2e="recommend-list-item"]');
    if(semantic && semantic!==root && root.contains(semantic))continue;
    for (const key of Object.keys(node).filter(k=>k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'))) {
      const owner=node[key];
      if (key.startsWith('__reactProps$')) {
        if(inspectProps(owner,'react_dom_props')===false) return {state:'identity_conflict',counters,identity:''};
      } else {
        for(let fiber=owner,level=0;fiber && level<24 && !visitedFibers.has(fiber);fiber=fiber.return,level++) {
          visitedFibers.add(fiber);
          const host=fiber.stateNode;
          if(host?.nodeType===1 && !root.contains(host)) break;
          reactDepth=Math.max(reactDepth,level);
          if(inspectProps(fiber.memoizedProps,'react_component')===false)
            return {state:'identity_conflict',counters,identity:''};
        }
      }
    }
  }
  for(const node of [root,video,...root.querySelectorAll('[data-video-id], [data-item-id], [data-id]')].filter(Boolean)) {
    const value = node.getAttribute('data-video-id') || node.getAttribute('data-item-id') || node.getAttribute('data-id') || node.id || '';
    const match = value.match(/(?:^|[^0-9])([0-9]{15,22})(?:$|[^0-9])/);
    if(match) {id=match[1];break;}
  }
  const canonical = value => {
    try {const u=new URL(value,location.href);const m=u.pathname.match(/^\/@[^/]+\/video\/([0-9]+)\/?$/);
      return ['www.tiktok.com','tiktok.com'].includes(u.hostname) && m ? {url:u.origin+u.pathname.replace(/\/$/,''),id:m[1]} : null;
    } catch {return null;}
  };
  const links=[...root.querySelectorAll('a[href*="/video/"], [data-share-url], input[readonly]')]
    .map(n=>canonical(n.href||n.getAttribute('data-share-url')||n.value||'')).filter(Boolean);
  const own=canonical(root.getAttribute('data-share-url')||'');if(own)links.unshift(own);
  const matching=links.filter(link=>!id || link.id===id);
  const distinct=[...new Set(matching.map(link=>link.url))];
  let url=distinct.length===1?distinct[0]:'';
  if(boundItem) {
    const itemId=String(boundItem.id);
    if((id && id!==itemId) || (url && canonical(url).id!==itemId))
      return {state:'identity_conflict',counters,identity:''};
    id=itemId; // ID alone can resolve the exact network-cache entry.
    let username=typeof boundItem.author?.uniqueId==='string' ? boundItem.author.uniqueId : '';
    if(!username) {
      const authors=[...root.querySelectorAll('a[href*="/@"]')].filter(visible).map(n=>{
        try{const u=new URL(n.href,location.href);return ['tiktok.com','www.tiktok.com'].includes(u.hostname)
          && /^\/@[A-Za-z0-9._]+\/?$/.test(u.pathname) ? u.pathname.replace(/^\/@/,'').replace(/\/$/,'') : null;}catch{return null;}
      }).filter(Boolean);
      const uniqueAuthors=[...new Set(authors)];
      if(uniqueAuthors.length===1) {username=uniqueAuthors[0];identitySource='react_id_card_author';}
    }
    if(username && /^[A-Za-z0-9._]{1,64}$/.test(username)) {
      const bound=canonical('https://www.tiktok.com/@'+encodeURIComponent(username)+'/video/'+id);
      if(bound)url=bound.url;
    }
  }
  if(url) id=canonical(url).id;
  if(!url && roots.length===1) {
    const route=canonical(location.href);
    if(route && (!id || id===route.id)) {url=route.url;id=route.id;}
  }
  const duration=video && Number.isFinite(video.duration) && video.duration>0 ? video.duration
    : Number(boundItem?.video?.duration)>0 ? Number(boundItem.video.duration) : null;
  const selectStats=stats=>Object.fromEntries(['diggCount','commentCount','collectCount'].map(k=>[k,
    ['string','number'].includes(typeof stats?.[k]) ? stats[k] : null]));
  return {state:'visible_card',url,video_id:id,title:title || String(boundItem?.desc||''),counters,duration,
    structured_stats:selectStats(boundItem?.statsV2),structured_legacy_stats:selectStats(boundItem?.stats),
    identity:id || [title,video?.currentSrc||video?.src||'',...counters].join('|'),
    visible_cards:roots.length,link_count:distinct.length,
    card_marker:root.dataset.collectorCard,
    identity_source:identitySource || (url?'card_permalink_or_route':id?'dom_id':'none'),
    react_nodes:reactNodes,react_depth:reactDepth,card_tag:root.tagName.toLowerCase()};
}"""


def valid_video_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme == "https" and parsed.hostname in {"www.tiktok.com", "tiktok.com"} and bool(
            re.fullmatch(r"/@[^/]+/video/\d+", parsed.path))
    except (TypeError, ValueError):
        return False


METRICS = {"likes":"diggCount", "comments":"commentCount", "favorites":"collectCount"}


def counter_value(value):
    """Missing/invalid is unknown, never a manufactured zero."""
    if isinstance(value,bool) or value is None:
        return None
    try:
        if isinstance(value,str) and not re.fullmatch(r"\d+",value.strip()):
            return None
        number=int(value)
        return number if number>=0 and number==float(value) else None
    except (TypeError,ValueError,OverflowError):
        return None


def stored_metric(value):
    number=counter_value(value)
    return number if number is not None else -1


def display_metric(value):
    number=counter_value(value)
    return f"{number:,}" if number is not None else "未知"


def required_metrics(config):
    return {name for name in METRICS if getattr(config,"min_"+name)>0}


def snapshot_candidate(snapshot, cache, parse_number, required=None):
    """Resolve identity first, then card stats -> exact-ID cache -> DOM."""
    identity = str(snapshot.get("identity") or "")
    snapshot["fingerprint"] = hashlib.sha256(identity.encode()).hexdigest()[:12] if identity else ""
    texts = snapshot.get("counters") or []
    snapshot["metrics_visible"] = len(texts) == 3 and all(
        text is not None and re.search(r"\d", str(text)) for text in texts)
    url = str(snapshot.get("url") or "")
    if snapshot.get("state") in {"no_visible_card","identity_conflict"}:
        snapshot["reason"]="page_not_ready" if snapshot["state"]=="no_visible_card" else "identity_conflict"
        return None
    video_id=str(snapshot.get("video_id") or (url.rsplit('/',1)[-1] if valid_video_url(url) else ''))
    cached=cache.get(video_id) or {}
    if not valid_video_url(str(cached.get("url") or '')) or cached['url'].rsplit('/',1)[-1]!=video_id:
        cached={}
    if not valid_video_url(url) and cached:
        url=cached['url'];snapshot['link_source']='api_id_match'
    if not valid_video_url(url):
        snapshot["reason"] = "missing_permalink"
        return None
    if video_id and url.rsplit('/',1)[-1]!=video_id:
        snapshot['reason']='identity_conflict';return None
    snapshot.setdefault("link_source",snapshot.get("identity_source") or "current_card")
    values={};sources={}
    for index,(name,field) in enumerate(METRICS.items()):
        options=[(counter_value((snapshot.get('structured_stats') or {}).get(field)),'card_statsV2'),
                 (counter_value((snapshot.get('structured_legacy_stats') or {}).get(field)),'card_stats'),
                 (counter_value(cached.get(name)),'api_cache')]
        text=texts[index] if index<len(texts) else None
        dom=counter_value(parse_number(text)) if text is not None and re.search(r'\d',str(text)) else None
        options.append((dom,'visible_counter'))
        value,source=next(((v,s) for v,s in options if v is not None),(None,'unknown'))
        values[name]=value;sources[name]=source
    missing=[name for name in (set(METRICS) if required is None else required) if values[name] is None]
    snapshot.update(metric_sources=sources,unknown_metrics=[k for k,v in values.items() if v is None],missing_required=sorted(missing))
    if missing:
        snapshot['reason']='missing_required_metrics';return None
    snapshot.pop('reason',None)
    used={source for source in sources.values() if source!='unknown'}
    kind=('card_structured' if used and used<={'card_stats','card_statsV2'} else
          'tiktok_api' if used=={'api_cache'} else 'visible_card' if used=={'visible_counter'} else 'mixed')
    duration=snapshot.get('duration') or cached.get('duration') or 0
    return {"url":url,"title":str(snapshot.get("title") or cached.get('title') or ""),**values,
            "duration":duration,"metric_source":kind,"metric_sources":sources,
            "unknown_metrics":snapshot['unknown_metrics'],"duration_known":bool(duration)}


def api_items(data):
    if not isinstance(data,dict):
        return []
    items=data.get("itemList",data.get("item_list",[]))
    if not isinstance(items,list):
        items=[]
    detail=data.get("itemInfo",{}).get("itemStruct") if isinstance(data.get("itemInfo"),dict) else None
    return items + ([detail] if isinstance(detail,dict) else [])


def normalize_api_item(item, required=None, require_duration=True):
    if not isinstance(item,dict):return None
    author=item.get("author") or {}
    video=item.get("video") or {}
    legacy=item.get("stats") or {}
    modern=item.get("statsV2") or {}
    if not all(isinstance(value,dict) for value in (author,video,legacy,modern)):return None
    stats={key:modern[key] if counter_value(modern.get(key)) is not None else legacy.get(key)
           for key in METRICS.values()}
    video_id=str(item.get("id") or "")
    username=str(author.get("uniqueId") or "")
    url=f"https://www.tiktok.com/@{username}/video/{video_id}"
    if not valid_video_url(url):return None
    try:
        values={name:counter_value(stats.get(field)) for name,field in METRICS.items()}
        if any(values[name] is None for name in (set(METRICS) if required is None else required)):return None
        try:duration=float(video.get("duration") or 0)
        except (ValueError,TypeError,OverflowError):duration=0
        known=math.isfinite(duration) and duration>0
        if require_duration and not known:return None
        if not known:duration=0
    except (KeyError,ValueError,TypeError,OverflowError):return None
    return {"url":url,"title":str(item.get("desc") or ""),**values,"duration":duration,
            "duration_known":known,"metric_source":"tiktok_api",
            "unknown_metrics":[name for name,value in values.items() if value is None]}


class RecoveryGuard:
    """An empty prefetch queue is not evidence that the API listener failed."""
    def __init__(self, now):
        self.last_api=now
        self.last_reload=now
        self.missing_rounds=0

    def timeout(self, now, metrics_visible, timeout_seconds):
        if metrics_visible:
            self.missing_rounds=0
            return False
        self.missing_rounds+=1
        cooldown=max(60.0,3*timeout_seconds)
        if self.missing_rounds>=3 and now-self.last_api>=cooldown and now-self.last_reload>=cooldown:
            self.last_reload=now
            self.missing_rounds=0
            return True
        return False
