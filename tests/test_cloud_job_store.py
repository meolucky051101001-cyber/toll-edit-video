"""
Kiểm thử chức năng CloudJobStore (SQLite queue cho Cloud GPU).
"""
import sys
import tempfile
from pathlib import Path

backend_dir = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_dir))

from cloud.cloud_job_store import CloudJobStore


def test_job_store_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_cloud_jobs.db"
        store = CloudJobStore(db_path=db_path, rollout_controlled=False)

        # 1. Enqueue job
        job1 = store.enqueue(
            video_path=r"C:\test\sample.mp4",
            video_name="sample.mp4",
            gpu_type="T4",
            task_type="full_pipeline",
        )
        assert job1 is not None
        assert job1.state == "queued"
        assert job1.job_id.startswith("cloud-")

        # 2. Claim next
        claimed = store.claim_next()
        assert claimed is not None
        assert claimed.job_id == job1.job_id
        assert claimed.state == "provisioning"
        assert claimed.legacy_state == "creating_session"
        assert claimed.attempt == 1
        assert claimed.owner_id is not None
        assert claimed.percent == 5

        # Claiming again should return None because queue is empty
        assert store.claim_next() is None

        # 3. Update progress
        assert store.update_progress(
            claimed.job_id,
            state="uploading",
            percent=20,
            step_name="Đang tải video lên Colab...",
            session_name="test-session-123",
        ) is True
        updated = store.get_job(claimed.job_id)
        assert updated.state == "uploading"
        assert updated.percent == 20
        assert updated.session_name == "test-session-123"

        # Invalid direct jump (uploading -> completed) must be rejected
        assert store.update_progress(
            claimed.job_id,
            state="completed",
            percent=100,
            step_name="Invalid jump",
        ) is False

        # 4. Complete job via canonical state sequence
        assert store.update_progress(claimed.job_id, state="preparing", percent=40, step_name="Chuẩn bị AI") is True
        assert store.update_progress(claimed.job_id, state="processing", percent=60, step_name="Đang xử lý") is True
        assert store.update_progress(claimed.job_id, state="downloading", percent=85, step_name="Đang tải về") is True
        assert store.update_progress(claimed.job_id, state="validating", percent=95, step_name="Kiểm tra") is True
        assert store.update_progress(
            claimed.job_id,
            state="completed",
            percent=100,
            step_name="Hoàn thành!",
            result_path=r"D:\banve\cloud_sample.mp4",
            cu_actual=0.35,
        ) is True
        completed = store.get_job(claimed.job_id)
        assert completed.state == "completed"
        assert completed.percent == 100
        assert completed.completed_at is not None
        assert completed.cu_actual == 0.35

        # 5. List jobs
        jobs = store.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == job1.job_id

        # 6. Test recover
        job2 = store.enqueue(r"C:\test\sample2.mp4")
        claimed2 = store.claim_next()
        assert claimed2.state == "provisioning"
        store.recover()
        recovered = store.get_job(claimed2.job_id)
        assert recovered.state == "failed"
        assert recovered.legacy_state == "error"
        assert "gián đoạn" in recovered.error

    print("[SUCCESS] Test CloudJobStore hoàn tất thành công!")


if __name__ == "__main__":
    test_job_store_lifecycle()
