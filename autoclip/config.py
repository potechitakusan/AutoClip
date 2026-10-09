from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def error_message(exc):
    value = str(exc).splitlines()[0] if str(exc) else ''
    if isinstance(exc, (ValueError, RuntimeError)) and any('\u3040' <= c <= '\u9fff' for c in value):
        return value
    return '処理に失敗しました。詳細は作業ログを参照してください。'


def natural_key(path):
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r'(\d+)', str(path))]


def job_directory(name, root=ROOT):
    if not re.fullmatch(r'[\w-]+', name) or name in ('.', '..'):
        raise ValueError('作業名には文字・数字・ハイフン・下線を使ってください。')
    path = (root / 'work' / name).resolve()
    if not path.is_relative_to((root / 'work').resolve()):
        raise ValueError('作業先がworkの外です。')
    return path


def save_job(args, root=ROOT):
    name = args.job or 'v2-' + Path(args.images or 'job').name
    if name == 'v2-setup':
        raise ValueError('v2-setup は校正専用の作業名です。別の作業名を指定してください。')
    work = job_directory(name, root)
    path = work / 'job.json'
    old = read_json(path) if path.exists() else {}
    job = dict(old)
    for key in ('images', 'cmc', 'upscale', 'frames_only', 'review', 'python', 'model', 'long_edge', 'tolerance', 'fit'):
        value = getattr(args, key, None)
        if value is not None:
            job[key] = str(Path(value).resolve()) if key in ('images', 'cmc', 'python', 'model') else value
    if not job.get('images') or not job.get('cmc'):
        raise ValueError('画像フォルダーと --cmc を指定してください。')
    defaults = {'frames_only': False, 'upscale': 'none', 'fit': 'cover'}
    for key in ('images', 'cmc', 'frames_only', 'upscale', 'fit'):
        if old and job.get(key, defaults.get(key)) != old.get(key, defaults.get(key)):
            raise ValueError('対象・投入内容の変更には新しい作業名を指定してください。')
    job.update(name=name, work=str(work))
    job.setdefault('upscale', 'none')
    job.setdefault('frames_only', False)
    job.setdefault('review', False)
    job.setdefault('long_edge', 'auto')
    job.setdefault('tolerance', 'medium')
    job.setdefault('fit', 'cover')
    write_json(path, job)
    return job
