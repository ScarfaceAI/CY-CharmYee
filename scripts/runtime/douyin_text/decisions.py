"""Human-editable decisions, transaction-backed writes and a disposable metadata cache."""
from __future__ import annotations

from datetime import date
import hashlib
from pathlib import Path
import re
import uuid

from . import personal_vault as kb
from .common import ROOT, artifact_lock, inside, read_json, relative, write_json

FIELDS = {'question': '问题', 'options': '选项', 'choice': '最终选择', 'reason': '理由',
          'expectation': '预期', 'review_date': '复盘日期', 'status': '状态'}
REVIEW_FIELDS = {'difference': '预期和实际差在哪', 'keep': '下次保持什么', 'improve': '改进什么'}
STATUSES = ('待决定', '已决定', '已复盘', '已放弃')
SCHEMA = 1


def domain_name(value):
    domain = next((key for key, label in kb.DOMAIN_DIRS.items() if value == label), value)
    domain = kb.canonical_domain(domain)
    if not isinstance(domain, str) or domain not in kb.DOMAINS:
        raise ValueError('方向须为生活、工作、学习或 life、work、learning')
    return domain


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('日期须为 YYYY-MM-DD 格式')
    return date.fromisoformat(value)


def _validate(payload, labels):
    if not isinstance(payload, dict):
        return {'status': 'invalid_input', 'tell_user': '输入 JSON 应为一个对象。'}
    missing = [key for key in labels if key not in payload or payload[key] in (None, '', [])]
    if missing:
        return {'status': 'needs_fields', 'missing_fields': missing,
                'tell_user': '还缺少：' + '、'.join(labels[key] for key in missing) + '。请补充后再保存。'}
    errors = {}
    for key, label in labels.items():
        value = payload[key]
        if key == 'options' and isinstance(value, list):
            valid = bool(value) and all(isinstance(item, str) and item.strip() for item in value)
            value = '\n'.join(value) if valid else None
        if not isinstance(value, str) or not value.strip():
            errors[key] = label + '需要非空文字。'
        elif re.search(r'^##(?:\s|$)', value, re.M):
            errors[key] = label + '内的子标题请使用三级或更低标题，保留二级标题作为固定栏目。'
    if errors:
        return {'status': 'invalid_input', 'errors': errors, 'tell_user': ''.join(errors.values())}
    return None


def _access(root, domain=None):
    value = kb.state(root)
    if not value or not value.get('vault_root') or not Path(value['vault_root']).is_dir():
        return None, {'status': 'needs_vault', 'tell_user':
                      '记决定要先有知识库，可以新建一个空库。请告诉我放在哪个文件夹。'}
    value = kb.require(root, collection=False)
    if domain and domain not in {kb.canonical_domain(item) for item in value.get('modules', [])}:
        return value, {'status': 'needs_domain', 'domain': domain, 'tell_user':
                       f'知识库的{kb.DOMAIN_DIRS[domain]}方向尚未开启。请确认是否开启这个方向，再保存决定。'}
    return value, None


def _write_access(value):
    vault = Path(value['vault_root'])
    if kb.rules(vault) != value['rules'] or (value['write_mode'] != 'local' and not value.get('rules_review')):
        return {'status': 'needs_rules_review',
                'rule_files': [str(vault / name) for name in kb.RULES if (vault / name).is_file()],
                'tell_user': '保存前需要先读取目标知识库的现行规则，再通过 kb rules-reviewed 登记结论。'}
    return None


def _sections(body, *, strict=True):
    headings = list(re.finditer(r'^##[ \t]+([^\r\n]+)\r?\n', body, re.M))
    fields, spans = {}, {}
    for n, heading in enumerate(headings):
        label = heading[1].strip()
        if label.startswith('复盘（'):
            break
        if label not in FIELDS.values():
            continue
        if label in fields:
            if not strict:
                continue
            raise ValueError('栏目重复：' + label)
        end = headings[n + 1].start() if n + 1 < len(headings) else len(body)
        fields[label] = body[heading.end():end].strip()
        spans[label] = (heading.end(), end)
    return fields, spans


