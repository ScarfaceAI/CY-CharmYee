"""Workspace-only acquisition probes and short-lived route ordering."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import time
from urllib import error, request
from urllib.parse import urlsplit

from . import acquisition, douyin_browser
from .common import ROOT, now, read_json, write_json

PROBE_BYTES = 1024 * 1024
HISTORY_LIMIT = 4
METHODS = {'public_share': '公开分享页', 'anonymous_browser': '匿名浏览器',
           'yt_dlp': 'yt-dlp', 'existing_profile': '已登录浏览器'}
ROUTE_METHODS = {'public_share': ('public_share',),
                 'anonymous_browser': ('anonymous_browser', 'yt_dlp'),
                 'existing_profile': ('existing_profile',)}
LAST_RESORT = 'existing_profile'
METADATA_REASONS = {'metadata_unavailable', 'media_unavailable', 'navigation_timeout',
                    'http_error', 'browser_error', 'verification_required', 'login_required'}


def sample_url(value: str) -> str:
    """Validate locally; ownership and public visibility are supplied by the user."""
    value = value.strip()
    parts = urlsplit(value)
    host = (parts.hostname or '').lower()
    if (parts.scheme not in {'http', 'https'} or parts.username or parts.password or
            not any(host == domain or host.endswith('.' + domain)
                    for domain in ('douyin.com', 'iesdouyin.com'))):
        raise ValueError('自检样本须为你自己的公开抖音作品链接')
    if host == 'v.douyin.com' and parts.path.strip('/'):
        return acquisition.clean_url(value)
    try:
        return 'https://www.douyin.com/video/' + acquisition.video_id(value)
    except ValueError:
        raise ValueError('自检样本须为单条作品链接，不接受作者主页或本地文件') from None


def samples(root: Path, additions: list[str], *, clear=False) -> list[str]:
    path = root / '.local/selftest/samples.json'
    if clear:
        write_json(path, {'samples': []})
        return []
    saved = read_json(path)['samples'] if path.is_file() else []
    values = list(dict.fromkeys([*saved, *(sample_url(value) for value in additions)]))
    if len(values) > 2:
        raise ValueError('自检最多保存 2 条样本；换样本请先用 selftest --clear-samples 清空')
    if additions:
        write_json(path, {'samples': values})
    return values


def _read_results(root: Path) -> dict:
    try:
        value = read_json(root / '.local/selftest/results.json')
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}  # Advisory probe state must not prevent normal acquisition.


def route_order(root: Path, routes: list[str], *, at: datetime | None = None) -> tuple[list[str], dict | None]:
    latest = _read_results(root).get('latest') or {}
    at = at or datetime.now(timezone.utc)
    failed = set()
    for row in latest.get('results', []) if isinstance(latest, dict) else []:
        if not isinstance(row, dict) or row.get('status') != 'failed':
            continue
        try:
            age = at - datetime.fromisoformat(row['checked_at'])
        except (KeyError, TypeError, ValueError):
            continue
        if timedelta(0) <= age < timedelta(hours=24):
            failed.add(row.get('method'))
    # Using the account is a deliberate last resort, so only anonymous routes are reordered.
    anonymous = [route for route in routes if route != LAST_RESORT]
    delayed = [route for route in anonymous if all(method in failed for method in ROUTE_METHODS[route])]
    ordered = sorted(anonymous, key=lambda route: route in delayed) + [route for route in routes if route == LAST_RESORT]
    if ordered == routes:
        return ordered, None
    causes = [method for method in METHODS if method in failed and
              any(method in ROUTE_METHODS[route] for route in delayed)]
    labels = '、'.join(METHODS[method] for method in causes)
    return ordered, {'reason': 'selftest_recent_failure',
                     'message': f'最近一轮自检中，以下方式在 24 小时内获取失败：{labels}；对应路线后移，仍会尝试。',
                     'default_order': routes, 'order': ordered,
                     'deprioritized_routes': delayed, 'failed_methods': causes}


def download_probe(url: str) -> dict:
    """Read at most one MiB even when the server ignores Range; never save media."""
    count = 0
    reason = 'ok'
    try:
        req = request.Request(url, headers={'User-Agent': douyin_browser.USER_AGENT,
            'Referer': 'https://www.douyin.com/', 'Range': f'bytes=0-{PROBE_BYTES - 1}',
            'Accept-Encoding': 'identity'})
        with request.urlopen(req, timeout=20) as response:
            content_type = response.headers.get('Content-Type', '').split(';')[0].lower()
            if content_type.startswith('text/') or content_type == 'application/json':
                reason = 'invalid_media_response'
            else:
                while count < PROBE_BYTES:
                    block = response.read(min(65536, PROBE_BYTES - count))
                    if not block:
                        break
                    count += len(block)
                expected = response.headers.get('Content-Length')
                if not count:
                    reason = 'empty_media'
                elif expected and count < min(int(expected), PROBE_BYTES):
                    reason = 'incomplete_download'
    except error.HTTPError:
        reason = 'http_error'
    except (TimeoutError, error.URLError) as exc:
        reason = 'navigation_timeout' if isinstance(getattr(exc, 'reason', exc), TimeoutError) else 'http_error'
    except Exception:
        reason = 'download_error'
    result = {'status': 'passed' if reason == 'ok' else 'failed', 'reason': reason, 'bytes_read': count}
    if reason != 'ok':
        result['stage'] = 'download'
    return result


def probe(method: str, url: str, profile: Path) -> dict:
    started = time.monotonic()
    result = {'status': 'failed', 'reason': 'metadata_unavailable', 'stage': 'metadata', 'bytes_read': 0}
    try:
        if method == 'existing_profile' and not profile.is_dir():
            result = {'status': 'skipped', 'reason': 'profile_missing', 'bytes_read': 0}
        else:
            # Resolve short links independently, so one route cannot suppress another probe.
            resolved = douyin_browser.resolve_url(url) if urlsplit(url).hostname == 'v.douyin.com' else url
            wanted = 'douyin-' + acquisition.video_id(resolved)
            if method == 'public_share':
                jobs, failures = acquisition.public_metadata([resolved])
            elif method == 'yt_dlp':
                jobs, failures = [douyin_browser._yt_dlp_video(resolved)], []
            else:
                jobs, failures = douyin_browser.browser_videos(
                    [resolved], profile if method == 'existing_profile' else None)
            job = next((job for job in jobs if job.get('id') == wanted and job.get('media')), None)
            if job:
                result = download_probe(job['media'])
            elif failures:
                reason = failures[0].get('reason')
                result['reason'] = reason if reason in METADATA_REASONS else 'metadata_unavailable'
    except error.HTTPError:
        result['reason'] = 'http_error'
    except (TimeoutError, error.URLError) as exc:
        result['reason'] = 'navigation_timeout' if isinstance(getattr(exc, 'reason', exc), TimeoutError) else 'http_error'
    except Exception:
        # Do not retain exception text, metadata, cookies, account details or media URLs.
        result['reason'] = 'browser_error' if method in {'anonymous_browser', 'existing_profile'} else 'metadata_unavailable'
    return {'method': method, **result, 'elapsed_seconds': round(time.monotonic() - started, 3), 'checked_at': now()}


def run(additions: list[str] | None = None, *, clear=False, root: Path = ROOT) -> dict:
    values = samples(root, additions or [], clear=clear)
    if not values:
        prefix = '已清空自检样本；' if clear else ''
        return {'status': 'samples_cleared' if clear else 'needs_samples', 'sample_count': 0,
                'tell_user': prefix + '请提供 1～2 条你自己的公开抖音作品链接，用 selftest --sample <链接> 保存并探测。'}
    profile = root / '.local/douyin-profile'
    rows = [{'sample': number, **probe(method, url, profile)}
            for number, url in enumerate(values, 1) for method in METHODS]
    latest = {'checked_at': now(), 'sample_count': len(values), 'results': rows}
    previous = _read_results(root)
    history = ([previous['latest']] if previous.get('latest') else []) + previous.get('history', [])
    write_json(root / '.local/selftest/results.json', {'latest': latest, 'history': history[:HISTORY_LIMIT]})
    failed = [method for method in METHODS if any(row['method'] == method and row['status'] == 'failed' for row in rows)]
    skipped = [method for method in METHODS if all(row['status'] == 'skipped' for row in rows if row['method'] == method)]
    available = len(METHODS) - len(failed) - len(skipped)
    # Samples no anonymous method could fetch; only these need the manual fallback.
    blocked = [number for number in range(1, len(values) + 1)
               if not any(row['sample'] == number and row['method'] != LAST_RESORT and row['status'] == 'passed'
                          for row in rows)]
    routes = ['anonymous_browser', 'public_share'] + (['existing_profile'] if profile.is_dir() else [])
    ordered, adjustment = route_order(root, routes)
    message = f'4 种方式里 {available} 种可用'
    if failed:
        message += '；获取失败：' + '、'.join(METHODS[method] for method in failed)
    if skipped:
        message += '；已登录浏览器无本机登录资料，未探测'
    if adjustment:
        message += '；' + '、'.join(METHODS[route] for route in adjustment['deprioritized_routes']) + '路线已后移，24 小时后恢复默认顺序'
    elif failed:
        message += '；路线顺序未变，后续获取仍会逐条尝试'
    result = {'status': 'incomplete' if failed else 'passed', 'sample_count': len(values),
              'available_methods': available, 'anonymous_unavailable_samples': blocked,
              'results_file': '.local/selftest/results.json',
              'results': rows, 'route_order': ordered, 'tell_user': message + '。'}
    if adjustment:
        result['route_adjustment'] = adjustment
    return result
