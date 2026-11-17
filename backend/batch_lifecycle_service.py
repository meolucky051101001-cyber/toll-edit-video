"""Local batch recovery and schedule binding, serialized across processes."""
import uuid

from batch_lifecycle_lock import lifecycle_lock
from batch_plan_service import BatchPlanStore, PlanConflict, public_plan

BINDING_KEYS = ('schedule_id', 'schedule_generation', 'schedule_operation_id')


class BatchLifecycle:
    def __init__(self, workspace, schedules):
        self.plans = BatchPlanStore(workspace)
        self.schedules = schedules

    def guard(self):
        return lifecycle_lock(self.plans.path.parent)

    @staticmethod
    def expected(plan, payload):
        if type(payload.get('expected_revision')) is not int:
            raise PlanConflict('stale_state: Cần revision batch đang hiển thị.')
        binding = {key: payload.get(key) for key in BINDING_KEYS}
        BatchPlanStore.check_expected(plan, payload['expected_revision'], binding)

    def describe(self, plan):
        result = public_plan(plan)
        result['action_identity'] = {'expected_revision': plan['revision'], **{key: plan.get(key) for key in BINDING_KEYS}}
        result['actions'] = []
        if plan['state'] == 'prepared' and not plan.get('execution_id'):
            result['actions'] = ['start', 'reschedule', 'abandon']
        elif plan['state'] == 'scheduled' and not plan.get('execution_id'):
            try:
                schedule = self.schedules.operation(plan.get('schedule_operation_id'))
            except (FileNotFoundError, ValueError, OSError):
                schedule = None
            matches = schedule and schedule.get('target_kind') == 'local' and schedule.get('batch_id') == plan['batch_id'] and (
                schedule.get('schedule_id'), schedule.get('generation')) == (plan.get('schedule_id'), plan.get('schedule_generation'))
            if matches:
                result['schedule'] = schedule
            if matches and schedule['state'] in ('canceled', 'expired') and schedule.get('binding_confirmed', True):
                result['actions'] = ['start', 'reschedule', 'abandon']
            elif not matches or schedule['state'] == 'needs_review' or not schedule.get('binding_confirmed', True):
                result['recovery_reason'] = 'needs_review: Chưa xác minh được liên kết lịch hoặc kết quả bàn giao; batch vẫn giữ chỗ.'
        elif plan['state'] == 'recovery_required':
            result['recovery_reason'] = 'Cần kiểm tra execution trước khi chạy lại hoặc nhả quyền xử lý.'
        return result

    def get(self, batch_id=None, *, operation_id=None):
        with self.guard():
            return self.describe(self.plans.get(batch_id, operation_id=operation_id))

    def recoverable(self):
        with self.guard():
            return [self.describe(plan) for plan in self.plans.list_recoverable()]

    def abandon(self, batch_id, payload):
        with self.guard():
            plan = self.plans.get(batch_id)
            if plan['state'] == 'abandoned':
                return self.describe(plan)
            self.expected(plan, payload)
            if 'abandon' not in self.describe(plan)['actions']:
                raise PlanConflict('Batch đang chạy, giữ lịch hoặc chưa xác minh bàn giao; không thể bỏ.')
            plan = self.plans.update(batch_id, expected_revision=plan['revision'], expected_state=plan['state'],
                                     expected_binding={key: plan.get(key) for key in BINDING_KEYS}, state='abandoned')
            return self.describe(plan)

    def create_schedule(self, payload):
        with self.guard():
            kind = payload.get('target_kind')
            operation_id = payload.get('operation_id') or uuid.uuid4().hex
            args = dict(target_kind=kind, batch_id=payload.get('batch_id'), target_job_ids=payload.get('target_job_ids'),
                        expected_revision=payload.get('expected_revision') if kind == 'telegram' else None,
                        operation_id=operation_id, timezone=payload.get('timezone') or 'Asia/Bangkok', require_binding=kind == 'local')
            hhmm = str(payload.get('time') or payload.get('hhmm') or '02:00').strip()
            mode = str(payload.get('video_mode') or 'AUTO').upper()
            try:
                known = self.schedules.operation(operation_id)
            except FileNotFoundError:
                known = None
            if known:
                # create validates that the replay key still carries the same intent.
                result = self.schedules.create(hhmm, mode, **args)
                if kind == 'local' and result.get('binding_confirmed') is not True:
                    plan = self.plans.get(payload.get('batch_id'))
                    current = self.schedules.get()
                    if (current.get('operation_id') == operation_id and plan['state'] == 'scheduled'
                            and plan.get('schedule_operation_id') == operation_id and not plan.get('execution_id')):
                        self._bind(plan, result)
                        result = self.schedules.confirm_binding(result['schedule_id'], result['generation'])
                return result
            if kind != 'local':
                return self.schedules.create(hhmm, mode, **args)
            plan = self.plans.get(payload.get('batch_id'))
            frozen_intent = {'time': hhmm, 'video_mode': mode, 'timezone': args['timezone'], 'operation_id': operation_id}
            retry_binding = (plan['state'] == 'scheduled' and plan.get('schedule_operation_id') == operation_id
                             and not plan.get('schedule_id') and not plan.get('execution_id'))
            if retry_binding:
                saved_intent = plan.get('schedule_intent') or {}
                if any(saved_intent.get(key) != value for key, value in frozen_intent.items()) or 'run_at' not in saved_intent:
                    raise PlanConflict('Operation ID hoặc nội dung lịch không khớp intent đã lưu; cần đối soát.')
                args['planned_run_at'] = saved_intent['run_at']
            else:
                self.expected(plan, payload)
                if 'reschedule' not in self.describe(plan)['actions']:
                    raise PlanConflict('Batch đang giữ lịch hoặc đã bàn giao; không thể đặt lại lịch.')
            # Validate timing and current schedule before acquiring the durable hold.
            import time
            run_at = self.schedules.next_run(hhmm, time.time(), args['timezone'])
            if self.schedules.get().get('active'):
                raise PlanConflict('Đã có lịch đang chờ; hãy hủy hoặc đối soát lịch đó trước.')
            previous = plan
            if not retry_binding:
                args['planned_run_at'] = run_at
                plan = self.plans.update(plan['batch_id'], expected_revision=plan['revision'], state='scheduled',
                    schedule_operation_id=operation_id, schedule_id=None, schedule_generation=None,
                    schedule_intent={**frozen_intent, 'run_at': run_at})
            try:
                result = self.schedules.create(hhmm, mode, **args)
            except (ValueError, RuntimeError):
                # Deterministic rejection cannot have committed a local schedule.
                current = self.schedules.get()
                if current.get('operation_id') != operation_id:
                    self.plans.update(plan['batch_id'], expected_revision=plan['revision'], state=previous['state'],
                                      schedule_intent=previous.get('schedule_intent'), **{key: previous.get(key) for key in BINDING_KEYS})
                raise
            # I/O failure leaves the durable binding intent held for explicit replay.
            self._bind(plan, result)
            return self.schedules.confirm_binding(result['schedule_id'], result['generation'])

    def _bind(self, plan, schedule):
        return self.plans.update(plan['batch_id'], expected_revision=plan['revision'], expected_state='scheduled',
                                 schedule_id=schedule['schedule_id'], schedule_generation=schedule['generation'])
