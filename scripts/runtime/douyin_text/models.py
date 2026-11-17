"""Verified local model storage. Downloads are data only; no inference or execution."""
from __future__ import annotations

from contextlib import contextmanager
import argparse
import http.client
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib import request, error
import uuid

from . import identity, processes
from .processes import _kernel, _birth, _process_birth
from .common import SKILL_ROOT, digest, now, read_json, write_json

MANIFEST = SKILL_ROOT / 'assets/models.json'
DEFAULT_MODEL = 'qwen3-asr-0.6b-int8'
ACTIVE = {'starting', 'downloading', 'verifying', 'importing'}
NETWORK_TIMEOUT = 20
PROGRESS_WINDOW = 30
MIN_PROGRESS_BYTES = 1024
CHUNK_SIZE = 256 * 1024
SPACE_RESERVE = 16 * 1024 * 1024


class ModelError(RuntimeError):
    pass


class SpaceError(ModelError):
    pass


def _relative(name: str) -> str:
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name or
            '\x00' in name or name.startswith('/') or
            any(p in {'', '.', '..'} or p.endswith(('.', ' ')) for p in name.split('/'))):
        raise ModelError('模型路径不安全：' + str(name))
    return PurePosixPath(name).as_posix()


def _linked(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def _path(base: Path, name: str) -> Path:
    name = _relative(name)
    current = base
    for part in name.split('/'):
        current = current / part
        if _linked(current):
            raise ModelError('模型目录不允许链接或联接点：' + str(current))
    if not current.resolve().is_relative_to(base.resolve()):
        raise ModelError('模型路径超出允许目录')
    return current


def manifest() -> dict:
    data = read_json(MANIFEST)
    if data.get('schema_version') != 1 or not data.get('models'):
        raise ModelError('模型清单版本或内容不正确')
    for model_id, spec in data['models'].items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', model_id):
            raise ModelError('模型标识不正确')
        _relative(model_id)
        names = [_relative(f['path']) for f in spec['files']]
        if not names or len(names) != len(set(names)):
            raise ModelError('模型清单有重复文件或没有文件')
        _relative(spec['archive']['top_directory'])
        for item in [*spec['files'], spec['archive']]:
            _validate_digest(item)
    for item in data['bundled'].values():
        _relative(item['path'])
        _validate_digest(item)
    return data


def _validate_digest(item: dict) -> None:
    if type(item.get('size')) is not int or item['size'] <= 0:
        raise ModelError('模型清单的文件大小必须为正整数')
    if not re.fullmatch(r'[0-9a-f]{64}', item.get('sha256', '')):
        raise ModelError('模型清单的 SHA-256 不正确')


def model_root() -> Path:
    selected = identity.env_value('MODEL_DIR')
    if not selected:
        settings = identity.state_root() / 'settings.json'
        selected = read_json(settings).get('model_dir') if settings.is_file() else None
    if selected:
        return Path(selected).expanduser().resolve()
    base = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData/Local')
    return (base / 'cy-charmyee/models').resolve()


def _model(model_id: str) -> tuple[dict, Path, dict]:
    data = manifest()
    if model_id not in data['models']:
        raise ModelError('未知模型：' + model_id)
    return data['models'][model_id], _path(model_root(), model_id), data['bundled']['silero_vad']


def _read(folder: Path, name: str) -> dict:
    path = _path(folder, name)
    try:
        return read_json(path)
    except FileNotFoundError:
        return {}


def _alive(progress: dict) -> bool:
    return bool(progress.get('process_birth') and
                _process_birth(progress.get('pid', 0)) == progress['process_birth'])


@contextmanager
def _lock(model_id: str, folder: Path, *, wait_timeout: float = 0):
    """OS lock survives stale metadata and is released automatically on process death.

    Keep it beside the model directory so removal cannot unlink an active lock.
    """
    folder.parent.mkdir(parents=True, exist_ok=True)
    path = _path(folder.parent, '.' + model_id + '.lock')
    with path.open('rb' if path.is_file() and path.stat().st_size else 'a+b') as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        deadline = time.monotonic() + wait_timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ModelError('本模型已有下载或管理操作进行中，请先查看 models status') from None
                time.sleep(0.02)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _idle(folder: Path) -> None:
    progress = _read(folder, 'progress.json')
    if progress.get('state') in ACTIVE and _alive(progress):
        raise ModelError('本模型已有下载或导入进行中，请先查看 models status')


def _verify(path: Path, spec: dict, cache: dict) -> dict:
    result = {'path': str(path), 'exists': path.is_file(), 'expected_size': spec['size'],
              'size': None, 'size_ok': False, 'hash_verified': False, 'hash_ok': False}
    if not result['exists']:
        return result
    stat = path.stat()
    result.update(size=stat.st_size, size_ok=stat.st_size == spec['size'], mtime_ns=stat.st_mtime_ns)
    if not result['size_ok']:
        return result
    key = str(path)
    saved = cache.get(key, {})
    signature = {'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
    cached = all(saved.get(k) == v for k, v in signature.items()) and bool(saved.get('sha256'))
    checksum = saved['sha256'] if cached else digest(path)
    after = path.stat()
    if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
        return result
    cache[key] = {**signature, 'sha256': checksum}
    result.update(sha256=checksum, hash_verified=True, hash_ok=checksum == spec['sha256'],
                  verification='cached' if cached else 'computed')
    return result


def status(model_id: str = DEFAULT_MODEL, *, cache: bool = True) -> dict:
    spec, folder, vad = _model(model_id)
    verified = _read(folder, 'verification.json')
    before = dict(verified)
    files = {f['path']: _verify(_path(folder, f['path']), f, verified) for f in spec['files']}
    bundled = _verify(_path(SKILL_ROOT, vad['path']), vad, verified)
    # doctor is strictly read-only. An absent model never creates a directory here.
    if cache and folder.is_dir() and before != verified:
        write_json(_path(folder, 'verification.json'), verified)
    progress = _read(folder, 'progress.json')
    active = progress.get('state') in ACTIVE and _alive(progress)
    interrupted = progress.get('state') in ACTIVE and not active
    ready = all(f['hash_ok'] for f in files.values()) and bundled['hash_ok']
    state = ('downloading' if active else 'ready' if ready else
             'interrupted' if interrupted else progress.get('state', 'missing'))
    if state == 'ready' and not ready:
        state = 'missing'
    hint = '模型文件已就绪。' if ready else '运行 models download 下载模型，约 1 GB；也可用 models import 导入。'
    if active:
        hint = '模型正在准备中，运行 models status 查看进度；后台下载可用 models cancel 取消。'
    elif interrupted or state in {'cancelled', 'failed'}:
        hint = '下载未完成，运行 models download 续传；宿主会结束后台进程时，改用 models download --wait。'
    if not bundled['hash_ok']:
        hint = '随包附带的 Silero VAD 缺失或校验失败，请修复安装包。'
    warning = progress.get('background_warning')
    if warning and not ready:
        hint += ' ' + warning
    return {'status': state, 'model': model_id, 'model_dir': str(folder), 'ready': ready,
            'files': files, 'silero_vad': bundled, 'download_active': active,
            'interrupted': interrupted, 'progress': progress, 'last_error': progress.get('last_error'), 'hint': hint}


def ready_paths(model_id: str = DEFAULT_MODEL, *, cache: bool = True) -> dict[str, str]:
    result = status(model_id, cache=cache)
    if not result['ready'] or result['download_active']:
        raise ModelError(result['hint'])
    return {**{name: f['path'] for name, f in result['files'].items()}, 'silero_vad': result['silero_vad']['path']}


class Progress:
    def __init__(self, folder: Path, data: dict):
        self.folder, self.data, self.last_write = folder, data, 0.0

    def update(self, *, force: bool = True, **values) -> None:
        self.data.update(values)
        if force or time.monotonic() - self.last_write >= 0.5:
            self.data['updated_at'] = now()
            write_json(_path(self.folder, 'progress.json'), self.data)
            self.last_write = time.monotonic()

    def failure(self, file: str, source: str, exc: Exception) -> None:
        reason = f'{file} 从 {source} 获取失败：{exc}'
        self.data['errors'].append({'file': file, 'source': source, 'reason': str(exc), 'time': now()})
        self.update(last_error=reason)


def _progress(folder: Path, spec: dict, *, background: bool = False) -> Progress:
    return Progress(folder, {'state': 'starting', 'downloaded_bytes': 0,
        'total_bytes': sum(f['size'] for f in spec['files']), 'current_file': None, 'source': None,
        'last_error': None, 'errors': [], 'files': {}, 'started_at': now(), 'updated_at': now(),
        'pid': os.getpid(), 'process_birth': _process_birth(os.getpid()),
        'background': background, 'token': uuid.uuid4().hex})


def _space(folder: Path, needed: int) -> None:
    existing = folder
    while not existing.exists():
        existing = existing.parent
    free = shutil.disk_usage(existing).free
    required = max(0, needed) + SPACE_RESERVE
    if free < required:
        raise SpaceError(f'磁盘空间不足：需要 {required:,} B，可用 {free:,} B，还差 {required - free:,} B。请更换 CY_MODEL_DIR 或释放空间。')


def _part(folder: Path, spec: dict) -> Path:
    return _path(folder, spec['path'] + '.part')


def _remaining(folder: Path, files: list[dict]) -> int:
    return sum(f['size'] - min(_part(folder, f).stat().st_size, f['size'])
               if _part(folder, f).is_file() else f['size'] for f in files)


class IntegrityError(ModelError):
    pass


def _check(path: Path, spec: dict) -> None:
    if not path.is_file() or path.stat().st_size != spec['size']:
        raise IntegrityError('文件大小与清单不符：' + str(path.name))
    if digest(path) != spec['sha256']:
        raise IntegrityError('SHA-256 与清单不符：' + str(path.name))


def _fetch(spec: dict, url: str, part: Path, progress: Progress, completed: int) -> dict:
    part.parent.mkdir(parents=True, exist_ok=True)
    offset = part.stat().st_size if part.exists() else 0
    if offset >= spec['size']:
        try:
            _check(part, spec)
            return {'source': url, 'resumed_from': offset, 'network_bytes': 0,
                    'elapsed_seconds': 0, 'bytes_per_second': 0, 'sha256': spec['sha256']}
        except IntegrityError:
            part.unlink()
            offset = 0
    _space(part.parent, spec['size'] - offset)
    progress.update(state='downloading', current_file=spec['path'], source=url,
                    downloaded_bytes=completed + offset, file_downloaded_bytes=offset,
                    file_total_bytes=spec['size'])
    headers = {'User-Agent': 'CY-CharmYee-models/1', 'Accept-Encoding': 'identity'}
    if offset:
        headers['Range'] = f'bytes={offset}-'
    started = window_start = time.monotonic()
    window_bytes = received = 0
    with request.urlopen(request.Request(url, headers=headers), timeout=NETWORK_TIMEOUT) as response:
        code = response.status
        if code == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
            if not match or tuple(map(int, match.groups())) != (offset, spec['size'] - 1, spec['size']):
                raise ModelError('服务器返回了错误的 Range 范围')
        elif code == 200:
            offset = 0  # A server ignoring Range must replace, never append.
        else:
            raise ModelError(f'服务器返回 HTTP {code}')
        with part.open('ab' if offset else 'wb') as handle:
            while True:
                block = response.read1(CHUNK_SIZE)
                if not block:
                    break
                if offset + received + len(block) > spec['size']:
                    raise IntegrityError('下载内容超过清单大小')
                handle.write(block)
                handle.flush()
                received += len(block)
                window_bytes += len(block)
                progress.update(force=False, downloaded_bytes=completed + offset + received,
                                file_downloaded_bytes=offset + received)
                if time.monotonic() - window_start >= PROGRESS_WINDOW:
                    if window_bytes < MIN_PROGRESS_BYTES:
                        raise TimeoutError('下载长时间没有有效进展，切换来源')
                    window_start, window_bytes = time.monotonic(), 0
    if part.stat().st_size < spec['size']:
        raise OSError('连接提前结束，已保留部分文件以便续传')
    progress.update(state='verifying', downloaded_bytes=completed + spec['size'])
    _check(part, spec)
    elapsed = time.monotonic() - started
    return {'source': url, 'http_status': code, 'resumed_from': offset, 'network_bytes': received,
            'elapsed_seconds': round(elapsed, 3), 'bytes_per_second': round(received / max(elapsed, 0.001)),
            'sha256': spec['sha256']}


def _fetch_sources(spec: dict, sources: list[str], folder: Path, progress: Progress, completed: int) -> dict | None:
    part = _part(folder, spec)
    for url in sources:
        # A bad prefix from a previous mirror must not poison the last healthy source.
        for attempt in range(2):
            resumed = part.exists() and part.stat().st_size > 0
            try:
                return _fetch(spec, url, part, progress, completed)
            except (OSError, error.URLError, http.client.HTTPException, ModelError) as exc:
                if isinstance(exc, SpaceError):
                    raise
                progress.failure(spec['path'], url, exc)
                if isinstance(exc, IntegrityError):
                    part.unlink(missing_ok=True)
                    if resumed and attempt == 0:
                        continue
                break
    return None


def _install_folder(source: Path, folder: Path, files: list[dict], progress: Progress) -> None:
    # Verify everything before replacing any model file.
    for f in files:
        _check(_path(source, f['path']), f)
    for f in files:
        target = _path(folder, f['path'])
        target.parent.mkdir(parents=True, exist_ok=True)
        _path(source, f['path']).replace(target)
        _part(folder, f).unlink(missing_ok=True)
        progress.update(current_file=f['path'])


def _import(source: Path, folder: Path, spec: dict, progress: Progress, files: list[dict]) -> None:
    _space(folder, sum(f['size'] for f in files))
    progress.update(state='importing', source=str(source))
    with tempfile.TemporaryDirectory(prefix='.import-', dir=folder) as temporary:
        stage = Path(temporary)
        if source.is_dir():
            if _linked(source):
                raise ModelError('导入目录不能是链接或联接点')
            for f in files:
                original = _path(source, f['path'])
                target = _path(stage, f['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original, target)
        else:
            if not source.name.endswith('.tar.bz2'):
                raise ModelError('请提供模型文件夹或官方 .tar.bz2 整包')
            _check(source, spec['archive'])
            top = spec['archive']['top_directory']
            wanted = {top + '/' + f['path']: f for f in files}
            seen = set()
            with tarfile.open(source, 'r|bz2') as archive:
                for member in archive:
                    name = _relative(member.name.rstrip('/') if member.isdir() else member.name)
                    if not (member.isdir() or member.isfile()) or member.issym() or member.islnk():
                        raise ModelError('整包含链接或特殊文件，拒绝导入：' + name)
                    if name != top and not name.startswith(top + '/'):
                        raise ModelError('整包条目超出顶层目录：' + name)
                    if name in wanted:
                        if name in seen or not member.isfile() or member.size != wanted[name]['size']:
                            raise ModelError('整包条目重复或大小不符：' + name)
                        seen.add(name)
                        archive.extract(member, path=stage, filter='data')
            if seen != set(wanted):
                raise ModelError('整包缺少清单中的模型文件')
            stage = stage / top
        _install_folder(stage, folder, files, progress)


def _run_download(model_id: str, spec: dict, folder: Path, progress: Progress) -> None:
    started = time.monotonic()
    try:
        result = status(model_id)
        if not result['silero_vad']['hash_ok']:
            raise ModelError(result['hint'])
        missing = [f for f in spec['files'] if not result['files'][f['path']]['hash_ok']]
        _space(folder, _remaining(folder, missing))
        completed = sum(f['size'] for f in spec['files'] if f not in missing)
        failed = []
        for f in missing:
            receipt = _fetch_sources(f, f['sources'], folder, progress, completed)
            if receipt is None:
                failed.append(f)
                continue
            _part(folder, f).replace(_path(folder, f['path']))
            completed += f['size']
            progress.data['files'][f['path']] = receipt
            progress.update(downloaded_bytes=completed)
        if failed:
            archive = {**spec['archive'], 'path': 'model.tar.bz2'}
            package = _path(folder, archive['path'])
            try:
                _check(package, archive)
                receipt = {'source': archive['url'], 'network_bytes': 0, 'reused_archive': True}
            except IntegrityError:
                _space(folder, _remaining(folder, [archive]) + sum(f['size'] for f in failed))
                receipt = _fetch_sources(archive, [archive['url']], folder, progress, 0)
                if receipt is None:
                    raise ModelError('全部逐文件来源和 GitHub 整包均失败，运行 models download 续传或手动导入。')
                _part(folder, archive).replace(package)
            _import(package, folder, spec, progress, failed)
            for f in failed:
                progress.data['files'][f['path']] = {**receipt, 'via_archive': True, 'sha256': f['sha256']}
            package.unlink()  # Verified temporary fallback archive is reproducible.
        progress.update(state='ready', current_file=None, downloaded_bytes=progress.data['total_bytes'],
                        elapsed_seconds=round(time.monotonic() - started, 3))
        status(model_id)  # Persist verification signatures for subsequent read-only checks.
    except KeyboardInterrupt:
        progress.update(state='cancelled', last_error='前台下载已中断，运行 models download 续传。')
        raise ModelError(progress.data['last_error']) from None
    except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
        progress.update(state='failed', last_error=str(exc))
        raise ModelError(str(exc)) from exc


def _launch(model_id: str, folder: Path, progress: Progress, *, command: list[str] | None = None):
    command = command or [sys.executable, '-B', '-X', 'utf8', '-m', 'douyin_text.models', model_id, progress.data['token']]
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent),
               CY_MODEL_DIR=str(folder.parent), CY_STATE_DIR=str(identity.state_root()), CY_UPDATE_CHECK='0')
    options = dict(stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env=env, cwd=Path(__file__).resolve().parent.parent)
    child, breakaway = processes.launch_background(command, **options)
    if os.name == 'nt':
        progress.data['job_breakaway'] = breakaway
        if not breakaway:
            progress.data['background_warning'] = ('宿主不允许脱离作业对象，后台下载可能随宿主结束；'
                                                   '可用 models download --wait 续传。')
    try:
        birth = _process_birth(child.pid)
        if not birth:
            raise ModelError('无法核实后台下载进程身份，请使用 models download --wait')
        progress.update(pid=child.pid, process_birth=birth)
    except Exception:
        child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        raise
    return child


def download(model_id: str = DEFAULT_MODEL, *, wait: bool = False) -> dict:
    spec, folder, _ = _model(model_id)
    child = None
    with _lock(model_id, folder):
        _idle(folder)
        result = status(model_id)
        if result['ready']:
            return result
        folder.mkdir(parents=True, exist_ok=True)
        progress = _progress(folder, spec, background=not wait)
        progress.update()
        try:
            missing = [f for f in spec['files'] if not result['files'][f['path']]['hash_ok']]
            if not result['silero_vad']['hash_ok']:
                raise ModelError(result['hint'])
            _space(folder, _remaining(folder, missing))
            if wait:
                _run_download(model_id, spec, folder, progress)
            else:
                child = _launch(model_id, folder, progress)
        except (OSError, RuntimeError) as exc:
            progress.update(state='failed', last_error=str(exc))
            raise
    if child:
        # Release our OS lock before allowing the child to acquire it.
        try:
            child.stdin.write(b'go\n')
            child.stdin.flush()
        finally:
            child.stdin.close()
    return status(model_id)


def import_model(source: str | Path, model_id: str = DEFAULT_MODEL) -> dict:
    spec, folder, _ = _model(model_id)
    source = Path(source).expanduser().absolute()
    with _lock(model_id, folder):
        _idle(folder)
        folder.mkdir(parents=True, exist_ok=True)
        progress = _progress(folder, spec)
        progress.update(state='importing')
        started = time.monotonic()
        try:
            _import(source, folder, spec, progress, spec['files'])
            progress.update(state='ready', downloaded_bytes=progress.data['total_bytes'],
                            elapsed_seconds=round(time.monotonic() - started, 3), current_file=None)
        except (OSError, ValueError, RuntimeError, tarfile.TarError) as exc:
            progress.update(state='failed', last_error=str(exc))
            raise ModelError(str(exc)) from exc
    return status(model_id)


def data_status(folder: Path, files: list[dict]) -> dict:
    """Read-only verification for small data bundles sharing the model store."""
    verified = _read(folder, 'verification.json')
    values = {f['path']: _verify(_path(folder, f['path']), f, verified) for f in files}
    progress = _read(folder, 'progress.json')
    active = progress.get('state') in ACTIVE and _alive(progress)
    interrupted = progress.get('state') in ACTIVE and not active
    ready = all(f['hash_ok'] for f in values.values())
    state = ('ready' if ready else 'downloading' if active else 'failed'
             if interrupted or progress.get('state') in {'failed', 'cancelled'} else 'unavailable')
    return {'ready': ready, 'files': values, 'status': state, 'progress': progress, 'download_active': active}


def acquire_data(folder: Path, files: list[dict], *, source: Path | None = None) -> dict:
    """Explicit foreground download or staged directory import of a small data bundle."""
    with _lock(folder.name, folder):
        _idle(folder)
        folder.mkdir(parents=True, exist_ok=True)
        spec = {'files': files}
        progress = _progress(folder, spec)
        progress.update()
        _run_data(folder, files, progress, source=source)
    return {**data_status(folder, files), 'progress': progress.data}


def _run_data(folder: Path, files: list[dict], progress: Progress, *, source: Path | None = None) -> None:
    started = time.monotonic()
    try:
        if source is not None:
            if not source.is_dir():
                raise ModelError('请提供包含清单文件的词库文件夹。')
            _import(source, folder, {'files': files}, progress, files)
        else:
            state = data_status(folder, files)
            missing = [f for f in files if not state['files'][f['path']]['hash_ok']]
            _space(folder, _remaining(folder, missing))
            completed = sum(f['size'] for f in files if f not in missing)
            for f in missing:
                receipt = _fetch_sources(f, f['sources'], folder, progress, completed)
                if receipt is None:
                    raise ModelError('词库所有来源均失败，可重新运行 lexicon download 续传或使用 lexicon import-public 手动导入。')
                target = _path(folder, f['path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                _part(folder, f).replace(target)
                completed += f['size']
                progress.data['files'][f['path']] = receipt
                progress.update(downloaded_bytes=completed)
        verified = {}
        for f in files:
            check = _verify(_path(folder, f['path']), f, verified)
            if not check['hash_ok']:
                raise IntegrityError('词库校验失败：' + f['path'])
        write_json(_path(folder, 'verification.json'), verified)
        progress.update(state='ready', current_file=None, downloaded_bytes=progress.data['total_bytes'],
                        elapsed_seconds=round(time.monotonic() - started, 3))
    except KeyboardInterrupt:
        progress.update(state='cancelled', last_error='词库下载已中断，可重新运行 lexicon download 续传。')
        raise ModelError(progress.data['last_error']) from None
    except (OSError, ValueError, RuntimeError) as exc:
        progress.update(state='failed', last_error=str(exc))
        raise ModelError(str(exc)) from exc


def ensure_data(folder: Path, files: list[dict]) -> dict:
    """Start once, without waiting for network; failed attempts require an explicit retry."""
    result = data_status(folder, files)
    if result['status'] != 'unavailable':
        return result
    child = None
    try:
        with _lock(folder.name, folder):
            result = data_status(folder, files)
            if result['status'] != 'unavailable':
                return result
            folder.mkdir(parents=True, exist_ok=True)
            progress = _progress(folder, {'files': files}, background=True)
            progress.update(data_files=files)
            try:
                _space(folder, _remaining(folder, files))
                # The Windows venv launcher redirects to a second PID. This data-only
                # worker uses the standard library, so launch the base interpreter directly.
                interpreter = getattr(sys, '_base_executable', sys.executable) if os.name == 'nt' else sys.executable
                command = [interpreter, '-B', '-X', 'utf8', '-m', 'douyin_text.models',
                           '--data', str(folder), progress.data['token']]
                child = _launch(folder.name, folder, progress, command=command)
            except (OSError, RuntimeError) as exc:
                progress.update(state='failed', last_error=str(exc))
                return data_status(folder, files)
    except ModelError:
        # Another starter or foreground import owns the lock. Never wait on it in run.
        result = data_status(folder, files)
        return {**result, 'status': result['status'] if result['status'] != 'unavailable' else 'downloading'}
    if child:
        try:
            child.stdin.write(b'go\n')
            child.stdin.flush()
        except OSError as exc:
            progress.update(state='failed', last_error=str(exc))
        finally:
            child.stdin.close()
    return data_status(folder, files)


def cancel(model_id: str = DEFAULT_MODEL) -> dict:
    _, folder, _ = _model(model_id)
    progress = _read(folder, 'progress.json')
    if progress.get('state') not in ACTIVE or not _alive(progress):
        return status(model_id)
    if not progress.get('background'):
        raise ModelError('这是前台下载或导入，请在启动它的终端按 Ctrl+C 中断。')
    try:
        processes.terminate_registered(progress, subject='后台下载', owner='本次下载',
                                       hint='请重新查看 models status',
                                       unsupported='本功能的后台取消仅支持 Windows，请在前台使用 --wait。')
    except RuntimeError as exc:
        raise ModelError(str(exc)) from None
    with _lock(model_id, folder):
        current = _read(folder, 'progress.json')
        if current.get('token') == progress['token'] and current.get('state') in ACTIVE:
            Progress(folder, current).update(state='cancelled', last_error='后台下载已取消，部分文件保留，可重新运行 models download 续传。')
    return status(model_id)


def remove(model_id: str = DEFAULT_MODEL) -> dict:
    _, folder, _ = _model(model_id)
    with _lock(model_id, folder):
        _idle(folder)
        if folder.parent != model_root() or folder.name != model_id:
            raise ModelError('只能删除模型根目录下的指定模型子目录')
        if folder.exists():
            for base, directories, files in os.walk(folder, followlinks=False):
                for name in directories + files:
                    if _linked(Path(base) / name):
                        raise ModelError('模型缓存含链接或联接点，拒绝删除')
            shutil.rmtree(folder)
    return {'status': 'removed', 'model': model_id, 'model_dir': str(folder), 'hint': '模型缓存已删除，需要时运行 models download 重新下载。'}


def add_parser(sub) -> None:
    group = sub.add_parser('models', help='下载、导入和管理本地识别模型，不执行识别')
    group.add_argument('--model', default=DEFAULT_MODEL, help='模型标识')
    commands = group.add_subparsers(dest='models_command', required=True)
    for name, help_text in [('status', '查看文件校验与下载进度'), ('download', '下载缺失或损坏的模型文件'),
                            ('import', '导入官方整包或模型文件夹'), ('cancel', '取消本模型后台下载'),
                            ('remove', '删除本模型缓存')]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument('--model', default=argparse.SUPPRESS, help='模型标识')
        if name == 'download':
            command.add_argument('--wait', action='store_true', help='在前台等待下载完成')
        if name == 'import':
            command.add_argument('path', type=Path, help='模型文件夹或 .tar.bz2 整包')


def dispatch(args) -> dict:
    if args.models_command == 'download':
        return download(args.model, wait=args.wait)
    if args.models_command == 'import':
        return import_model(args.path, args.model)
    return {'status': status, 'cancel': cancel, 'remove': remove}[args.models_command](args.model)


def _worker() -> None:
    if sys.argv[1:2] == ['--data']:
        folder, token = Path(sys.argv[2]), sys.argv[3]
        if sys.stdin.readline().strip() != 'go':
            return
        with _lock(folder.name, folder, wait_timeout=5):
            progress = _read(folder, 'progress.json')
            if progress.get('token') != token or progress.get('pid') != os.getpid() or not _alive(progress):
                raise ModelError('后台词库下载身份不匹配')
            _run_data(folder, progress['data_files'], Progress(folder, progress))
        return
    model_id, token = sys.argv[1:]
    if sys.stdin.readline().strip() != 'go':
        return
    if os.name != 'nt':
        os.nice(10)
    spec, folder, _ = _model(model_id)
    with _lock(model_id, folder, wait_timeout=5):
        progress = _read(folder, 'progress.json')
        if progress.get('token') != token or progress.get('pid') != os.getpid() or not _alive(progress):
            raise ModelError('后台下载身份不匹配')
        _run_download(model_id, spec, folder, Progress(folder, progress))


if __name__ == '__main__':
    _worker()