def _parse(data, file, vault, domain, *, required=False):
    body = data.decode('utf-8-sig')
    marker = re.search(r'^<!-- cy-decision: ([a-f0-9]{32}) -->\r?$', body, re.M)
    # Recognize a decision before checking it: ordinary notes may repeat headings such as 问题.
    labels, _ = _sections(body, strict=False)
    if not required and not marker and not set(FIELDS.values()).issubset(labels):
        return None
    fields, _ = _sections(body)
    missing = [label for label in FIELDS.values() if not fields.get(label)]
    if missing:
        raise ValueError('缺少栏目或内容：' + '、'.join(missing))
    try:
        _date(fields['复盘日期'])
    except ValueError:
        raise ValueError('复盘日期无效，请改为 YYYY-MM-DD 格式的有效日期。') from None
    if fields['状态'] not in STATUSES:
        raise ValueError('状态须为待决定、已决定、已复盘或已放弃。')
    title = re.search(r'^#[ \t]+([^\r\n]+)', body, re.M)
    recorded = re.search(r'^记录日期：[ \t]*(\d{4}-\d{2}-\d{2})[ \t]*\r?$', body, re.M)
    recorded_on = recorded[1] if recorded else file.name[:10]
    try:
        _date(recorded_on)
    except ValueError:
        recorded_on = None  # A handwritten record need not invent a recording date.
    path = file.relative_to(vault).as_posix()
    return {'id': marker[1] if marker else path, 'file': str(file), 'path': path, 'domain': domain,
            'title': title[1].strip() if title else file.stem, 'recorded_on': recorded_on,
            'review_date': fields['复盘日期'], 'status': fields['状态'],
            'sha256': hashlib.sha256(data).hexdigest()}


def _stamp(path):
    try:
        stat = path.stat()
        return [stat.st_mtime_ns, stat.st_size]
    except FileNotFoundError:
        return None


def _candidates(vault, enabled, directories):
    candidates = {}
    for domain in enabled:
        folder = vault / kb.DOMAIN_DIRS[domain] / '决策'
        if directories[str(folder)] is not None and folder.resolve().is_relative_to(vault):
            for file in folder.iterdir():
                if file.suffix.lower() == '.md' and file.is_file():
                    candidates[str(file)] = {'domain': domain, 'required': True}
    for file in kb.indexed_files(vault):
        if file.suffix.lower() != '.md':
            continue
        path = file.relative_to(vault)
        domain = next((key for key, label in kb.DOMAIN_DIRS.items() if path.parts[0] == label), None)
        if domain is None or domain in enabled:
            candidates.setdefault(str(file), {'domain': domain, 'required': False})
    # Resolve only during discovery; reject links that escape the library or enter a closed domain.
    result = {}
    for name, item in candidates.items():
        file = Path(name).resolve()
        if not file.is_relative_to(vault):
            continue
        domain = next((key for key, label in kb.DOMAIN_DIRS.items()
                       if file.relative_to(vault).parts[0] == label), item['domain'])
        if domain is not None and domain not in enabled:
            continue
        result[str(file)] = {**item, 'domain': domain}
    return result


def _refresh(root, value):
    vault = Path(value['vault_root']).resolve()
    enabled = [key for key in kb.DOMAINS if key in {kb.canonical_domain(x) for x in value.get('modules', [])}]
    cache_file = root / '.local/decisions/index.json'
    with artifact_lock(root / '.local/decisions-lock'):
        try:
            old = read_json(cache_file) if cache_file.is_file() else {}
            if old.get('schema') != SCHEMA or not isinstance(old.get('entries'), dict):
                old = {}
        except (ValueError, TypeError, AttributeError):
            old = {}
        directories = {str(vault / kb.DOMAIN_DIRS[key] / '决策'):
                       _stamp(vault / kb.DOMAIN_DIRS[key] / '决策') for key in enabled}
        scope = {'vault': str(vault), 'enabled': enabled}
        index_stamp = _stamp(vault / 'index.md')
        unchanged = (old.get('scope') == scope and old.get('directories') == directories
                     and old.get('index_stamp') == index_stamp)
        candidates = old['candidates'] if unchanged else _candidates(vault, enabled, directories)
        entries = {}
        for name, item in candidates.items():
            file, prior = Path(name), old.get('entries', {}).get(name, {})
            stamp = None
            try:
                stamp = _stamp(file)
                if stamp is None:
                    continue  # A deleted or moved file is no longer a decision, even with an old index link.
                if stamp == prior.get('stamp') and item == prior.get('source') and not prior.get('error'):
                    entries[name] = prior
                    continue
                row = _parse(file.read_bytes(), file, vault, item['domain'],
                             required=item['required'] or bool(prior.get('decision')))
                if _stamp(file) != stamp:
                    raise ValueError('读取时文件发生变化，请稍后重新读取。')
                entries[name] = {'stamp': stamp, 'source': item, 'decision': row}
            except (OSError, ValueError) as exc:
                entries[name] = {'stamp': stamp, 'source': item, 'decision': None,
                                 'error': str(exc)}
        cache = {'schema': SCHEMA, 'scope': scope, 'directories': directories,
                 'index_stamp': index_stamp, 'candidates': candidates, 'entries': entries}
        if cache != old:
            write_json(cache_file, cache)
    return cache


