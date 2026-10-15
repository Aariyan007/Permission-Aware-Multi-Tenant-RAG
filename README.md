# Permission-Aware Multi-Tenant RAG

A Retrieval-Augmented Generation (RAG) backend where many organizations (tenants) share one deployment, and **Postgres itself enforces who can see what**.

- **Multi-tenant:** Org A can never see Org B's data.
- **Permission-aware:** inside an org, a user only retrieves chunks their role and groups allow. The filter runs inside the vector query, before anything reaches the LLM, so nothing leaks through the prompt.
- **Enforced in the database (Row-Level Security), not only in app code:** a missing `WHERE` clause in the API cannot leak data.

> New to RAG or Row-Level Security? Start with the beginner guide: [docs/HOW_IT_WORKS.md](docs/HOW_IT_WORKS.md).

## Stack

| Piece | Choice |
|---|---|
| API | Python 3.12 + FastAPI + asyncpg |
| Database | Postgres 17 + pgvector (HNSW) + Row-Level Security |
| Permissions | Roles (admin / member / viewer) + document-level ACL groups |
| LLM | Claude via the Anthropic SDK (falls back to an extractive answer without a key) |
| Embeddings | Voyage (`voyage-3.5`, 256-dim) or a deterministic offline hash embedder |
| Observability | OpenTelemetry → Jaeger, Prometheus, Grafana, structured JSON logs |
| Deploy / CI | Docker Compose, GitHub Actions |

## Quick start

Needs Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d --build        # API, Postgres, Jaeger, Prometheus, Grafana
uv run python scripts/seed.py       # two demo orgs: acme, globex
```

Ask the same question as different people (password for all demo users: `password-123`):

```bash
TOKEN=$(curl -s localhost:8000/auth/login -H 'content-type: application/json' \
  -d '{"email":"bob@acme.test","password":"password-123"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s localhost:8000/query -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"question":"what are the salary bands"}'
```

| Login | Sees the salary doc? |
|---|---|
| `bob@acme.test` (HR group) | yes |
| `alice@acme.test` (engineering group) | no |
| `carol@acme.test` (viewer, no groups) | no |
| `admin@acme.test` | yes |
| `admin@globex.test` asking about acme | no, only globex data |

Set `ANTHROPIC_API_KEY` (and optionally `VOYAGE_API_KEY`) in your environment for real answers and embeddings. Interactive API docs: http://localhost:8000/docs · Jaeger: http://localhost:16686 · Prometheus: http://localhost:9090 · Grafana: http://localhost:3000 (dashboard "Permission-Aware RAG").

## How it works

```
client --JWT--> FastAPI --> auth (user_id, tenant_id from signed token)
                   |
                   |- POST /documents -> chunk -> embed -> store (chunks carry their ACL)
                   |- POST /query     -> embed question
                   |                     -> BEGIN; set_config('app.tenant_id'/'app.user_id', local)
                   |                     -> vector search  (embedding <=> $q)      } no WHERE on tenant/ACL:
                   |                     -> keyword search (full-text, stemmed)    } RLS filters both
                   |                     -> merge by Reciprocal Rank Fusion -> rerank (Voyage) -> top k
                   |                     -> LLM answers with [n] citations; audit row written
                   '- admin routes    -> users, groups, memberships, document access
