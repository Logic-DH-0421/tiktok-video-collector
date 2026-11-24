import asyncio
import copy
import queue
import unittest
from unittest.mock import patch

from playwright.async_api import async_playwright
from automation import AutomationWorker, compact_number
from config_store import AppConfig, edge_executable
from feed_metrics import RecoveryGuard, normalize_api_item, valid_video_url


VIDEO_ID="1234567890123456789"
URL=f"https://www.tiktok.com/@fixture/video/{VIDEO_ID}"
STATS='<strong data-e2e="like-count">600K</strong><strong data-e2e="comment-count">10</strong><strong data-e2e="favorite-count">20</strong>'


def card(extra="", stats=STATS, style="height:600px;width:400px"):
    return f'<article style="{style}"><video style="width:300px;height:400px"></video>{stats}{extra}</article>'


def api_item():
    return {"id":VIDEO_ID,"author":{"uniqueId":"fixture"},"video":{"duration":12},
            "stats":{"diggCount":600000,"commentCount":10,"collectCount":20}}


class FeedPolicyTests(unittest.TestCase):
    def test_only_verified_tiktok_permalink_accepted(self):
        self.assertTrue(valid_video_url(URL))
        for url in ("https://evil.example/@a/video/123","https://tiktok.com.evil.example/@a/video/123", "javascript:alert(1)"):
            self.assertFalse(valid_video_url(url))

    def test_api_string_counters_and_stats_v2(self):
        item=api_item()
        item["statsV2"]={key:str(value) for key,value in item.pop("stats").items()}
        self.assertEqual(normalize_api_item(item)["likes"],600000)

    def test_api_incomplete_and_invalid_not_zero_filled(self):
        for field,value in (("collectCount",None),("diggCount","bad"),("commentCount",-1)):
            item=api_item();item["stats"][field]=value
            self.assertIsNone(normalize_api_item(item))
        item=api_item();item["video"]["duration"]=float("nan")
        self.assertIsNone(normalize_api_item(item))

    def test_unknown_duration_does_not_pass_active_bounds(self):
        worker=AutomationWorker(AppConfig(min_likes=0,max_duration_seconds=60),queue.Queue())
        for duration in (None,0,float("nan"),float("inf")):
            self.assertIn("时长未知"," ".join(worker._qualification_failures({"duration":duration})))
        worker.config.max_duration_seconds=0
        self.assertTrue(worker._qualifies({"duration":None}))
        worker.config.min_duration_seconds=1
        self.assertFalse(worker._qualifies({"duration":None}))

    def test_duration_and_metric_boundaries(self):
        worker=AutomationWorker(AppConfig(min_likes=500000,min_comments=10,min_favorites=20,
                                          min_duration_seconds=5,max_duration_seconds=60),queue.Queue())
        for duration,expected in ((4.9,False),(5,True),(60,True),(60.1,False)):
            self.assertEqual(worker._qualifies(dict(likes=500000,comments=10,favorites=20,duration=duration)),expected)

    def test_recovery_does_not_reload_visible_or_recent_healthy_feed(self):
        guard=RecoveryGuard(0)
        self.assertFalse(any(guard.timeout(t,True,10) for t in (10,20,80,140)))
        guard.last_api=140
        self.assertFalse(any(guard.timeout(t,False,10) for t in (150,160,170)))
        self.assertTrue(guard.timeout(210,False,10))
        self.assertFalse(guard.timeout(220,False,10))

    def test_compact_number_zero_and_units(self):
        for text,expected in (("0",0),("1.5M",1500000),("50万",500000),("1,234",1234)):
            self.assertEqual(compact_number(text),expected)


class FeedDomTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright=await async_playwright().start()
        self.browser=await self.playwright.chromium.launch(executable_path=edge_executable(),headless=True)
        self.page=await self.browser.new_page(viewport={"width":1000,"height":800})
        self.worker=AutomationWorker(AppConfig(max_duration_seconds=60),queue.Queue())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_hidden_counters_are_not_metrics(self):
        await self.page.set_content(card(style="display:none"))
        self.assertFalse(await self.worker._page_has_readable_metrics(self.page))
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_visible_counters_without_identity_waits_safely(self):
        await self.page.set_content(card())
        self.assertTrue(await self.worker._page_has_readable_metrics(self.page))
        self.assertEqual(await self.worker._extract_candidates(self.page),[])
        self.assertEqual(self.worker._last_metric_snapshot["reason"],"missing_permalink")

    async def test_sibling_permalink_outside_small_stats_wrapper(self):
        await self.page.set_content(f'<div><div><video style="width:300px;height:400px"></video>{STATS}</div><a href="{URL}">link</a></div>')
        self.worker.config.max_duration_seconds=0
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]["url"],URL)

    async def test_react_card_identity_without_permalink(self):
        await self.page.set_content(card())
        await self.page.evaluate("item => document.querySelector('video')['__reactProps$test']={item}",api_item())
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual((items[0]["url"],items[0]["duration"]),(URL,12))

    async def test_react_parent_component_recovers_identity(self):
        await self.page.set_content(card())
        await self.page.evaluate("""item => {
          const video=document.querySelector('video');
          video.__reactFiber$test={stateNode:video,memoizedProps:{src:'blob:fixture'},return:
            {memoizedProps:{item},return:{stateNode:document.querySelector('article')}}};
        }""",api_item())
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]["url"],URL)
        self.assertEqual(self.worker._last_metric_snapshot["link_source"],"react_component")
        self.assertGreaterEqual(self.worker._last_metric_snapshot["react_depth"],1)

    async def test_canvas_player_hidden_video_item_on_descendant_component(self):
        await self.page.set_content(card('<canvas width="300" height="400"></canvas><div id="media-provider"></div>').replace('width:300px;height:400px','width:0;height:0'))
        await self.page.evaluate("""item => {
          const node=document.getElementById('media-provider');
          node.__reactFiber$test={stateNode:node,memoizedProps:{},return:{memoizedProps:{item},return:{stateNode:document.querySelector('article')}}};
        }""",api_item())
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]['url'],URL)

    async def test_normalized_author_string_uses_unique_same_card_author(self):
        await self.page.set_content(card('<a href="https://www.tiktok.com/@fixture">author</a>'))
        item=api_item();item['author']='author-store-key'
        await self.page.evaluate("item => document.querySelector('article').__reactProps$test={item}",item)
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]['url'],URL)
        self.assertEqual(self.worker._last_metric_snapshot['identity_source'],'react_id_card_author')

    async def test_normalized_author_string_can_resolve_exact_api_cache(self):
        await self.page.set_content(card())
        item=api_item();item['author']='author-store-key'
        await self.page.evaluate("item => document.querySelector('article').__reactProps$test={item}",item)
        self.worker._api_metric_cache[VIDEO_ID]=normalize_api_item(api_item())
        self.assertEqual((await self.worker._extract_candidates(self.page))[0]['url'],URL)

    async def test_normalized_author_with_multiple_profiles_does_not_guess(self):
        await self.page.set_content(card('<a href="https://www.tiktok.com/@fixture">author</a><a href="https://www.tiktok.com/@other">mention</a>'))
        item=api_item();item['author']='author-store-key'
        await self.page.evaluate("item => document.querySelector('article').__reactProps$test={item}",item)
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_nested_card_item_is_not_used_for_outer_card(self):
        await self.page.set_content(card('<article style="display:none"><div id="nested-provider"></div></article>'))
        await self.page.evaluate("item => document.getElementById('nested-provider').__reactProps$test={item}",api_item())
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_react_detail_wrapper_on_parent_component(self):
        await self.page.set_content(card())
        await self.page.evaluate("item => document.querySelector('video').__reactFiber$test={return:{memoizedProps:{itemInfo:{itemStruct:item}}}}",api_item())
        self.assertEqual((await self.worker._extract_candidates(self.page))[0]["url"],URL)

    async def test_react_does_not_cross_card_host_boundary(self):
        await self.page.set_content(card()+card())
        await self.page.evaluate("""item => {
          document.querySelector('video').__reactFiber$test={memoizedProps:{},return:
            {stateNode:document.body,memoizedProps:{item}}};
        }""",api_item())
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_react_feed_array_and_cyclic_fiber_are_not_searched(self):
        await self.page.set_content(card())
        await self.page.evaluate("""item => {
          const fiber={memoizedProps:{data:[item],children:[{props:{item}}]}};
          fiber.return=fiber;document.querySelector('video').__reactFiber$test=fiber;
        }""",api_item())
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_react_identity_conflict_is_not_downloaded(self):
        await self.page.set_content(card(f'<a href="{URL.replace(VIDEO_ID,"2234567890123456789")}">other</a>'))
        await self.page.evaluate("item => document.querySelector('video')['__reactProps$test']={item}",api_item())
        self.assertEqual(await self.worker._extract_candidates(self.page),[])
        self.assertEqual(self.worker._last_metric_snapshot["state"],"identity_conflict")

    async def test_cache_resolves_id_without_link(self):
        self.worker._api_metric_cache[VIDEO_ID]=normalize_api_item(api_item())
        await self.page.set_content(card().replace('<article ',f'<article data-video-id="{VIDEO_ID}" '))
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]["url"],URL)
        self.assertEqual(items[0]["duration"],12)
        self.assertEqual(self.worker._last_metric_snapshot["link_source"],"api_id_match")

    async def test_neighbour_link_not_borrowed(self):
        await self.page.set_content(card()+f'<aside><a href="{URL}">unrelated</a></aside>')
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_card_permalink_with_unknown_duration_is_not_qualified(self):
        await self.page.set_content(card(f'<a href="{URL}">video</a>'))
        self.assertEqual(await self.worker._extract_candidates(self.page),[])
        self.assertEqual(self.worker._last_metric_snapshot["reason"],"unknown_duration")
        self.worker._api_metric_cache[VIDEO_ID]=normalize_api_item(api_item())
        item=(await self.worker._extract_candidates(self.page))[0]
        self.assertEqual(item["likes"],600000)
        self.assertTrue(self.worker._qualifies(item))

    async def test_multiple_cards_do_not_mix_counters(self):
        second=card(f'<a href="{URL.replace(VIDEO_ID,"2234567890123456789")}">next</a>',stats=STATS.replace("600K","1"))
        await self.page.set_content(card(f'<a href="{URL}">current</a>')+second)
        self.worker.config.max_duration_seconds=0
        item=(await self.worker._extract_candidates(self.page))[0]
        self.assertEqual((item["url"],item["likes"]),(URL,600000))

    async def test_detail_route_recovers_missing_anchor(self):
        await self.page.route("**/*",lambda route:route.fulfill(body=card(),content_type="text/html"))
        await self.page.goto(URL)
        self.worker.config.max_duration_seconds=0
        self.assertEqual((await self.worker._extract_candidates(self.page))[0]["url"],URL)

    async def test_response_detail_and_alternate_list_are_captured(self):
        class Response:
            ok=True;status=200;url="https://www.tiktok.com/api/item/detail/?token=private"
            async def json(self):return {"itemInfo":{"itemStruct":api_item()}}
        output=asyncio.Queue()
        await self.worker._capture_recommend_response(Response(),output)
        self.assertEqual(output.get_nowait()["url"],URL)
        self.assertIn(VIDEO_ID,self.worker._api_metric_cache)
        Response.url="https://www.tiktok.com/api/recommend/item_list/"
        async def alternate():return {"item_list":[api_item()]}
        response=Response();response.json=alternate
        await self.worker._capture_recommend_response(response,output)
        self.assertEqual(output.qsize(),1)

    async def test_response_failure_is_logged_not_healthy(self):
        class Response:
            ok=False;status=503;url="https://www.tiktok.com/api/recommend/item_list/"
        output=asyncio.Queue()
        before=self.worker._feed_recovery.last_api
        with patch("automation.log") as log:
            await self.worker._capture_recommend_response(Response(),output)
            self.assertEqual(log.call_args.args[2],"feed.response_http_failed")
        self.assertTrue(output.empty())
        self.assertEqual(self.worker._feed_recovery.last_api,before)

    async def test_advance_does_not_scroll_twice(self):
        await self.page.set_content(card(f'<a href="{URL}">current</a>')+
                                    card(f'<a href="{URL.replace(VIDEO_ID,"2234567890123456789")}">next</a>'))
        await self.page.evaluate("window.scrollBy=(...args)=>{window.scrollCalls=(window.scrollCalls||0)+1}; document.addEventListener('keydown',e=>{if(e.key==='ArrowDown')document.querySelector('article').remove()})")
        await self.worker._advance_tiktok(self.page)
        self.assertEqual(await self.page.evaluate("window.scrollCalls||0"),0)
