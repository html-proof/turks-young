from unittest.mock import AsyncMock, Mock

import pytest

from api.cache.redis_cache import RedisCache
from api.catalog.service import CatalogService, LanguageCatalog


@pytest.mark.asyncio
async def test_loader_timeout_is_not_retried_as_cache_failure():
    cache = RedisCache('', '')
    loader = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await cache.get_or_set('album-search', loader, 60)
    loader.assert_awaited_once()
    assert await cache.get('album-search') is None


@pytest.mark.asyncio
async def test_album_timeout_returns_response_and_next_request_retries():
    catalog = Mock()
    catalog.search_albums = AsyncMock(side_effect=TimeoutError())
    service = CatalogService(catalog, LanguageCatalog([]), RedisCache('', ''))
    await service.search('Sagar alias jacky', 'album', 1, 50)
    await service.search('Sagar alias jacky', 'album', 1, 50)
    assert catalog.search_albums.await_count == 2


@pytest.mark.asyncio
async def test_multi_search_outage_is_not_cached_as_no_results():
    catalog = Mock()
    for name in ('search_songs', 'search_artists', 'search_albums', 'search_playlists'):
        setattr(catalog, name, AsyncMock(side_effect=TimeoutError()))
    service = CatalogService(catalog, LanguageCatalog([]), RedisCache('', ''))
    for _ in range(2):
        result = await service.search('Mudhalvan', None, 1, 15)
        assert result['songs'] == []
        assert result['albums'] == []
    # Empty outage pages must not be cached or memoized: the second request
    # has to reach the provider again.
    assert catalog.search_songs.await_count == 2
