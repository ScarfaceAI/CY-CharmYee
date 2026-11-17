"""CPU Qwen3-ASR, bounded speech segments and a parent-owned worker."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid
import wave

from . import models, processes
from .common import read_json, write_json

ENGINE = 'sherpa-onnx-qwen3-asr'
SAMPLE_RATE = 16000
DECODE = {'provider': 'cpu', 'decoding_method': 'greedy_search', 'max_new_tokens': 512,
          'max_total_len': 512, 'seed': 42, 'temperature': 1e-6}
VAD = {'threshold': 0.5, 'min_silence_duration': 0.5, 'min_speech_duration': 0.25,
       'window_size': 512, 'max_speech_duration': 20}
SPLIT = {'max_seconds': 15, 'search_from_seconds': 10, 'frame_samples': 320,
         'pause_min_seconds': 0.2, 'pause_search_from_seconds': 0.25,
         'pause_rms': 0.01, 'pause_relative_rms': 0.15}
PARAMETERS = {'decode': DECODE, 'vad': VAD, 'split': SPLIT, 'sample_rate': SAMPLE_RATE,
              'vad_num_threads': 1, 'vad_provider': 'cpu', 'vad_buffer_seconds': 120}
_recognizers = {}


def speed_coefficient(profile: str, threads: int) -> float:
    path = models.model_root() / 'asr-calibration.json'
    data = read_json(path) if path.is_file() else {}
    value = data.get(f'{profile}:{threads}', {}).get('coefficient', 0.3)
    return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else 0.3


def calibrate(profile: str, threads: int, audio_seconds: float, recognition_seconds: float) -> dict:
    # Called by the child while holding the machine recognition lock, excluding queue/verification time.
    if audio_seconds <= 0 or recognition_seconds <= 0:
        raise ValueError('速度校准需要大于 0 的音频时长和识别耗时。')
    path = models.model_root() / 'asr-calibration.json'
    data = read_json(path) if path.is_file() else {}
    key = f'{profile}:{threads}'
    prior = data.get(key, {})
    ratio = recognition_seconds / audio_seconds
    value = {'coefficient': 0.8 * prior['coefficient'] + 0.2 * ratio if prior else ratio,
             'samples': prior.get('samples', 0) + 1, 'last_ratio': ratio,
             'audio_seconds': audio_seconds, 'recognition_seconds': recognition_seconds}
    data[key] = value
    write_json(path, data)
    return value


def profile_for(metadata: dict) -> str:
    basis = {key: metadata[key] for key in ('model', 'model_sha256', 'vad_sha256', 'parameters')}
    return 'local-' + hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:12]


def specification() -> dict:
    manifest = models.manifest()
    result = {'model': models.DEFAULT_MODEL,
              'model_sha256': {f['path']: f['sha256'] for f in manifest['models'][models.DEFAULT_MODEL]['files']},
              'vad_sha256': manifest['bundled']['silero_vad']['sha256'], 'parameters': PARAMETERS}
    return {**result, 'asr_profile': profile_for(result)}


def physical_cores() -> int | None:
    if os.name != 'nt':
        return None
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    query = api.GetLogicalProcessorInformationEx
    query.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    size = wintypes.DWORD()
    query(0, None, ctypes.byref(size))  # RelationProcessorCore, variable-sized entries.
    if not size.value:
        return None
    buffer = ctypes.create_string_buffer(size.value)
    if not query(0, buffer, ctypes.byref(size)):
        return None
    offset, count = 0, 0
    while offset + 8 <= size.value:
        relation = wintypes.DWORD.from_buffer(buffer, offset).value
        length = wintypes.DWORD.from_buffer(buffer, offset + 4).value
        if length < 8 or offset + length > size.value:
            return None
        count += relation == 0
        offset += length
    return count or None


def thread_count(cfg: dict) -> int:
    override = cfg.get('asr_threads')
    if override is not None:
        if type(override) is not int or override < 1:
            raise ValueError('asr_threads 应为正整数。')
        return override
    cores = physical_cores()
    return max(1, min(4, cores // 2 if cores else (os.cpu_count() or 1) // 4))


@contextmanager
def recognition_lock(timeout: float, on_wait=None):
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
        raise ValueError('asr_queue_timeout_seconds 应为不小于 0 的有限秒数。')
    # A fixed, machine-shared lock in the model root. Also excludes model management.
    # Reuse the shipped model's lock, opened read-only when it already exists.
    folder = models.model_root() / models.DEFAULT_MODEL
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        lock = models._lock(models.DEFAULT_MODEL, folder)
        try:
            lock.__enter__()
            break
        except models.ModelError:
            if not announced and on_wait:
                on_wait()
                announced = True
            if time.monotonic() >= deadline:
                raise TimeoutError('本地识别排队超时；已完成的分片已保留，再次 run 可继续。') from None
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def create(paths: dict, threads: int, profile: str):
    import sherpa_onnx
    key = (profile, tuple(sorted(paths.items())), threads)
    if key not in _recognizers:
        _recognizers[key] = sherpa_onnx.OfflineRecognizer.from_qwen3_asr(
            encoder=paths['encoder.int8.onnx'], decoder=paths['decoder.int8.onnx'],
            conv_frontend=paths['conv_frontend.onnx'], tokenizer=str(Path(paths['tokenizer/vocab.json']).parent),
            num_threads=threads, **DECODE)
    return _recognizers[key]


def vad_new(path: str):
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = path
    for key, value in VAD.items():
        setattr(cfg.silero_vad, key, value)
    cfg.sample_rate, cfg.num_threads, cfg.provider = SAMPLE_RATE, 1, 'cpu'
    return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, SAMPLE_RATE):
            raise ValueError('本地识别需要 16 kHz、单声道、16 位 WAV 分片。')
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768


def vad_segments(vad, audio):
    import numpy as np
    vad.reset()
    parts = []

    def drain():
        while not vad.empty():
            segment = vad.front
            start = int(segment.start)
            length = max(0, min(len(segment.samples), len(audio) - start))
            if length:
                parts.append((start, np.array(segment.samples[:length], dtype=np.float32, copy=True)))
            vad.pop()

    for at in range(0, len(audio), VAD['window_size']):
        window = audio[at:at + VAD['window_size']]
        if len(window) < VAD['window_size']:
            window = np.pad(window, (0, VAD['window_size'] - len(window)))
        vad.accept_waveform(window)
        drain()
    vad.flush()
    drain()
    return parts


def split_segment(start: int, samples):
    """Partition without overlap or dropped samples; prefer a pause before the hard limit."""
    import numpy as np
    limit = int(SPLIT['max_seconds'] * SAMPLE_RATE)
    lower = int(SPLIT['search_from_seconds'] * SAMPLE_RATE)
    frame = SPLIT['frame_samples']
    offset = 0
    while len(samples) - offset > limit:
        window = samples[offset:offset + limit]
        energies = np.sqrt(np.mean(window.reshape(-1, frame).astype(np.float64) ** 2, axis=1))
        threshold = min(SPLIT['pause_rms'], float(np.max(energies)) * SPLIT['pause_relative_rms'])
        quiet = energies <= threshold
        edges = np.diff(np.r_[False, quiet, False].astype(int))
        pauses = [(b - a, a, b) for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1))
                  if (b - a) * frame >= SPLIT['pause_min_seconds'] * SAMPLE_RATE and
                  (a + b) * frame // 2 >= SPLIT['pause_search_from_seconds'] * SAMPLE_RATE]
        if pauses:
            _, a, b = max(pauses)
            cut = (a + b) * frame // 2
        else:
            cut = lower + int(np.argmin(energies[lower // frame:])) * frame + frame // 2
        yield start + offset, samples[offset:offset + cut]
        offset += cut
    if offset < len(samples):
        yield start + offset, samples[offset:]


def recognize(audio: Path, paths: dict, threads: int, profile: str, *, hotwords: str | None = None) -> dict:
    # Silence never loads the large recognizer or creates a decoding stream.
    parts = vad_segments(vad_new(paths['silero_vad']), read_wav(audio))
    segments = []
    timings = {}
    recognizer_threads = None
    if parts:
        load_started = time.monotonic()
        recognizer = create(paths, threads, profile)
        timings['model_load_seconds'] = time.monotonic() - load_started
        recognizer_threads = recognizer.config.model_config.num_threads
        for start, samples in parts:
            for at, piece in split_segment(start, samples):
                stream = recognizer.create_stream()
                if hotwords is not None:
                    stream.set_option('hotwords', hotwords)
                stream.accept_waveform(SAMPLE_RATE, piece)
                recognizer.decode_stream(stream)
                raw = stream.result.text
                segments.append({'start': at / SAMPLE_RATE, 'end': (at + len(piece)) / SAMPLE_RATE,
                                 'text': raw, 'raw_text': raw})
    return {'text': ''.join(s['text'] for s in segments), 'segments': segments, 'hotwords': hotwords,
            'recognizer_threads': recognizer_threads, **timings}


def waiting_for_model() -> dict | None:
    state = models.status(cache=False)
    if state['ready'] and not state['download_active']:
        return None
    if not state['download_active']:
        state = models.download()
    return {'status': 'waiting_model', 'model_status': state,
            'tell_user': '本地识别模型尚未就绪，已启动或续接后台下载。模型就绪后再次 run 即可继续；'
                         '可运行 models status 查看下载进度。'}


class Worker(processes.Worker):
    def __init__(self, log: Path):
        super().__init__(log, [sys.executable, '-B', '-X', 'utf8', '-m', 'douyin_text.local_asr_worker'],
                         low_priority=True)
        self.on_progress = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def recognize(self, audio: Path, folder: Path, cfg: dict, profile: str, *, hotwords=None):
        if self.process is None:
            self.start()
        request_file = folder / 'requests' / (uuid.uuid4().hex + '.json')
        response = request_file.with_suffix('.response.json')
        progress = request_file.with_suffix('.progress.json')
        write_json(request_file, {'audio': str(audio), 'threads': thread_count(cfg), 'profile': profile,
            'queue_timeout': cfg['asr_queue_timeout_seconds'], 'hotwords': hotwords,
            'response_file': str(response), 'progress_file': str(progress)})
        self.process.stdin.write((str(request_file) + '\n').encode('utf-8'))
        self.process.stdin.flush()
        last_state = None
        while not response.is_file():
            if progress.is_file():
                update = read_json(progress)
                if update['status'] != last_state:
                    last_state = update['status']
                    if update.get('tell_user'):
                        print(update['tell_user'], file=sys.stderr, flush=True)
                    if self.on_progress:
                        self.on_progress(update)
            if self.process.poll() is not None:
                raise RuntimeError('本地识别子进程提前退出，已完成的分片已保留；详情见本地识别日志。')
            time.sleep(0.025)
        outcome = read_json(response)
        if 'error' in outcome:
            raise RuntimeError(outcome['error'])
        return outcome['result']
