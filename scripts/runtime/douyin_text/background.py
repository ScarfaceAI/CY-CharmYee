"""Local ASR task handoff, progress and identity-checked cancellation."""
from __future__ import annotations

import math
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from . import local_asr, models, processes
from .common import ROOT, ArtifactBusyError, artifact_lock, config, read_json, relative, write_json

FINISHED = {'completed', 'published', 'awaiting_review', 'awaiting_edit', 'awaiting_analysis',
            'awaiting_compile', 'ready_for_ingest', 'ready_to_deliver', 'ready_to_publish'}


def folder(task_id: str, root: Path) -> Path:
    from .workflow import task_path
    return task_path(task_id, root).parent


def record(task_id: str, root: Path) -> dict:
    path = folder(task_id, root) / 'asr-run.json'
    return read_json(path) if path.is_file() else {}


def task_lock(task_id: str, root: Path, *, wait_timeout=0):
    return artifact_lock(folder(task_id, root) / 'asr-lock', wait_timeout=wait_timeout)


def finished(task: dict) -> bool:
    return task['status'] in FINISHED


class Progress:
    def __init__(self, task_id: str, root: Path, data: dict):
        self.path = folder(task_id, root) / 'asr-progress.json'
        self.data = data
        self.save()

    def save(self):
        write_json(self.path, self.data)

    def current(self, job: dict, index: int):
        self.data.update(current_chunk={'id': job['id'], 'index': index + 1},
                         phase='preparing', phase_started=time.time())
        self.save()

    def phase(self, update: dict):
        self.data.update(phase=update['status'], phase_started=time.time())
        self.save()

    def finish(self):
        self.data.update(phase='finished', finished=time.time(), current_chunk=None)
        self.save()


def prepare_job(job: dict, task: dict, root: Path, cfg: dict) -> tuple[list[dict], float]:
    from . import workflow, media
    cached = workflow.cached_result(job, task, root)
    if cached:
        return [], read_json(root / cached['result'])['duration']
    chunks = []
    cache = root / '.cache' / job['id'] / workflow.version_for(task)
    # Extraction and transcription use the same artifact lock and source checks.
    with artifact_lock(cache):
        original = job.get('input', '')
        expected = task.get('input_fingerprints', {}).get(original)
        if expected and Path(original).is_file() and workflow.digest(Path(original)) != expected:
            raise ValueError('原本地素材已经变化，缺少旧版有效缓存；保留任务，请指定原素材或新建任务')
        for index, chunk in enumerate(media.extract(job, cache / 'audio', cfg['chunk_seconds'])):
            chunks.append({'id': job['id'], 'index': index + 1, 'duration': chunk['duration'],
                           'sha256': chunk['sha256'],
                           'receipt': relative(root, cache / f'chunk-{index:05d}.json')})
    return chunks, 0.0


def prepare(task: dict, jobs: list[dict], root: Path) -> Progress:
    from . import workflow
    cfg = config(root)
    threshold = cfg['asr_background_threshold_seconds']
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or threshold < 0:
        raise ValueError('asr_background_threshold_seconds 应为不小于 0 的有限秒数。')
    chunks, cached_seconds, failures = [], 0.0, []
    for job in jobs:
        try:
            pending, cached_duration = prepare_job(job, task, root, cfg)
            chunks.extend(pending)
            cached_seconds += cached_duration
        except Exception as exc:
            # Preserve run's per-media failure behavior: one bad input must not abort the batch.
            failures.append({'id': job['id'], 'input_number': job['input_number'],
                             'number': job.get('number', job['input_number']), 'error': str(exc)})
    coefficient = local_asr.speed_coefficient(workflow.profile_for(task), local_asr.thread_count(cfg))
    data = {'chunks': chunks, 'audio_duration_seconds': cached_seconds + sum(c['duration'] for c in chunks),
            'speed_coefficient': coefficient, 'started': time.time(), 'phase': 'preparing',
            'current_chunk': None, 'threshold_seconds': threshold, 'failures': failures}
    counts = count_chunks(data, root)
    data.update(estimated_seconds=counts['remaining_audio_seconds'] * coefficient,
                initial_completed_chunks=counts['completed_chunks'])
    return Progress(task['id'], root, data)


