def test_sync_settles(request):
    """Alternates between failing and passing, run after run, through a counter kept in pytest's cache."""
    cache = request.config.cache
    runs = cache.get("tested_app/sync-runs", 0)
    cache.set("tested_app/sync-runs", runs + 1)
    assert runs % 2 == 1, "the replica had not caught up yet"
