from __future__ import annotations

import copy
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import wave

import support
from support import WorkspaceCase, read_json, write_json
from douyin_text import asr, cli, content, continuation, creator, local_asr, media, models, subtitles, workflow
from douyin_text.common import digest


def recognized(text='这是本地测试原文。'):
    return {**local_asr.specification(), 'text': text, 'segments': [
        {'start': 0, 'end': 0.1, 'text': text, 'raw_text': text}], 'hotwords': None,
        'engine': local_asr.ENGINE, 'sherpa_onnx_version': '2.0.1', 'threads': 3,
        'worker_pid': 123, 'elapsed_seconds': 0.01, 'recognizer_threads': 3}


class LocalASRTests(WorkspaceCase):
    def local_config(self, **values):
        cfg = read_json(self.root / 'config.json')
        write_json(self.root / 'config.json', {**cfg, 'asr_engine': 'local', **values})

    def local_run(self, task, worker=None):
        worker = worker or Mock(recognize=Mock(side_effect=lambda *a, **k: recognized()))
        with patch.object(local_asr, 'waiting_for_model', return_value=None), \
             patch.object(local_asr, 'Worker', return_value=worker), \
             patch.object(asr, 'recognize', side_effect=AssertionError('不应调用豆包')), \
             patch.object(workflow.credentials, 'load', side_effect=AssertionError('不应读取凭据')):
            return workflow.run(task['id'], self.root)

    def test_long_segments_partition_at_pause_without_losing_samples(self):
        import numpy as np
        samples = np.ones(37 * 16000 + 111, dtype=np.float32)
        samples[12 * 16000:13 * 16000] = 0
        parts = list(local_asr.split_segment(320, samples))
        self.assertEqual(12.5, len(parts[0][1]) / 16000)
        self.assertGreater(len(parts), 2)
        cursor = 320
        for start, piece in parts:
            self.assertEqual(cursor, start)
            self.assertLessEqual(len(piece), 15 * 16000)
            self.assertGreater(len(piece), 0)
            cursor += len(piece)
        np.testing.assert_array_equal(samples, np.concatenate([p for _, p in parts]))
        self.assertEqual(320 + len(samples), cursor)

    def test_no_pause_uses_lowest_energy_in_ten_to_fifteen_seconds(self):
        import numpy as np
        samples = np.ones(22 * 16000, dtype=np.float32)
        samples[14 * 16000:14 * 16000 + 320] = 0.25
        parts = list(local_asr.split_segment(0, samples))
        self.assertEqual(14.01, len(parts[0][1]) / 16000)
        self.assertEqual(22 * 16000, sum(len(p) for _, p in parts))
        self.assertTrue(all(len(p) <= 15 * 16000 for _, p in parts))

    def test_pause_before_ten_seconds_takes_precedence_over_energy_fallback(self):
        import numpy as np
        samples = np.ones(18 * 16000, dtype=np.float32)
        samples[4 * 16000:4 * 16000 + 6400] = 0
        parts = list(local_asr.split_segment(0, samples))
        self.assertEqual(4.2, len(parts[0][1]) / 16000)
        self.assertEqual(18 * 16000, sum(len(p) for _, p in parts))

    def test_new_stream_per_segment_hotwords_and_sample_times(self):
        import numpy as np
        streams = []
        def new_stream():
            stream = Mock(result=SimpleNamespace(text='原文。'))
            streams.append(stream)
            return stream
        recognizer = Mock(create_stream=Mock(side_effect=new_stream))
        with patch.object(local_asr, 'vad_new'), patch.object(local_asr, 'vad_segments', return_value=[
                (123, np.ones(22 * 16000 + 17, dtype=np.float32))]), \
             patch.object(local_asr, 'create', return_value=recognizer):
            result = local_asr.recognize(self.wav, {'silero_vad': 'vad'}, 2, 'profile', hotwords='承意')
        self.assertEqual(2, len(streams))
        for stream in streams:
            stream.set_option.assert_called_once_with('hotwords', '承意')
        self.assertEqual(123 / 16000, result['segments'][0]['start'])
        self.assertEqual((123 + 22 * 16000 + 17) / 16000, result['segments'][-1]['end'])
        self.assertEqual(result['segments'][0]['end'], result['segments'][1]['start'])
        self.assertTrue(all(s['raw_text'] == s['text'] for s in result['segments']))

    def test_no_vad_segments_never_loads_recognizer(self):
        with patch.object(local_asr, 'vad_new'), patch.object(local_asr, 'vad_segments', return_value=[]), \
             patch.object(local_asr, 'create') as create:
            result = local_asr.recognize(self.wav, {'silero_vad': 'vad'}, 2, 'profile')
        create.assert_not_called()
        self.assertEqual('', result['text'])
        self.assertEqual([], result['segments'])

    def test_real_bundled_silero_thirty_seconds_of_silence(self):
        # Mandatory lightweight integration: no large model or network, no skip.
        with wave.open(str(self.wav), 'wb') as audio:
            audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\x00\x00' * (16000 * 30))
        with patch.object(local_asr, 'create', side_effect=AssertionError('静音不应加载大模型')):
            result = local_asr.recognize(self.wav, {'silero_vad': str(support.SKILL_ROOT / 'assets/silero_vad.onnx')}, 1, 'silence')
        self.assertEqual([], result['segments'])
        self.assertEqual('', result['text'])

    def test_recognizer_reused_only_for_same_model_and_threads(self):
        import sherpa_onnx
        paths = {'encoder.int8.onnx': 'e', 'decoder.int8.onnx': 'd', 'conv_frontend.onnx': 'c',
                 'tokenizer/vocab.json': 'tokens/vocab.json'}
        with patch.dict(local_asr._recognizers, clear=True), \
             patch.object(sherpa_onnx.OfflineRecognizer, 'from_qwen3_asr', side_effect=lambda **kw: object()) as factory:
            one = local_asr.create(paths, 3, 'a')
            self.assertIs(one, local_asr.create(paths, 3, 'a'))
            self.assertIsNot(one, local_asr.create(paths, 2, 'a'))
            self.assertIsNot(one, local_asr.create(paths, 3, 'b'))
            self.assertEqual(3, factory.call_count)
            self.assertEqual(1e-6, factory.call_args.kwargs['temperature'])
            self.assertEqual('cpu', factory.call_args.kwargs['provider'])

    def test_threads_use_physical_cores_fallback_and_explicit_override(self):
        for cores, expected in [(6, 3), (1, 1), (32, 4)]:
            with patch.object(local_asr, 'physical_cores', return_value=cores):
                self.assertEqual(expected, local_asr.thread_count({}))
        with patch.object(local_asr, 'physical_cores', return_value=None), patch.object(os, 'cpu_count', return_value=12):
            self.assertEqual(3, local_asr.thread_count({}))
        self.assertEqual(7, local_asr.thread_count({'asr_threads': 7}))
        for value in (0, -1, True, '3'):
            with self.assertRaises(ValueError):
                local_asr.thread_count({'asr_threads': value})

    def test_new_task_pins_engine_on_first_run_then_ignores_config_change(self):
        task = self.task()
        self.assertNotIn('asr_profile', task)
        self.local_config()
        done = self.local_run(task)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        self.assertEqual('local', done['asr_engine'])
        self.assertEqual(local_asr.specification()['asr_profile'], done['asr_profile'])
        cfg = read_json(self.root / 'config.json')
        write_json(self.root / 'config.json', {**cfg, 'asr_engine': 'doubao'})
        self.assertEqual('local', workflow.run(task['id'], self.root)['asr_engine'])
        result = read_json(self.path(done['results'][0]['result']))
        self.assertEqual(local_asr.ENGINE, result['engine'])
        self.assertTrue(all('raw_text' in s for s in result['segments']))
        self.assertEqual(local_asr.PARAMETERS, result['parameters'])

    def test_doubao_default_and_explicit_selection_keep_existing_result_bytes(self):
        first = self.recognize_run(self.task())
        path = self.path(first['results'][0]['result'])
        before = path.read_bytes()
        self.local_config(asr_engine='doubao')
        with patch.object(asr, 'recognize', side_effect=AssertionError('成功结果应复用')):
            second = workflow.run(self.task()['id'], self.root)
        self.assertEqual('reused', second['results'][0]['status'])
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(asr.PROFILE, workflow.LEGACY_ASR_PROFILE)

    def test_invalid_engine_is_reported_without_recognition(self):
        self.local_config(asr_engine='other')
        task = self.local_run(self.task())
        self.assertEqual('failed', task['status'])
        self.assertIn('asr_engine', task['failures'][0]['error'])

    def test_legacy_task_reuses_old_profile_even_with_changed_global_profile(self):
        done = self.recognize_run(self.task())
        original = self.path(done['results'][0]['result']).read_bytes()
        done.pop('asr_engine')
        done.pop('asr_profile')
        done.update(status='incomplete', results=[])
        workflow.save(done, self.root)
        self.local_config()
        with patch.object(asr, 'PROFILE', 'future-profile'), \
             patch.object(asr, 'recognize', side_effect=AssertionError('不应重新识别')), \
             patch.object(local_asr, 'waiting_for_model', side_effect=AssertionError('旧任务不需要模型')):
            restored = workflow.run(done['id'], self.root, saved_scope=True)
        self.assertEqual('awaiting_review', restored['status'])
        self.assertEqual('reused', restored['results'][0]['status'])
        self.assertEqual(original, self.path(restored['results'][0]['result']).read_bytes())
        self.assertEqual(workflow.LEGACY_ASR_PROFILE + '-revision', workflow.version_for({'revision': 'revision'}))

    def test_explicit_retranscription_uses_separate_local_revision(self):
        old = self.recognize_run(self.task())
        path = self.path(old['results'][0]['result'])
        before = path.read_bytes()
        self.local_config()
        new = self.local_run(self.task(retranscribe=True))
        self.assertIn(new['revision'], new['results'][0]['result'])
        self.assertIn('local-', new['results'][0]['result'])
        self.assertEqual(before, path.read_bytes())

    def test_failed_local_chunk_retries_automatically_and_success_is_reused(self):
        self.local_config()
        task = self.task()
        worker = Mock(recognize=Mock(side_effect=[recognized(), RuntimeError('模拟中断')]))
        first = self.local_run(task, worker)
        self.assertEqual('incomplete', first['status'])
        jobs = read_json(workflow.task_path(task['id'], self.root).parent / 'jobs.json')
        folder = self.root / '.cache' / jobs[0]['id'] / workflow.version_for(first)
        prior_bytes = (folder / 'chunk-00000.json').read_bytes()
        actions = continuation.document_summary(first, self.root)['next_actions']
        self.assertNotIn('resolve_paid_requests', [a['action'] for a in actions])
        worker = Mock(recognize=Mock(side_effect=lambda *a, **k: recognized()))
        done = self.local_run(task, worker)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        self.assertEqual(3, worker.recognize.call_count)
        self.assertEqual(prior_bytes, (folder / 'chunk-00000.json').read_bytes())
        self.assertNotIn('retry_confirmations', done)

    def test_missing_model_starts_download_and_run_resumes_same_profile(self):
        self.local_config()
        task = self.task()
        state = {'ready': False, 'download_active': False, 'progress': {'downloaded_bytes': 12}}
        with patch.object(models, 'status', return_value=state), \
             patch.object(models, 'download', return_value={**state, 'status': 'downloading'}) as download, \
             patch.object(local_asr.Worker, 'start', side_effect=AssertionError('不应开始识别')):
            waiting = workflow.run(task['id'], self.root)
        download.assert_called_once_with()
        self.assertEqual('waiting_model', waiting['status'])
        self.assertEqual(12, waiting['model_status']['progress']['downloaded_bytes'])
        self.assertIn('再次 run', waiting['tell_user'])
        done = self.local_run(task)
        self.assertEqual('awaiting_review', done['status'])
        self.assertEqual(waiting['asr_profile'], done['asr_profile'])
        self.assertNotIn('model_status', done)

    def test_active_download_is_not_restarted(self):
        with patch.object(models, 'status', return_value={'ready': False, 'download_active': True}), \
             patch.object(models, 'download') as download:
            self.assertEqual('waiting_model', local_asr.waiting_for_model()['status'])
        download.assert_not_called()

    def test_completed_local_cache_does_not_need_model_or_download(self):
        self.local_config()
        done = self.local_run(self.task())
        self.assertEqual('awaiting_review', done['status'])
        another = self.task()
        with patch.object(local_asr, 'waiting_for_model', side_effect=AssertionError('缓存复用不需要模型')):
            reused = workflow.run(another['id'], self.root)
        self.assertEqual('awaiting_review', reused['status'], reused.get('failures'))
        self.assertEqual('reused', reused['results'][0]['status'])

    def test_interruption_keeps_successes_and_retries_submitting_chunk(self):
        self.local_config()
        task = self.task()
        worker = Mock(recognize=Mock(side_effect=[recognized(), KeyboardInterrupt()]))
        with self.assertRaises(KeyboardInterrupt):
            self.local_run(task, worker)
        worker.close.assert_called_once_with()
        saved = workflow.load(task['id'], self.root)
        jobs = read_json(workflow.task_path(task['id'], self.root).parent / 'jobs.json')
        folder = self.root / '.cache' / jobs[0]['id'] / workflow.version_for(saved)
        first = (folder / 'chunk-00000.json').read_bytes()
        self.assertEqual('submitting', read_json(folder / 'chunk-00001.json')['status'])
        worker = Mock(recognize=Mock(side_effect=lambda *a, **k: recognized()))
        done = self.local_run(task, worker)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        self.assertEqual(3, worker.recognize.call_count)
        self.assertEqual(first, (folder / 'chunk-00000.json').read_bytes())

    def test_local_cache_validates_own_profile_and_keeps_hotwords_out_of_profile(self):
        self.local_config()
        done = self.local_run(self.task())
        item = done['results'][0]
        path = self.path(item['result'])
        result = read_json(path)
        self.assertEqual(local_asr.profile_for(result), local_asr.profile_for({**result, 'hotwords': '承意'}))
        self.assertIsNotNone(workflow.cached_result({'id': item['id']}, done, self.root))
        result['model_sha256']['encoder.int8.onnx'] = '0' * 64
        write_json(path, result)
        with self.assertRaisesRegex(RuntimeError, '参数不一致'):
            workflow.cached_result({'id': item['id']}, done, self.root)

    def test_local_results_export_plain_reviewed_and_short_subtitles(self):
        self.local_config()
        done = self.local_run(self.task())
        files = workflow.export_srt(done['id'], self.root)
        self.assertIn('这是本地测试原文。', Path(files[0]).read_text(encoding='utf-8'))
        review = self.root / 'review.txt'
        review.write_text(read_json(self.path(done['results'][0]['result']))['text'], encoding='utf-8')
        content.register_content(done['id'], [review], 'review', self.root)
        for reviewed in (False, True):
            exported = subtitles.export(done['id'], self.root, reviewed=reviewed, max_chars=14)
            self.assertTrue(exported['files'])

    def test_machine_lock_serializes_processes_and_recovers_after_kill(self):
        model_root = self.parent / 'models'
        model_root.mkdir()
        lock_path = model_root / ('.' + models.DEFAULT_MODEL + '.lock')
        lock_path.write_bytes(b'0')
        before = (digest(lock_path), lock_path.stat().st_mtime_ns)
        env = {**os.environ, 'CY_MODEL_DIR': str(model_root),
               'PYTHONPATH': str(support.SKILL_ROOT / 'scripts/runtime')}
        command = [sys.executable, '-B', '-X', 'utf8', '-c',
            "from douyin_text.local_asr import recognition_lock; import sys; "
            "lock=recognition_lock(1); lock.__enter__(); print('locked', flush=True); sys.stdin.readline()"]
        child = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, creationflags=local_asr.processes.HIDDEN)
        try:
            self.assertEqual(b'locked', child.stdout.readline().strip())
            with patch.dict(os.environ, {'CY_MODEL_DIR': str(model_root)}):
                waiting = Mock()
                with self.assertRaisesRegex(TimeoutError, '排队超时'):
                    with local_asr.recognition_lock(0.05, waiting):
                        self.fail('跨进程锁失效')
                waiting.assert_called_once_with()
                child.kill()
                child.wait(timeout=5)
                with local_asr.recognition_lock(0):
                    pass
            self.assertEqual(before, (digest(lock_path), lock_path.stat().st_mtime_ns))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)

    def test_worker_below_normal_priority_and_job_cleanup(self):
        import ctypes
        from ctypes import wintypes
        worker = local_asr.Worker(self.root / 'worker.log')
        worker.command = [sys.executable, '-B', '-c', 'import sys; sys.stdin.readline()']
        try:
            worker.start()
            process = worker.process
            query = ctypes.WinDLL('kernel32', use_last_error=True).GetPriorityClass
            query.argtypes, query.restype = [wintypes.HANDLE], wintypes.DWORD
            self.assertEqual(0x4000, query(int(process._handle)))
            self.assertEqual(1, worker.job.accounting()['active_processes'])
        finally:
            worker.close()
        self.assertIsNotNone(process.poll())
        self.assertEqual(0, worker.receipts[-1]['active_processes'])

    def test_abrupt_parent_exit_kills_owned_worker(self):
        import time
        script = self.root / 'parent.py'
        script.write_text("from pathlib import Path\nimport sys\nfrom douyin_text.local_asr import Worker\n"
            "w=Worker(Path(sys.argv[1])); w.command=[sys.executable, '-B', '-c', 'import sys; sys.stdin.readline()']\n"
            "w.start(); print(w.process.pid, flush=True); sys.stdin.readline()\n", encoding='utf-8')
        parent = subprocess.Popen([sys.executable, '-B', str(script), str(self.root / 'owned.log')],
            env={**os.environ, 'PYTHONPATH': str(support.SKILL_ROOT / 'scripts/runtime')},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=local_asr.processes.HIDDEN)
        try:
            pid = int(parent.stdout.readline().strip())
            self.assertIsNotNone(models._process_birth(pid))
            parent.kill()
            parent.wait(timeout=5)
            until = time.monotonic() + 5
            while models._process_birth(pid) and time.monotonic() < until:
                time.sleep(0.02)
            self.assertIsNone(models._process_birth(pid))
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=5)

    def test_creator_speech_profiles_and_metering_remain_usable(self):
        from test_creator import post
        from douyin_text import catalog
        self.local_config()
        task_id = creator.plan('https://www.douyin.com/user/MS4test', images=True, root=self.root)['task']
        task = creator.load(task_id, self.root)
        write_json(creator.task_path(task_id, self.root).parent / 'catalog.json', {'posts': [catalog.normalize_post(post())]})
        quote = creator.preflight(task_id, root=self.root)['usage']
        pinned = creator.load(task_id, self.root)
        self.assertEqual('local', pinned['speech_task']['asr_engine'])
        self.assertEqual(pinned['speech_task']['asr_profile'], quote['asr_profile'])
        self.assertEqual(1, quote['new_or_remaining_items'])
        creator.authorize(task_id, quote['token'], '授权测试', root=self.root)
        row = creator.records(pinned, self.root)[0]
        self.assertTrue(workflow.version_for(creator.speech_task(row)).startswith('local-'))
        self.assertEqual(workflow.LEGACY_ASR_PROFILE, workflow.profile_for(creator.speech_task(post(media_fingerprint='a' * 64))))

    def test_doctor_reports_import_failure_as_dependency_failure(self):
        with patch.object(cli, 'ROOT', self.root), patch.object(models, 'status', return_value={'ready': False}), \
             patch.object(cli.importlib, 'import_module', side_effect=OSError('DLL 缺失')):
            result = cli.doctor()
        self.assertFalse(result['checks']['sherpa_onnx'])
        self.assertEqual('environment_incomplete', result['status'])

    @unittest.skipUnless(os.environ.get('CY_TEST_MODEL_DIR'), '设置 CY_TEST_MODEL_DIR 才运行真实大模型测试')
    def test_opt_in_real_model_load_and_decode(self):
        import numpy as np
        with patch.dict(os.environ, {'CY_MODEL_DIR': os.environ['CY_TEST_MODEL_DIR']}), \
             local_asr.recognition_lock(5):
            paths = models.ready_paths(cache=False)
            rec = local_asr.create(paths, 1, local_asr.specification()['asr_profile'])
            stream = rec.create_stream()
            stream.accept_waveform(16000, np.zeros(16000, dtype=np.float32))
            rec.decode_stream(stream)
            self.assertIsInstance(stream.result.text, str)
