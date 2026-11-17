"""Offline search timing/profile receipt; intentionally not a timed unit test."""
from __future__ import annotations

import argparse
import cProfile
import hashlib
import io
import json
from pathlib import Path
import platform
import pstats
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/runtime'))
from douyin_text import personal_vault as kb, search
from douyin_text.common import write_json


def populate(base):
    root, library = base / 'workspace', base / 'vault'
    root.mkdir()
    library.mkdir()
    kb.save({'vault_root': str(library), 'modules': ['work', 'learning']}, root)
    body = ('项目复盘结合 Python 分析换岗选择，记录依据与预期，事后对照实际结果。'
            '用户反馈帮助调整团队协作，并把复盘经验用于下次计划。\n') * 60
    links, source_bytes = [], 0
    for n in range(800):
        task_id = f'dy-bench-{n:04d}'
        folder = root / 'outputs' / task_id / 'v1'
        folder.mkdir(parents=True)
        raw = body + f'\n合成机器原稿 {n:04d}。'
        txt, md, result = (folder / name for name in ('transcript.txt', 'transcript.md', 'result.json'))
        txt.write_text(raw, encoding='utf-8')
        md.write_text('# 合成文稿\n' + raw, encoding='utf-8')
        checksum = hashlib.sha256(txt.read_bytes()).hexdigest()
        write_json(result, {'source': {'title': f'合成会议 {n:04d}'}, 'text': raw,
                            'output_sha256': {'txt': checksum, 'md': hashlib.sha256(md.read_bytes()).hexdigest()}})
        item = {'id': task_id, 'number': 1, 'text': txt.relative_to(root).as_posix(),
                'markdown': md.relative_to(root).as_posix(), 'result': result.relative_to(root).as_posix()}
        task = {'id': task_id, 'results': [item]}
        if n < 400:
            reviewed = root / 'runs' / task_id / 'review.txt'
            reviewed.parent.mkdir(parents=True)
            reviewed.write_text(body + f'\n合成校对稿 {n:04d}。', encoding='utf-8')
            task['review'] = {'files': [{'id': task_id, 'file': reviewed.relative_to(root).as_posix(),
                                        'sha256': hashlib.sha256(reviewed.read_bytes()).hexdigest(),
                                        'source_basis': {'raw_sha256': checksum}}]}
            source_bytes += reviewed.stat().st_size
        write_json(root / 'runs' / task_id / 'task.json', task)
        source_bytes += txt.stat().st_size
        page = library / '工作' / f'项目-{n:04d}.md'
        page.parent.mkdir(exist_ok=True)
        page.write_text(f'# 合成知识页 {n:04d}\n\n' + body, encoding='utf-8')
        links.append(f'[{n}](工作/项目-{n:04d}.md)')
        source_bytes += page.stat().st_size
    (library / 'index.md').write_text('\n'.join(links), encoding='utf-8')
    return root, library, source_bytes


def measure(root, text, **options):
    started = time.perf_counter()
    result = search.query(text, root, **options)
    return {'seconds': round(time.perf_counter() - started, 6), 'engine': result['engine'],
            'total': result['total'], 'index': result['index']}


def main():
    parser = argparse.ArgumentParser(description='用 2,000 份合成文档测量本地搜索，不设置速度断言')
    parser.add_argument('--output', type=Path, required=True, help='日志目录')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root, library, source_bytes = populate(Path(temporary).resolve())
        receipt = {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                   'documents': 2000, 'kinds': {'transcript': 800, 'reviewed': 400, 'knowledge': 800},
                   'source_utf8_bytes': source_bytes,
                   'timing_scope': '包含增量检查与查询；不含造数及 CLI 启动；无变化查询各测 3 次，另跑 cProfile。',
                   'first_index': measure(root, '项目复盘 Python')}
        for n in range(20):
            file = library / '工作' / f'项目-{n:04d}.md'
            file.write_text(file.read_text(encoding='utf-8') + '\n补充：本轮已更新。', encoding='utf-8')
        receipt['incremental'] = measure(root, '项目复盘 Python')
        for name, text, options in [('trigram', '项目复盘 Python', {}), ('scan', '换岗', {}),
                                    ('knowledge', '项目复盘 Python', {'kind': 'knowledge'})]:
            runs = [measure(root, text, **options) for _ in range(3)]
            profile = cProfile.Profile()
            profile.runcall(search.query, text, root, **options)
            profile.dump_stats(str(args.output / (name + '.prof')))
            stream = io.StringIO()
            stats = pstats.Stats(profile, stream=stream).strip_dirs()
            stats.sort_stats('cumulative').print_stats(18)
            stats.sort_stats('tottime').print_stats(12)
            (args.output / (name + '-profile.txt')).write_text(stream.getvalue(), encoding='utf-8')
            receipt[name] = {'runs': runs, 'median_seconds': round(statistics.median(row['seconds'] for row in runs), 6),
                             'profile_seconds': round(stats.total_tt, 6)}
            print(json.dumps({name: receipt[name]['median_seconds'], 'profile_seconds': stats.total_tt}), flush=True)
        receipt['cache_bytes'] = (root / '.local/search.sqlite').stat().st_size
        write_json(args.output / 'benchmark.json', receipt)
        print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