```

Key code: [migrations/001_init.sql](migrations/001_init.sql) (schema and RLS policies), [app/db.py](app/db.py) (`tenant_tx`: pins identity into each transaction), [app/api/query.py](app/api/query.py) (the search has no tenant/ACL filter on purpose).

## API

| Route | Who | What |
|---|---|---|
| `POST /tenants` | bootstrap key | create an org + its first admin |
| `POST /auth/login` | anyone | email + password → JWT |
| `POST /users`, `GET /users` | admin | manage users |
| `POST /groups`, `GET /groups` | admin | manage groups |
| `PUT/DELETE /groups/{g}/members/{u}` | admin | add / revoke membership (takes effect immediately) |
| `POST /documents` | admin, member | ingest text or a file (txt, md, pdf); members may only share with groups they belong to |
| `GET /documents`, `DELETE /documents/{id}` | any / owner or admin | list what you may see, delete |
| `PUT /documents/{id}/access` | admin | set visibility and ACL; chunk ACLs re-sync by trigger |
| `POST /query` | any role | permission-filtered retrieval + answer with citations; `mode`: `hybrid` (default), `vector` or `keyword` |
| `GET /audit` | admin | who asked what and which chunks were returned |
| `GET /metrics`, `GET /health` | open | Prometheus, liveness |

## Hybrid search

Vector search matches **meaning** but is weak on exact codes and word forms; keyword search (Postgres full-text) matches **exact, stemmed words** but not synonyms. `hybrid` mode runs both, merges the two rankings with **Reciprocal Rank Fusion** (each chunk scores `1/(60 + position)` per list), then a **reranker** re-orders the shortlist. Both searches run in the same RLS-scoped transaction, and the leakage suite runs in all three modes.

Same question, same data, on the demo stack (offline embedder):

| Question | `vector` | `keyword` | `hybrid` |
|---|---|---|---|
| "what are the salaries" | runbook, handbook (missed) | Salary bands | **Salary bands** first |

The offline embedder treats "salaries" and "salary" as unrelated words; full-text search stems both to `salari`. With a real embedding model the gap is smaller but appears on exact codes (e.g. `PTO-2024-X7`). Measuring this properly is the next step (retrieval evaluation).

## Security model

1. The API connects as `rag_app`: not a superuser, `NOBYPASSRLS`, and it cannot read `users.password_hash`.
2. Each request runs in one transaction that sets the caller's tenant and user (transaction-local, so pooled connections cannot carry identity over).
3. RLS policies on every table filter by tenant. On `chunks` they also check visibility, ownership and group overlap.
4. Composite foreign keys `(id, tenant_id)` make cross-tenant references impossible.
5. Triggers keep each chunk's denormalized ACL in sync when document access changes.

Full threat table and the limits of this model: [docs/SECURITY_MODEL.md](docs/SECURITY_MODEL.md).

## Tests

Needs the Postgres from `docker compose up -d db` (override with `TEST_PG`).

```bash
uv run pytest -q          # 85 tests, ~25 s
uv run ruff check . && uv run mypy app
```

The **leakage suite** ([tests/leakage/](tests/leakage/test_isolation.py)) seeds three tenants with unique "canary" tokens in public, engineering-only and HR-only documents, then asserts:

1. No user ever sees another tenant's canary (every user x every foreign canary).
2. Group-restricted documents never reach non-members; a viewer with no groups sees only public documents.
3. Removing a user from a group, or editing a document's ACL, takes effect on the next request.
4. Raw SQL as `rag_app` with no identity set returns 0 rows from every table; it is not a superuser and cannot read password hashes; a viewer's writes are rejected; cross-tenant foreign keys are refused.
5. Prompt-injection queries, including a malicious document, gain nothing.
6. A randomized property test (300 random user x query pairs, fixed seed) checks results are always a subset of what an independent oracle allows.
7. Positive controls and completeness: users find everything they may see, for several questions and `top_k` values, **next to a 4,000-chunk neighbouring tenant**, so a pass cannot come from "returns nothing" or from a small tenant being crowded out of the vector index.
8. The same isolation and completeness checks with the ANN path forced on.
9. Cross-tenant, cross-group, prompt-injection and property tests run in all three search modes (vector, keyword, hybrid).

**The tests can fail.** I mutation-checked them: removing the group check from the `chunks` policy, or the tenant check, makes the suite fail. Finding worth knowing: `/query` also joins `documents`, which has its own RLS, so the API-level tests alone did not notice a broken `chunks` policy (two policy layers); the raw-SQL tests pin the `chunks` policy directly.

## Benchmark

`uv run python scripts/bench.py [chunks] [queries]` on a laptop: Docker Postgres, hash embeddings, extractive generator (no network). One tenant, a third of its chunks restricted, queried as a user outside the group.

| Tenant size | Retrieval path | `/query` p50 / p95 | Restricted chunks leaked |
|---|---|---|---|
| ~3,000 chunks | exact scan | 5.3 ms / 7.1 ms | 0 |
| ~20,000 chunks | HNSW (ANN) | 5.6 ms / 7.0 ms | 0 |

300 queries each, end to end through the API. These exclude real embedding and LLM latency, and the dataset is small. Treat them as a sanity check, not a scale claim.

## Design notes and known limits

- **Trust boundary:** the database trusts the API to set the identity from a verified JWT. RLS protects against bugs in queries (a forgotten filter, a bad join), not against a compromised API process that sets someone else's identity.
- **HNSW + filters (a bug I hit and fixed):** RLS filters *after* the HNSW graph walk returns candidates. Next to a 3,000-chunk tenant, a 3-chunk tenant got **zero results** from the index, and with `top_k=1` it silently got the wrong chunk. pgvector's iterative scan and a wider `ef_search` were not enough. Fix in [app/api/query.py](app/api/query.py): tenants up to `EXACT_SEARCH_MAX_CHUNKS` (default 5,000) are searched exactly (index disabled, RLS still applies); larger tenants use ANN, with an exact rerun if ANN returns fewer than `k` rows. Prometheus counter `rag_search_mode_total{mode}` shows which path ran. Known remaining gap: a large tenant's restricted user who sees few chunks gets correct but slower results via the fallback.
- **Rate limiting** is per-tenant, in process memory (fine for one replica; use Redis to share across replicas).
- **Migrations** are plain SQL files applied in order by `python -m app.migrate` (not Alembic): RLS policies are SQL, so SQL files stay readable.
- **Offline embedder** is a bag-of-words hash: good for tests and demos, much weaker than a real model. Use Voyage for real retrieval quality.
- `POST /tenants` is guarded by a shared bootstrap key; replace with real sign-up before exposing it.

## Layout

```
app/            FastAPI app (api/ routes, rag/ chunker-embedder-generator, auth, db, observability)
migrations/     SQL schema + RLS + triggers
tests/          unit, integration, leakage
scripts/        seed.py (demo data), bench.py (latency + leak check)
deploy/         Prometheus + Grafana config
docs/           PLAN.md (original design), SECURITY_MODEL.md
```
