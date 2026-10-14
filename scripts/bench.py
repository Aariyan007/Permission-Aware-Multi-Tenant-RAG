"""Latency benchmark against a running API (default http://localhost:8000).
Builds a throwaway tenant with ~N chunks, a third of them restricted, then times /query and
asserts no restricted chunk is ever returned to a user outside the group.
Usage: uv run python scripts/bench.py [chunks=3000] [queries=300]"""
import random
import statistics
import sys
import time
import uuid

import httpx

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
Q = int(sys.argv[2]) if len(sys.argv) > 2 else 300
import os

BASE, PW = os.environ.get("BENCH_BASE", "http://localhost:8000"), "password-123"
VOCAB = [f"term{i}" for i in range(400)]


def main() -> None:
    rng = random.Random(7)
    c = httpx.Client(base_url=BASE, timeout=60)
    org = f"bench-{uuid.uuid4().hex[:6]}"
    c.post("/tenants", headers={"X-Bootstrap-Key": "dev-bootstrap-key"},
           json={"name": org, "admin_email": f"admin@{org}.test", "admin_password": PW}).raise_for_status()
    h = {"Authorization": "Bearer " + c.post("/auth/login", json={"email": f"admin@{org}.test", "password": PW}).json()["access_token"]}
    gid = c.post("/groups", headers=h, json={"name": "secret"}).json()["id"]
    c.post("/users", headers=h, json={"email": f"outsider@{org}.test", "password": PW, "role": "member"}).raise_for_status()
    out = {"Authorization": "Bearer " + c.post("/auth/login", json={"email": f"outsider@{org}.test", "password": PW}).json()["access_token"]}

    restricted_ids: set[str] = set()
    t0 = time.perf_counter()
    for i in range(N // 3):  # each doc ~3 chunks of 180 words
        restricted = i % 3 == 0
        text = "\n\n".join(" ".join(rng.choices(VOCAB, k=170)) for _ in range(3))
        r = c.post("/documents", headers=h, data={"title": f"d{i}", "text": text,
                   "visibility": "restricted" if restricted else "tenant", "group_ids": gid if restricted else ""})
        r.raise_for_status()
        if restricted:
            restricted_ids.add(r.json()["id"])
    print(f"ingested ~{N} chunks in {time.perf_counter() - t0:.1f}s")

    lat, leaks = [], 0
    for _ in range(Q):
        q = " ".join(rng.choices(VOCAB, k=6))
        t = time.perf_counter()
        res = c.post("/query", headers=out, json={"question": q, "top_k": 5}).json()
        lat.append((time.perf_counter() - t) * 1000)
        leaks += sum(s["document_id"] in restricted_ids for s in res["sources"])
    lat.sort()
    print(f"{Q} queries  p50={statistics.median(lat):.1f}ms  p95={lat[int(Q * 0.95)]:.1f}ms  max={lat[-1]:.1f}ms")
    print(f"restricted chunks leaked to outsider: {leaks}")
    sys.exit(1 if leaks else 0)


if __name__ == "__main__":
    main()
