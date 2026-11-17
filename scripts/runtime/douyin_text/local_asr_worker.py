"""Child entrypoint. Do not load a model before the parent's Job assignment/handshake."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import sys
import time

from . import local_asr, models
from .common import read_json, write_json


def main():
    with ExitStack() as stack:
        locked = False
        paths = None
        metadata = None
        for line in sys.stdin.buffer:
            request = read_json(Path(line.decode('utf-8').strip()))
            started = time.monotonic()
            try:
                if not locked:
                    stack.enter_context(local_asr.recognition_lock(request['queue_timeout'], lambda: write_json(
                        Path(request['progress_file']), {'status': 'queued',
                            'tell_user': '其他任务正在使用本地识别模型，当前任务正在排队；已完成的分片会保留。'})))
                    locked = True
                if paths is None:
                    paths = models.ready_paths(cache=False)
                    metadata = local_asr.specification()
                if metadata['asr_profile'] != request['profile']:
                    raise ValueError('任务的本地识别档案与当前模型或参数不同，请恢复原环境或另建任务重新识别。')
                import sherpa_onnx
                result = local_asr.recognize(Path(request['audio']), paths, request['threads'],
                                             request['profile'], hotwords=request.get('hotwords'))
                result.update(**metadata, engine=local_asr.ENGINE, sherpa_onnx_version=sherpa_onnx.__version__,
                              threads=request['threads'], worker_pid=os.getpid(),
                              elapsed_seconds=round(time.monotonic() - started, 3))
                outcome = {'result': result}
            except Exception as exc:
                outcome = {'error': str(exc)}
            write_json(Path(request['response_file']), outcome)


if __name__ == '__main__':
    main()
