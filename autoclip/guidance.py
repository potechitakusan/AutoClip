"""Build user-facing HTML from one template and shared Japanese wording."""
from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
from string import Template

from .common import CODE_ROOT as ROOT, read_json


TEMPLATES = ROOT/'docs/templates'
STATES = {'pending','running','complete','interrupted','example'}


def render_page(path, *, title, summary, status, status_title, status_detail, body, display, next_action):
    if status not in STATES:
        raise ValueError('Unknown guide state')
    values = {k:escape(v) for k,v in dict(title=title,summary=summary,status=status,
               status_title=status_title,status_detail=status_detail,next_action=next_action).items()}
    # Only repository-owned HTML fragments are inserted as markup; status/error
    # text and file/project names always go through HTML escaping.
    combined = body+display
    navigation = ''.join(f'<a href="#{key}">{label}</a>' for key,label in
                         [('environment','画面設定'),('procedure','実行手順'),('completion','完了の合図'),
                          ('recovery','中断時'),('next','次の操作')]
                         if key == 'next' or f'id="{key}"' in combined)
    values.update(body_html=body,display_html=display,nav_html=f'<nav aria-label="ページ内の案内">{navigation}</nav>')
    output = Template((TEMPLATES/'user-guide.html').read_text(encoding='utf-8')).substitute(values)
    path = Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(output,encoding='utf-8')
    return output


def guide_specs():
    """Public pages from docs/guides/ plus maintainer-only pages from maintenance/guides/ if present."""
    specs = {}
    for folder in (ROOT/'docs/guides', ROOT/'maintenance/guides'):
        index = folder/'pages.json'
        if index.is_file():
            specs.update({name:(folder,spec) for name,spec in read_json(index).items()})
    return specs


def build_guides():
    data = read_json(ROOT/'config/guidance.json')
    display = (TEMPLATES/'display-setup.html').read_text(encoding='utf-8')
    feedback = (TEMPLATES/'calibration-feedback.html').read_text(encoding='utf-8')
    for name,(folder,spec) in guide_specs().items():
        body = (folder/spec['body']).read_text(encoding='utf-8')
        body = body.replace('<!-- CALIBRATION_FEEDBACK -->',feedback)
        if '<!-- MESSAGE_EXAMPLES -->' in body:
            values = {'stage':'キャリブレーション','next_action':'HTML手順書で配置を確認する',
                      'reason':'画面設定が校正時と異なります'}
            rows = ''.join(f'<tr><th>{escape(label)}</th><td>{escape(data["messages"][key].format(**values))}</td></tr>'
                           for key,label in [('running','開始'),('complete','完了'),('interrupted','中断'),('returned','画面復帰')])
            body = body.replace('<!-- MESSAGE_EXAMPLES -->','<table>'+rows+'</table>')
        render_page(ROOT/'docs'/name,body=body,display=display if spec.get('display',True) else '',
                    **{k:spec[k] for k in ('title','summary','status','status_title','status_detail','next_action')})
    print('HTML guides: '+', '.join(guide_specs()))


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    build_guides()
