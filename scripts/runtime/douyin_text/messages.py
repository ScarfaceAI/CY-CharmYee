"""User-facing text at the JSON boundary; task records remain machine-readable."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from . import acquisition, identity, models
from .common import ROOT, SKILL_ROOT, inside, read_json

ACQUISITION_FALLBACK = '在抖音 App 里保存到本地，再把文件拖给我。'
ACQUISITION_STAGES = {'metadata', 'audio', 'acquisition', 'download'}
ACQUISITION_REASONS = {'metadata_unavailable', 'media_unavailable', 'navigation_timeout',
                       'http_error', 'browser_error', 'verification_required', 'login_required',
                       'acquisition_timeout', 'acquisition_error'}


class UsageError(ValueError):
    """Invalid command arguments, distinct from a failure while executing a task."""


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def join(*parts: str) -> str:
    """Keep original sentences, removing exact duplicates without truncating instructions."""
    sentences = []
    for part in parts:
        for sentence in re.split(r'(?<=[。！？])\s*', part.strip()):
            if sentence and sentence not in sentences:
                sentences.append(sentence)
    return ''.join(sentences)


def _claim_folder(session: str) -> Path:
    return identity.state_root() / 'task-notices' / hashlib.sha256(session.encode('utf-8')).hexdigest()


def _claim_name(key: str) -> str:
    return hashlib.sha256(key.encode('utf-8')).hexdigest() + '.json'


def is_claimed(key: str, claimed: set[str]) -> bool:
    """Check a key against claimed_notices() without exposing the claim file format."""
    return _claim_name(key) in claimed


def once(key: str) -> bool:
    # Share the existing host-session identity and exclusive-claim convention.
    from updates import session_identity
    session = session_identity()
    if not session:
        return True  # As with updates, the Agent deduplicates when the host supplies no ID.
    folder = _claim_folder(session)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / _claim_name(key)).open('x', encoding='utf-8') as stream:
            stream.write('{}\n')
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def claimed_notices() -> set[str]:
    """Read this session's claim names in one listing; never invent a session or create files."""
    from updates import session_identity
    session = session_identity()
    if not session:
        return set()  # The Agent still deduplicates when the host supplies no ID.
    try:
        return set(os.listdir(_claim_folder(session)))
    except OSError:
        return set()  # Missing/unreadable receipts leave once's existing fallback in control.


def acquisition_failed(result: dict, command: str = '', root: Path = ROOT) -> bool:
    if result.get('stage') in ACQUISITION_STAGES or result.get('reason') in ACQUISITION_REASONS:
        return True
    if command == 'inspect-source' and result.get('status') in {'error', 'failed', 'incomplete'}:
        return True
    if any(word in str(result.get('error', '')) for word in ('获取失败', '素材获取', '下载视频失败')):
        return True
    if result.get('media_error') or result.get('media', {}).get('errors'):
        return True
    # Creator summaries keep per-item errors in the recorded task, not in counts.
    if result.get('counts', {}).get('failed_items') and result.get('task_file'):
        try:
            if acquisition_failed(read_json(inside(root, result['task_file'])), root=root):
                return True
        except (OSError, ValueError, KeyError):
            pass
    for key in ('failures', 'acquisition_gaps', 'items', 'gaps', 'results'):
        items = result.get(key, [])
        if isinstance(items, dict):
            items = items.values()
        if any(acquisition_failed(item, root=root) for item in items if isinstance(item, dict)):
            return True
    return False


def error_result(exc: Exception, command: str = '') -> dict:
    result = {'status': 'error', 'error': str(exc)}
    if isinstance(exc, acquisition.AcquisitionError):
        result.update(reason=exc.code, stage='acquisition')
    if isinstance(exc, UsageError):
        message = (f'命令参数不正确。可运行 {command or "scripts/invoke.ps1"} --help '
                   '核对用法，再按提示重试。')
    elif isinstance(exc, FileNotFoundError):
        message = '没有找到所需文件，请核对输入路径或任务编号后重试。'
    elif isinstance(exc, PermissionError):
        message = '文件暂时无法读写，请检查目录权限或关闭占用文件的程序后重试。'
    elif isinstance(exc, KeyError):
        message = '任务记录或配置缺少必要信息，请根据 error 检查对应文件后重试。'
    else:
        detail = str(exc).strip()
        message = ('这次没有完成：' + detail.rstrip('。') + '。' if re.search(r'[\u4e00-\u9fff]', detail)
                   and len(detail) <= 280 else '这次没有完成，请根据 error 检查输入、配置或运行环境。')
    result['tell_user'] = message
    return result


def download_notice(result: dict) -> tuple[str, str]:
    state = result.get('model_status') or (result if 'download_active' in result else None)
    if state is None:
        # Read only the download receipt, avoiding model hashing on every command.
        _, folder, _ = models._model(models.DEFAULT_MODEL)
        progress = models._read(folder, 'progress.json')
        active = progress.get('state') in models.ACTIVE and models._alive(progress)
        state = {'download_active': active, 'progress': progress, 'model': models.DEFAULT_MODEL}
    if not state.get('download_active'):
        return '', ''
    progress = state.get('progress', {})
    total, done = progress.get('total_bytes', 0), progress.get('downloaded_bytes', 0)
    amount = f'已完成 {min(100, done / total * 100):.1f}%' if total else f'已下载 {done / 1024 / 1024:.1f} MB'
    return ('model-download:' + state.get('model', models.DEFAULT_MODEL),
            f'模型正在后台下载，{amount}；可用 models status 查看进度，models cancel 取消。')


def tell_user(result: dict, *, command: str = '', update_notice: str = '', root: Path = ROOT) -> dict:
    own = result.get('tell_user')
    if not isinstance(own, str) or not own.strip():
        if result.get('error') or result.get('status') in {'error', 'failed', 'incomplete', 'publish_failed', 'publication_unknown'}:
            own = '这次处理未全部完成，请查看返回的 error 或 failures，处理原因后再续做。'
        else:
            own = next((result[key] for key in ('hint', 'message', 'next', 'question')
                        if isinstance(result.get(key), str) and result[key].strip()), '')
    extra = []
    try:
        key, notice = download_notice(result)
        if notice and once(key):
            extra.append(notice)
    except (OSError, ValueError, RuntimeError, KeyError):
        pass  # An optional download notice must not hide the command's own result/error.
    if acquisition_failed(result, command, root) and once('acquisition-fallback'):
        extra.append(ACQUISITION_FALLBACK)
    if command in {'quick', 'plan', 'plan-text', 'creator plan', 'kb context'}:
        try:
            from . import decisions
            for key, notice in decisions.notices(root, claimed=claimed_notices()):
                if once(key):
                    extra.append(notice)
        except Exception:
            pass  # Optional decision reminders must not change the task result.
    return {**result, 'tell_user': join(own, update_notice, *extra)}


def emit_json(result: dict, *, command: str = '', update_notice: str | None = None,
              root: Path = ROOT) -> None:
    if update_notice is None:
        update_notice = ''
        if command in {'quick', 'plan', 'plan-text', 'creator plan', 'kb context'}:
            from updates import notify_on_task
            update_notice = notify_on_task(SKILL_ROOT, identity.state_root())
    result.setdefault('workspace', str(root))
    result = tell_user(result, command=command, update_notice=update_notice, root=root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
