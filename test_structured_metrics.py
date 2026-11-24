import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_feed_metrics as fixtures
from feed_metrics import snapshot_candidate, normalize_api_item, counter_value, stored_metric, display_metric
from automation import compact_number
import database


class PolicyTests(unittest.TestCase):
    def snapshot(self):
        return dict(state='visible_card',video_id=fixtures.VIDEO_ID,url=fixtures.URL,identity=fixtures.VIDEO_ID,
                    counters=[],structured_stats={'diggCount':'500001'},duration=12)

    def test_optional_missing_is_unknown_not_zero(self):
        snapshot=self.snapshot()
        item=snapshot_candidate(snapshot,{},compact_number,{'likes'})
        self.assertEqual(item['likes'],500001)
        self.assertIsNone(item['comments']);self.assertIsNone(item['favorites'])
        self.assertEqual(item['metric_source'],'card_structured')
        self.assertEqual(set(item['unknown_metrics']),{'comments','favorites'})

    def test_positive_gate_requires_actual_value(self):
        snapshot=self.snapshot()
        self.assertIsNone(snapshot_candidate(snapshot,{},compact_number,{'likes','comments'}))
        self.assertEqual(snapshot['reason'],'missing_required_metrics')
        self.assertEqual(snapshot['missing_required'],['comments'])

    def test_card_stats_beat_stale_cache_and_rounded_dom(self):
        snapshot=self.snapshot();snapshot['counters']=['500K','100','200']
        cached=normalize_api_item(fixtures.api_item());cached['likes']=499999
        item=snapshot_candidate(snapshot,{fixtures.VIDEO_ID:cached},compact_number,{'likes'})
        self.assertEqual(item['likes'],500001)
        self.assertEqual(item['comments'],10)
        self.assertEqual(item['metric_sources']['comments'],'api_cache')

    def test_wrong_id_cache_is_never_borrowed(self):
        snapshot=self.snapshot();snapshot['structured_stats']={}
        wrong=normalize_api_item(fixtures.api_item());wrong['url']=fixtures.URL.replace(fixtures.VIDEO_ID,'2234567890123456789')
        self.assertIsNone(snapshot_candidate(snapshot,{fixtures.VIDEO_ID:wrong},compact_number,{'likes'}))

    def test_invalid_modern_value_can_use_valid_legacy(self):
        snapshot=self.snapshot();snapshot['structured_stats']['diggCount']='bad'
        snapshot['structured_legacy_stats']={'diggCount':500002}
        item=snapshot_candidate(snapshot,{},compact_number,{'likes'})
        self.assertEqual(item['likes'],500002)

    def test_api_optional_fields_respect_active_rules(self):
        raw=fixtures.api_item();raw['stats'].pop('collectCount')
        self.assertIsNone(normalize_api_item(raw))
        item=normalize_api_item(raw,{'likes'},False)
        self.assertIsNone(item['favorites'])
        self.assertEqual(item['unknown_metrics'],['favorites'])

    def test_zero_is_known_invalid_values_are_unknown(self):
        self.assertEqual(counter_value('0'),0)
        for value in (None,-1,True,float('nan'),1.5,'bad','500K'):
            self.assertIsNone(counter_value(value))
        self.assertEqual(stored_metric(None),-1)
        self.assertEqual(display_metric(-1),'未知')
        self.assertEqual(display_metric(0),'0')

    def test_unknown_survives_database_write_without_migration(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(database,'DB_PATH',Path(folder)/'test.db'):
            database.initialize()
            database.add_candidate(dict(source_url=fixtures.URL,title='fixture',likes=500001,comments=None,favorites=None))
            row=database.query_videos()[0]
            self.assertEqual((row['likes'],row['comments'],row['favorites']),(500001,-1,-1))


class StructuredDomTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.FeedDomTests.asyncSetUp
    asyncTearDown=fixtures.FeedDomTests.asyncTearDown

    async def install(self,item,stats=''):
        await self.page.set_content(fixtures.card(stats=stats))
        await self.page.evaluate("item=>document.querySelector('article').__reactProps$test={item}",item)

    async def test_complete_structured_metrics_need_no_visible_counters(self):
        await self.install(fixtures.api_item())
        items=await self.worker._extract_candidates(self.page)
        self.assertEqual(items[0]['likes'],600000)
        self.assertEqual(items[0]['metric_source'],'card_structured')
        self.assertFalse(self.worker._last_metric_snapshot['metrics_visible'])
        self.assertTrue(self.worker._qualifies(items[0]))

    async def test_likes_only_can_pass_zero_optional_gates(self):
        raw=fixtures.api_item();raw['stats']={'diggCount':600000}
        await self.install(raw)
        items=await self.worker._extract_candidates(self.page)
        self.assertTrue(self.worker._qualifies(items[0]))
        self.assertIsNone(items[0]['favorites'])
        self.worker.config.min_favorites=1
        self.assertEqual(await self.worker._extract_candidates(self.page),[])
        self.assertIn('favorites',self.worker._last_metric_snapshot['missing_required'])

    async def test_precise_structured_threshold_not_display_abbreviation(self):
        raw=fixtures.api_item();raw['statsV2']={'diggCount':'499999','commentCount':'0','collectCount':'0'}
        await self.install(raw,fixtures.STATS.replace('600K','500K'))
        item=(await self.worker._extract_candidates(self.page))[0]
        self.assertEqual(item['likes'],499999)
        self.assertFalse(self.worker._qualifies(item))
