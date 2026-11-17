from __future__ import annotations

import re
import copy
import time
import uuid
from datetime import date
from pathlib import Path

from . import acquisition, asr, credentials, media, local_asr, background, processes, numbers, lexicon, suspects, hotwords
from .common import ROOT, ArtifactBusyError, artifact_lock, config, digest, inside, now, read_json, relative, write_json

PURPOSES = {"transcribe": "只转写", "analyze": "总结／分析", "ingest": "原文留底与校对稿入库", "compile": "入库并编译", "rewrite": "口播改写"}
LEGACY_ASR_PROFILE = '0426d7a84e24'


def task_path(task_id: str, root: Path = ROOT) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
        raise ValueError("任务 ID 无效")
    return root / "runs" / task_id / "task.json"


def load(task_id: str, root: Path = ROOT) -> dict:
    return relocate(read_json(task_path(task_id, root)), root)


def relocate(task: dict, root: Path = ROOT) -> dict:
    """Express recorded workspace files relative to the workspace.

    Versions before 1.3.0 stored absolute paths. Once the folder is renamed or moved they point at
    the old location, which is recognised from the fixed layout of results and text snapshots.
    User inputs, vault targets and publisher manifests live outside the workspace and stay as they are.
    """
    imports = task.get("text_imports") or ([task["text_import"]] if task.get("text_import") else [])
    bases = [root.resolve()]
    for item in task.get("results", []):
        path = Path(item.get("result", ""))
        if path.is_absolute() and path.name == "result.json" and path.parts[-4:-2] == ("outputs", item.get("id")):
            bases.append(path.parents[3])
    for item in imports:
        path = Path(item.get("file", ""))
        if path.is_absolute() and path.parts[-4:-1] == ("runs", task.get("id"), "sources"):
            bases.append(path.parents[3])

    def convert(value: str) -> str:
        path = Path(value)
        for base in bases if path.is_absolute() else []:
            if path.is_relative_to(base):
                return path.relative_to(base).as_posix()
        return value

    publication = task.get("publication", {})
    fields = [(item, key) for item in task.get("results", []) for key in ("result", "markdown", "text")]
    fields += [(item, "file") for item in imports]
    fields += [(item, "file") for kind in ("review", "spoken", "archive") for item in task.get(kind, {}).get("files", [])]
    fields += [(task[key], "file") for key in ("delivery", "analysis") if key in task]
    fields += [(publication, "receipt_file")] + [(item, "source") for item in publication.get("files", [])]
    for holder, key in fields:
        if key in holder:
            holder[key] = convert(holder[key])
    return task


def save(task: dict, root: Path = ROOT) -> None:
    write_json(task_path(task["id"], root), task)


def suspect_summary(task: dict, root: Path = ROOT) -> dict:
    if not task.get('results'):
        return task
    previous = task.get('suspects', {}).get('tell_user', '')
    notice = task.get('tell_user', '')
    if previous:
        notice = notice.replace(previous, '')
    counts = dict.fromkeys(suspects.LABELS, 0)
    notices = set()
    ready = 0
    for item in task['results']:
        old_notice = item.get('suspects', {}).get('tell_user', '')
        if old_notice:
            notice = notice.replace(old_notice, '')
        saved = suspects.read_summary(inside(root, item['result']), root)
        item['suspects'] = saved
        if saved['status'] == 'ready':
            ready += 1
            notices.update(saved.get('notices', []))
            for kind in counts:
                counts[kind] += saved['counts'].get(kind, 0)
    missing = len(task['results']) - ready
    message = suspects.summary(counts) if ready else ''
    if len(task['results']) > 1 and message:
        message = message.replace('这条稿子', f'已检查的 {ready} 条稿子合计')
    if missing:
        message += f'有 {missing} 条稿子的疑点清单尚未生成或已过期，可用 suspects {task["id"]} 重算。'
    message += ''.join(sorted(notices))
    task['suspects'] = {'counts': counts, 'total': sum(counts.values()), 'ready': ready,
                        'missing': missing, 'tell_user': message}
    task['tell_user'] = notice + message
    return task


def show(task_id: str, root: Path = ROOT) -> dict:
    return suspect_summary(load(task_id, root), root)


def recalculate_suspects(task_id: str, root: Path = ROOT) -> dict:
    from . import content
    with background.task_lock(task_id, root):
        registered = background.record(task_id, root)
        if registered.get('background') and processes.alive(registered):
            raise ValueError('本任务仍在识别，请完成或取消后再重算疑点。')
        task = load(task_id, root)
        originals = content.originals(task, root)
        vocabulary = suspects.Vocabulary(root)
        for item, result in zip(task['results'], originals):
            item['suspects'] = suspects.generate(inside(root, item['result']), result['source'], vocabulary, root)
        suspect_summary(task, root)
        save(task, root)
        return task


