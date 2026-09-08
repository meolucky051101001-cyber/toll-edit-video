"""Persist Telegram inputs before acknowledging, without serializing SDK objects."""
import asyncio
import json
import logging
import uuid
from telegram import Update
from telegram.ext import CallbackContext
try:
    from .durable_jobs import JobStore
except ImportError:
    from durable_jobs import JobStore


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

    async def put(self, job):
        if self.store is None:
            raise RuntimeError('Durable intake is not initialized')
        update = job['update']
        payload = {k:v for k,v in job.items() if k not in {'update','context'}}
        payload['update'] = json.loads(update.to_json())
        payload['job_key'] = 'queue_' + uuid.uuid4().hex
        identifier = payload.get('url') or payload.get('file_id') or payload.get('path', '')
        key = '{}:{}:{}'.format(update.update_id, job['type'], identifier)
        return self.store.enqueue(key, payload)

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

    def checkpoint(self, **values):
        if self.active:
            job_id, payload = self.active
            payload.update(values)
            self.store.checkpoint(job_id, payload)

    def task_done(self):
        if self.active:
            self.store.finish(self.active[0], 'completed')
            self.active = None

    def fail(self, error):
        if self.active:
            self.store.finish(self.active[0], 'failed', str(error)[:1000])
            self.active = None

    def cancel(self):
        if self.store:
            self.store.cancel()
        self.active = None

    def qsize(self):
        return self.store.counts().get('queued', 0) if self.store else 0

    def empty(self):
        return self.qsize() == 0
