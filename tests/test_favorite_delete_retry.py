from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from api.personalization.routes import remove_favorite


@pytest.mark.asyncio
async def test_repeated_favorite_delete_is_acknowledged():
    repository = SimpleNamespace(remove_favorite=AsyncMock(return_value=False))
    result = await remove_favorite(
        seokey="saved-slug", user=SimpleNamespace(uid="user-a"), repository=repository
    )
    assert result == {"deleted": True}
    repository.remove_favorite.assert_awaited_once_with("user-a", "saved-slug")
