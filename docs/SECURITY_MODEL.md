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

## Limits of the model
- **The database trusts the API to set the caller's identity.** The API derives `app.tenant_id` / `app.user_id` from a signature-verified JWT in exactly one place (`app/db.py: tenant_tx`). RLS protects against *query bugs* (a missing filter, a wrong join). It does not protect against a compromised API process that sets someone else's identity.
- **Two layers on retrieval.** `/query` joins `documents`, which has its own RLS policy, in addition to the `chunks` policy. Raw-SQL tests pin the `chunks` policy on its own.
- **Role changes use the database's view of the role.** The JWT role claim is advisory; policies call `app_role()`, which reads the current role from `users`.
- **Admins are tenant-wide trusted.** They see every document in their tenant by design.

## Out of scope for v1
- Encryption at rest per tenant
- Field-level redaction inside a chunk
- Network-level isolation between tenants

## How we prove it
The leakage suite (`tests/leakage/`) plants canary strings in each tenant's documents and asserts they never appear in another tenant's or group's results. It runs on every CI build.