def plan(values: list[str], root: Path = ROOT) -> dict:
    normalized = media.inputs(values)
    if not normalized:
        raise ValueError("输入为空")
    task = {"id": "dy-" + uuid.uuid4().hex[:16], "created": now(), "status": "needs_intent",
            "inputs": normalized, "purpose": None, "results": [], "failures": [], "asr_pending": True,
            "question": "本次要做什么？只转写、总结／分析、原稿入库，还是入库并编译？",
            "choices": PURPOSES}
    task['input_fingerprints'] = {value: digest(Path(value)) for value in normalized
                                  if not value.startswith(('http://', 'https://')) and Path(value).is_file()}
    if any(media.has_profile(value) for value in normalized):
        task["scope_question"] = "作者主页请同时选择：最新 N 条、指定日期范围或全部可访问视频。"
    save(task, root)
    return task


def confirm(task_id: str, purpose: str, evidence: str, *, scope: dict | None = None,
            instruction: str = "", category: str = "reference", retranscribe: bool = False,
            root: Path = ROOT) -> dict:
    task = load(task_id, root)
    if task["status"] not in {"needs_intent", "confirmed", "needs_scope", "failed"}:
        raise ValueError("当前任务已执行，请为新的用途创建任务；已有转写结果可复用")
    if purpose not in PURPOSES or not evidence.strip():
        raise ValueError("必须记录本次明确选定的用途和用户确认原话")
    if purpose in {"analyze", "rewrite"} and not instruction.strip():
        raise ValueError("请通过 --instruction 记录用户已给出的具体分析或改稿要求；只有要求不清楚时才补问")
    if category not in {"reference", "benchmark", "own"}:
        raise ValueError("材料分类无效")
    scope = scope or {}
    modes = sum(bool(scope.get(key)) for key in ("all", "latest", "start"))
    if modes > 1 or scope.get("latest", 1) < 1:
        raise ValueError("主页范围应为最新 N 条、日期范围或全部之一")
    if scope.get("start") or scope.get("end"):
        if not scope.get("start") or not scope.get("end"):
            raise ValueError("日期范围需要起止日期")
        if date.fromisoformat(scope["start"]) > date.fromisoformat(scope["end"]):
            raise ValueError("开始日期晚于结束日期")
    if any(media.has_profile(value) for value in task["inputs"]) and not media.scope_selected(scope):
        raise ValueError("作者主页必须同时确认处理范围")
    if task.get("results") and retranscribe != bool(task.get("retranscribe")):
        raise ValueError("任务已有结果，重新识别请创建新任务")
    task.update(purpose=purpose, evidence=evidence, confirmed_at=now(), scope=scope,
                instruction=instruction, category=category, status="confirmed", retranscribe=retranscribe)
    if retranscribe and not task.get("revision"):
        task["revision"] = uuid.uuid4().hex[:12]
    save(task, root)
    return task


def render_markdown(result: dict) -> str:
    job = result["source"]
    lines = ["# " + job["title"], "", "- 类型：机器语音识别原稿（未经人工校对）",
             "- 来源：" + job["source"], "- 作者：" + job.get("author", "未指定"),
             "- 转写时间：" + result["created"], "- 服务：豆包录音文件识别极速版",
             "- 处理：保留口头语和重复表达；开启标点；关闭语义顺滑和数字规整"]
    if result['engine'] == local_asr.ENGINE:
        lines[-2:] = ['- 服务：本地 Qwen3-ASR（sherpa-onnx，CPU）',
                      '- 处理：Silero VAD 断句；每段不超过 15 秒；保留模型原始文本']
        if 'display' in result:
            display = result['display']
            lines[-1] += ('；数字转换：' + ('开启' if display['number_conversion'] else '关闭') +
                          '；中英文与数字间距：' + ('开启' if display['cjk_spacing'] else '关闭') +
                          '；模型原文保存在 result.json。')
    if job.get("published"):
        lines.append("- 视频发布时间：" + job["published"])
    return "\n".join(lines) + "\n\n## 逐字稿\n\n" + result["text"] + "\n"


def engine_for(task: dict) -> str:
    return task.get('asr_engine', 'doubao')


def profile_for(task: dict) -> str:
    return task.get('asr_profile') or LEGACY_ASR_PROFILE


def pin_asr(task: dict, root: Path) -> None:
    # Absence of the marker means a historical task, never a new default selection.
    if not task.get('asr_pending'):
        return
    engine = config(root)['asr_engine']
    if engine not in {'doubao', 'local'}:
        raise ValueError('asr_engine 只能设为 doubao 或 local。')
    task.update(asr_engine=engine, asr_profile=(local_asr.specification()['asr_profile']
                                               if engine == 'local' else LEGACY_ASR_PROFILE))
    task.pop('asr_pending', None)


def version_for(task: dict) -> str:
    return profile_for(task) + ("-" + task["revision"] if task.get("revision") else "")


def display_options(cfg: dict, *, number_conversion=None, cjk_spacing=None) -> dict:
    options = {'number_conversion': cfg['number_conversion'] if number_conversion is None else number_conversion,
               'cjk_spacing': cfg['cjk_spacing'] if cjk_spacing is None else cjk_spacing}
    for key, value in options.items():
        if type(value) is not bool:
            raise ValueError(f'{key} 必须是布尔值 true 或 false。')
    return options


