import hashlib
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

# Set this BEFORE importing settings/engine: legacy tests use SessionLocal directly.
# Every pytest invocation owns a fresh database; never reuse the workstation DB.
_TEST_DB_ROOT = Path(tempfile.mkdtemp(prefix="video-research-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_TEST_DB_ROOT / 'suite.db').as_posix()}"

from backend.app.main import app  # noqa: E402
from backend.core.database import Base, engine, get_db, make_engine  # noqa: E402
from backend.models import SearchJob, Video  # noqa: E402, F401
from backend.services.search_service import SearchService  # noqa: E402

# Standalone regression files using SessionLocal must not depend on test order.
Base.metadata.create_all(engine)


@pytest.fixture(autouse=True)
def isolate_runtime_services(monkeypatch):
    from pydantic import SecretStr

    import backend.core.queue as queues
    from backend.core.config import settings

    # Offline tests must not call the workstation's configured paid/free AI endpoint.
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setattr(settings, "ai_api_key", SecretStr(""))
    # TestClient creates a fresh event loop; asyncio primitives cannot span loops.
    queue = queues.InMemoryPriorityQueue(max_capacity=5000)
    monkeypatch.setattr(queues, "system_queue", queue)
    monkeypatch.setattr(queues, "system_worker_pool", queues.AdaptiveWorkerPool(queue))


@pytest.fixture(autouse=True)
def isolate_live_capture_manifest(tmp_path, monkeypatch):
    """Keep tests away from the persistent operational capture manifest."""
    release_path = os.environ.get("RELEASE_GATE_CAPTURE_MANIFEST_PATH")
    path = release_path or str(tmp_path / "isolated_live_capture_manifest.jsonl")
    monkeypatch.setenv("LIVE_CAPTURE_MANIFEST_PATH", path)






@pytest.fixture(scope="session", autouse=True)
def operational_manifest_unchanged_during_tests():
    """Prove a pytest run never changes the persistent operational manifest."""
    from backend.services.identity_policy import DEFAULT_LIVE_MANIFEST_PATH

    def digest():
        if not DEFAULT_LIVE_MANIFEST_PATH.exists():
            return None
        return hashlib.sha256(DEFAULT_LIVE_MANIFEST_PATH.read_bytes()).hexdigest()

    before = digest()
    yield
    assert digest() == before, "Tests modified the persistent operational live-capture manifest"


@pytest.fixture
def sessions(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def client(sessions):
    def override():
        with sessions() as db:
            yield db

    app.dependency_overrides[get_db] = override
    app.state.search = SearchService(sessions)
    from backend.services.browser_service import BrowserService
    from backend.services.douyin_browser_service import DouyinBrowserService
    from backend.services.download.service import DownloadService
    app.state.download = DownloadService(sessions)
    if not hasattr(app.state, "browser"):
        app.state.browser = BrowserService()
    if not hasattr(app.state, "douyin_browser"):
        app.state.douyin_browser = DouyinBrowserService()
    # No lifespan: fixture controls an isolated engine and worker.
    yield TestClient(app)
    app.dependency_overrides.clear()
