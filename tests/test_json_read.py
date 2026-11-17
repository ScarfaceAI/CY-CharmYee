from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from support import read_json
from douyin_text import common


class JsonReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cy-json-read-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'asr-progress.json'
        self.path.write_text('\ufeff{"phase": "识别中", "completed_chunks": 1}', encoding='utf-8')

    def clock(self):
        ticks, delays = [0.0], []

        def sleep(delay):
            delays.append(delay)
            ticks[0] += delay

        return SimpleNamespace(monotonic=lambda: ticks[0], sleep=sleep), delays

    def test_windows_open_denied_then_reads_complete_json(self):
        actual_open = Path.open
        attempts = Mock(side_effect=[PermissionError('占用'), PermissionError('占用'), None])

        def open_file(path, *args, **kwargs):
            attempts()
            return actual_open(path, *args, **kwargs)

        clock, delays = self.clock()
        with patch.object(Path, 'open', new=open_file), patch.object(common, 'os', SimpleNamespace(name='nt')), \
                patch.object(common, 'time', clock):
            result = read_json(self.path)
        self.assertEqual({'phase': '识别中', 'completed_chunks': 1}, result)
        self.assertEqual(3, attempts.call_count)
        self.assertEqual([.02, .04], delays)

    def test_windows_persistent_denial_stops_at_two_seconds_and_preserves_error(self):
        denial = PermissionError('持续占用')
        clock, delays = self.clock()
        with patch.object(Path, 'open', side_effect=denial) as open_file, \
                patch.object(common, 'os', SimpleNamespace(name='nt')), patch.object(common, 'time', clock):
            with self.assertRaises(PermissionError) as raised:
                read_json(self.path)
        self.assertIs(denial, raised.exception)
        self.assertGreater(open_file.call_count, 1)
        self.assertAlmostEqual(2.0, sum(delays))
        self.assertLessEqual(sum(delays), 2.0)
        self.assertTrue(all(0 < delay <= .25 for delay in delays))
        self.assertEqual({'phase': '识别中', 'completed_chunks': 1}, read_json(self.path))

    def test_non_windows_and_other_io_errors_are_not_retried(self):
        for platform, error in [('posix', PermissionError('拒绝')), ('nt', OSError('磁盘失败')),
                                ('nt', FileNotFoundError('文件不存在'))]:
            with self.subTest(platform=platform, error=error):
                clock, delays = self.clock()
                with patch.object(Path, 'open', side_effect=error) as open_file, \
                        patch.object(common, 'os', SimpleNamespace(name=platform)), patch.object(common, 'time', clock):
                    with self.assertRaises(type(error)) as raised:
                        read_json(self.path)
                self.assertIs(error, raised.exception)
                open_file.assert_called_once()
                self.assertEqual([], delays)

    def test_malformed_json_is_not_retried(self):
        self.path.write_text('{broken', encoding='utf-8')
        actual_open = Path.open
        clock, delays = self.clock()
        with patch.object(Path, 'open', autospec=True, side_effect=actual_open) as open_file, \
                patch.object(common, 'os', SimpleNamespace(name='nt')), patch.object(common, 'time', clock):
            with self.assertRaises(json.JSONDecodeError):
                read_json(self.path)
        open_file.assert_called_once()
        self.assertEqual([], delays)


if __name__ == '__main__':
    unittest.main()
