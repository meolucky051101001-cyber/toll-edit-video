import json
import time
from pathlib import Path

urls = [
    'https://v.douyin.com/yNh3YGBYjvc/',
    'https://v.douyin.com/qNKKPDL3KzQ/',
    'https://v.douyin.com/CZV0G0apGSA/',
    'http://xhslink.com/o/A3fp4BGEJ45',
    'http://xhslink.com/o/7mXjYjCYyG0',
    'https://v.douyin.com/xHIvlFx55zc/',
    'https://v.douyin.com/PPucGLW8230/',
    'https://v.douyin.com/T0sz7H9Y508/',
    'https://v.douyin.com/qOiBUYnbkLo/',
    'https://v.douyin.com/m5FPjbcg8C8/',
    'http://xhslink.com/o/18AIuQWELCw',
    'http://xhslink.com/o/3QO98etZIUN',
    'http://xhslink.com/o/4DFhNDt41Yn',
    'http://xhslink.com/o/AVfz7uSz62o',
    'http://xhslink.com/o/5VI5RKWNXZL',
    'http://xhslink.com/o/3Au4JC8M1FB',
    'http://xhslink.com/o/26EqpmvxCRM',
    'http://xhslink.com/o/3IlPndqQ6eZ',
    'http://xhslink.com/o/57ZArwjeLpe',
    'http://xhslink.com/o/7V0WkE4O9MH',
    'http://xhslink.com/o/6nzKBJPdZAA',
    'http://xhslink.com/o/7vDxaj5hwjH',
    'http://xhslink.com/o/8MAPfz0Mupz',
    'http://xhslink.com/o/1eBNEOWA5bw',
    'http://xhslink.com/o/86qAoYqYFIf',
    'http://xhslink.com/o/649BIJOFPHd',
    'http://xhslink.com/o/2ALM04Kok90'
]

items = []
for idx, u in enumerate(urls, 1):
    items.append({
        'position': idx,
        'telegram_position': idx,
        'name': u,
        'url': u,
        'type': 'url',
        'source': 'Telegram',
        'chat_id': 8393150305,
        'file_id': '',
        'filename': '',
        'voice_mode': 'manual'
    })

bot_system = Path(r'C:\tool v1\workspace\bot_system')
queue_file = bot_system / 'telegram_queue.json'
trigger_flag = bot_system / 'telegram_queue_trigger.flag'
reorder_flag = bot_system / 'telegram_queue_reorder.flag'

data = {
    'items': items,
    'updated_at': time.time(),
    'pid': 12264
}

queue_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
print(f'Written {len(items)} items to {queue_file}')

trigger_flag.write_text('1', encoding='utf-8')
print('Triggered queue load')
time.sleep(3.0)

reorder_flag.write_text('1', encoding='utf-8')
print('Triggered queue reorder')
time.sleep(3.0)

updated = json.loads(queue_file.read_text(encoding='utf-8-sig'))
print(f'Done! Current items in queue: {len(updated.get("items", []))}')
