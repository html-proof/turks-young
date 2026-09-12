"""Time current lyrics code against the real provider without database writes."""
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.lyrics.provider import LRCLibProvider
from api.lyrics.service import LyricsService


class MemoryRepository:
    def __init__(self):
        self.rows = {}

    async def get(self, track_id, max_age_seconds, status=None):
        row = self.rows.get(track_id)
        return row if row and (status is None or row['status'] == status) else None

    async def put(self, track_id, data):
        self.rows[track_id] = data


async def main():
    provider = LRCLibProvider()
    calls = 0
    original = provider._request

    async def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    provider._request = counted
    service = LyricsService(provider, MemoryRepository(), 3600, 60)
    tracks = [
        {'id': 'benchmark-tum-hi-ho', 'title': 'Tum Hi Ho', 'artists': 'Arijit Singh',
         'album': 'Aashiqui 2', 'duration': 262},
        {'id': 'benchmark-perfect', 'title': 'Perfect', 'artists': 'Ed Sheeran',
         'album': 'Divide', 'duration': 263},
        {'id': 'benchmark-pranayanila', 'title': 'Pranayanila', 'artists': 'Shaan Rahman',
         'album': 'Teja Bhai and Family', 'duration': 93},
    ]
    try:
        for track in tracks:
            for mode in ('first_fetch', 'memory_repository_repeat'):
                start = perf_counter()
                before = calls
                try:
                    result = await asyncio.wait_for(service.get_lyrics(track), timeout=30)
                    output = {'track': track['title'], 'mode': mode,
                              'seconds': round(perf_counter() - start, 6),
                              'provider_calls': calls - before,
                              'status': result['status'], 'verified': result['verified'],
                              'synced_lines': len(result['lines']),
                              'has_plain_lyrics': bool(result['plainLyrics'])}
                except Exception as exc:
                    output = {'track': track['title'], 'mode': mode,
                              'seconds': round(perf_counter() - start, 3),
                              'provider_calls': calls - before, 'error': type(exc).__name__}
                print(json.dumps(output), flush=True)
                if 'error' in output:
                    break
    finally:
        await provider.close()


if __name__ == '__main__':
    asyncio.run(main())
