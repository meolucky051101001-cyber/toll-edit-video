"""Shared dashboard/bot voice choice, read at the start of voice generation."""
import json
import os
from pathlib import Path
BASE=Path(r"C:\tool v1\workspace\control")
ALIASES = {'capcut-vi-VN-HoaiMyNeural': 'microsoft-hoaimy', 'capcut-vi-VN-NamMinhNeural': 'microsoft-namminh'}

def catalog():
    return json.loads((BASE/'voice_catalog.json').read_text(encoding='utf-8'))
def selected():
    try:
        value=json.loads((BASE/'voice_selection.json').read_text(encoding='utf-8'))['id']
    except FileNotFoundError:
        value='chi-mai'
    value = ALIASES.get(value, value)
    for voice in catalog():
        if voice['id']==value:return voice
    raise ValueError('Giọng đã lưu không còn trong danh mục. Hãy chọn lại trên dashboard.')
def save(voice_id):
    voice_id = ALIASES.get(voice_id, voice_id)
    if not any(v['id']==voice_id for v in catalog()):raise ValueError('Mã giọng không hợp lệ.')
    tmp=BASE/'voice_selection.tmp'
    tmp.write_text(json.dumps({'id':voice_id}),encoding='utf-8')
    os.replace(tmp,BASE/'voice_selection.json')
def resolve_voice(default_source, default_param):
    voice=selected()
    if voice['id']=='chi-mai':
        if default_source!='rvc' or not default_param:
            raise ValueError('Chí Mai cần model RVC khả dụng. Hãy chọn CapCut hoặc Microsoft nếu chưa có model.')
        return default_source,str(default_param),voice['label']
    return voice['source'],voice['param'],voice['label']