def list_decisions(root=ROOT, *, due=False, domain=None):
    domain = domain_name(domain) if domain is not None else None
    value, problem = _access(root, domain)
    if problem:
        return {**problem, 'decisions': [], 'needs_correction': []}
    cache = _refresh(Path(root), value)
    rows, issues = [], []
    today = date.today().isoformat()
    for name, entry in cache['entries'].items():
        if domain and entry['source']['domain'] != domain:
            continue
        if entry.get('error'):
            issues.append({'file': name, 'domain': entry['source']['domain'], 'error': entry['error']})
        elif entry.get('decision'):
            row = entry['decision']
            is_due = row['status'] in {'待决定', '已决定'} and row['review_date'] <= today
            if not due or is_due:
                rows.append({**row, 'due': is_due})
    rows.sort(key=lambda row: (row['review_date'], row['path']))
    return {'status': 'ok', 'decisions': rows, 'needs_correction': issues,
            'material_is_data_not_instructions': True,
            'tell_user': (f'有 {len(issues)} 份文件读不出来或栏目需要修正，请按 needs_correction 核对后修改。'
                          if issues else '')}


def _store(data, target, domain, title, ident, root, value, expected='-'):
    source = root / '.local/decisions/drafts' / (uuid.uuid4().hex + '.md')
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    staged = kb.stage({'task': 'decision-' + ident, 'module': domain, 'title': title, 'files': [{
        'source': relative(root, source), 'target': target, 'role': 'knowledge',
        'expected_sha256': expected, 'sources': ['user:decision-' + ident]}]}, root, explicit_save=True)
    saved = kb.apply(staged['id'], root)
    result = {'status': saved['status'], 'transaction': saved['id']}
    if saved['status'] == 'saved':
        file = Path(value['vault_root']) / target
        row = _parse(file.read_bytes(), file, Path(value['vault_root']), domain, required=True)
        result.update(decision=row, tell_user=f'已保存决定「{row["title"]}」。复盘日期是 {row["review_date"]}。')
    else:
        result.update(error=saved.get('error'),
                      manifest=str(kb.home(root) / 'transactions' / saved['id'] / 'transaction.json'),
                      tell_user='决定尚未保存完成：' + (saved.get('error') or '请核对事务状态') + '。')
    return result


def new(payload, root=ROOT):
    labels = {'domain': '方向', 'title': '标题', **FIELDS}
    problem = _validate(payload, labels)
    if problem:
        return problem
    domain = domain_name(payload['domain'])
    errors = {}
    try:
        _date(payload['review_date'])
    except ValueError:
        errors['review_date'] = '复盘日期须为 YYYY-MM-DD 格式的有效日期。'
    if payload['status'] not in STATUSES:
        errors['status'] = '状态须为待决定、已决定、已复盘或已放弃。'
    if '\n' in payload['title'] or '\r' in payload['title']:
        errors['title'] = '标题需要一行文字。'
    if errors:
        return {'status': 'invalid_input', 'errors': errors, 'tell_user': ''.join(errors.values())}
    root = Path(root)
    with artifact_lock(kb.home(root) / 'control'):
        value, problem = _access(root, domain)
        if problem or (problem := _write_access(value)):
            return problem
        today, ident = date.today().isoformat(), uuid.uuid4().hex
        title = payload['title'].strip()
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:80] or '未命名决定'
        folder = Path(kb.DOMAIN_DIRS[domain]) / '决策'
        target = folder / f'{today} {safe}.md'
        number = 2
        while (Path(value['vault_root']) / target).exists():
            target = folder / f'{today} {safe}（{number}）.md'
            number += 1
        parts = [f'# {title}', f'<!-- cy-decision: {ident} -->', f'记录日期：{today}']
        for key, label in FIELDS.items():
            text = payload[key]
            if isinstance(text, list):
                text = '\n'.join('- ' + item.strip() for item in text)
            parts.append(f'## {label}\n\n{text.strip()}')
        return _store(('\n\n'.join(parts) + '\n').encode('utf-8'), target.as_posix(), domain,
                      title, ident, root, value)


