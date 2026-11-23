from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest

from app.feed import FeedCache, redis_client


@pytest.fixture
def account(client, register):
    def create():
        user_id, profile = register()
        response = client.post("/login", json={"id": user_id, "password": profile["password"]})
        assert response.status_code == 200
        return user_id, {"Authorization": f"Bearer {response.json()['token']}"}

    return create


def create_post(client, headers, text):
    response = client.post("/post/create", headers=headers, json={"text": text})
    assert response.status_code == 200, response.text
    return response.json()


def feed(client, headers, **params):
    response = client.get("/post/feed", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_invalidate_cached_feed_when_posts_and_friends_change(client, account):
    viewer, viewer_headers = account()
    author, author_headers = account()
    outsider, outsider_headers = account()

    assert client.get("/post/feed").status_code == 401
    assert client.get("/post/feed?offset=-1", headers=viewer_headers).status_code == 400
    assert client.get("/post/feed?limit=0", headers=viewer_headers).status_code == 400
    assert client.put(f"/friend/set/{viewer}", headers=viewer_headers).status_code == 400
    assert client.put(f"/friend/set/{uuid4()}", headers=viewer_headers).status_code == 400
    assert client.post("/post/create", json={"text": "Без токена"}).status_code == 401
    assert feed(client, viewer_headers) == []

    old_id = create_post(client, author_headers, "Первый пост")
    outsider_id = create_post(client, outsider_headers, "Пост незнакомого автора")
    create_post(client, viewer_headers, "Свой пост")
    assert client.put(f"/friend/set/{author}", headers=viewer_headers).status_code == 200
    assert client.put(f"/friend/add/{author}", headers=viewer_headers).status_code == 200
    old_post = {"id": old_id, "author_user_id": author, "text": "Первый пост"}
    assert feed(client, viewer_headers) == [old_post]
    assert feed(client, author_headers) == []

    new_id = create_post(client, author_headers, "Следующий пост")
    assert [p["id"] for p in feed(client, viewer_headers)] == [new_id, old_id]
    assert feed(client, viewer_headers, offset=1, limit=1) == [old_post]
    assert feed(client, viewer_headers, offset=2) == []

    edit = {"id": old_id, "text": "Исправленный пост"}
    assert client.put("/post/update", headers=viewer_headers, json=edit).status_code == 400
    assert client.put(f"/post/delete/{old_id}", headers=viewer_headers).status_code == 400
    assert client.put("/post/update", headers=author_headers, json=edit).status_code == 200
    edited_post = old_post | {"text": edit["text"]}
    assert client.get(f"/post/get/{old_id}").json() == edited_post
    assert feed(client, viewer_headers)[0] == edited_post
    assert client.put(f"/post/delete/{new_id}", headers=author_headers).status_code == 200
    assert client.get(f"/post/get/{new_id}").status_code == 400
    assert feed(client, viewer_headers) == [edited_post]

    assert client.put(f"/friend/set/{outsider}", headers=viewer_headers).status_code == 200
    assert [p["id"] for p in feed(client, viewer_headers)] == [old_id, outsider_id]
    assert client.put(f"/friend/delete/{author}", headers=viewer_headers).status_code == 200
    assert [p["id"] for p in feed(client, viewer_headers)] == [outsider_id]


def test_feed_limit_warm_page_and_rebuild(client, account, monkeypatch):
    viewer, viewer_headers = account()
    author, author_headers = account()
    other, _ = account()
    for friend in (author, other):
        assert client.put(f"/friend/set/{friend}", headers=viewer_headers).status_code == 200
    with client.app.state.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO posts (id, author_user_id, text, updated_at)
            SELECT gen_random_uuid(), CASE WHEN number %% 2 = 0 THEN %s::uuid ELSE %s::uuid END,
                   number::text, TIMESTAMPTZ '2026-01-01 00:00:00+00' + number * INTERVAL '1 minute'
            FROM generate_series(1, 1002) AS series(number)
            """,
            (author, other),
        )

    expected = [str(number) for number in range(1002, 2, -1)]
    assert [p["text"] for p in feed(client, viewer_headers, limit=2000)] == expected
    cache = client.app.state.feed.redis
    (key,) = cache.scan_iter(match=f"feed:v1:{viewer}:*")
    assert cache.llen(key) == 1001  # one marker plus 1000 posts

    def no_author_load(*args, **kwargs):
        pytest.fail("A warm feed should be paginated directly in Redis")

    with monkeypatch.context() as patch:
        patch.setattr(FeedCache, "_author_streams", no_author_load)
        assert [p["text"] for p in feed(client, viewer_headers, offset=999, limit=10)] == ["3"]
    assert feed(client, viewer_headers, offset=1000) == []

    assert cache.delete(key) == 1
    assert feed(client, viewer_headers, offset=999)[0]["text"] == "3"
    assert cache.exists(key) == 1
    author_keys = list(cache.scan_iter(match=f"feed:author:v1:{author}:*"))
    assert len(author_keys) == 1
    cache.delete(key, *author_keys)
    assert client.app.state.feed.rebuild(UUID(viewer)) == 1000
    assert cache.exists(key, *author_keys) == 2

    newest_id = feed(client, viewer_headers, limit=1)[0]["id"]
    assert client.put(f"/post/delete/{newest_id}", headers=author_headers).status_code == 200
    assert feed(client, viewer_headers, offset=999)[0]["text"] == "2"


def test_concurrent_rebuild_cannot_publish_stale_feed_for_new_version(client, account, monkeypatch):
    _, viewer_headers = account()
    author, author_headers = account()
    assert client.put(f"/friend/set/{author}", headers=viewer_headers).status_code == 200
    post_id = create_post(client, author_headers, "До правки")
    snapshot_taken = Event()
    commit_finished = Event()
    load = FeedCache._author_streams

    def pause_after_snapshot(self, conn, keys, *, rebuild):
        snapshot_taken.set()
        assert commit_finished.wait(5)
        return load(self, conn, keys, rebuild=rebuild)

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as executor:
        patch.setattr(FeedCache, "_author_streams", pause_after_snapshot)
        old_request = executor.submit(feed, client, viewer_headers)
        try:
            assert snapshot_taken.wait(5)
            response = client.put(
                "/post/update", headers=author_headers, json={"id": post_id, "text": "После правки"}
            )
            assert response.status_code == 200
        finally:
            commit_finished.set()
        assert old_request.result(timeout=5)[0]["text"] == "До правки"
    assert feed(client, viewer_headers)[0]["text"] == "После правки"


def test_writes_during_redis_outage_are_visible_after_recovery(client, account, monkeypatch):
    _, viewer_headers = account()
    author, author_headers = account()
    assert client.put(f"/friend/set/{author}", headers=viewer_headers).status_code == 200
    assert feed(client, viewer_headers) == []

    with monkeypatch.context() as patch, redis_client("redis://127.0.0.1:1/0") as unavailable:
        patch.setattr(client.app.state.feed, "redis", unavailable)
        response = client.get("/post/feed", headers=viewer_headers)
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "5"
        post_id = create_post(client, author_headers, "Создан во время сбоя Redis")

    assert feed(client, viewer_headers)[0]["id"] == post_id
