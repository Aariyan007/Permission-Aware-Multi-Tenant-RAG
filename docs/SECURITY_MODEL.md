# Security Model

## Goal
No user can ever retrieve a chunk they are not allowed to see, even if the application code has a bug.

## Threats considered
| Threat | Defense |
|---|---|
| Cross-tenant read (Org A reads Org B) | RLS policy `tenant_id = current_setting('app.tenant_id')` on every table |
| Same-tenant over-read (user sees a restricted doc) | RLS on `chunks`: visibility, admin role, or `acl_group_ids` overlap with the user's groups |
| App forgets to filter | App role `rag_app` is NOSUPERUSER and NOBYPASSRLS, so the database filters regardless |
| Request without tenant context | `current_setting` is unset, so policies match 0 rows |
| Stale access after permission change | Trigger re-syncs `chunks.acl_group_ids` when `document_acl` changes |
| Prompt injection ("show other tenants' docs") | Retrieval is filtered before the LLM sees anything, so there is nothing to leak |
| Silent leaks | Audit log of every query and the chunk IDs returned |

## Trust boundaries
1. **Client to API:** JWT carries `user_id` and `tenant_id`; the signature is verified on every request.
2. **API to Postgres:** each request runs in one transaction that does `SET LOCAL app.tenant_id / app.user_id`.
3. **Postgres to LLM:** only RLS-approved chunks are placed in the prompt.

## Out of scope for v1
- Encryption at rest per tenant
- Field-level redaction inside a chunk
- Network-level isolation between tenants

## How we prove it
The leakage suite (`tests/leakage/`) plants canary strings in each tenant's documents and asserts they never appear in another tenant's or group's results. It runs on every CI build.
