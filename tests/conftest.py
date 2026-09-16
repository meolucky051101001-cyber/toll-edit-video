import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.app.main import app
from backend.core.database import Base, get_db, make_engine
from backend.models import SearchJob, Video  # noqa: F401
from backend.services.search_service import SearchService


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