def count_chunks(data: dict, root: Path) -> dict:
    completed, remaining = 0, 0.0
    for chunk in data.get('chunks', []):
        path = root / chunk['receipt']
        prior = read_json(path) if path.is_file() else {}
        if prior.get('status') == 'succeeded' and prior.get('audio_sha256') == chunk['sha256']:
            completed += 1
        else:
            remaining += chunk['duration']
    return {'completed_chunks': completed, 'total_chunks': len(data.get('chunks', [])),
            'remaining_audio_seconds': remaining}


def status(task_id: str, root: Path = ROOT) -> dict:
    from . import workflow
    task = workflow.load(task_id, root)
    if finished(task):
        return task  # Exactly the same result summary as show, including the review stage.
    run = record(task_id, root)
    path = folder(task_id, root) / 'asr-progress.json'
    data = read_json(path) if path.is_file() else {}
    counts = count_chunks(data, root)
    alive = processes.alive(run)
    elapsed = max(0, data.get('finished', time.time()) - data.get('started', time.time()))
    remaining = counts['remaining_audio_seconds'] * data.get('speed_coefficient', 0.3)
    if data.get('phase') == 'recognizing':
        remaining = max(0, remaining - max(0, time.time() - data.get('phase_started', time.time())))
    progress = {**counts, 'current_chunk': data.get('current_chunk'), 'elapsed_seconds': round(elapsed, 3),
                'estimated_remaining_seconds': round(remaining, 3),
                'queued': alive and data.get('phase') == 'queued', 'phase': data.get('phase', 'preparing')}
    result = {**task, 'progress': progress, 'background_process_alive': alive and run.get('background', False),
              'process_alive': alive, 'background': run,
              'audio_duration_seconds': data.get('audio_duration_seconds', 0),
              'estimated_seconds': round(data.get('estimated_seconds', 0), 3),
              'speed_coefficient': data.get('speed_coefficient', 0.3)}
    if run.get('background') and not alive and task['status'] != 'cancelled':
        result.update(status='interrupted', tell_user='后台识别已中断，已完成的分片已保留；再次 run 即可续接。')
    elif task['status'] == 'cancelled':
        result['tell_user'] = '后台识别已取消，已完成的分片已保留；再次 run 即可续接。'
    elif alive and run.get('background'):
        result['status'] = 'transcribing_in_background'
        result['tell_user'] = (f"后台识别已完成 {counts['completed_chunks']}／{counts['total_chunks']} 个分片，"
                               + ('正在排队等待本地识别锁；' if progress['queued'] else '')
                               + f'预计还需 {remaining / 60:.1f} 分钟。可运行 status 查看进度，cancel 取消。')
    if run.get('background_warning'):
        result['tell_user'] = result.get('tell_user', '') + ' ' + run['background_warning']
    return result


def register_foreground(task_id: str, root: Path):
    write_json(folder(task_id, root) / 'asr-run.json', {
        'background': False, 'pid': os.getpid(), 'process_birth': processes._process_birth(os.getpid())})


def child_identity(child, path: Path, token: str) -> dict:
    # Windows venv python.exe may be a redirector. Register the actual interpreter,
    # which owns the task lock and the inference Job, rather than its launcher PID.
    deadline = time.monotonic() + 5
    while not path.is_file():
        if child.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError('后台识别启动握手未完成，详情见任务目录的 background.log。')
        time.sleep(0.02)
    identity = read_json(path)
    if (identity.get('token') != token or child.pid not in {identity.get('pid'), identity.get('parent_pid')} or
            not processes.alive(identity)):
        raise RuntimeError('无法核实后台识别进程身份，请再次 run。')
    return identity


