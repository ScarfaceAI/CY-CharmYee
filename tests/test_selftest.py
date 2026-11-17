from __future__ import annotations

from contextlib import nullcontext, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

from support import SKILL_ROOT, read_json, write_json
from douyin_text import acquisition, asr, cli, douyin_browser as browser, media, messages, selftest

URL = 'https://www.douyin.com/video/123'
OTHER = 'https://www.douyin.com/video/456'
MEDIA = 'https://media.example.invalid/private/video.mp4?token=secret'
ROUTES = ['public_share', 'anonymous_browser', 'existing_profile']


def job(url=URL):
    return {'id': 'douyin-' + acquisition.video_id(url), 'media': MEDIA,
            'author': '私有账号信息', 'cookies': '不能落盘的 Cookie'}


class Stream(io.BytesIO):
    def __init__(self, data=b'x', *, status=206, content_type='video/mp4', length=None):
        super().__init__(data)
        self.status = status
        self.headers = {'Content-Type': content_type, 'Content-Length': str(len(data) if length is None else length)}
        self.sizes = []
        self.consumed = 0

    def read(self, size=-1):
        self.sizes.append(size)
        value = super().read(size)
        self.consumed += len(value)
        return value


class SelftestTests(unittest.TestCase):
    def setUp(self):
        base = SKILL_ROOT / '.scratch/m1-selftest'
        base.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.profile = self.root / '.local/douyin-profile'
        self.results = self.root / '.local/selftest/results.json'
        for replacement in (
            patch.dict(os.environ, {'CY_STATE_DIR': str(self.root / 'state'),
                       'CY_MODEL_DIR': str(self.root / 'models'), 'CY_SESSION_ID': 'selftest-chat'}),
            patch.object(cli, 'ROOT', self.root),
            patch.object(messages, 'download_notice', return_value=('', '')),
            patch.object(browser, 'resolve_url', side_effect=AssertionError('不能访问网络')),
            patch.object(acquisition, 'public_metadata', side_effect=AssertionError('不能访问网络')),
            patch.object(browser, 'browser_videos', side_effect=AssertionError('不能启动真实浏览器')),
            patch.object(browser, '_yt_dlp_video', side_effect=AssertionError('不能访问网络')),
            patch.object(selftest.request, 'urlopen', side_effect=AssertionError('不能访问网络')),
            patch.object(browser, 'login', side_effect=AssertionError('不能自动登录')),
            patch.object(media, 'materialize', side_effect=AssertionError('不能下载整个文件')),
            patch.object(media, 'extract', side_effect=AssertionError('不能提取音轨')),
            patch.object(asr, 'recognize', side_effect=AssertionError('不能调用识别')),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def invoke(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(['selftest', *args])
        return code, json.loads(output.getvalue())

    def fake_methods(self):
        for target, name, effect in (
            (acquisition, 'public_metadata', lambda urls: ([job(urls[0])], [])),
            (browser, 'browser_videos', lambda urls, profile: ([job(urls[0])], [])),
            (browser, '_yt_dlp_video', lambda url: job(url)),
            (selftest.request, 'urlopen', lambda *a, **k: Stream()),
        ):
            replacement = patch.object(target, name, side_effect=effect)
            replacement.start()
            self.addCleanup(replacement.stop)

    def save_health(self, failed, *, age=0, skipped=()):
        checked = (datetime.now(timezone.utc) - timedelta(hours=age)).isoformat()
        rows = [{'sample': 1, 'method': method,
                 'status': 'failed' if method in failed else 'skipped' if method in skipped else 'passed',
                 'reason': 'metadata_unavailable' if method in failed else 'profile_missing' if method in skipped else 'ok',
                 'elapsed_seconds': 0.1, 'checked_at': checked, 'bytes_read': 0}
                for method in selftest.METHODS]
        write_json(self.results, {'latest': {'checked_at': checked, 'sample_count': 1, 'results': rows}, 'history': []})

    def test_samples_add_deduplicate_limit_and_clear_without_probe(self):
        self.assertEqual([URL], selftest.samples(self.root, [URL + '?tracking=private']))
        self.assertEqual([URL, OTHER], selftest.samples(self.root, [URL, OTHER]))
        before = (self.root / '.local/selftest/samples.json').read_bytes()
        with self.assertRaisesRegex(ValueError, '最多保存 2 条'):
            selftest.samples(self.root, ['https://www.douyin.com/video/789'])
        self.assertEqual(before, (self.root / '.local/selftest/samples.json').read_bytes())
        with patch.object(selftest, 'probe') as probe:
            code, result = self.invoke('--clear-samples')
        self.assertEqual(0, code)
        self.assertEqual('samples_cleared', result['status'])
        self.assertEqual([], read_json(self.root / '.local/selftest/samples.json')['samples'])
        probe.assert_not_called()

    def test_missing_samples_asks_for_own_public_works_and_never_probes(self):
        with patch.object(selftest, 'probe') as probe:
            code, result = self.invoke()
        self.assertEqual(0, code)
        self.assertEqual('needs_samples', result['status'])
        self.assertIn('1～2 条你自己的公开抖音作品链接', result['tell_user'])
        self.assertNotIn(messages.ACQUISITION_FALLBACK, result['tell_user'])
        self.assertFalse(self.results.exists())
        probe.assert_not_called()

    def test_cli_repeated_sample_flags_save_two_and_reuse_them(self):
        self.fake_methods()
        code, first = self.invoke('--sample', URL, '--sample', OTHER)
        self.assertEqual(0, code)
        self.assertEqual(2, first['sample_count'])
        self.assertEqual(8, len(first['results']))
        _, second = self.invoke()
        self.assertEqual(2, second['sample_count'])
        code, invalid = self.invoke('--sample', 'https://www.douyin.com/video/789')
        self.assertEqual(1, code)
        self.assertIn('最多保存 2 条', invalid['tell_user'])
        self.assertEqual([URL, OTHER], selftest.samples(self.root, []))
        code, _ = self.invoke('--sample', URL, '--clear-samples')
        self.assertEqual(1, code)

    def test_samples_reject_profiles_local_files_and_unrelated_hosts(self):
        for value in ('https://www.douyin.com/user/123', 'movie.mp4',
                      'https://douyin.com.example.invalid/video/123', 'https://u:secret@www.douyin.com/video/123'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                selftest.samples(self.root, [value])
        self.assertFalse((self.root / '.local/selftest/samples.json').exists())

    def test_four_methods_record_success_and_failure_independently_per_sample(self):
        self.profile.mkdir(parents=True)
        self.fake_methods()
        failure = {'reason': 'verification_required', 'message': 'Cookie private；' + MEDIA}
        def metadata(urls, *args):
            return ([job()], []) if urls == [URL] else ([], [failure])
        def ytdlp(url):
            if url == OTHER:
                raise RuntimeError('Cookie private；' + MEDIA)
            return job()
        with patch.object(acquisition, 'public_metadata', side_effect=metadata) as public, \
                patch.object(browser, 'browser_videos', side_effect=metadata) as browser_only, \
                patch.object(browser, '_yt_dlp_video', side_effect=ytdlp) as ytdlp_only:
            code, result = self.invoke('--sample', URL, '--sample', OTHER)
        self.assertEqual(1, code)
        self.assertEqual(2, public.call_count)
        self.assertEqual([None, self.profile, None, self.profile], [call.args[1] for call in browser_only.call_args_list])
        self.assertEqual(2, ytdlp_only.call_count)
        saved = read_json(self.results)['latest']['results']
        self.assertEqual(result['results'], saved)
        for method in selftest.METHODS:
            rows = [row for row in saved if row['method'] == method]
            self.assertEqual(['passed', 'failed'], [row['status'] for row in rows])
            self.assertEqual(['ok', 'metadata_unavailable' if method == 'yt_dlp' else 'verification_required'],
                             [row['reason'] for row in rows])
            for row in rows:
                self.assertGreaterEqual(row['elapsed_seconds'], 0)
                self.assertIsNotNone(datetime.fromisoformat(row['checked_at']).tzinfo)
        serialized = json.dumps(read_json(self.results), ensure_ascii=False) + json.dumps(result, ensure_ascii=False)
        for forbidden in ('Cookie', 'cookies', '私有账号信息', 'private', 'token=secret', MEDIA, URL):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(1, result['tell_user'].count(messages.ACQUISITION_FALLBACK))

    def test_each_method_records_download_failures_and_continues(self):
        self.profile.mkdir(parents=True)
        self.fake_methods()
        with patch.object(selftest.request, 'urlopen', side_effect=HTTPError(MEDIA, 403, 'Cookie secret', {}, None)) as fetch:
            _, result = self.invoke('--sample', URL)
        self.assertEqual(4, fetch.call_count)
        self.assertEqual(['failed'] * 4, [row['status'] for row in result['results']])
        self.assertEqual(['http_error'] * 4, [row['reason'] for row in result['results']])
        self.assertEqual(['download'] * 4, [row['stage'] for row in result['results']])
        self.assertNotIn(MEDIA, self.results.read_text(encoding='utf-8'))

    def test_download_range_and_ignored_range_both_stop_at_one_mib_and_close(self):
        for status in (206, 200):
            stream = Stream(b'x' * (selftest.PROBE_BYTES * 3), status=status)
            with self.subTest(status=status), patch.object(selftest.request, 'urlopen', return_value=stream) as fetch:
                result = selftest.download_probe(MEDIA)
            self.assertEqual('passed', result['status'])
            self.assertEqual(selftest.PROBE_BYTES, result['bytes_read'])
            self.assertEqual(selftest.PROBE_BYTES, stream.consumed)
            self.assertTrue(all(0 < size <= 65536 for size in stream.sizes))
            self.assertTrue(stream.closed)
            req = fetch.call_args.args[0]
            self.assertEqual('bytes=0-1048575', req.get_header('Range'))
            self.assertNotIn('Cookie', req.headers)

    def test_download_short_empty_html_truncated_and_timeout_results(self):
        for stream, reason in ((Stream(b'abc'), 'ok'), (Stream(b''), 'empty_media'),
                               (Stream(b'login', content_type='text/html'), 'invalid_media_response'),
                               (Stream(b'abc', length=20), 'incomplete_download')):
            with self.subTest(reason=reason), patch.object(selftest.request, 'urlopen', return_value=stream):
                result = selftest.download_probe(MEDIA)
            self.assertEqual(reason, result['reason'])
            self.assertEqual(reason == 'ok', result['status'] == 'passed')
            self.assertTrue(stream.closed)
        with patch.object(selftest.request, 'urlopen', side_effect=URLError(TimeoutError(MEDIA))):
            self.assertEqual('navigation_timeout', selftest.download_probe(MEDIA)['reason'])

    def test_without_saved_profile_does_not_probe_or_create_profile_or_login(self):
        self.fake_methods()
        with patch.object(browser, 'browser_videos', return_value=([job()], [])) as browser_only:
            code, result = self.invoke('--sample', URL)
        self.assertEqual(0, code)
        browser_only.assert_called_once_with([URL], None)
        browser.login.assert_not_called()
        self.assertFalse(self.profile.exists())
        skipped = result['results'][-1]
        self.assertEqual(('skipped', 'profile_missing'), (skipped['status'], skipped['reason']))
        self.assertEqual(3, result['available_methods'])
        self.assertIn('未探测', result['tell_user'])
        self.assertNotIn(messages.ACQUISITION_FALLBACK, result['tell_user'])

    def test_short_link_is_resolved_separately_and_does_not_store_redirect(self):
        self.fake_methods()
        short = 'https://v.douyin.com/sample/'
        with patch.object(browser, 'resolve_url', side_effect=[TimeoutError(MEDIA), URL, URL]) as resolve:
            _, result = self.invoke('--sample', short)
        self.assertEqual(3, resolve.call_count)
        self.assertEqual(['failed', 'passed', 'passed', 'skipped'], [row['status'] for row in result['results']])
        self.assertEqual(['anonymous_browser', 'public_share'], result['route_order'])
        self.assertNotIn(URL, self.results.read_text(encoding='utf-8'))

    def test_wrong_video_is_not_downloaded(self):
        with patch.object(acquisition, 'public_metadata', return_value=([job(OTHER)], [])), \
                patch.object(selftest.request, 'urlopen') as fetch:
            result = selftest.probe('public_share', URL, self.profile)
        self.assertEqual('failed', result['status'])
        fetch.assert_not_called()

    def test_history_is_bounded_and_latest_replaces_previous_health(self):
        self.fake_methods()
        with patch.object(acquisition, 'public_metadata', return_value=([], [{'reason': 'http_error'}])):
            selftest.run([URL], root=self.root)
        self.assertEqual(['anonymous_browser', 'public_share'], selftest.route_order(self.root, ROUTES[:2])[0])
        selftest.run(root=self.root)
        self.assertEqual('failed', read_json(self.results)['history'][0]['results'][0]['status'])
        self.assertEqual((ROUTES, None), selftest.route_order(self.root, ROUTES))
        for _ in range(6):
            selftest.run(root=self.root)
        saved = read_json(self.results)
        self.assertEqual(4, len(saved['history']))
        self.assertEqual(1, saved['latest']['sample_count'])
        self.assertEqual((ROUTES, None), selftest.route_order(self.root, ROUTES))

    def test_route_mapping_requires_both_anonymous_methods_to_fail(self):
        cases = [({'public_share'}, ['anonymous_browser', 'public_share', 'existing_profile']),
                 ({'anonymous_browser'}, ROUTES), ({'yt_dlp'}, ROUTES),
                 ({'public_share', 'anonymous_browser'}, ['anonymous_browser', 'public_share', 'existing_profile']),
                 ({'public_share', 'anonymous_browser', 'yt_dlp'}, ROUTES),
                 ({'existing_profile'}, ROUTES),
                 ({'public_share', 'existing_profile'}, ['anonymous_browser', 'public_share', 'existing_profile']),
                 (set(selftest.METHODS), ROUTES)]
        for failed, expected in cases:
            with self.subTest(failed=failed):
                self.save_health(failed)
                ordered, adjustment = selftest.route_order(self.root, ROUTES)
                self.assertEqual(expected, ordered)
                self.assertEqual(expected != ROUTES, adjustment is not None)
        self.save_health({'public_share'}, skipped={'existing_profile'})
        self.assertEqual(['anonymous_browser', 'public_share'], selftest.route_order(self.root, ROUTES[:2])[0])

    def test_logged_in_route_stays_last_whatever_failed(self):
        methods = list(selftest.METHODS)
        for mask in range(1 << len(methods)):
            failed = {method for bit, method in enumerate(methods) if mask >> bit & 1}
            with self.subTest(failed=failed):
                self.save_health(failed)
                ordered, adjustment = selftest.route_order(self.root, ROUTES)
                self.assertEqual('existing_profile', ordered[-1])
                self.assertNotIn('existing_profile', (adjustment or {}).get('deprioritized_routes', []))

    def test_route_failure_ttl_boundary_and_invalid_state_restore_default(self):
        for age, changed in ((23.99, True), (24, False), (25, False), (-1, False)):
            with self.subTest(age=age):
                self.save_health({'public_share'}, age=age)
                self.assertEqual(changed, selftest.route_order(self.root, ROUTES)[1] is not None)
        self.results.write_text('{broken', encoding='utf-8')
        self.assertEqual((ROUTES, None), selftest.route_order(self.root, ROUTES))

    def test_metadata_reorders_but_still_attempts_failed_route_and_reports_why(self):
        self.profile.mkdir(parents=True)
        self.save_health({'public_share'}, age=1)
        calls = []
        failure = {'input_url': URL, 'reason': 'metadata_unavailable'}
        def browse(urls, profile):
            calls.append('anonymous_browser' if profile is None else 'existing_profile')
            return ([job()], []) if profile else ([], [failure])
        def public(urls):
            calls.append('public_share')
            return [], [failure]
        report = self.root / 'metadata.json'
        with patch.object(browser, 'videos', side_effect=browse), \
                patch.object(acquisition, 'public_metadata', side_effect=public):
            jobs, failures = media.metadata([URL], self.profile, report, root=self.root)
        self.assertEqual(['anonymous_browser', 'public_share', 'existing_profile'], calls)
        self.assertEqual(1, len(jobs))
        self.assertFalse(failures)
        saved = read_json(report)
        self.assertTrue(saved['anonymous_first'])
        self.assertEqual('selftest_recent_failure', saved['route_adjustment']['reason'])
        self.assertIn('24 小时', saved['route_adjustment']['message'])
        self.assertEqual(calls, saved['route_adjustment']['order'])

    def test_discover_passes_workspace_and_anonymous_only_keeps_profile_excluded(self):
        self.profile.mkdir(parents=True)
        self.save_health({'public_share'})
        calls = []
        def browse(urls, profile):
            calls.append(profile)
            return [], [{'input_url': URL, 'reason': 'metadata_unavailable'}]
        with patch.object(browser, 'videos', side_effect=browse), \
                patch.object(acquisition, 'public_metadata', return_value=([job()], [])):
            jobs = media.discover([URL], {}, self.root, self.root, anonymous_only=True)
        self.assertEqual([None], calls)
        self.assertEqual(1, len(jobs))
        report = read_json(self.root / 'metadata-0.json')
        self.assertEqual(['anonymous_browser', 'public_share'], report['route_adjustment']['order'])
        self.assertTrue(report['anonymous_first'])

    def test_expired_failures_use_default_metadata_route_without_adjustment_report(self):
        self.save_health({'public_share'}, age=25)
        with patch.object(acquisition, 'public_metadata', return_value=([job()], [])), \
                patch.object(browser, 'videos') as fetch:
            media.metadata([URL], self.profile, self.root / 'metadata.json', root=self.root)
        fetch.assert_not_called()
        self.assertNotIn('route_adjustment', read_json(self.root / 'metadata.json'))

    def test_failure_fallback_is_added_by_shared_exit_once_per_conversation(self):
        self.fake_methods()
        with patch.object(acquisition, 'public_metadata', return_value=([], [{'reason': 'http_error'}])):
            for number in range(2):
                code, result = self.invoke('--sample', URL)
                self.assertEqual(1, code)
                self.assertEqual(number == 0, messages.ACQUISITION_FALLBACK in result['tell_user'])
                self.assertIn('已后移', result['tell_user'])
            with patch.dict(os.environ, {'CY_SESSION_ID': 'another-selftest-chat'}):
                _, result = self.invoke()
        self.assertIn(messages.ACQUISITION_FALLBACK, result['tell_user'])


class BrowserIsolationTests(unittest.TestCase):
    def test_browser_only_does_not_fallback_but_original_videos_still_does(self):
        page, context = MagicMock(), MagicMock()
        context.new_page.return_value = page
        page.goto.return_value.status = 200
        with patch.object(browser, '_driver', return_value=nullcontext(MagicMock())), \
                patch.object(browser, '_session', return_value=nullcontext(context)), \
                patch.object(browser, '_hydrate', return_value=[]), \
                patch.object(browser, '_browser_detail', return_value={}), \
                patch.object(browser, '_visible_blocker', return_value=None), \
                patch.object(browser, '_yt_dlp_video', return_value=job()) as fallback:
            jobs, failures = browser.browser_videos([URL])
            self.assertFalse(jobs)
            self.assertEqual('metadata_unavailable', failures[0]['reason'])
            fallback.assert_not_called()
            context.cookies.assert_not_called()
            jobs, failures = browser.videos([URL], None)
        self.assertFalse(failures)
        self.assertEqual('douyin-123', jobs[0]['id'])
        fallback.assert_called_once_with(URL, context.cookies.return_value)
        self.assertEqual(2, page.close.call_count)

    def test_probe_browser_session_stays_headless_and_success_needs_no_ytdlp(self):
        page, driver = MagicMock(), MagicMock()
        context = driver.chromium.launch.return_value.new_context.return_value
        context.new_page.return_value = page
        detail = {'aweme_id': '123', 'video': {'play_addr': {'url_list': [MEDIA]}}}
        with patch.object(browser, '_driver', return_value=nullcontext(driver)), \
                patch.object(browser, '_hydrate', return_value=[detail]), \
                patch.object(browser, '_yt_dlp_video') as fallback, \
                patch.object(browser, 'login') as login:
            jobs, failures = browser.browser_videos([URL])
        self.assertEqual(1, len(jobs))
        self.assertFalse(failures)
        driver.chromium.launch.assert_called_once_with(headless=True)
        driver.chromium.launch_persistent_context.assert_not_called()
        fallback.assert_not_called()
        login.assert_not_called()
        page.close.assert_called_once()
        context.close.assert_called_once()
