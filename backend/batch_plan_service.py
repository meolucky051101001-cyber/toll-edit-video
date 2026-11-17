"""Durable, immutable local batch selections and acceptance receipts.

SQLite owns the acceptance transaction. A plan embeds its effective configuration;
filename aliases in the legacy configuration store are never its source of truth.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from batch_lifecycle_lock import lifecycle_lock

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv"}


class PlanConflict(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(path):
    path = Path(path)
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise PlanConflict("Nguồn đang thay đổi; hãy chờ ghi file hoàn tất.")
    return {"sha256": digest.hexdigest(), "size": after.st_size}


def output_receipt_path(workspace, output):
    canonical = os.path.normcase(os.path.abspath(str(output)))
    key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Path(workspace) / "output_receipts" / (key + ".json")


def bare_name(value):
    name = str(value or "")
    if (not name or name != name.strip() or name in {".", ".."}
            or any(c in name for c in '\\/:<>"|?*\x00') or name.endswith((".", " "))):
        raise ValueError("Tên video không hợp lệ; chỉ nhận tên file trong thư mục nguồn.")
    return name


def validate_source(item, input_root):
    root = Path(input_root).resolve()
    source = Path(item["source_ref"])
    if source.resolve().parent != root or source.name != item["filename"]:
        raise PlanConflict("Nguồn không còn thuộc thư mục đã xác nhận.")
    if not source.is_file() or fingerprint(source) != item["source_fingerprint"]:
        raise PlanConflict(f"Nguồn đã mất hoặc thay đổi: {item['filename']}")
    return source


class BatchPlanStore:
    def __init__(self, workspace):
        self.path = Path(workspace) / "control" / "batch_plans.sqlite3"

    @contextmanager
    def transaction(self):
        with lifecycle_lock(self.path.parent):
            with self._transaction() as db:
                yield db

    @contextmanager
    def _transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("CREATE TABLE IF NOT EXISTS batch_plans (batch_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, plan TEXT NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def prepare(self, payload, input_root, output_root, freeze_config):
        input_root, output_root = Path(input_root).resolve(), Path(output_root).resolve()
        operation_id = str(payload.get("idempotency_key") or uuid.uuid4().hex)
        if len(operation_id) > 160 or not operation_id.strip():
            raise ValueError("Idempotency key không hợp lệ.")
        request = {k: v for k, v in payload.items() if k not in {"idempotency_key", "batch_id"}}
        # Bind the request key to supplied intent, not today's server roots. A retry
        # after a settings change still retrieves the original frozen plan.
        payload_hash = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM batch_plans WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                if row["payload_hash"] != payload_hash:
                    raise PlanConflict("Idempotency key đã được dùng cho yêu cầu khác.")
                return json.loads(row["plan"])
            for key, root in (("input_dir", input_root), ("output_dir", output_root)):
                if payload.get(key) and Path(payload[key]).resolve() != root:
                    raise PlanConflict("Thư mục đã thay đổi; hãy làm mới bản xác nhận.")
            raw_items = payload.get("items")
            if not isinstance(raw_items, list) or not raw_items:
                raise ValueError("Danh sách video rỗng.")
            validated, names, destinations = [], set(), set()
            for raw in raw_items:
                if not isinstance(raw, dict):
                    raise ValueError("Batch item phải là object.")
                name = bare_name(raw.get("filename") or raw.get("name"))
                path = input_root / name
                if path.resolve().parent != input_root or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                    raise ValueError(f"Video không tồn tại trong thư mục nguồn: {name}")
                dest = "Dubbed_" + (path.stem[7:] if path.stem.startswith("Dubbed_") else path.stem) + ".mp4"
                if name.casefold() in names or dest.casefold() in destinations:
                    raise PlanConflict("Các video được chọn trùng nguồn hoặc cùng tên thành phẩm.")
                names.add(name.casefold()); destinations.add(dest.casefold())
                validated.append((raw, path, dest, fingerprint(path)))
            batch_id = uuid.uuid4().hex
            items = []
            for position, (raw, path, dest, source_hash) in enumerate(validated, 1):
                job_id = uuid.uuid4().hex
                overrides = dict(raw.get("overrides") or {})
                overrides.setdefault("video_mode", str(raw.get("video_mode") or payload.get("video_mode") or "auto"))
                snapshot = freeze_config(video_name=path.name, preset_id=raw.get("preset_id"),
                                         overrides=overrides, job_id=job_id, batch_id=batch_id, source_path=path)
                items.append({"job_id": job_id, "position": position, "filename": path.name,
                              "source_ref": str(path), "source_fingerprint": source_hash,
                              "output_ref": str(output_root / dest), "config_snapshot_id": snapshot.get("snapshot_id", job_id),
                              "config_revision": snapshot.get("revision", 1), "config_snapshot": snapshot,
                              "effective_config": snapshot["effective_config"], "preset_name": snapshot.get("preset_name"),
                              "warnings": snapshot.get("warnings", []), "state": "pending"})
            plan = {"schema_version": 1, "revision": 1, "batch_id": batch_id, "operation_id": operation_id,
                    "source_kind": "local", "input_root": str(input_root), "output_root": str(output_root),
                    "items": items, "state": "prepared", "created_at": time.time(), "execution_id": None,
                    "video_mode": str(payload.get("video_mode") or "auto")}
            plan["archive_source"] = payload.get("archive_source", True) is not False
            plan["operation_kind"] = payload.get("operation_kind", "local_batch")
            db.execute("INSERT INTO batch_plans VALUES (?,?,?,?)", (batch_id, operation_id, payload_hash, canonical_json(plan)))
            return plan

    def get(self, batch_id=None, *, operation_id=None):
        with self.transaction() as db:
            column, value = ("operation_id", operation_id) if operation_id is not None else ("batch_id", batch_id)
            row = db.execute(f"SELECT plan FROM batch_plans WHERE {column}=?", (value,)).fetchone()
            if not row:
                raise FileNotFoundError("Không tìm thấy batch plan.")
            return json.loads(row[0])

    @staticmethod
    def check_expected(plan, expected_revision=None, expected_binding=None):
        if expected_revision is not None and (type(expected_revision) is not int or plan['revision'] != expected_revision):
            raise PlanConflict('stale_state: Batch đã thay đổi; hãy tải lại trạng thái.')
        if expected_binding is not None and any(plan.get(key) != expected_binding.get(key)
                for key in ('schedule_id', 'schedule_generation', 'schedule_operation_id')):
            raise PlanConflict('stale_state: Lịch của batch đã thay đổi; hãy tải lại trạng thái.')

    def update(self, batch_id, *, expected_state=None, expected_revision=None, expected_binding=None, **changes):
        allowed = {"state", "execution_id", "error", "accepted_at", "finished_at", "schedule_id", "schedule_generation", "schedule_operation_id", "schedule_intent"}
        if changes.keys() - allowed:
            raise ValueError("Batch selection and configuration are immutable.")
        with self.transaction() as db:
            row = db.execute("SELECT plan FROM batch_plans WHERE batch_id=?", (batch_id,)).fetchone()
            if not row:
                raise FileNotFoundError(batch_id)
            plan = json.loads(row[0])
            self.check_expected(plan, expected_revision, expected_binding)
            if expected_state is not None and plan["state"] != expected_state:
                raise PlanConflict(f"Batch đang ở trạng thái {plan['state']}.")
            if plan['state'] == 'abandoned':
                if changes.get('state', 'abandoned') != 'abandoned':
                    raise PlanConflict('Batch đã bỏ không thể được nhận chạy lại.')
                return plan
            if changes.get("state") in {"accepted", "scheduled"}:
                conflict = self._conflict(db, plan)
                if conflict:
                    raise PlanConflict(f"Nguồn/thành phẩm được batch {conflict} giữ quyền xử lý.")
            plan.update(changes, revision=plan["revision"] + 1, updated_at=time.time())
            db.execute("UPDATE batch_plans SET plan=? WHERE batch_id=?", (canonical_json(plan), batch_id))
            return plan

    def list_recoverable(self):
        with self.transaction() as db:
            rows = db.execute("SELECT plan FROM batch_plans WHERE json_extract(plan, '$.state') IN ('prepared','scheduled','recovery_required') ORDER BY rowid DESC").fetchall()
            return [json.loads(row[0]) for row in rows]

    def conflicting_reservation(self, plan):
        with self.transaction() as db:
            return self._conflict(db, plan)

    @staticmethod
    def _conflict(db, plan):
        targets = {os.path.normcase(i["source_ref"]) for i in plan["items"]}
        outputs = {os.path.normcase(i["output_ref"]) for i in plan["items"]}
        rows = db.execute("SELECT plan FROM batch_plans WHERE batch_id<>? AND json_extract(plan, '$.state') IN ('accepted','running','scheduled','recovery_required')", (plan["batch_id"],)).fetchall()
        for row in rows:
            other = json.loads(row[0])
            if any(os.path.normcase(i["source_ref"]) in targets or os.path.normcase(i["output_ref"]) in outputs for i in other["items"]):
                return other["batch_id"]
        return None

    def for_execution(self, execution_id):
        with self.transaction() as db:
            row = db.execute("SELECT plan FROM batch_plans WHERE json_extract(plan, '$.execution_id')=?", (execution_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def reconcile_interrupted(self, live_execution_id=None):
        """Never replay uncertain acceptance after restart; retain it for review."""
        changed = []
        with self.transaction() as db:
            rows = db.execute("SELECT batch_id, plan FROM batch_plans WHERE json_extract(plan, '$.state') IN ('accepted','running')").fetchall()
            for row in rows:
                plan = json.loads(row["plan"])
                if plan.get("execution_id") == live_execution_id:
                    continue
                plan.update(state="recovery_required", error="Owner exited before terminal receipt; inspect manifest before retry.", updated_at=time.time())
                plan["revision"] += 1
                db.execute("UPDATE batch_plans SET plan=? WHERE batch_id=?", (canonical_json(plan), row["batch_id"]))
                changed.append(row["batch_id"])
        return changed

    def pending_recovery(self):
        with self.transaction() as db:
            return [row[0] for row in db.execute("SELECT batch_id FROM batch_plans WHERE json_extract(plan, '$.state')='recovery_required'").fetchall()]

    def item_result(self, batch_id, job_id, state, **details):
        with self.transaction() as db:
            row = db.execute("SELECT plan FROM batch_plans WHERE batch_id=?", (batch_id,)).fetchone()
            if not row:
                raise FileNotFoundError(batch_id)
            plan = json.loads(row[0])
            item = next(i for i in plan["items"] if i["job_id"] == job_id)
            item.update(state=state, result=details, updated_at=time.time())
            plan["revision"] += 1
            db.execute("UPDATE batch_plans SET plan=? WHERE batch_id=?", (canonical_json(plan), batch_id))
            return plan


def public_plan(plan):
    return {**plan, "total_items": len(plan["items"]), "status": "accepted" if plan["state"] in {"accepted", "running"} else plan["state"],
            "counters": {key: sum(i["state"] == state for i in plan["items"]) for key, state in
                         (("pending", "pending"), ("running", "running"), ("completed", "completed"), ("failed", "failed"), ("canceled", "canceled"))}}
