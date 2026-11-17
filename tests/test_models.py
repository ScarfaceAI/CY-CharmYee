from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support import SKILL_ROOT
from douyin_text import cli, models
from douyin_text.common import digest, read_json, write_json


def checksum(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def server(routes, requests):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get('Range')))
            body = routes.get(self.path)
            if body is None:
                self.send_error(503)
                return
            if callable(body):
                body(self)
                return
            offset = int(self.headers.get('Range', 'bytes=0-')[6:-1])
            self.send_response(206 if offset else 200)
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(body) - 1}/{len(body)}')
            self.send_header('Content-Length', str(len(body) - offset))
            self.end_headers()
            try:
                self.wfile.write(body[offset:])
            except (BrokenPipeError, ConnectionResetError):
                pass

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}'
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cy-model-tests-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'CY_MODEL_DIR': str(self.base / 'models'),
            'CY_STATE_DIR': str(self.base / 'state'), 'CY_UPDATE_CHECK': '0'})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.routes, self.requests = {}, []
        self.http = server(self.routes, self.requests)
        self.url = self.http.__enter__()
        self.addCleanup(self.http.__exit__, None, None, None)
        self.payloads = {'encoder.int8.onnx': bytes(range(256)) * 256,
                         'tokenizer/vocab.json': b'{"token":"fixture"}\r\n' * 100}
        self.spec = {'files': [{'path': name, 'size': len(body), 'sha256': checksum(body),
                               'sources': [self.url + '/' + name]} for name, body in self.payloads.items()],
                     'archive': {'path': 'model.tar.bz2', 'size': 1, 'sha256': '0' * 64,
                                 'url': self.url + '/archive', 'top_directory': 'model'}}
        self.vad = b'synthetic vad fixture\r\n\x00'
        (self.base / 'vad.onnx').write_bytes(self.vad)
        self.data = {'schema_version': 1, 'models': {models.DEFAULT_MODEL: self.spec},
                    'bundled': {'silero_vad': {'path': 'vad.onnx', 'size': len(self.vad), 'sha256': checksum(self.vad)}}}
        self.manifest = self.base / 'models.json'
        self.save()
        for name, body in self.payloads.items():
            self.routes['/' + name] = body
        self.root_patch = patch.object(models, 'SKILL_ROOT', self.base)
        self.manifest_patch = patch.object(models, 'MANIFEST', self.manifest)
        self.root_patch.start()
        self.manifest_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.addCleanup(self.manifest_patch.stop)
        self.folder = self.base / 'models' / models.DEFAULT_MODEL
        # Fail closed if a test accidentally points at any real model host.
        urlopen = models.request.urlopen
        def local_only(req, *args, **kwargs):
            self.assertTrue(req.full_url.startswith(self.url + '/'), req.full_url)
            return urlopen(req, *args, **kwargs)
        self.network = patch.object(models.request, 'urlopen', side_effect=local_only)
        self.network.start()
        self.addCleanup(self.network.stop)

    def save(self):
        write_json(self.manifest, self.data)

    def archive(self, extra=None):
        source = self.base / 'fixture.tar.bz2'
        with tarfile.open(source, 'w:bz2') as archive:
            for name, data in self.payloads.items():
                info = tarfile.TarInfo('model/' + name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            if extra:
                archive.addfile(extra, io.BytesIO(b'x') if extra.isfile() else None)
        self.spec['archive'].update(size=source.stat().st_size, sha256=digest(source))
        self.save()
        return source

    def test_real_manifest_and_bundled_vad(self):
        with patch.object(models, 'MANIFEST', SKILL_ROOT / 'assets/models.json'):
            data = models.manifest()
        self.assertEqual(1, data['schema_version'])
        self.assertEqual([models.DEFAULT_MODEL], list(data['models']))
        spec = data['models'][models.DEFAULT_MODEL]
        self.assertEqual(6, len(spec['files']))
        self.assertEqual(987015347, sum(f['size'] for f in spec['files']))
        for f in spec['files']:
            self.assertEqual(['modelscope.cn', 'hf-mirror.com', 'huggingface.co'],
                             [url.split('/')[2] for url in f['sources']])
        vad = data['bundled']['silero_vad']
        self.assertEqual('bundled', vad['distribution'])
        self.assertEqual(vad['size'], (SKILL_ROOT / vad['path']).stat().st_size)
        self.assertEqual(vad['sha256'], digest(SKILL_ROOT / vad['path']))

    def test_manifest_rejects_bad_size_hash_and_path(self):
        for field, value in [('size', True), ('size', 0), ('sha256', 'xyz'),
                             ('path', '../outside'), ('path', '/absolute'), ('path', 'C:/outside')]:
            with self.subTest(field=field, value=value):
                data = copy.deepcopy(self.data)
                data['models'][models.DEFAULT_MODEL]['files'][0][field] = value
                write_json(self.manifest, data)
                with self.assertRaises(models.ModelError):
                    models.manifest()

    def test_model_directory_precedence_and_legacy_environment(self):
        with patch.dict(os.environ, {'DYB_MODEL_DIR': str(self.base / 'legacy'),
                                     'LOCALAPPDATA': str(self.base / 'appdata')}):
            self.assertEqual(self.base / 'models', models.model_root())
            del os.environ['CY_MODEL_DIR']
            self.assertEqual(self.base / 'legacy', models.model_root())
            del os.environ['DYB_MODEL_DIR']
            write_json(self.base / 'state/settings.json', {'model_dir': str(self.base / 'setting')})
            self.assertEqual(self.base / 'setting', models.model_root())
            (self.base / 'state/settings.json').unlink()
            self.assertEqual(self.base / 'appdata/cy-charmyee/models', models.model_root())

    def test_source_failure_and_bad_hash_try_next_source(self):
        f = self.spec['files'][0]
        self.routes['/corrupt'] = b'x' * f['size']
        f['sources'] = [self.url + '/fail', self.url + '/corrupt', self.url + '/' + f['path']]
        self.save()
        result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertEqual(['/fail', '/corrupt', '/' + f['path']], [r[0] for r in self.requests[:3]])
        self.assertEqual(2, len(result['progress']['errors']))
        self.assertIn('SHA-256', result['progress']['errors'][1]['reason'])

    def test_resume_uses_range_and_verifies_whole_file(self):
        f = self.spec['files'][0]
        part = self.folder / (f['path'] + '.part')
        part.parent.mkdir(parents=True)
        part.write_bytes(self.payloads[f['path']][:12345])
        result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertEqual('bytes=12345-', self.requests[0][1])
        receipt = result['progress']['files'][f['path']]
        self.assertEqual(12345, receipt['resumed_from'])
        self.assertEqual(206, receipt['http_status'])
        self.assertEqual(f['size'] - 12345, receipt['network_bytes'])
        self.assertEqual(f['sha256'], digest(self.folder / f['path']))
        self.assertFalse(part.exists())

    def test_ignored_range_restarts_without_appending(self):
        f = self.spec['files'][0]
        part = self.folder / (f['path'] + '.part')
        part.parent.mkdir(parents=True)
        part.write_bytes(b'bad prefix')
        def ignore_range(handler):
            handler.send_response(200)
            handler.end_headers()
            handler.wfile.write(self.payloads[f['path']])
        self.routes['/' + f['path']] = ignore_range
        result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertEqual(0, result['progress']['files'][f['path']]['resumed_from'])

    def test_corrupt_prefix_retries_last_source_from_zero(self):
        f = self.spec['files'][0]
        part = self.folder / (f['path'] + '.part')
        part.parent.mkdir(parents=True)
        part.write_bytes(b'bad prefix')
        self.assertTrue(models.download(wait=True)['ready'])
        self.assertEqual([('bytes=10-'), None], [r[1] for r in self.requests[:2]])

    def test_timeout_switches_source(self):
        f = self.spec['files'][0]
        def stalled(handler):
            handler.send_response(200)
            handler.end_headers()
            time.sleep(0.15)
        self.routes['/stall'] = stalled
        f['sources'].insert(0, self.url + '/stall')
        self.save()
        with patch.object(models, 'NETWORK_TIMEOUT', 0.05):
            result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertEqual(1, len(result['progress']['errors']))

    def test_slow_trickle_switches_source_instead_of_waiting_forever(self):
        f = self.spec['files'][0]
        def trickle(handler):
            handler.send_response(200)
            handler.end_headers()
            try:
                for _ in range(10):
                    handler.wfile.write(b'x')
                    handler.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        self.routes['/trickle'] = trickle
        f['sources'].insert(0, self.url + '/trickle')
        self.save()
        with patch.object(models, 'PROGRESS_WINDOW', 0.02):
            result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertIn('长时间没有有效进展', result['progress']['errors'][0]['reason'])

    def test_disk_shortage_stops_before_network_or_spawn(self):
        with patch.object(models.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)), \
                patch.object(models, '_launch') as launch:
            with self.assertRaisesRegex(models.ModelError, '还差 .* B'):
                models.download()
            launch.assert_not_called()
        self.assertFalse(self.requests)
        self.assertEqual('failed', models.status()['status'])
        self.assertIn('磁盘空间不足', models.status()['last_error'])

    def test_lock_prevents_second_download_import_and_remove(self):
        with models._lock(models.DEFAULT_MODEL, self.folder):
            for operation in (lambda: models.download(wait=True), lambda: models.import_model(self.base), models.remove):
                with self.assertRaisesRegex(models.ModelError, '已有下载或管理操作'):
                    operation()
        self.assertFalse(self.requests)

    def test_pending_background_launch_is_also_exclusive(self):
        progress = models._progress(self.folder, self.spec, background=True)
        progress.update()
        with self.assertRaisesRegex(models.ModelError, '已有下载或导入'):
            models.download()

    def test_import_archive_verifies_all_files_and_only_extracts_manifest(self):
        extra = tarfile.TarInfo('model/do-not-run.py')
        extra.size = 1
        result = models.import_model(self.archive(extra))
        self.assertTrue(result['ready'])
        for name, body in self.payloads.items():
            self.assertEqual(body, (self.folder / name).read_bytes())
        self.assertFalse((self.folder / 'do-not-run.py').exists())
        self.assertEqual(0, len(self.requests))

    def test_import_rejects_archive_hash_mismatch(self):
        source = self.archive()
        source.write_bytes(source.read_bytes()[:-1] + b'!')
        with self.assertRaisesRegex(models.ModelError, 'SHA-256'):
            models.import_model(source)
        self.assertFalse((self.folder / 'encoder.int8.onnx').exists())

    def test_import_rejects_traversal_absolute_and_links_before_publication(self):
        for name, kind in [('model/../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                           ('C:/absolute', tarfile.REGTYPE), ('model/link', tarfile.SYMTYPE),
                           ('model/hardlink', tarfile.LNKTYPE), ('model/pipe', tarfile.FIFOTYPE)]:
            with self.subTest(name=name):
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = 1 if kind == tarfile.REGTYPE else 0
                info.linkname = '../outside'
                with self.assertRaises(models.ModelError):
                    models.import_model(self.archive(info))
                self.assertFalse((self.folder / 'encoder.int8.onnx').exists())
                self.assertFalse((self.base / 'escape').exists())

    def test_import_folder_checks_hashes_then_installs(self):
        source = self.base / 'source'
        for name, body in self.payloads.items():
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        self.assertTrue(models.import_model(source)['ready'])
        self.assertTrue(all(Path(p).is_absolute() for p in models.ready_paths().values()))
        self.assertTrue((source / 'encoder.int8.onnx').is_file())

    def test_all_file_sources_fail_then_archive_fallback(self):
        source = self.archive()
        self.routes['/archive'] = source.read_bytes()
        self.routes.pop('/encoder.int8.onnx')
        result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertTrue(result['progress']['files']['encoder.int8.onnx']['via_archive'])
        self.assertFalse((self.folder / 'model.tar.bz2').exists())

    def test_fallback_reuses_verified_archive_after_interruption(self):
        source = self.archive()
        self.routes.pop('/encoder.int8.onnx')
        self.folder.mkdir(parents=True)
        (self.folder / 'model.tar.bz2').write_bytes(source.read_bytes())
        part = self.folder / 'encoder.int8.onnx.part'
        part.write_bytes(b'partial')
        result = models.download(wait=True)
        self.assertTrue(result['ready'])
        self.assertTrue(result['progress']['files']['encoder.int8.onnx']['reused_archive'])
        self.assertFalse(any(path == '/archive' for path, _ in self.requests))
        self.assertFalse(part.exists())

    def test_status_cache_read_only_doctor_and_changed_file(self):
        models.download(wait=True)
        before = (self.folder / 'verification.json').read_bytes()
        with patch.object(models, 'digest', side_effect=AssertionError('重复计算哈希')), \
                patch.object(cli, 'ROOT', self.base / 'workspace'):
            self.assertTrue(models.status()['ready'])
            self.assertTrue(cli.doctor()['local_models']['ready'])
        self.assertEqual(before, (self.folder / 'verification.json').read_bytes())
        target = self.folder / 'encoder.int8.onnx'
        target.write_bytes(b'x' * target.stat().st_size)
        self.assertFalse(models.status()['ready'])
        with self.assertRaisesRegex(models.ModelError, 'models download'):
            models.ready_paths()
        self.requests.clear()
        self.assertTrue(models.download(wait=True)['ready'])
        self.assertEqual(['/encoder.int8.onnx'], [r[0] for r in self.requests])

    def test_missing_status_and_doctor_never_create_directories(self):
        self.assertFalse(models.status()['ready'])
        with patch.object(cli, 'ROOT', self.base / 'workspace'):
            self.assertFalse(cli.doctor()['local_models']['ready'])
        self.assertFalse((self.base / 'models').exists())
        self.assertFalse((self.base / 'state').exists())

    def test_doctor_missing_model_does_not_block_installed_environment(self):
        workspace = self.base / 'workspace'
        browser = workspace / '.local/browsers/chromium-test/chrome-win/chrome.exe'
        browser.parent.mkdir(parents=True)
        browser.touch()
        output = io.StringIO()
        with patch.object(cli, 'ROOT', workspace), \
                patch.object(cli.shutil, 'which', return_value='installed-tool'), \
                patch.object(cli.importlib.util, 'find_spec', return_value=object()), \
                patch('sys.stdout', output):
            self.assertEqual(0, cli.main(['doctor']))
        result = json.loads(output.getvalue())
        self.assertEqual('environment_ready', result['status'])
        self.assertTrue(all(result['checks'].values()))
        self.assertNotIn('local_models', result['checks'])
        self.assertFalse(result['local_models']['ready'])
        self.assertFalse((self.base / 'models').exists())
        self.assertFalse((self.base / 'state').exists())
        self.assertFalse(self.requests)

    def test_bundled_vad_is_checked_at_runtime(self):
        (self.base / 'vad.onnx').write_bytes(b'bad')
        with self.assertRaisesRegex(models.ModelError, 'Silero VAD'):
            models.download(wait=True)
        self.assertFalse(self.requests)

    def test_remove_rejects_outside_path_and_preserves_sibling(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        for name in ('../outside', str(outside), 'unknown'):
            with self.assertRaises(models.ModelError):
                models.remove(name)
        models.download(wait=True)
        self.assertEqual('removed', models.remove()['status'])
        self.assertFalse(self.folder.exists())
        self.assertEqual('keep', (outside / 'keep').read_text())

    def test_remove_rejects_directory_junction(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        self.folder.parent.mkdir()
        if os.name == 'nt':
            command = f"New-Item -ItemType Junction -Path '{self.folder}' -Target '{outside}' | Out-Null"
            subprocess.run(['powershell.exe', '-NoProfile', '-Command', command], check=True,
                           creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
        else:
            self.folder.symlink_to(outside, target_is_directory=True)
        try:
            with self.assertRaisesRegex(models.ModelError, '链接或联接点'):
                models.remove()
            self.assertEqual('keep', (outside / 'keep').read_text())
        finally:
            if os.name == 'nt':
                self.folder.rmdir()
            else:
                self.folder.unlink()

    def test_dead_or_reused_pid_reports_interrupted_resume(self):
        progress = models._progress(self.folder, self.spec, background=True)
        progress.update(state='downloading', process_birth='not-this-process')
        result = models.status()
        self.assertEqual('interrupted', result['status'])
        self.assertFalse(result['download_active'])
        self.assertIn('续传', result['hint'])
        self.assertEqual('interrupted', models.cancel()['status'])

    def test_cancel_stops_owned_background_process_and_keeps_partial(self):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(30)'],
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        self.addCleanup(cleanup)
        progress = models._progress(self.folder, self.spec, background=True)
        progress.update(state='downloading', pid=child.pid, process_birth=models._process_birth(child.pid))
        part = self.folder / 'encoder.int8.onnx.part'
        part.write_bytes(b'partial')
        result = models.cancel()
        self.assertEqual('cancelled', result['status'])
        child.wait(timeout=5)
        self.assertEqual(b'partial', part.read_bytes())

    def test_background_job_breakaway_denial_reports_wait_fallback(self):
        progress = models._progress(self.folder, self.spec, background=True)
        denied = OSError('denied')
        denied.winerror = 5
        child = SimpleNamespace(pid=os.getpid())
        with patch.object(models.subprocess, 'Popen', side_effect=[denied, child]) as popen:
            result = models._launch(models.DEFAULT_MODEL, self.folder, progress)
        self.assertIs(result, child)
        first, second = [c.kwargs['creationflags'] for c in popen.call_args_list]
        self.assertTrue(first & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(second & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(second & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(second & subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        self.assertFalse(progress.data['job_breakaway'])
        self.assertIn('--wait', progress.data['background_warning'])

    def test_background_download_registers_the_worker_not_a_venv_redirector(self):
        # A Windows venv python.exe redirects to the base interpreter under a second PID, and the
        # worker refuses to download unless the PID registered at launch is its own.
        progress = models._progress(self.folder, self.spec, background=True)
        commands = []
        def launch(command, **options):
            commands.append(command)
            return SimpleNamespace(pid=os.getpid()), True
        venv = SimpleNamespace(executable=str(self.base / 'venv/Scripts/python.exe'), _base_executable=sys.executable)
        with patch.object(models, 'sys', venv), \
                patch.object(models.processes, 'launch_background', side_effect=launch):
            models._launch(models.DEFAULT_MODEL, self.folder, progress)
        self.assertEqual(venv._base_executable if os.name == 'nt' else venv.executable, commands[0][0])
        self.assertEqual(os.getpid(), progress.data['pid'])

    def test_cli_models_dispatch_and_model_option(self):
        for argv in (['models', '--model', models.DEFAULT_MODEL, 'status'],
                     ['models', 'status', '--model', models.DEFAULT_MODEL]):
            output = io.StringIO()
            with patch('sys.stdout', output):
                self.assertEqual(0, cli.main(argv))
            self.assertEqual(models.DEFAULT_MODEL, json.loads(output.getvalue())['model'])


if __name__ == '__main__':
    unittest.main()
