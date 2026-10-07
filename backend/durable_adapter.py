"""Persist Telegram inputs before acknowledging, without serializing SDK objects."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import uuid
from typing import Optional
from telegram import Update
from telegram.ext import CallbackContext
try:
    from .durable_jobs import JobStore
except ImportError:
    from durable_jobs import JobStore


def compute_source_fingerprint(payload: dict) -> str:
    """Stable fingerprint for intake deduplication across retries and restarts."""
    if payload.get("path"):
        p = Path(payload["path"])
        if p.is_file():
            try:
                st = p.stat()
                return f"file:{p.name}:{st.st_size}:{int(st.st_mtime)}"
            except Exception:
                pass
        return f"path:{payload['path']}"
    if payload.get("url"):
        normalized = payload["url"].strip().split("?")[0].lower()
        return f"url:{normalized}"
    if payload.get("file_id"):
        return f"tg_file:{payload['file_id']}"
    return f"raw:{uuid.uuid4().hex}"


class DurableQueue:
    def __init__(self, path):
        self.path = path
        self.store = None
        self.application = None
        self.active = None

    def initialize(self, application):
        self.application = application
        self.store = JobStore(self.path)
        self.store.recover()

    async def put(self, job, allow_retry: bool = False):
        if self.store is None:
            raise RuntimeError('Durable intake is not initialized')
        update = job['update']
        payload = {k: v for k, v in job.items() if k not in {'update', 'context'}}
        if not payload.get('chat_id'):
            if hasattr(update, 'effective_chat') and update.effective_chat:
                payload['chat_id'] = update.effective_chat.id
            elif hasattr(update, 'message') and getattr(update.message, 'chat_id', None):
                payload['chat_id'] = update.message.chat_id
        payload['update'] = json.loads(update.to_json())
        payload['job_key'] = 'queue_' + uuid.uuid4().hex
        fingerprint = compute_source_fingerprint(payload)
        payload['source_fingerprint'] = fingerprint
        identifier = payload.get('url') or payload.get('file_id') or payload.get('path', '')
        key = '{}:{}:{}'.format(update.update_id, job['type'], identifier)
        if allow_retry:
            existing_id = self.store.retry_by_dedupe(key, payload)
            if existing_id is not None:
                return existing_id
        return self.store.enqueue(key, payload, source_fingerprint=fingerprint)

    def retry(self, job_id: int) -> int:
        if self.store is None:
            raise RuntimeError('Durable intake is not initialized')
        return self.store.retry(job_id)

    async def get(self):
        while True:
            claimed = self.store.claim()
            if claimed:
                self.active = claimed
                job_id, payload = claimed
                try:
                    update = Update.de_json(payload['update'], self.application.bot)
                    if update is None or update.message is None:
                        raise ValueError('Queued update has no message')
                    if payload.get('type') not in {'url', 'video', 'local'}:
                        raise ValueError('Unsupported queued job type')
                    return dict(payload, update=update,
                        context=CallbackContext.from_update(update, self.application))
                except (KeyError, TypeError, ValueError, AttributeError) as exc:
                    self.fail(exc)
                    logging.getLogger(__name__).error('Invalid queued input %s: %s', job_id, exc)
                    continue
            await asyncio.sleep(.5)

    def checkpoint(self, stage: Optional[str] = None, **values):
        if self.active:
            job_id, payload = self.active
            payload.update(values)
            self.store.checkpoint(job_id, payload, stage=stage)

    def task_done(self):
        if self.active:
            self.store.finish(self.active[0], 'completed')
            self.active = None

    def fail(self, error):
        if self.active:
            self.store.finish(self.active[0], 'failed', str(error)[:1000])
            self.active = None

    def cancel(self, job_id: Optional[int] = None):
        if self.store:
            self.store.cancel(job_id=job_id)
        if self.active and (job_id is None or self.active[0] == job_id):
            self.active = None

    def qsize(self):
        return self.store.counts().get('queued', 0) if self.store else 0

    def empty(self):
        return self.qsize() == 0
