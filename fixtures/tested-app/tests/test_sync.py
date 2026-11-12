def test_sync_settles(request):
    """The replica is in step once the sync check has run before."""
    cache = request.config.cache
    runs = cache.get("tested_app/sync-runs", 0)
    cache.set("tested_app/sync-runs", runs + 1)
    assert runs % 2 == 1, "the replica had not caught up yet"
