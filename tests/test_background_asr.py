from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import support
from support import WorkspaceCase, read_json, write_json
from test_local_asr import recognized
from douyin_text import asr, background, cli, common, local_asr, local_asr_worker, media, processes, workflow
from douyin_text.common import ArtifactBusyError


class BackgroundASRTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.models = self.parent / 'models'
        self.models.mkdir()
        self.enterContext(patch.dict(os.environ, {'CY_MODEL_DIR': str(self.models)}))
        self.enterContext(patch.object(local_asr, 'waiting_for_model', return_value=None))
        self.enterContext(patch.object(asr, 'recognize', side_effect=AssertionError('不能调用豆包')))
        self.worker = Mock(recognize=Mock(side_effect=lambda *a, **k: recognized()))
        self.enterContext(patch.object(local_asr, 'Worker', return_value=self.worker))
        self.configure()

    def configure(self, **values):
        write_json(self.root / 'config.json', {**read_json(self.root / 'config.json'),
            'asr_engine': 'local', 'asr_threads': 3, **values})

    def fake_start(self, task, root, progress, options):
        write_json(background.folder(task['id'], root) / 'asr-run.json', {
            'background': True, 'token': 'fake', 'pid': os.getpid(),
            'process_birth': processes._process_birth(os.getpid()), 'options': options})
        task['status'] = 'transcribing_in_background'
        workflow.save(task, root)
        return background.status(task['id'], root)

    def launch(self, task):
        with patch.object(background, 'start', side_effect=self.fake_start) as start:
            result = workflow.run(task['id'], self.root)
        self.assertEqual(1, start.call_count)
        return result

    def test_default_coefficient_and_exponential_rolling_update_by_profile_and_threads(self):
        self.assertEqual(0.3, local_asr.speed_coefficient('p', 3))
        one = local_asr.calibrate('p', 3, 100, 20)
        self.assertEqual(0.2, one['coefficient'])
        two = local_asr.calibrate('p', 3, 100, 40)
        self.assertAlmostEqual(0.24, two['coefficient'])
        self.assertEqual(2, two['samples'])
        self.assertAlmostEqual(0.24, local_asr.speed_coefficient('p', 3))
        self.assertEqual(0.3, local_asr.speed_coefficient('p', 4))
        self.assertEqual(0.3, local_asr.speed_coefficient('new', 3))

    def test_worker_calibration_excludes_queue_and_verification_time(self):
        request = self.root / 'request.json'
        response, progress = self.root / 'response.json', self.root / 'progress.json'
        profile = local_asr.specification()['asr_profile']
        write_json(request, {'queue_timeout': 600, 'audio': str(self.wav), 'threads': 3,
                            'profile': profile, 'response_file': str(response), 'progress_file': str(progress)})
        stdin = SimpleNamespace(buffer=io.BytesIO((str(request) + '\n').encode()))
        with patch.object(sys, 'stdin', stdin), patch.object(local_asr, 'recognition_lock', return_value=nullcontext()), \
             patch.object(local_asr_worker.models, 'ready_paths', return_value={}), \
             patch.object(local_asr, 'recognize', return_value=recognized()), \
             patch.object(local_asr_worker.time, 'monotonic', side_effect=[0, 100, 102, 103]):
            local_asr_worker.main()
        result = read_json(response)['result']
        self.assertEqual(103, result['elapsed_seconds'])
        self.assertEqual(2, result['recognition_seconds'])
        self.assertEqual(2 / 3.2, local_asr.speed_coefficient(profile, 3))

    def test_default_threshold_foreground_and_exact_threshold_not_background(self):
        task = self.task()
        with patch.object(background, 'start') as start:
            result = workflow.run(task['id'], self.root)
        start.assert_not_called()
        self.assertEqual('awaiting_review', result['status'])
        self.assertEqual(4, self.worker.recognize.call_count)
        task = self.task(retranscribe=True)
        self.configure(asr_background_threshold_seconds=3.2 * 0.3)
        with patch.object(background, 'start') as start:
            result = workflow.run(task['id'], self.root)
        start.assert_not_called()
        self.assertEqual('awaiting_review', result['status'])

    def test_bad_media_in_preparation_keeps_other_foreground_results(self):
        bad = self.parent / 'bad.wav'
        bad.write_bytes(b'not audio')
        task = workflow.plan([str(bad), str(self.wav)], self.root)
        workflow.confirm(task['id'], 'transcribe', '测试混合素材', root=self.root)
        extract = media.extract
        def fail_one(job, *args):
            if job['media'] == str(bad):
                raise RuntimeError('测试音轨失败')
            return extract(job, *args)
        with patch.object(media, 'extract', side_effect=fail_one):
            done = workflow.run(task['id'], self.root)
        self.assertEqual('incomplete', done['status'])
        self.assertEqual(1, len(done['results']))
        self.assertEqual(2, done['results'][0]['number'])
        self.assertEqual(1, len(done['failures']))
        self.assertIn('测试音轨失败', done['failures'][0]['error'])

    def test_estimate_handoff_repeated_run_and_task_lock_do_not_spawn_twice(self):
        self.configure(asr_background_threshold_seconds=0.1)
        task = self.task()
        result = self.launch(task)
        self.assertEqual('transcribing_in_background', result['status'])
        self.assertAlmostEqual(3.2, result['audio_duration_seconds'])
        self.assertAlmostEqual(0.96, result['estimated_seconds'])
        self.assertEqual(4, result['progress']['total_chunks'])
        self.worker.recognize.assert_not_called()
        with patch.object(background, 'start', side_effect=AssertionError('重复启动')):
            self.assertTrue(workflow.run(task['id'], self.root)['background_process_alive'])
            with background.task_lock(task['id'], self.root):
                self.assertTrue(workflow.run(task['id'], self.root)['background_process_alive'])
                with self.assertRaises(ArtifactBusyError):
                    with background.task_lock(task['id'], self.root):
                        pass

    def test_status_interrupted_and_cancel_refuses_reused_pid(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        self.launch(task)
        path = background.folder(task['id'], self.root) / 'asr-run.json'
        write_json(path, {**read_json(path), 'process_birth': 'wrong'})
        state = background.status(task['id'], self.root)
        self.assertEqual('interrupted', state['status'])
        self.assertIn('再次 run', state['tell_user'])
        with patch.object(processes, '_kernel') as kernel, patch.object(processes, '_process_birth', return_value='different'):
            api = kernel.return_value
            self.assertEqual('interrupted', background.cancel(task['id'], self.root)['status'])
            api.TerminateProcess.assert_not_called()
            kernel.assert_not_called()

    def test_termination_checks_and_ends_the_same_handle(self):
        with patch.object(processes, '_kernel') as kernel, patch.object(processes, '_birth', return_value='birth') as birth:
            api = kernel.return_value
            api.OpenProcess.return_value = 456
            api.WaitForSingleObject.return_value = 0
            processes.terminate_registered({'pid': 123, 'process_birth': 'birth'})
            api.OpenProcess.assert_called_once_with(0x1000 | 0x100000 | 1, False, 123)
            birth.assert_called_once_with(api, 456)
            api.TerminateProcess.assert_called_once_with(456, 130)
            api.WaitForSingleObject.assert_called_once_with(456, 5000)
            api.CloseHandle.assert_called_once_with(456)

    def test_cancel_then_resume_keeps_successful_chunk_bytes_and_final_matches_foreground(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        self.launch(task)
        self.worker.recognize.side_effect = [recognized(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            background.execute(task['id'], self.root, 'fake')
        progress = read_json(background.folder(task['id'], self.root) / 'asr-progress.json')
        receipt = self.root / progress['chunks'][0]['receipt']
        before = receipt.read_bytes()
        with patch.object(processes, 'terminate_registered') as terminate, \
             patch.object(processes, 'alive', side_effect=[True, False]):
            self.assertEqual('cancelled', background.cancel(task['id'], self.root)['status'])
        terminate.assert_called_once()
        # Simulate the old child being gone, then let the fake new child own the task.
        with patch.object(processes, 'alive', return_value=False):
            resumed = self.launch(task)
        self.assertEqual(1, resumed['progress']['completed_chunks'])
        self.assertEqual(round((3.2 - progress['chunks'][0]['duration']) * 0.3, 3), resumed['estimated_seconds'])
        self.worker.recognize.reset_mock(side_effect=True)
        self.worker.recognize.side_effect = lambda *a, **k: recognized()
        done = background.execute(task['id'], self.root, 'fake')
        self.assertEqual(3, self.worker.recognize.call_count)
        self.assertEqual(before, receipt.read_bytes())
        self.assertEqual('awaiting_review', done['status'])
        self.assertEqual(workflow.load(task['id'], self.root), background.status(task['id'], self.root))
        self.configure(asr_background_threshold_seconds=999)
        foreground = workflow.run(self.task(retranscribe=True)['id'], self.root)
        self.assertEqual(done['status'], foreground['status'])
        bg_result = read_json(self.root / done['results'][0]['result'])
        fg_result = read_json(self.root / foreground['results'][0]['result'])
        for result in (bg_result, fg_result):
            result.pop('created')
            result.pop('output_sha256')
        self.assertEqual(bg_result, fg_result)
        self.assertEqual((self.root / done['results'][0]['text']).read_bytes(),
                         (self.root / foreground['results'][0]['text']).read_bytes())

    def test_status_reports_queue_current_chunk_elapsed_and_remaining(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        self.launch(task)
        path = background.folder(task['id'], self.root) / 'asr-progress.json'
        data = read_json(path)
        progress = background.Progress(task['id'], self.root, data)
        progress.current({'id': 'sample'}, 1)
        progress.phase({'status': 'queued'})
        state = background.status(task['id'], self.root)
        self.assertTrue(state['progress']['queued'])
        self.assertEqual({'id': 'sample', 'index': 2}, state['progress']['current_chunk'])
        self.assertGreaterEqual(state['progress']['elapsed_seconds'], 0)
        self.assertAlmostEqual(0.96, state['progress']['estimated_remaining_seconds'])
        progress.phase({'status': 'recognizing'})
        self.assertFalse(background.status(task['id'], self.root)['progress']['queued'])

    def test_foreground_cancel_explains_without_terminating(self):
        task = self.task()
        background.register_foreground(task['id'], self.root)
        with patch.object(processes, 'terminate_registered') as terminate:
            self.assertIn('前台任务', background.cancel(task['id'], self.root)['tell_user'])
        terminate.assert_not_called()

    def test_child_identity_is_checked_before_execution(self):
        task = self.task()
        with self.assertRaisesRegex(RuntimeError, '身份不匹配'):
            background.execute(task['id'], self.root, 'wrong')
        self.worker.recognize.assert_not_called()

    def test_cli_status_and_cancel(self):
        for name in ('status', 'cancel'):
            output = io.StringIO()
            with patch('sys.stdout', output), patch.object(background, name, return_value={'status': 'cancelled'}) as command:
                self.assertEqual(0, cli.main([name, 'dy-test']))
            command.assert_called_once_with('dy-test')
            self.assertEqual('cancelled', json.loads(output.getvalue())['status'])

    def test_hidden_low_priority_breakaway_fallback_keeps_handshake_and_log(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        self.launch(task)  # Prepare real WAV chunks before replacing only the background process.
        path = background.folder(task['id'], self.root) / 'asr-run.json'
        write_json(path, {**read_json(path), 'process_birth': 'gone'})
        denied = OSError('denied')
        denied.winerror = 5
        child = SimpleNamespace(pid=os.getpid(), stdin=Mock())
        with patch.object(processes.subprocess, 'Popen', side_effect=[denied, child]) as popen, \
             patch.object(background, 'child_identity', return_value={
                 'pid': os.getpid(), 'process_birth': processes._process_birth(os.getpid())}):
            state = workflow.run(task['id'], self.root)
        first, second = [c.kwargs for c in popen.call_args_list]
        self.assertTrue(first['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(second['creationflags'] & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(second['creationflags'] & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(second['creationflags'] & subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        self.assertEqual(str(self.root), second['env']['CY_WORKSPACE'])
        self.assertEqual('background.log', Path(second['stdout'].name).name)
        child.stdin.write.assert_called_once_with(b'go\n')
        self.assertFalse(state['background']['job_breakaway'])
        self.assertIn('宿主不允许', state['tell_user'])

    def test_handshake_registers_interpreter_beneath_venv_launcher(self):
        path = self.root / 'startup.json'
        identity = {'pid': os.getpid(), 'parent_pid': 99999, 'token': 'token',
                    'process_birth': processes._process_birth(os.getpid())}
        write_json(path, identity)
        self.assertEqual(identity, background.child_identity(SimpleNamespace(pid=99999), path, 'token'))
        with self.assertRaisesRegex(RuntimeError, '身份'):
            background.child_identity(SimpleNamespace(pid=99998), path, 'token')
        with self.assertRaisesRegex(RuntimeError, '身份'):
            background.child_identity(SimpleNamespace(pid=99999), path, 'other-token')

    def test_task_lock_is_cross_process(self):
        task = self.task()
        script = ('from pathlib import Path; from douyin_text.background import task_lock; import sys; '
                  'lock=task_lock(sys.argv[1], Path(sys.argv[2])); lock.__enter__(); '
                  "print('locked', flush=True); sys.stdin.readline()")
        child = subprocess.Popen([sys.executable, '-B', '-c', script, task['id'], str(self.root)],
            env={**os.environ, 'PYTHONPATH': str(support.SKILL_ROOT / 'scripts/runtime')},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=processes.HIDDEN)
        try:
            self.assertEqual(b'locked', child.stdout.readline().strip())
            with patch.object(background, 'start') as start:
                workflow.run(task['id'], self.root)
            start.assert_not_called()
            self.worker.recognize.assert_not_called()
        finally:
            child.communicate(b'go\n', timeout=5)

    def test_real_venv_handoff_finishes_with_substitute_recognizer(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        helper = self.root / 'fake-background.py'
        helper.write_text('''import runpy, sys, time
from douyin_text import local_asr, lexicon
# Parent-process mocks do not cross the real subprocess boundary. Keep this
# handoff fixture offline, including the display layer's lexicon preparation.
lexicon.ensure_public = lambda: {'ready': False, 'status': 'downloading', 'tell_user': lexicon.DOWNLOADING}
lexicon.ensure_general = lambda: {'ready': False, 'status': 'downloading', 'tell_user': lexicon.GENERAL_DOWNLOADING}
class FakeWorker:
    def __init__(self, *args): self.on_progress = None
    def recognize(self, *args, hotwords=None):
        assert isinstance(hotwords, list)
        if self.on_progress: self.on_progress({'status': 'recognizing'})
        time.sleep(0.15)
        return ''' + repr(recognized()) + '''
    def close(self): pass
local_asr.Worker = FakeWorker
local_asr.waiting_for_model = lambda: None
runpy.run_module('douyin_text.background', run_name='__main__')
''', encoding='utf-8')
        launch = processes.launch_background
        children = []
        def substitute(command, **options):
            result = launch([sys.executable, '-B', str(helper), *command[-3:]], **options)
            children.append(result[0])
            return result
        with patch.object(processes, 'launch_background', side_effect=substitute):
            state = workflow.run(task['id'], self.root)
        try:
            self.assertEqual('transcribing_in_background', state['status'], state)
            self.assertTrue(state['background_process_alive'])
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                state = background.status(task['id'], self.root)
                if state['status'] != 'transcribing_in_background':
                    break
                time.sleep(0.05)
            self.assertEqual('awaiting_review', state['status'], state)
            self.assertEqual(workflow.load(task['id'], self.root), state)
            self.assertEqual('这是本地测试原文。\n' * 4, (self.root / state['results'][0]['text']).read_text(encoding='utf-8'))
        finally:
            run = background.record(task['id'], self.root)
            if processes.alive(run):
                processes.terminate_registered(run)
            for child in children:
                child.wait(timeout=5)

    def test_status_preserves_background_failure_after_normal_exit(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        self.launch(task)
        self.worker.recognize.side_effect = [recognized(), RuntimeError('测试分片识别失败')]
        done = background.execute(task['id'], self.root, 'fake')
        self.assertEqual('incomplete', done['status'])
        self.assertEqual('测试分片识别失败', done['failures'][0]['error'])
        for alive in (False, True):  # The normal outcome also wins while the process is still closing.
            with self.subTest(alive=alive), patch.object(processes, 'alive', return_value=alive):
                self.assertEqual(done, background.status(task['id'], self.root))

        done.update(status='failed', tell_user='识别失败，请查看分片错误。')
        workflow.save(done, self.root)
        with patch.object(processes, 'alive', return_value=False):
            self.assertEqual(done, background.status(task['id'], self.root))

    def test_progress_write_failure_logs_and_recognition_still_completes(self):
        self.configure(asr_background_threshold_seconds=10000)
        task = self.task()

        def refuse_progress(path, value):
            if path.name == 'asr-progress.json':
                raise PermissionError('测试进度文件持续被占用')
            return write_json(path, value)

        with patch.object(background, 'write_json', side_effect=refuse_progress), \
             self.assertLogs('douyin_text.background', level='WARNING') as logs:
            done = workflow.run(task['id'], self.root)
        self.assertEqual('awaiting_review', done['status'], done.get('failures'))
        self.assertEqual([], done['failures'])
        self.assertEqual(4, self.worker.recognize.call_count)
        self.assertEqual('这是本地测试原文。\n' * 4, (self.root / done['results'][0]['text']).read_text(encoding='utf-8'))
        self.assertTrue(any('进度文件写入失败' in line and 'asr-progress.json' in line for line in logs.output))

    def test_status_preserves_background_waiting_model_outcome(self):
        self.configure(asr_background_threshold_seconds=0)
        task = self.task()
        self.launch(task)
        waiting = {'status': 'waiting_model', 'model_status': {'ready': False},
                   'tell_user': '模型尚未就绪，请等待下载完成后再次 run。'}
        with patch.object(local_asr, 'waiting_for_model', return_value=waiting):
            done = background.execute(task['id'], self.root, 'fake')
        self.assertEqual('waiting_model', done['status'])
        self.assertEqual(waiting['tell_user'], done['tell_user'])
        with patch.object(processes, 'alive', return_value=False):
            self.assertEqual(done, background.status(task['id'], self.root))

    def test_status_reports_dead_registered_foreground_as_interrupted(self):
        task = self.task()
        task['status'] = 'running'
        workflow.save(task, self.root)
        background.register_foreground(task['id'], self.root)
        with patch.object(processes, 'alive', return_value=False):
            state = background.status(task['id'], self.root)
        self.assertEqual('interrupted', state['status'])
        self.assertFalse(state['process_alive'])
        self.assertFalse(state['background_process_alive'])
        self.assertIn('再次 run', state['tell_user'])
        self.assertEqual('running', workflow.load(task['id'], self.root)['status'])

    def test_status_does_not_infer_interruption_without_local_registration(self):
        task = self.task()
        task.update(status='running', asr_engine='doubao')
        workflow.save(task, self.root)
        self.assertEqual('running', background.status(task['id'], self.root)['status'])

    def test_missing_model_blocks_audio_preparation_and_handoff(self):
        self.configure(asr_background_threshold_seconds=0)
        waiting = {'status': 'waiting_model', 'model_status': {'ready': False},
                   'tell_user': '本地识别模型正在下载，请稍后再次 run。'}
        for source in (str(self.wav), 'https://www.douyin.com/video/1234567890123456789'):
            with self.subTest(source=source):
                task = workflow.plan([source], self.root)
                workflow.confirm(task['id'], 'transcribe', '测试模型未就绪', root=self.root)
                with patch.object(local_asr, 'waiting_for_model', return_value=waiting) as check, \
                     patch.object(media, 'extract') as extract, patch.object(media, 'acquire_batch') as acquire, \
                     patch.object(background, 'start') as start:
                    state = workflow.run(task['id'], self.root)
                check.assert_called_once_with()
                extract.assert_not_called()
                acquire.assert_not_called()
                start.assert_not_called()
                self.assertEqual('waiting_model', state['status'])
                self.assertEqual(waiting['tell_user'], state['tell_user'])
                self.assertEqual(waiting['model_status'], state['model_status'])
                self.assertEqual(state, workflow.load(task['id'], self.root))
        self.worker.recognize.assert_not_called()

    def test_all_cached_results_skip_model_check_with_background_threshold_zero(self):
        done = workflow.run(self.task()['id'], self.root)
        self.assertEqual('awaiting_review', done['status'])
        task = self.task()
        self.configure(asr_background_threshold_seconds=0)
        with patch.object(local_asr, 'waiting_for_model', side_effect=AssertionError('缓存不需要模型')) as check, \
             patch.object(media, 'extract') as extract, patch.object(background, 'start') as start:
            reused = workflow.run(task['id'], self.root)
        check.assert_not_called()
        extract.assert_not_called()
        start.assert_not_called()
        self.assertEqual('awaiting_review', reused['status'])
        self.assertEqual('reused', reused['results'][0]['status'])
        self.assertEqual(done['results'][0]['result'], reused['results'][0]['result'])

    def test_first_worker_chunk_calibration_excludes_recognizer_creation(self):
        import numpy as np
        clock = [0.0]
        def advance(seconds, result):
            clock[0] += seconds
            return result
        recognizer = Mock(config=SimpleNamespace(model_config=SimpleNamespace(num_threads=3)),
                          create_stream=Mock(return_value=Mock(result=SimpleNamespace(text='测试原文。'))))
        recognizer.decode_stream.side_effect = lambda stream: advance(5, None)
        request = self.root / 'request.json'
        response, progress = self.root / 'response.json', self.root / 'progress.json'
        profile = local_asr.specification()['asr_profile']
        write_json(request, {'queue_timeout': 600, 'audio': str(self.wav), 'threads': 3,
                            'profile': profile, 'response_file': str(response), 'progress_file': str(progress)})
        stdin = SimpleNamespace(buffer=io.BytesIO((str(request) + '\n').encode()))
        with patch.object(sys, 'stdin', stdin), patch.object(local_asr, 'recognition_lock', return_value=nullcontext()), \
             patch.object(local_asr_worker.models, 'ready_paths', side_effect=lambda **k: advance(100, {'silero_vad': 'vad'})), \
             patch.object(local_asr, 'vad_new'), \
             patch.object(local_asr, 'vad_segments', side_effect=lambda *a: advance(2, [(0, np.ones(1600))])), \
             patch.object(local_asr, 'create', side_effect=lambda *a: advance(30, recognizer)), \
             patch.object(local_asr.time, 'monotonic', side_effect=lambda: clock[0]):
            local_asr_worker.main()
        result = read_json(response)['result']
        self.assertEqual(30, result['model_load_seconds'])
        self.assertEqual(137, result['elapsed_seconds'])
        self.assertEqual(7, result['recognition_seconds'])
        self.assertEqual(7 / 3.2, result['calibration']['last_ratio'])
        self.assertEqual(7 / 3.2, local_asr.speed_coefficient(profile, 3))

    def test_handshake_accepts_delayed_start_before_thirty_seconds(self):
        path = self.root / 'delayed-start.json'
        identity = {'pid': os.getpid(), 'parent_pid': 99999, 'token': 'token',
                    'process_birth': processes._process_birth(os.getpid())}
        write_json(path, identity)
        child = Mock(pid=99999, poll=Mock(return_value=None))
        with patch.object(Path, 'is_file', side_effect=[False, False, True]), \
             patch.object(background.time, 'monotonic', side_effect=[0, 6, 29]), \
             patch.object(background.time, 'sleep') as sleep:
            self.assertEqual(identity, background.child_identity(child, path, 'token'))
        self.assertEqual(2, sleep.call_count)
        with patch.object(Path, 'is_file', return_value=False), \
             patch.object(background.time, 'monotonic', side_effect=[0, 29, 30]), \
             patch.object(background.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, '启动握手未完成'):
                background.child_identity(child, path, 'token')

class JsonWriteTests(WorkspaceCase):
    def clock(self):
        ticks, delays = [0.0], []

        def sleep(delay):
            delays.append(delay)
            ticks[0] += delay

        return SimpleNamespace(monotonic=lambda: ticks[0], sleep=sleep), delays

    def test_windows_permission_errors_retry_then_atomically_replace(self):
        path = self.root / 'state.json'
        write_json(path, {'old': True})
        replace = Mock(side_effect=[PermissionError('占用'), PermissionError('占用'), None])
        actual_replace = os.replace

        def attempt(source, target):
            replace(source, target)
            actual_replace(source, target)

        clock, delays = self.clock()
        with patch.object(common, 'os', SimpleNamespace(name='nt', replace=attempt)), patch.object(common, 'time', clock):
            write_json(path, {'new': True})
        self.assertEqual(3, replace.call_count)
        self.assertEqual([.02, .04], delays)
        self.assertEqual({'new': True}, read_json(path))
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_windows_persistent_denial_is_bounded_and_cleans_temporary(self):
        path = self.root / 'state.json'
        write_json(path, {'old': True})
        clock, delays = self.clock()
        replace = Mock(side_effect=PermissionError('持续占用'))
        with patch.object(common, 'os', SimpleNamespace(name='nt', replace=replace)), patch.object(common, 'time', clock):
            with self.assertRaisesRegex(PermissionError, '持续占用'):
                write_json(path, {'new': True})
        self.assertGreater(replace.call_count, 1)
        self.assertLessEqual(sum(delays), 2.0)
        self.assertAlmostEqual(2.0, sum(delays))
        self.assertTrue(all(a <= b for a, b in zip(delays[:-2], delays[1:-1])))
        self.assertEqual({'old': True}, read_json(path))
        self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_non_windows_permission_error_and_other_errors_do_not_retry(self):
        for platform, error in [('posix', PermissionError('拒绝')), ('nt', OSError('磁盘失败'))]:
            with self.subTest(platform=platform, error=error):
                clock, delays = self.clock()
                replace = Mock(side_effect=error)
                with patch.object(common, 'os', SimpleNamespace(name=platform, replace=replace)), patch.object(common, 'time', clock):
                    with self.assertRaises(type(error)):
                        write_json(self.root / 'failed.json', {})
                self.assertEqual(1, replace.call_count)
                self.assertEqual([], delays)
                self.assertEqual([], list(self.root.glob('*.tmp')))

    def test_serialization_failure_also_cleans_temporary(self):
        with self.assertRaises(TypeError):
            write_json(self.root / 'invalid.json', {'invalid': object()})
        self.assertEqual([], list(self.root.glob('*.tmp')))

