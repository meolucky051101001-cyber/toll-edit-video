"""Quality-threshold filters (min likes / min views) for faster, higher-quality search."""

from backend.providers.base import apply_search_filters_and_sorting
from backend.schemas.contracts import SearchFilters, VideoResult


def _item(item_id: str, likes: int | None, views: int | None) -> VideoResult:
    return VideoResult(
        platform="douyin",
        platform_video_id=item_id,
        url=f"https://www.douyin.com/video/{item_id}",
        title="Ứng viên kiểm thử",
        like_count=likes,
        view_count=views,
    )


def test_minimum_views_rejects_known_counts_below_threshold():
    filters = SearchFilters(minimum_views=10_000)
    kept = apply_search_filters_and_sorting(
        [
            _item("a", likes=100, views=5_000),      # below threshold -> dropped
            _item("b", likes=100, views=50_000),     # above -> kept
            _item("c", likes=100, views=None),       # unknown (XHS-style) -> kept
        ],
        filters=filters,
    )
    assert [v.platform_video_id for v in kept] == ["b", "c"]


def test_minimum_likes_still_rejects_unknown_counts():
    filters = SearchFilters(minimum_likes=500)
    kept = apply_search_filters_and_sorting(
        [
            _item("a", likes=100, views=1),
            _item("b", likes=1_000, views=1),
            _item("c", likes=None, views=1),  # likes unread -> dropped (strict)
        ],
        filters=filters,
    )
    assert [v.platform_video_id for v in kept] == ["b"]


def test_zero_thresholds_disable_filtering():
    kept = apply_search_filters_and_sorting(
        [_item("a", likes=0, views=0), _item("b", likes=None, views=None)],
        filters=SearchFilters(),
    )
    assert len(kept) == 2


def test_api_search_persists_quality_thresholds(client, sessions, monkeypatch):
    from backend.app.main import app
    from backend.models import SearchPlan

    submitted = []
    monkeypatch.setattr(app.state.search, "submit", submitted.append)
    response = client.post(
        "/api/search",
        json={
            "query": "unbox đồ cute",
            "mode": "douyin",
            "platforms": ["douyin"],
            "limit": 5,
            "min_likes": 5000,
            "min_views": 50000,
        },
    )
    assert response.status_code == 202
    job_id = response.json()["id"]

    with sessions() as db:
        plan = db.get(SearchPlan, job_id)
        assert plan is not None
        assert plan.filters["minimum_likes"] == 5000
        assert plan.filters["minimum_views"] == 50000


def test_api_search_rejects_negative_thresholds(client):
    assert (
        client.post(
            "/api/search",
            json={"query": "unbox đồ cute", "mode": "mock", "min_likes": -1},
        ).status_code
        == 422
    )