def apply_display(result: dict, options: dict) -> None:
    """Rebuild from segment RAW, retaining the original chunk separators verbatim."""
    raw = result.get('raw_text', result['text'])
    exceptions, vocabulary = lexicon.display_exceptions(raw, result.get('display', {}).get('lexicon_exceptions'))
    cursor, parts, records = 0, [], []
    for index, segment in enumerate(result['segments'], 1):
        original = segment.get('raw_text')
        if not isinstance(original, str):
            raise ValueError('本地结果缺少逐段模型原文，无法重算展示文字；已有文件未改动。')
        start = raw.find(original, cursor)
        if start < 0 or raw[cursor:start].strip():
            raise ValueError('本地全文与逐段模型原文不一致，保留现场；未生成展示版本。')
        segment['text'], changes = numbers.apply(original, **options, extra_exceptions=exceptions)
        parts.extend((raw[cursor:start], segment['text']))
        records.append({'segment': index, 'raw_start': start, 'changes': changes})
        cursor = start + len(original)
    if raw[cursor:].strip():
        raise ValueError('本地全文缺少对应的模型原文段落，保留现场；未生成展示版本。')
    parts.append(raw[cursor:])
    result.update(raw_text=raw, text=''.join(parts),
                  display={**options, 'rules_version': numbers.RULES_VERSION, 'segments': records,
                           'lexicon_exceptions': vocabulary})


def write_result(result: dict, output: Path, root: Path, *, status='transcribed') -> dict:
    output.mkdir(parents=True, exist_ok=True)
    (output / 'transcript.txt').write_text(result['text'] + '\n', encoding='utf-8')
    (output / 'transcript.md').write_text(render_markdown(result), encoding='utf-8')
    result['output_sha256'] = {suffix: digest(output / ('transcript.' + suffix)) for suffix in ('md', 'txt')}
    write_json(output / 'result.json', result)
    notice = lexicon.display_notice(result)
    hints = {}
    if result.get('engine') == local_asr.ENGINE:
        hints = suspects.generate(output / 'result.json', result['source'], suspects.Vocabulary(root), root)
        notice = (notice or '') + hints['tell_user']
    return {**({'tell_user': notice} if notice else {}), **({'suspects': hints} if hints else {}),
            'id': result['source']['id'], 'status': status, 'result': relative(root, output / 'result.json'),
            'markdown': relative(root, output / 'transcript.md'), 'text': relative(root, output / 'transcript.txt')}


def redisplay(task_id: str, root: Path = ROOT, *, number_conversion=None, cjk_spacing=None) -> dict:
    from . import content
    task = load(task_id, root)
    # No lock file, configuration migration or task write for ineligible tasks.
    if not any(read_json(inside(root, item['result'])).get('engine') == local_asr.ENGINE
               for item in task.get('results', [])) or task.get('input_kind') == 'text':
        return {'status': 'no_local_result', 'id': task_id,
                'tell_user': '任务里没有可用的本地识别结果，未改动任何文件。'}
    with background.task_lock(task_id, root):
        registered = background.record(task_id, root)
        if registered.get('background') and processes.alive(registered):
            raise ValueError('本任务仍在识别，请完成或取消后再重算展示文字。')
        task = load(task_id, root)
        originals = content.originals(task, root)
        options = display_options(config(root), number_conversion=number_conversion, cjk_spacing=cjk_spacing)
        revision = uuid.uuid4().hex[:12]
        prepared = []
        for item, original in zip(task['results'], originals):
            if original['engine'] != local_asr.ENGINE:
                continue
            if original['asr_profile'] != profile_for(task) or local_asr.profile_for(original) != profile_for(task):
                raise ValueError('本地结果的识别档案不一致，保留现场；未生成展示版本。')
            result = copy.deepcopy(original)
            apply_display(result, options)
            result['created'] = now()
            result['display']['source_result'] = {'file': item['result'], 'sha256': digest(inside(root, item['result']))}
            output = root / 'outputs' / item['id'] / (profile_for(task) + '-' + revision)
            if output.exists():
                raise ValueError('展示版本目录已存在，保留现场；请重新运行 redisplay。')
            prepared.append((item, result, output))
        history = {key: copy.deepcopy(task[key]) for key in
                   ('revision', 'results', 'status', 'delivery', 'analysis', 'archive', 'publication', 'completed') if key in task}
        for item, result, output in prepared:
            item.update(write_result(result, output, root, status='redisplayed'))
        task.setdefault('display_history', []).append({**history, 'replaced_at': now()})
        for key in ('delivery', 'analysis', 'archive', 'publication', 'completed'):
            task.pop(key, None)
        task.update(revision=revision, status='incomplete' if coverage_gaps(task, root) else 'awaiting_review',
                    tell_user='已用模型原文另存新的展示版本，没有重新识别，旧版本文件保持不变。'
                              '已登记校对稿仍保留；正文哈希变化的条目需要复核并重新登记，再导出校对字幕或交付。')
        notices = {notice for _, result, _ in prepared if (notice := lexicon.display_notice(result))}
        task['tell_user'] += ''.join(sorted(notices))
        save(task, root)
        return task


