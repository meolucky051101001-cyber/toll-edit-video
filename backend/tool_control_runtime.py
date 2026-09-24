"""Controller heartbeat and admission barrier; no credentials are exported."""
import asyncio
import json
import os
import time
from pathlib import Path
from telegram import Update
from telegram.ext import TypeHandler, ApplicationHandlerStop

FLAGS=Path(r"C:\tool v1\workspace\control")

def is_allowed_command_during_pause(cmd_text: str) -> bool:
    """Codex Requirement: Cho phép lệnh xem/đổi chế độ /voice_auto và /start đi qua ngay cả khi V1 đang pause."""
    if not cmd_text:
        return False
    txt = cmd_text.strip().split()[0].lower()
    return txt.startswith("/voice_auto") or txt.startswith("/voice") or txt == "/start"

def install(application, namespace, key):
    flag=FLAGS/(key+'.pause')
    async def gate(update, context):
        if flag.exists():
            if isinstance(update, Update) and update.message and update.message.text:
                if is_allowed_command_during_pause(update.message.text):
                    return
            raise ApplicationHandlerStop
    application.add_handler(TypeHandler(Update, gate), group=-1000)
    active_callbacks = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, TypeHandler):
                continue
            callback = handler.callback
            async def tracked(update, context, callback=callback):
                task = asyncio.current_task()
                active_callbacks.add(task)
                try:
                    return await callback(update, context)
                finally:
                    active_callbacks.discard(task)
            handler.callback = tracked
    previous=application.post_init
    async def initialized(app):
        if previous:
            await previous(app)
        async def heartbeat():
            while True:
                try:
                    queue = namespace.get('global_queue')
                    tracker = namespace.get('job_tracker')
                    busy = bool(tracker and tracker.get_status().get('active')) or bool(getattr(queue, 'active', None))
                    count = queue.qsize() if queue and hasattr(queue, 'qsize') else 0
                    # V1 keeps a task unfinished from get() until render/delivery completes.
                    busy = busy or bool(active_callbacks) or (getattr(queue, '_unfinished_tasks', 0) > count if queue else False)
                    payload={'pid':os.getpid(),'at':time.time(),'busy':busy,'queue':count,
                             'paused':flag.exists(),'polling':bool(app.updater and app.updater.running),
                             'version':'v1_codex_voice_lock_1.0'}
                    FLAGS.mkdir(parents=True,exist_ok=True)
                    tmp=FLAGS/(key+'.'+str(os.getpid())+'.tmp')
                    tmp.write_text(json.dumps(payload),encoding='utf-8')
                    os.replace(tmp,FLAGS/(key+'.json'))
                except Exception:
                    # A missing heartbeat is shown as unknown, never healthy.
                    pass
                await asyncio.sleep(2)
        app.bot_data['_control_heartbeat']=asyncio.create_task(heartbeat())
        queue_watcher_fn = namespace.get('queue_signal_watcher')
        if queue_watcher_fn:
            app.bot_data['_queue_watcher'] = asyncio.create_task(queue_watcher_fn(app))
        enqueue_pending_fn = namespace.get('enqueue_pending_queue_jobs')
        if enqueue_pending_fn:
            asyncio.create_task(enqueue_pending_fn(app))
        enqueue_v2_fn = namespace.get('enqueue_interrupted_v2_jobs')
        if enqueue_v2_fn:
            asyncio.create_task(enqueue_v2_fn(app))
    application.post_init=initialized
    previous_shutdown=application.post_shutdown
    async def shutdown(app):
        task=app.bot_data.pop('_control_heartbeat',None)
        if task: task.cancel()
        q_watcher = app.bot_data.pop('_queue_watcher', None)
        if q_watcher: q_watcher.cancel()
        if previous_shutdown: await previous_shutdown(app)
    application.post_shutdown=shutdown
