import json
from unittest.mock import AsyncMock
import test_feed_metrics as fixtures
from test_feed_metrics import STATS, URL, VIDEO_ID, card
from feed_evidence import EVIDENCE_SCRIPT


# Reuse the isolated browser lifecycle, not the inherited test suite.
class EvidenceTests(__import__('unittest').IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FeedDomTests.asyncSetUp
    asyncTearDown = fixtures.FeedDomTests.asyncTearDown

    async def share_page(self, links, change=False, video_id=""):
        title='<h2>fixture-title</h2>'
        button='<button data-e2e="share-button">Share</button>'
        markup=card(title+button)
        if video_id:markup=markup.replace('<article ',f'<article data-video-id="{video_id}" ')
        await self.page.set_content(markup+'<div role="dialog" id="fixture-dialog" style="display:none"></div>')
        await self.page.evaluate("""({links,change})=>{
          const dialog=document.getElementById('fixture-dialog');
          for(const url of links){const input=document.createElement('input');input.readOnly=true;input.value=url;dialog.append(input);}
          document.querySelector('button').onclick=()=>{dialog.style.display='block';if(change)document.querySelector('h2').textContent='another-card';};
          document.addEventListener('keydown',event=>{if(event.key==='Escape')dialog.style.display='none';});
        }""",dict(links=links,change=change))
        self.worker.config.max_duration_seconds=0

    async def test_share_dialog_recovery_is_bound_to_same_card(self):
        await self.share_page([URL+'?tracking=private'])
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]['url'],URL)
        self.assertEqual(self.worker._last_metric_snapshot['identity_source'],'card_share_dialog')
        self.assertFalse(await self.page.locator('#fixture-dialog').is_visible())

    async def test_changed_card_during_share_is_rejected(self):
        await self.share_page([URL],change=True)
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_ambiguous_or_conflicting_share_links_rejected(self):
        other=URL.replace(VIDEO_ID,'2234567890123456789')
        await self.share_page([URL,other])
        self.assertEqual(await self.worker._extract_candidates(self.page),[])
        await self.share_page([other],video_id=VIDEO_ID)
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_existing_unrelated_share_url_is_not_borrowed(self):
        await self.share_page([URL])
        await self.page.locator('#fixture-dialog').evaluate("n=>n.style.display='block'")
        self.assertEqual(await self.worker._extract_candidates(self.page),[])

    async def test_evidence_contains_shapes_not_values_or_sensitive_keys(self):
        await self.page.set_content(card('<h2>private-user-caption</h2>'))
        await self.page.evaluate("""() => document.querySelector('article').__reactProps$fixture={
          cookie:'private-cookie',accessToken:'private-token',item:{id:'1234567890123456789',desc:'private-description'}}""")
        await self.worker._extract_candidates(self.page)
        marker=self.worker._last_metric_snapshot['card_marker']
        evidence=await self.page.evaluate(EVIDENCE_SCRIPT,marker)
        text=json.dumps(evidence)
        self.assertTrue(evidence['card_found'])
        for sensitive in ('private-user-caption','private-cookie','private-token','private-description','accessToken','cookie'):
            self.assertNotIn(sensitive,text)
        self.assertLessEqual(len(evidence['components']),28)

    async def test_third_identity_failure_pauses_without_advancing(self):
        await self.page.set_content(card())
        await self.worker._extract_candidates(self.page)
        snapshot=self.worker._last_metric_snapshot
        self.worker._request_manual_attention=AsyncMock()
        self.assertFalse(await self.worker._record_identity_failure(self.page,snapshot))
        self.assertFalse(await self.worker._record_identity_failure(self.page,snapshot))
        self.assertTrue(await self.worker._record_identity_failure(self.page,snapshot))
        self.worker._request_manual_attention.assert_awaited_once()
        self.assertEqual(self.worker._identity_failure_count,0)