def cached_result(job: dict, task: dict, root: Path) -> dict | None:
    output = root / 'outputs' / job['id'] / version_for(task)
    result_path = output / 'result.json'
    if not result_path.is_file():
        return None
    result = read_json(result_path)
    if engine_for(task) == 'local':
        try:
            valid = (result.get('engine') == local_asr.ENGINE and
                     result.get('asr_profile') == profile_for(task) == local_asr.profile_for(result))
        except (KeyError, TypeError):
            valid = False
    else:
        valid = result.get('engine') == 'doubao-auc-turbo' and result.get('parameters') == asr.PARAMETERS
    if not valid or result.get('source', {}).get('id') != job['id']:
        raise RuntimeError('已有识别缓存的来源或识别参数不一致，保留现场；重新识别须另存版本')
    for suffix in ('md', 'txt'):
        path = output / ('transcript.' + suffix)
        if not path.is_file() or digest(path) != result.get('output_sha256', {}).get(suffix):
            raise RuntimeError('已有原稿被修改，保留现场；请明确要求重新转写以另存新版本')
    notice = lexicon.display_notice(result)
    return {**({'tell_user': notice} if notice else {}),
            'suspects': suspects.read_summary(result_path, root),
            'id': job['id'], 'status': 'reused', 'result': relative(root, result_path),
            'markdown': relative(root, output / 'transcript.md'), 'text': relative(root, output / 'transcript.txt')}


def local_jobs(value: str, saved: list[dict], task: dict, root: Path) -> list[dict]:
    """Look up identity locally before attempting any remote metadata request."""
    matches = [job for job in saved if job.get('input') == value or job.get('source') == value]
    if not matches:
        try:
            ident = ('douyin-' + acquisition.video_id(value)) if value.startswith('http') else 'local-' + digest(Path(value))
        except (ValueError, OSError):
            return []
        matches = [job for job in saved if job['id'] == ident]
        if not matches:
            result = root / 'outputs' / ident / version_for(task) / 'result.json'
            if result.is_file():
                matches = [{**read_json(result)['source'], 'media': value}]
            else:
                source_manifest = root / '.cache' / ident / 'source/source.json'
                if source_manifest.is_file():
                    source = read_json(source_manifest)
                    if source.get('job'):
                        matches = [{**source['job'], 'media': value}]
    ready = []
    for job in matches:
        local_media = media.local_source(job, root)
        if local_media:
            job = {**job, 'media': str(local_media)}
        if cached_result(job, task, root) is not None or media.cached_chunks(
                root / '.cache' / job['id'] / version_for(task) / 'audio') is not None or local_media:
            ready.append(job)
    return ready if len(ready) == len(matches) else []


def transcribe(job: dict, task: dict, root: Path = ROOT, *, retry_failed: bool = False,
               worker=None, progress=None) -> dict:
    pin_asr(task, root)
    if engine_for(task) == 'local':
        cached = cached_result(job, task, root)
        if cached is not None:
            return cached
        waiting = local_asr.waiting_for_model()
        if waiting:
            return {'id': job['id'], **waiting}
        if worker is None:
            with local_asr.Worker(root / '.cache' / job['id'] / version_for(task) / 'local-asr.log') as owned:
                return _transcribe(job, task, root, retry_failed=retry_failed, worker=owned, progress=progress)
    return _transcribe(job, task, root, retry_failed=retry_failed, worker=worker, progress=progress)


