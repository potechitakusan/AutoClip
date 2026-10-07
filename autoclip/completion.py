"""Shared calibration messages, HTML receipt, and success-only window handoff."""
from __future__ import annotations

from html import escape
import os
from pathlib import Path
import subprocess

from .common import CODE_ROOT, ROOT, now, read_json, relative, resolve, work_directory, write_json
from .window_handoff import Handoff


def messages():
    return read_json(CODE_ROOT/'config/guidance.json')


def phrase(key, **values):
    return messages()['messages'][key].format(**values)


def notify(title, message):
    # A short, non-modal tray notification. No terminal window or input dialog.
    if os.name != 'nt':
        return False
    try:
        subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
                          '-File',str(CODE_ROOT/'scripts/show-notification.ps1'),
                          '-Title',title,'-Message',message],
                         creationflags=subprocess.CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def write_receipt(config, record, name='calibration'):
    from .guidance import render_page
    work = work_directory(config)
    guide = (CODE_ROOT/record['guide']).as_uri()
    recovery = (CODE_ROOT/record['recovery']).as_uri()
    record['updated_at'] = now()
    write_json(work/f'{name}-status.json',record)
    body = f'<p>対象：<code>{escape(config["project"])}</code></p><p>更新日時：{escape(record["updated_at"])}</p>'
    if record.get('handoff'):
        body += '<p>'+escape(record['return_message'])+'</p>'
    body += f'<p><a href="{guide}">作業手順を開く</a> / <a href="{recovery}">中断時の手順を開く</a></p>'
    render_page(work/f'{name}-status.html',title=f'{record.get("stage","キャリブレーション")}の実行結果',
                summary='最後に実行した処理の状態です。',
                status=record['state'],status_title=record['message'],status_detail='',
                body=body,display='',next_action=record.get('next_action','停止理由と復旧手順を確認してください。'))


def run_calibration(config, operation, key='calibration'):
    info = messages()[key]
    stage = info['stage']
    record = {'state':'running','stage':stage,'guide':info['guide'],'recovery':info['recovery'],
              'message':phrase('running',stage=stage)}
    try:
        handoff = Handoff(CODE_ROOT.name)
        handoff_error = None
    except OSError as exc:
        handoff, handoff_error = None,str(exc)
    write_receipt(config,record,key)
    print(record['message'],flush=True)
    try:
        result = operation()
    except Exception as exc:
        record.update(state='interrupted',message=phrase('interrupted',stage=stage,reason=str(exc)))
        write_receipt(config,record,key)
        print(record['message'],flush=True)
        # No minimization on failure; retain the canvas/dialog for recovery.
        raise
    next_action = info['next_action'].format(project=str(resolve(config['project'])))
    record.update(state='complete',next_action=next_action,result=relative(result),
                  message=phrase('complete',stage=stage,next_action=next_action))
    # Persist success before attempting UI feedback. A notification failure must
    # never turn a verified template/copy into a failed calibration.
    write_receipt(config,record,key)
    try:
        ui = handoff.finish() if handoff else {'clip_minimized':False,'codex_foreground':False,'reason':handoff_error}
    except Exception as exc:
        ui = {'clip_minimized':False,'codex_foreground':False,'reason':str(exc)}
    record['handoff'] = ui
    record['return_message'] = phrase('returned' if ui['clip_minimized'] and ui['codex_foreground'] else 'return_failed')
    record['notification_requested'] = notify(stage+'完了',record['message']+' '+record['return_message'])
    write_receipt(config,record,key)
    print(record['message']+' '+record['return_message'],flush=True)
    print('HTML: '+str(work_directory(config)/f'{key}-status.html'),flush=True)
    return result


def run_stage_with_handoff(config, key, operation):
    """Run a long GUI stage; on success minimize CLIP STUDIO and return to the agent's window."""
    return run_calibration(config, operation, key)