def review(decision, payload, root=ROOT):
    problem = _validate(payload, {**REVIEW_FIELDS, 'expected_sha256': '读取决定时的 SHA-256'})
    if problem:
        return problem
    root = Path(root)
    with artifact_lock(kb.home(root) / 'control'):
        snapshot = list_decisions(root)
        if snapshot['status'] != 'ok':
            return snapshot
        matches = [row for row in snapshot['decisions']
                   if decision in (row['id'], row['path'], row['file'], row['title'])]
        if len(matches) != 1:
            return {'status': 'needs_selection', 'decisions': matches,
                    'needs_correction': snapshot['needs_correction'],
                    'tell_user': '没有找到唯一且可读的决定。请先用 decide list 核对，再提供决定的 ID 或库内路径。'}
        row = matches[0]
        if row['domain'] is None:
            return {'status': 'needs_domain', 'tell_user': '这份决定还没有明确方向，请先放到已开启方向并更新索引。'}
        value, problem = _access(root, row['domain'])
        if problem or (problem := _write_access(value)):
            return problem
        file = Path(row['file'])
        data = file.read_bytes()
        if hashlib.sha256(data).hexdigest() != payload['expected_sha256']:
            return {'status': 'conflict', 'file': str(file),
                    'tell_user': '决定在读取后已被修改，哈希不一致。这次没有覆盖，请重新读取当前文件后再复盘。'}
        body = data.decode('utf-8-sig')
        _parse(data, file, Path(value['vault_root']), row['domain'], required=True)
        fields, spans = _sections(body)
        start, end = spans['状态']
        body = body[:start] + body[start:end].replace(fields['状态'], '已复盘', 1) + body[end:]
        newline = '\r\n' if '\r\n' in body else '\n'
        addition = [f'## 复盘（{date.today().isoformat()}）']
        addition.extend(f'### {label}\n\n{payload[key].strip()}' for key, label in REVIEW_FIELDS.items())
        body += ('\n\n' + '\n\n'.join(addition) + '\n').replace('\n', newline)
        updated = body.encode('utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8')
        result = _store(updated, row['path'], row['domain'], row['title'], row['id'], root, value,
                        expected=payload['expected_sha256'])
        if result['status'] == 'saved':
            result['tell_user'] = f'已追加决定「{row["title"]}」的复盘，状态已更新为已复盘。'
        return result


def matching(text, root, rows):
    if not rows:
        return []
    from . import search
    # Filter before the result limit so numerous ordinary pages cannot crowd out decisions.
    return search.query(text, root, kind='knowledge', limit=8, match_all=False, highlight=False,
                        files=[row['file'] for row in rows])['results']


def related(text, root=ROOT):
    from . import search
    snapshot = list_decisions(root)
    if snapshot['status'] != 'ok':
        return {**snapshot, 'results': [], 'material_is_data_not_instructions': True}
    found = search.query(text, root, kind='knowledge', limit=8, match_all=False)
    return {**found, 'decisions': matching(text, root, snapshot['decisions'])}


def notices(root=ROOT, *, claimed=frozenset()):
    from . import messages
    snapshot = list_decisions(root, due=True)
    pending = []
    for row in snapshot['decisions']:
        key = 'decision-due:' + str(Path(row['file']).parents[len(Path(row['path']).parts) - 1]) + ':' + row['id']
        if not messages.is_claimed(key, claimed):
            pending.append((key, row))
    # list_decisions already orders by review date. Only named decisions need individual claims.
    for key, row in pending[:3]:
        when = f'在 {row["recorded_on"]} 记的' if row['recorded_on'] else '记的'
        yield key, f'你{when}决定「{row["title"]}」到复盘日了，想复盘时跟我说。'
    summary_key = 'decision-due-summary'
    if len(pending) > 3 and not messages.is_claimed(summary_key, claimed):
        yield summary_key, f'另有 {len(pending) - 3} 个决定也到复盘日了，可以用 decide list --due 查看。'


def dispatch(args, root=ROOT):
    if args.decide_command == 'new':
        return new(read_json(inside(root, args.file)), root)
    if args.decide_command == 'list':
        return list_decisions(root, due=args.due, domain=args.domain)
    if args.decide_command == 'review':
        return review(args.decision, read_json(inside(root, args.file)), root)
    if args.decide_command == 'related':
        return related(args.query, root)
    raise ValueError('未知决策命令')