def _transcribe(job: dict, task: dict, root: Path, *, retry_failed: bool, worker, progress=None) -> dict:
    version = version_for(task)
    output = root / "outputs" / job["id"] / version
    folder = root / ".cache" / job["id"] / version
    with artifact_lock(folder):
        cached = cached_result(job, task, root)
        if cached is not None:
            return cached
        cfg = config(root)
        display = display_options(cfg) if engine_for(task) == 'local' else None
        chunks = media.extract(job, folder / "audio", cfg["chunk_seconds"])
        audio_source = folder / 'audio/source.json'
        binding = read_json(audio_source) if audio_source.is_file() else None
        if binding and (binding.get('media_id') != job['id'] or
                        binding.get('chunks_sha256') != [chunk['sha256'] for chunk in chunks]):
            raise RuntimeError('音轨来源绑定与分片不一致，保留现场；未提交豆包')
        recognized = []
        selected = None
        for index, chunk in enumerate(chunks):
            chunk_path = folder / f"chunk-{index:05d}.json"
            prior = read_json(chunk_path) if chunk_path.exists() else {}
            if prior.get("status") == "succeeded":
                if prior.get('audio_sha256') != chunk['sha256']:
                    raise RuntimeError('已成功请求的音轨与当前分片不一致，保留现场；未重复提交')
                recognized.append(prior["result"])
                continue
            if prior and not retry_failed and engine_for(task) != 'local':
                raise RuntimeError("本媒体有失败或结果未知的请求。请先核对，再明确重试；已成功片段会复用")
            auth = credentials.load(root) if engine_for(task) != 'local' else None
            request_id = str(uuid.uuid4())
            attempt = {"status": "submitting", "request_id": request_id, "created": now(),
                       "audio_sha256": chunk["sha256"], "audio_duration": chunk['duration']}
            write_json(chunk_path, attempt)
            response_path = folder / "responses" / (request_id + ".json")
            try:
                if engine_for(task) == 'local':
                    if selected is None:
                        if type(cfg['asr_hotwords']) is not bool:
                            raise ValueError('asr_hotwords 必须是布尔值 true 或 false。')
                        selected = hotwords.select(job, root, limit=cfg['asr_hotwords_limit']) if cfg['asr_hotwords'] else []
                    if progress:
                        progress.current(job, index)
                    result = worker.recognize(folder / 'audio' / chunk['file'], folder, cfg, profile_for(task), hotwords=selected)
                else:
                    result = asr.recognize(folder / "audio" / chunk["file"], auth, response_path,
                                           timeout=cfg["asr_timeout_seconds"], request_id=request_id)
            except Exception as exc:
                attempt["status"] = "outcome_unknown" if isinstance(exc, asr.OutcomeUnknown) else "failed"
                attempt["error"] = str(exc)
                write_json(chunk_path, attempt)
                raise
            attempt.update(status="succeeded", result=result, completed=now())
            write_json(chunk_path, attempt)
            recognized.append(result)
        text = "\n".join(item["text"] for item in recognized if item["text"])
        if not text.strip():
            raise RuntimeError("音频未识别到可用语音，未生成逐字稿；已保存识别结果")
        segments = [{**segment, "start": segment["start"] + chunk["offset"],
                     "end": segment["end"] + chunk["offset"]}
                    for chunk, item in zip(chunks, recognized) for segment in item["segments"]]
        result = {"created": now(), "source": {key: value for key, value in job.items() if key != "media"},
                  "engine": "doubao-auc-turbo", "parameters": asr.PARAMETERS, "text": text,
                  "segments": segments, "duration": sum(chunk["duration"] for chunk in chunks),
                  "request_ids": [item.get("request_id") for item in recognized]}
        if engine_for(task) == 'local':
            result.pop('request_ids')
            result.update({key: recognized[0][key] for key in ('model', 'model_sha256', 'vad_sha256',
                'parameters', 'asr_profile', 'sherpa_onnx_version', 'threads')})
            result['hotwords'] = list({entry['word']: entry for item in recognized
                                      for entry in (item.get('hotwords') or []) if isinstance(entry, dict)}.values())
            result['hotword_seconds'] = sum(item.get('hotword_seconds', 0) for item in recognized)
            result['hotword_reviews'] = []
            segment_offset = 0
            for chunk_index, (chunk, item) in enumerate(zip(chunks, recognized), 1):
                result['hotword_reviews'].extend(hotwords.offset_review(r, segment_offset, chunk['offset'], chunk_index)
                                                for r in item.get('hotword_reviews', []))
                segment_offset += len(item['segments'])
            result.update(engine=local_asr.ENGINE, chunk_execution=[{
                **{key: item[key] for key in ('threads', 'recognizer_threads', 'worker_pid',
                                              'elapsed_seconds', 'sherpa_onnx_version')},
                **{key: item[key] for key in ('recognition_seconds', 'hotword_seconds') if key in item}}
                for item in recognized])
            apply_display(result, display)
        if binding:
            result['source_media_sha256'] = binding['source_sha256']
        return write_result(result, output, root)


def coverage_gaps(task: dict, root: Path = ROOT) -> list[dict]:
    """Include interrupted saved jobs even before a failure receipt was written."""
    gaps = copy.deepcopy(task.get('failures', []))
    for gap in task.get('acquisition_gaps', []):
        if gap not in gaps:
            gaps.append(copy.deepcopy(gap))
    jobs_file = task_path(task['id'], root).parent / 'jobs.json'
    jobs = read_json(jobs_file) if jobs_file.is_file() else []
    ready = {item['id'] for item in task.get('results', [])}
    for job in jobs:
        if job['id'] not in ready and not any(gap.get('id') == job['id'] for gap in gaps):
            gaps.append({'id': job['id'], 'stage': 'unfinished', 'input_number': job.get('input_number'),
                         'number': job.get('number'), 'error': '已保存作品尚未完成，需沿原范围续办'})
    return gaps