def start(task: dict, root: Path, progress: Progress, options: dict) -> dict:
    from . import workflow
    token = uuid.uuid4().hex
    command = [sys.executable, '-B', '-X', 'utf8', '-m', 'douyin_text.background', str(root.resolve()), task['id'], token]
    env = {k: v for k, v in os.environ.items() if not k.startswith('DOUBAO_ASR_')}
    env.update(PYTHONPATH=str(Path(__file__).resolve().parent.parent), CY_WORKSPACE=str(root.resolve()),
               CY_MODEL_DIR=str(models.model_root()), CY_UPDATE_CHECK='0')
    with (folder(task['id'], root) / 'background.log').open('ab') as log:
        child, breakaway = processes.launch_background(command, stdin=subprocess.PIPE, stdout=log,
            stderr=subprocess.STDOUT, env=env, cwd=Path(__file__).resolve().parent.parent)
    try:
        identity = child_identity(child, folder(task['id'], root) / f'asr-start-{token}.json', token)
        run = {'background': True, 'token': token, 'pid': identity['pid'],
               'process_birth': identity['process_birth'], 'launcher_pid': child.pid,
               'job_breakaway': breakaway, 'options': options, 'started': time.time()}
        if breakaway is False:
            run['background_warning'] = ('宿主不允许脱离作业对象，后台识别可能随宿主结束；'
                                         '若中断，再次 run 可续接，也可调高阈值后前台运行。')
        write_json(folder(task['id'], root) / 'asr-run.json', run)
        task['status'] = 'transcribing_in_background'
        workflow.save(task, root)
        child.stdin.write(b'go\n')
        child.stdin.flush()
    except BaseException:
        child.kill()
        child.wait(timeout=5)
        raise
    finally:
        child.stdin.close()
    result = status(task['id'], root)
    result['tell_user'] = (f"这段录音约 {progress.data['audio_duration_seconds'] / 60:.1f} 分钟，"
                           f"预计 {progress.data['estimated_seconds'] / 60:.1f} 分钟识别完，已在后台开始；"
                           '可以先做别的，想看进度时告诉我。')
    if run.get('background_warning'):
        result['tell_user'] += ' ' + run['background_warning']
    return result


def cancel(task_id: str, root: Path = ROOT) -> dict:
    from . import workflow
    task = workflow.load(task_id, root)
    if finished(task):
        return task
    run = record(task_id, root)
    if not run.get('background'):
        return {**task, 'tell_user': '这是前台任务，不能用 cancel 取消；请在启动它的终端按 Ctrl+C 中断。'}
    if processes._process_birth(run.get('pid', 0)) is None:
        return status(task_id, root)
    processes.terminate_registered(run)
    # A concurrent run may have resumed already; never overwrite that new run's state.
    try:
        with task_lock(task_id, root, wait_timeout=5):
            task = workflow.load(task_id, root)
            if record(task_id, root).get('token') == run['token'] and not finished(task):
                task['status'] = 'cancelled'
                workflow.save(task, root)
                path = folder(task_id, root) / 'asr-progress.json'
                if path.is_file():
                    write_json(path, {**read_json(path), 'phase': 'cancelled', 'finished': time.time()})
    except ArtifactBusyError:
        pass
    return status(task_id, root)


def execute(task_id: str, root: Path, token: str) -> dict:
    from . import workflow
    with task_lock(task_id, root, wait_timeout=5):
        run = record(task_id, root)
        if (run.get('token') != token or run.get('pid') != os.getpid() or not processes.alive(run)):
            raise RuntimeError('后台识别身份不匹配，未开始任务。')
        return workflow._run(task_id, root, _background=True, **run['options'])


if __name__ == '__main__':
    root, task_id, token = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    write_json(folder(task_id, root) / f'asr-start-{token}.json', {
        'token': token, 'pid': os.getpid(), 'parent_pid': os.getppid(),
        'process_birth': processes._process_birth(os.getpid())})
    if sys.stdin.readline().strip() == 'go':
        if os.name != 'nt':
            os.nice(10)
        execute(task_id, root, token)
