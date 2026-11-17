"""The single-input path through the existing task workflow."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from . import acquisition, content, media, workflow
from .common import ROOT

DEFAULT_EVIDENCE = '用户调用 quick，要求只转写这一条输入。'


def full_flow(values: list[str], task: dict | None = None) -> dict:
    if task and any(media.has_profile(value) for value in task['inputs']):
        command = (f"confirm {task['id']} --purpose transcribe "
                   "--evidence '<用户原话>' --latest <用户指定的条数>")
    else:
        command = 'plan ' + ' '.join("'" + value.replace("'", "''") + "'" for value in values)
    return {'id': task['id'] if task else None, 'status': 'needs_scope',
            'text': None, 'markdown': None, 'suspects': {'total': 0, 'files': [], 'missing': 0},
            'reused': False, 'next_command': command,
            'tell_user': 'quick 只处理一条作品链接、分享文案或一个本地文件。'
                         '多个输入、主页、合集或需要选择范围的链接，请改用完整流程，明确作品和范围；'
                         f'下一条命令：{command}。'}


def summary(task: dict) -> dict:
    items = task.get('results', [])
    first = items[0] if items else {}
    hints = task.get('suspects', {})
    result = {'id': task['id'], 'status': task['status'],
              'text': first.get('text'), 'markdown': first.get('markdown'),
              'suspects': {'total': hints.get('total', 0),
                           'files': [{key: item['suspects'][key] for key in ('json', 'markdown')}
                                     for item in items if item.get('suspects', {}).get('status') == 'ready'],
                           'missing': hints.get('missing', 0)},
              'reused': bool(items) and all(item['status'] == 'reused' or item.get('reused') for item in items)}
    notice = task.get('tell_user', '')
    if task['status'] == 'transcribing_in_background':
        notice += f"可用 status {task['id']} 查看进度，cancel {task['id']} 取消。"
    elif task['status'] == 'waiting_model':
        notice += f"下载完成后运行 run {task['id']} 继续；可用 models cancel 取消下载。"
    elif task.get('failures'):
        notice = '这次未能完成出稿，任务记录已保留。' + notice
        notice += f"可用 show {task['id']} 查看失败原因，处理后用 run {task['id']} 续做。"
    elif items:
        if result['reused']:
            notice = '已复用以前的结果，没有重新识别。' + notice
        elif task.get('input_kind') == 'text':
            notice = '已原样导入文稿，没有调用语音识别。' + notice
        else:
            notice = '机器稿已生成，尚未校对。' + notice
        notice += f"可用 show {task['id']} 查看任务，接着校对或导出。"
    for key in ('failures', 'acquisition_gaps', 'model_status', 'progress'):
        if task.get(key):
            result[key] = task[key]
    result['tell_user'] = notice
    return result


def run(values: list[str], evidence: str = '', root: Path = ROOT) -> dict:
    if len(values) != 1:
        return full_flow(values)
    value = values[0]
    path = Path(value).expanduser()
    try:
        text_file = path.is_file() and path.suffix.lower() in {'.txt', '.md'}
    except OSError:
        text_file = False
    normalized = None
    needs_resolution = False
    if text_file and path.suffix.lower() == '.txt':
        try:
            candidate = media.inputs(values)
            lines = [line.strip() for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
            if all(line in candidate or Path(line).expanduser().is_file() for line in lines):
                if len(lines) > 1:
                    return full_flow(values)
                normalized, text_file = candidate, False
        except (OSError, ValueError):
            pass  # Ordinary prose follows plan-text, as before.
    if text_file:
        task = content.plan_text(path, root)
    else:
        if len(re.findall(r'https?://', value)) > 1:
            return full_flow(values)
        normalized = normalized if normalized is not None else media.inputs(values, allow_list=False)
        if len(normalized) != 1 or any(media.has_profile(item) for item in normalized):
            return full_flow(normalized)
        value = normalized[0]
        if value.startswith('http'):
            try:
                acquisition.video_id(value)
            except ValueError:
                needs_resolution = urlsplit(value).hostname in {
                    'v.douyin.com', 'www.iesdouyin.com', 'iesdouyin.com'}
                if not needs_resolution:
                    return full_flow(normalized)
        task = workflow.plan(normalized, root)
    task = workflow.confirm(task['id'], 'transcribe', evidence or DEFAULT_EVIDENCE, root=root)
    if needs_resolution:
        try:
            resolved = media.douyin_browser.resolve_url(value)
        except (ValueError, RuntimeError, OSError) as exc:
            task.update(status='failed', failures=[{'stage': 'acquisition', 'error': str(exc)}])
            workflow.save(task, root)
            return summary(task)
        task['inputs'] = media.inputs([resolved], allow_list=False)
        try:
            acquisition.video_id(resolved)
        except ValueError:
            task['status'] = 'needs_scope'
            workflow.save(task, root)
            return full_flow(task['inputs'], task)
        workflow.save(task, root)
    return summary(workflow.run(task['id'], root))