def reanalyze(task_id: str, instruction: str, root: Path = ROOT) -> dict:
    from . import content
    if not instruction.strip():
        raise ValueError('需要新的分析问题')
    source = load(task_id, root)
    content.originals(source, root)
    task = {key: copy.deepcopy(source[key]) for key in
            ('inputs', 'results', 'failures', 'input_kind', 'text_imports', 'input_fingerprints', 'category',
             'asr_engine', 'asr_profile', 'revision') if key in source}
    task.update(failures=coverage_gaps(source, root),
                id='dy-' + uuid.uuid4().hex[:16], created=now(), purpose='analyze', evidence=instruction.strip(),
                instruction=instruction.strip(), scope={}, offline=True, status='awaiting_review',
                snapshot={'source_task': task_id, 'source_created': source.get('created')})
    if source.get('review'):
        # Fork immutable, verified copies so later editing the parent cannot silently change this analysis.
        try:
            reviewed = content.checked_content(source, 'review', root)
        except (ValueError, OSError):
            reviewed = []
        files = []
        for entry in reviewed:
            original = inside(root, entry['file'])
            target = task_path(task['id'], root).parent / 'review-source' / (entry['id'] + original.suffix)
            content.immutable(target, original.read_bytes())
            files.append({**entry, 'file': relative(root, target)})
        if files:
            task['review'] = {**copy.deepcopy(source['review']), 'files': files}
            task['status'] = 'awaiting_analysis'
    save(task, root)
    return task


def run(task_id: str, root: Path = ROOT, *, retry_evidence: str = "", anonymous_only: bool = False,
        saved_scope: bool = False) -> dict:
    task = load(task_id, root)
    engine = config(root)['asr_engine'] if task.get('asr_pending') else engine_for(task)
    options = dict(retry_evidence=retry_evidence, anonymous_only=anonymous_only, saved_scope=saved_scope)
    if engine != 'local' or task.get('input_kind') == 'text':
        return suspect_summary(_run(task_id, root, **options), root)
    try:
        with background.task_lock(task_id, root):
            registered = background.record(task_id, root)
            if registered.get('background') and processes.alive(registered):
                return background.status(task_id, root)
            return suspect_summary(_run(task_id, root, **options), root)
    except ArtifactBusyError:
        result = background.status(task_id, root)
        result.setdefault('tell_user', '本任务已经在运行或准备中，未重复启动；可用 status 查看进度。')
        return result


