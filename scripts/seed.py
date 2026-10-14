"""Demo data against a running API: two tenants, restricted docs, three personas.
Usage: uv run python scripts/seed.py [http://localhost:8000]"""
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
BOOT = {"X-Bootstrap-Key": "dev-bootstrap-key"}
PW = "password-123"


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=30)
    for org in ("acme", "globex"):
        r = c.post("/tenants", headers=BOOT, json={"name": org, "admin_email": f"admin@{org}.test", "admin_password": PW})
        if r.status_code == 409:
            print(f"{org}: already seeded"); continue
        r.raise_for_status()
        tok = c.post("/auth/login", json={"email": f"admin@{org}.test", "password": PW}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        eng = c.post("/groups", headers=h, json={"name": "engineering"}).json()["id"]
        hr = c.post("/groups", headers=h, json={"name": "hr"}).json()["id"]
        for name, role, gid in (("alice", "member", eng), ("bob", "member", hr), ("carol", "viewer", None)):
            uid = c.post("/users", headers=h, json={"email": f"{name}@{org}.test", "password": PW, "role": role}).json()["id"]
            if gid:
                c.put(f"/groups/{gid}/members/{uid}", headers=h)
        docs = [
            ("Handbook", "Everyone gets 25 vacation days at " + org + ". Office hours are 9 to 5.", "tenant", ""),
            ("Engineering runbook", "Deploys at " + org + " happen on Tuesdays. The pager rotation is weekly.", "restricted", eng),
            ("Salary bands", "Salary bands at " + org + ": L3 is 90k, L4 is 120k. Confidential to HR.", "restricted", hr),
        ]
        for title, text, vis, gid in docs:
            c.post("/documents", headers=h, data={"title": title, "text": text, "visibility": vis, "group_ids": gid}).raise_for_status()
        print(f"{org}: seeded (login as admin/alice/bob/carol@{org}.test, password {PW})")


if __name__ == "__main__":
    main()
