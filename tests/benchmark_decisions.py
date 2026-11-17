"""Report unchanged decision-reminder timing and cProfile; no speed assertions."""
from __future__ import annotations

import argparse
import cProfile
from datetime import date
import io
import json
import os
from pathlib import Path
import pstats
import statistics
import tempfile
from time import perf_counter
from unittest.mock import patch

from support import SKILL_ROOT
from douyin_text import decisions, messages, personal_vault as kb
from douyin_text.common import write_json


def benchmark(label):
    output = SKILL_ROOT / '.scratch/m2-decide'
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='m2-reminders-') as temporary:
        parent = Path(temporary)
        root, vault = parent / 'workspace', parent / 'vault'
        root.mkdir()
        env = {'CY_STATE_DIR': str(parent / 'state'), 'CY_MODEL_DIR': str(parent / 'models'),
               'CY_SESSION_ID': 'decision-reminder-benchmark', 'CY_UPDATE_CHECK': '0'}
        with patch.dict(os.environ, env):
            kb.enable(root, vault=vault, create=True, scope='workspace', evidence='合成提醒性能测试')
            folder = vault / '工作/决策'
            folder.mkdir()
            today, links = date.today().isoformat(), []
            for number in range(500):
                title, ident = f'合成决定 {number + 1:03d}', f'{number + 1:032x}'
                file = folder / f'{today} {title}.md'
                fields = {'问题': '是否学习 Python？', '选项': '- 学习\n- 暂缓', '最终选择': '学习',
                          '理由': '工作需要', '预期': '完成练习', '复盘日期': today, '状态': '已决定'}
                body = [f'# {title}', f'<!-- cy-decision: {ident} -->', f'记录日期：{today}']
                body += [f'## {key}\n\n{value}' for key, value in fields.items()]
                file.write_bytes(('\n\n'.join(body) + '\n').encode('utf-8'))
                links.append(f'[{title}](<{file.relative_to(vault).as_posix()}>)')
                # Seed the existing once format: every decision was named earlier in this session.
                messages.once('decision-due:' + str(vault.resolve()) + ':' + ident)
            with (vault / 'index.md').open('a', encoding='utf-8') as index:
                index.write('\n' + '\n'.join(links) + '\n')
            decisions.list_decisions(root)  # Warm discovery and metadata, outside both measurements.
            cache = root / '.local/decisions/index.json'
            stamp = cache.stat().st_mtime_ns

            def run():
                return messages.tell_user({'status': 'ok', 'tell_user': '合成任务完成。'},
                                          command='quick', root=root)

            samples = []
            for _ in range(5):
                started = perf_counter()
                result = run()
                samples.append(round((perf_counter() - started) * 1000, 3))
            profile = cProfile.Profile()
            profile.runcall(run)  # Separate run: profiler overhead is not included in wall times.
            profile.dump_stats(str(output / f'reminders-{label}.prof'))
            stream = io.StringIO()
            stats = pstats.Stats(profile, stream=stream).sort_stats('cumulative')
            stats.print_stats(12)
            stats.sort_stats('tottime').print_stats(8)
            (output / f'reminders-{label}-profile.txt').write_text(stream.getvalue(), encoding='utf-8')
            hotspots = [{'file': key[0], 'line': key[1], 'function': key[2], 'calls': value[1],
                         'self_ms': round(value[2] * 1000, 3), 'cumulative_ms': round(value[3] * 1000, 3)}
                        for key, value in sorted(stats.stats.items(), key=lambda item: -item[1][3])[:12]]
            report = {'label': label, 'synthetic_decisions': 500, 'all_due_and_claimed': True,
                      'operation': 'messages.tell_user：未变化检查及同会话提醒去重',
                      'wall_ms': samples, 'median_ms': statistics.median(samples),
                      'profile_total_ms': round(stats.total_tt * 1000, 3), 'cprofile_top': hotspots,
                      'once_calls_in_profile': sum(value[1] for key, value in stats.stats.items()
                                                   if key[0].endswith('messages.py') and key[2] == 'once'),
                      'reminders': result['tell_user'].count('到复盘日了'),
                      'cache_mtime_unchanged': stamp == cache.stat().st_mtime_ns,
                      'search_cache_created': (root / '.local/search.sqlite').exists()}
            write_json(output / f'reminders-{label}.json', report)
            print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='合成决定的提醒性能对照，不访问网络或用户知识库')
    parser.add_argument('--label', choices=('before', 'after'), required=True)
    benchmark(parser.parse_args().label)
