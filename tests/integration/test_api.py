
from tests.conftest import ask, canary


async def test_health(world):
    assert (await world.client.get("/health")).json() == {"status": "ok"}


async def test_tenant_signup_needs_bootstrap_key(world):
    r = await world.client.post("/tenants", headers={"X-Bootstrap-Key": "nope"},
                                json={"name": "x", "admin_email": "x@x.test", "admin_password": "password-123"})
    assert r.status_code == 403


async def test_login_rejects_bad_credentials(world):
    for body in ({"email": "admin@a.test", "password": "wrong-password"}, {"email": "ghost@a.test", "password": "password-123"}):
        assert (await world.client.post("/auth/login", json=body)).status_code == 401


async def test_requires_token(world):
    assert (await world.client.post("/query", json={"question": "hi"})).status_code == 401
    r = await world.client.post("/query", headers={"Authorization": "Bearer garbage"}, json={"question": "hi"})
    assert r.status_code == 401


async def test_duplicate_email_is_conflict(world):
    admin = world.user("a", "admin")
    r = await world.client.post("/users", headers=admin.headers, json={"email": "alice@a.test", "password": "password-123"})
    assert r.status_code == 409


async def test_only_admin_manages_users(world):
    r = await world.client.post("/users", headers=world.user("a", "alice").headers,
                                json={"email": "new@a.test", "password": "password-123"})
    assert r.status_code == 403


async def test_viewer_cannot_ingest(world):
    r = await world.client.post("/documents", headers=world.user("a", "carol").headers, data={"title": "t", "text": "hello world"})
    assert r.status_code == 403


async def test_restricted_document_needs_groups(world):
    r = await world.client.post("/documents", headers=world.user("a", "alice").headers,
                                data={"title": "t", "text": "hello", "visibility": "restricted"})
    assert r.status_code == 422


async def test_member_cannot_attach_group_they_are_not_in(world):
    alice, hr = world.user("a", "alice"), world.group_ids["a:hr"]  # alice is eng only
    r = await world.client.post("/documents", headers=alice.headers,
                                data={"title": "t", "text": "hello there", "visibility": "restricted", "group_ids": hr})
    assert r.status_code == 403


async def test_member_can_share_with_own_group_and_sees_own_upload(world):
    alice = world.user("a", "alice")
    r = await world.client.post("/documents", headers=alice.headers, data={
        "title": "mine", "text": "uniqueuploadtoken here", "visibility": "restricted", "group_ids": world.group_ids["a:eng"]})
    assert r.status_code == 201
    try:
        res = await ask(world, alice, "uniqueuploadtoken", top_k=1)
        assert "uniqueuploadtoken" in res["sources"][0]["snippet"]
        assert "uniqueuploadtoken" not in str(await ask(world, world.user("a", "bob"), "uniqueuploadtoken"))
    finally:  # keep the shared world pristine for the leakage oracle
        await world.client.delete(f"/documents/{r.json()['id']}", headers=alice.headers)


async def test_query_returns_citations_and_writes_audit(world):
    admin = world.user("c", "admin")
    res = await ask(world, admin, f"secret marker {canary('c', 'pub')}", top_k=2)
    assert res["sources"] and res["sources"][0]["n"] == 1 and "[1]" in res["answer"]
    audit = (await world.client.get("/audit", headers=admin.headers)).json()
    assert any(a["action"] == "query" and a["chunk_ids"] for a in audit)
    assert (await world.client.get("/audit", headers=world.user("c", "alice").headers)).status_code == 403


async def test_delete_document_removes_its_chunks(world):
    alice = world.user("b", "alice")
    r = await world.client.post("/documents", headers=alice.headers, data={"title": "tmp", "text": "deletemetoken body"})
    doc = r.json()["id"]
    assert (await world.client.delete(f"/documents/{doc}", headers=alice.headers)).status_code == 204
    assert "deletemetoken" not in str(await ask(world, alice, "deletemetoken"))
    assert (await world.client.delete(f"/documents/{doc}", headers=alice.headers)).status_code == 404


async def test_other_tenants_document_is_404_not_403(world):
    r = await world.client.delete(f"/documents/{world.docs['a:pub']}", headers=world.user("b", "admin").headers)
    assert r.status_code == 404


async def test_rate_limit(world):
    from app.ratelimit import TenantRateLimiter
    lim = TenantRateLimiter()
    assert all(lim.allow("t", 3, now=i) for i in range(3))
    assert not lim.allow("t", 3, now=3)
    assert lim.allow("t", 3, now=61)
