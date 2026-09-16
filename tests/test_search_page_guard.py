import pytest

from backend.providers.base import SelectorChangedError
from backend.providers.xiaohongshu.provider import require_search_results


@pytest.mark.parametrize('url', [
    'https://www.rednote.com/?source=tourist_search',
    'https://www.xiaohongshu.com/explore',
    'https://www.xiaohongshu.com/search_result?keyword=sticker%20haul',
    'https://www.xiaohongshu.com/search_result',
])
def test_reject_recommendations_and_wrong_query(url):
    with pytest.raises(SelectorChangedError):
        require_search_results(url, 'sticker')

def test_accept_exact_encoded_search():
    require_search_results('https://www.xiaohongshu.com/search_result?keyword=%E8%B4%B4%E7%BA%B8&type=51', '贴纸')