def _run(task_id: str, root: Path = ROOT, *, retry_evidence: str = "", anonymous_only: bool = False,
         saved_scope: bool = False, _background: bool = False) -> dict:
    task = load(task_id, root)
    if not task.get("purpose") or not task.get("evidence"):
        raise ValueError("先询问并确认本次用途；未下载、未调用豆包、未入库")
    if task.get('offline'):
        raise ValueError('离线分析使用已有材料；需要补获取或识别时另行建立明确授权的任务')
    gaps = coverage_gaps(task, root)
    if not gaps and task["status"] in {"completed", "published"}:
        return task
    if not gaps and task["status"] in {"awaiting_review", "awaiting_edit", "awaiting_analysis", "awaiting_compile", "ready_for_ingest", "ready_to_deliver"}:
        return task
    if task["status"] in {"publishing", "publication_unknown", "publish_failed", "ready_to_publish"}:
        from .vault import publish
        return publish(task_id, root=root)
    if retry_evidence:
        task.setdefault("retry_confirmations", []).append({"evidence": retry_evidence, "at": now()})
    if anonymous_only:
        task['anonymous_only'] = True
    acquisition_stages = {'metadata', 'audio', 'acquisition'}
    prior_gaps = list(task.get('acquisition_gaps', []))
    for failure in task.get('failures', []):
        if failure.get('stage') in acquisition_stages and failure not in prior_gaps:
            prior_gaps.append(failure)
    task.update(status="transcribing_in_background" if _background else "running", failures=[], acquisition_gaps=prior_gaps)
    run_started = time.monotonic()
    task['run_timing'] = {'started': now(), 'events': []}
    save(task, root)
    local_worker = None
    local_progress = None
    model_wait = None
    try:
        if task.get("input_kind") == "text":
            from .content import restore_text_result
            sources = task.get("text_imports") or [task["text_import"]]
            task["results"] = [restore_text_result(task, root, item) for item in sources]
            task["status"] = "awaiting_review"
            save(task, root)
            return task
        pin_asr(task, root)
        save(task, root)
        if engine_for(task) == 'local':
            if not _background:
                background.register_foreground(task_id, root)
            task.pop('model_status', None)
            task.pop('tell_user', None)
            local_worker = local_asr.Worker(task_path(task_id, root).parent / 'local-asr.log')
        run_dir = task_path(task_id, root).parent
        saved = read_json(run_dir / 'jobs.json') if (run_dir / 'jobs.json').is_file() else []
        if saved_scope and not saved:
            raise ValueError('尚无已保存的作品范围，不能把重新发现来源当作恢复')
        jobs, pending, positions = [], [], []
        result_by_id = {item['id']: item for item in task.get('results', [])}
        for number, value in enumerate(task['inputs'], 1):
            try:
                if saved_scope:
                    jobs.extend(job for job in saved if job.get('input_number', 1) == number)
                    continue
                cached_jobs = local_jobs(value, saved, task, root)
                unresolved_input = any(gap.get('input_number', number) == number for gap in prior_gaps)
                if cached_jobs and not unresolved_input:
                    for subindex, job in enumerate(cached_jobs, 1):
                        job = {**job, 'input': value, 'input_number': number, 'input_item': subindex}
                        jobs.append(job)
                        existing = cached_result(job, task, root)
                        if existing:
                            result_by_id[job['id']] = existing
                else:
                    pending.append(value)
                    positions.append(number)
            except (OSError, ValueError, RuntimeError) as exc:
                task['failures'].append({'input_number': number, 'number': number, 'stage': 'cache', 'error': str(exc)})
        task['results'] = list(result_by_id.values())
        save(task, root)
        attempted = set()

        def recognize_ready(job):
            nonlocal model_wait
            if job['id'] in attempted:
                return
            attempted.add(job['id'])
            started = time.monotonic()
            event = {'id': job['id'], 'input_number': job['input_number'],
                     'ready_at_seconds': round(started - run_started, 3)}
            try:
                original_input = job.get('input', '')
                expected = task.get('input_fingerprints', {}).get(original_input)
                if expected and Path(original_input).is_file() and digest(Path(original_input)) != expected:
                    if cached_result(job, task, root) is None:
                        raise ValueError('原本地素材已经变化，缺少旧版有效缓存；保留任务，请指定原素材或新建任务')
                options = {'worker': local_worker, 'progress': local_progress} if local_worker is not None else {}
                outcome = transcribe(job, task, root, retry_failed=bool(retry_evidence), **options)
                if outcome['status'] == 'waiting_model':
                    model_wait = {key: outcome[key] for key in ('status', 'model_status', 'tell_user')}
                else:
                    result_by_id[job['id']] = {**outcome, 'number': job.get('number', job['input_number'])}
                    if outcome.get('tell_user'):
                        task['tell_user'] = outcome['tell_user']
                event['status'] = outcome['status']
            except Exception as exc:
                task['failures'].append({'id': job['id'], 'input_number': job['input_number'],
                                        'number': job.get('number', job['input_number']), 'error': str(exc)})
                event['status'] = 'failed'
            event.update(finished_at_seconds=round(time.monotonic() - run_started, 3),
                         recognition_seconds=round(time.monotonic() - started, 3))
            task['run_timing']['events'].append(event)
            task['results'] = sorted(result_by_id.values(), key=lambda item: item.get('number', 0))
            save(task, root)

        def consume(outcome):
            number = positions[outcome['input_number'] - 1]
            ready_jobs = [{**job, 'input_number': number, 'input': pending[outcome['input_number'] - 1]}
                          for job in outcome['jobs']]
            jobs.extend(ready_jobs)
            # A profile can return successful jobs and unresolved identities in
            # the same outcome. Persist both before paid work or interruption.
            task['acquisition_gaps'] = [*task['acquisition_gaps'], *[
                {**failure, 'input_number': number, 'number': number} for failure in outcome.get('failures', [])]]
            save(task, root)
            # Persist identities before submitting anything paid. Resume can
            # find a completed/unknown receipt even after a process interruption.
            write_json(run_dir / 'jobs.json', jobs)
            if local_worker is None:
                for job in ready_jobs:
                    recognize_ready(job)

        if local_worker is not None:
            needs_model = bool(pending)
            for job in jobs:
                if needs_model:
                    break
                try:
                    needs_model = cached_result(job, task, root) is None
                except (OSError, ValueError, RuntimeError):
                    # Invalid caches still follow the existing per-media failure path once the model is ready.
                    needs_model = True
            if needs_model:
                waiting = local_asr.waiting_for_model()
                if waiting:
                    task.update(waiting)
                    task['run_timing']['elapsed_seconds'] = round(time.monotonic() - run_started, 3)
                    save(task, root)
                    return task
        if pending:
            try:
                if any(value.startswith('http') for value in pending):
                    discovered = media.acquire_batch(pending, task['scope'], run_dir, root,
                        version=version_for(task), anonymous_only=task.get('anonymous_only', False), on_ready=consume)
                else:
                    discovered = media.discover(pending, task['scope'], run_dir, root, keep_going=True)
                jobs.extend({**job, 'input_number': positions[job.get('input_number', 1) - 1]} for job in discovered)
                for failure in getattr(discovered, 'failures', []):
                    task['failures'].append({**failure, 'input_number': positions[failure['input_number'] - 1],
                                             'number': positions[failure['input_number'] - 1]})
            except Exception as exc:
                task['failures'].append({'stage': 'metadata', 'reason': getattr(exc, 'code', type(exc).__name__),
                    'error': str(exc), 'reports': [relative(root, p) for p in run_dir.glob('metadata-*.json')]})
        ordered = {}
        for job in sorted(jobs, key=lambda x: (x['input_number'], x.get('input_item', 1))):
            ordered.setdefault(job['id'], job)
        jobs = list(ordered.values())
        for gap in prior_gaps:
            group = [job for job in jobs if gap.get('input_number', job['input_number']) == job['input_number']]
            resolved = (any(job['id'] == gap['id'] for job in group) if gap.get('id') else
                        bool(group) and not any(f.get('stage') in acquisition_stages and
                            f.get('input_number') == gap.get('input_number') for f in task['failures']))
            if saved_scope and not gap.get('id'):
                resolved = False
            if not resolved and not any(f.get('id') == gap.get('id') and
                                       f.get('input_number') == gap.get('input_number') for f in task['failures']):
                task['failures'].append(gap)
        task['acquisition_gaps'] = [f for f in task['failures'] if f.get('stage') in acquisition_stages]
        # Reserve a slot for a failed input, including mixed profile/video batches.
        next_number = 1
        for position in range(1, len(task['inputs']) + 1):
            group = [job for job in jobs if job['input_number'] == position]
            for job in group:
                job['number'] = next_number
                next_number += 1
            if not group:
                for failure in task['failures']:
                    if failure.get('input_number') == position:
                        failure['number'] = next_number
                next_number += 1
        if not jobs:
            if not task['failures']:
                raise ValueError("所选范围没有可处理的视频")
        write_json(task_path(task_id, root).parent / "jobs.json", jobs)
        if local_worker is not None:
            local_progress = background.prepare(task, jobs, root)
            task['failures'].extend(local_progress.data['failures'])
            attempted.update(failure['id'] for failure in local_progress.data['failures'])
            local_worker.on_progress = local_progress.phase
            if not _background and local_progress.data['estimated_seconds'] > local_progress.data['threshold_seconds']:
                return background.start(task, root, local_progress, dict(retry_evidence=retry_evidence,
                    anonymous_only=anonymous_only, saved_scope=saved_scope))
        for job in jobs:
            recognize_ready(job)
            if job['id'] in result_by_id:
                result_by_id[job['id']]['number'] = job['number']
            for failure in task['failures']:
                if failure.get('id') == job['id']:
                    failure['number'] = job['number']
        task['results'] = sorted(result_by_id.values(), key=lambda x: x.get('number', 0))
        task['run_timing']['elapsed_seconds'] = round(time.monotonic() - run_started, 3)
        if model_wait:
            task.update(model_wait)
        elif task["failures"]:
            task["status"] = "incomplete" if task['results'] or jobs or any(
                failure.get('stage') == 'cache' for failure in task['failures']) else 'failed'
        else:
            task["status"] = "awaiting_review"
        if engine_for(task) == 'local':
            suspect_summary(task, root)
        save(task, root)
        if task["status"] == "ready_to_publish":
            from .vault import publish
            return publish(task_id, root=root)
    except Exception as exc:
        failure = {"stage": "prepare_or_publish", "error": str(exc)}
        if isinstance(exc, acquisition.AcquisitionError):
            failure["reason"] = exc.code
            failure["reports"] = [relative(root, path) for path in sorted(
                task_path(task_id, root).parent.glob("metadata-*.json"))]
        task.update(status="incomplete" if task.get('results') else "failed", failures=[*task['failures'], failure])
    finally:
        if local_worker is not None:
            local_worker.close()
        if local_progress is not None and task['status'] != 'transcribing_in_background':
            local_progress.finish()
    save(task, root)
    return task


