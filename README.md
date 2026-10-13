# Permission-Aware Multi-Tenant RAG

A Retrieval-Augmented Generation (RAG) backend where many organizations (tenants) share one deployment, and **the database itself enforces who can see what**.

- **Multi-tenant:** Org A can never see Org B's data.
- **Permission-aware:** inside an org, a user only retrieves document chunks their role and groups allow. The check happens at retrieval time, before anything reaches the LLM, so nothing leaks through the prompt.
- **Enforced in Postgres (Row-Level Security), not just app code:** a bug in the API cannot leak data.

> Status: planning done, implementation in progress. See [docs/PLAN.md](docs/PLAN.md) for the full design.

## Stack

| Piece | Choice |
|---|---|
| API | Python + FastAPI |
| Database | Postgres + pgvector + Row-Level Security |
| Permissions | RBAC (admin / member / viewer) + document-level ACL groups |
| LLM | Claude via the Anthropic SDK |
| Embeddings | Voyage, with a local sentence-transformers fallback |
| Observability | OpenTelemetry, Prometheus, Grafana, Jaeger |
| Deploy | Docker Compose, GitHub Actions CI |

## How it works

```
client --JWT--> FastAPI --> auth (user_id, tenant_id)
                   |
                   |- /ingest -> parse -> chunk -> embed -> store chunks (+ ACL groups)
                   |- /query  -> embed question
                   |             -> SET LOCAL app.tenant_id / app.user_id
                   |             -> vector search (RLS filters tenant + ACL)
                   |             -> LLM answers with citations
                   '- /admin  -> tenants, users, groups, document ACLs
```

## Security model

1. The app connects as a non-superuser Postgres role (`rag_app`) that cannot bypass RLS.
2. Every request runs in a transaction that sets the caller's tenant and user ID.
3. RLS policies on every table filter by tenant. On `chunks`, they also filter by visibility and group membership.
4. Leakage tests (see below) try to break these guarantees on every CI run.

## Leakage tests

The headline feature. Seeded tenants and documents contain unique "canary" strings, and tests assert that:

1. No query ever returns another tenant's canaries.
2. A member outside group G never gets G-restricted chunks.
3. Removing a user from a group revokes access immediately.
4. Raw `SELECT * FROM chunks` as `rag_app`, with no tenant set, returns 0 rows.
5. Prompt-injection queries return nothing from other tenants.
6. Property-based tests (hypothesis) over random users and queries never violate the above.

## Milestones

- [ ] 1. Skeleton: FastAPI, Docker Compose, Postgres + pgvector, Alembic, CI
- [ ] 2. Schema + RLS + `rag_app` role (leak tests 1 and 4)
- [ ] 3. Auth (JWT) + admin API + ACL sync trigger (leak test 3)
- [ ] 4. Ingest: parse, chunk, embed, store
- [ ] 5. Query: retrieve + generate + audit log (leak tests 2, 5, 6)
- [ ] 6. Observability + per-tenant rate limiting
- [ ] 7. Benchmarks and final docs

## Getting started

_To be filled in as milestone 1 lands._

## License

TBD