def finish_analysis(task_id: str, file: Path, root: Path = ROOT) -> dict:
    from . import content
    task = load(task_id, root)
    if task["purpose"] != "analyze" or task["status"] != "awaiting_analysis":
        raise ValueError("本任务不在等待分析稿的阶段")
    content.originals(task, root)
    content.checked_content(task, 'review', root)
    gaps = coverage_gaps(task, root)
    source = inside(root, file)
    if source.suffix != ".md" or not source.is_file():
        raise ValueError("分析稿应为项目内已生成的 Markdown 文件")
    if any(source == inside(root, item["markdown"]) for item in task["results"]):
        raise ValueError("分析稿与转写原稿应分别保存")
    if task.get('analysis'):
        task.setdefault('analysis_history', []).append(copy.deepcopy(task['analysis']))
    task.update(status="incomplete" if gaps else "completed",
                analysis={"file": relative(root, source), "sha256": digest(source),
                          "complete": not gaps, "gaps": gaps}, completed=now())
    save(task, root)
    return task


def export_srt(task_id: str, root: Path = ROOT) -> list[str]:
    task = load(task_id, root)
    if not task.get("results"):
        raise ValueError("尚无完整转写结果")

    def stamp(seconds: float) -> str:
        milliseconds = round(seconds * 1000)
        hours, milliseconds = divmod(milliseconds, 3600000)
        minutes, milliseconds = divmod(milliseconds, 60000)
        seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

    paths = []
    for item in task["results"]:
        result_path = inside(root, item["result"])
        result = read_json(result_path)
        if not result["segments"]:
            raise ValueError("已有识别结果没有分句时间，无法生成有效 SRT")
        path = result_path.with_name("transcript.srt")
        text = "\n\n".join(f"{index}\n{stamp(segment['start'])} --> {stamp(segment['end'])}\n{segment['text']}"
                             for index, segment in enumerate(result["segments"], 1)) + "\n"
        if path.exists() and path.read_text(encoding="utf-8") != text:
            raise ValueError("已有字幕被修改，保留现场，请另存后再导出")
        path.write_text(text, encoding="utf-8")
        paths.append(str(path))
    return paths
