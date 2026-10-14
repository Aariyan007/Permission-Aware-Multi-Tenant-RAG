-- Schema + Row-Level Security. Runs as the DB owner (superuser); the API connects as `rag_app`.
CREATE EXTENSION IF NOT EXISTS vector;

-- ───────────── tables ─────────────
CREATE TABLE tenants (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    email         text NOT NULL UNIQUE,
    password_hash text NOT NULL,
    role          text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id)
);

CREATE TABLE groups (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name      text NOT NULL,
    UNIQUE (tenant_id, name),
    UNIQUE (id, tenant_id)
);

-- Composite FKs (…, tenant_id) make cross-tenant references impossible even for a buggy writer.
CREATE TABLE user_groups (
    tenant_id uuid NOT NULL,
    user_id   uuid NOT NULL,
    group_id  uuid NOT NULL,
    PRIMARY KEY (user_id, group_id),
    FOREIGN KEY (user_id,  tenant_id) REFERENCES users(id,  tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (group_id, tenant_id) REFERENCES groups(id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE documents (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title      text NOT NULL,
    source     text,
    created_by uuid NOT NULL,
    visibility text NOT NULL DEFAULT 'tenant' CHECK (visibility IN ('tenant', 'restricted')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, tenant_id),
    FOREIGN KEY (created_by, tenant_id) REFERENCES users(id, tenant_id)
);

CREATE TABLE document_acl (
    tenant_id   uuid NOT NULL,
    document_id uuid NOT NULL,
    group_id    uuid NOT NULL,
    PRIMARY KEY (document_id, group_id),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE,
    FOREIGN KEY (group_id,    tenant_id) REFERENCES groups(id,    tenant_id) ON DELETE CASCADE
);

-- ACL is denormalized onto each chunk so the vector search needs no joins.
CREATE TABLE chunks (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid NOT NULL,
    document_id   uuid NOT NULL,
    owner_id      uuid NOT NULL,
    chunk_index   int  NOT NULL,
    content       text NOT NULL,
    embedding     vector(256) NOT NULL,
    restricted    boolean NOT NULL DEFAULT false,
    acl_group_ids uuid[] NOT NULL DEFAULT '{}',
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(id, tenant_id) ON DELETE CASCADE
);
CREATE INDEX chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_acl_gin        ON chunks USING gin (acl_group_ids);
CREATE INDEX chunks_tenant_idx     ON chunks (tenant_id);
CREATE INDEX chunks_document_idx   ON chunks (document_id);

CREATE TABLE audit_log (
    id        bigserial PRIMARY KEY,
    tenant_id uuid NOT NULL,
    user_id   uuid NOT NULL,
    action    text NOT NULL,
    query     text,
    chunk_ids uuid[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ───────────── identity helpers (read the per-transaction settings) ─────────────
CREATE FUNCTION app_tenant() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

CREATE FUNCTION app_user() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

-- SECURITY DEFINER = runs as the owner, so these lookups are not themselves blocked by RLS.
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT role FROM users WHERE id = app_user() AND tenant_id = app_tenant() $$;

-- Ownership check as DEFINER: lets document_acl policies consult documents without an RLS cycle.
CREATE FUNCTION app_owns_document(doc uuid) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT EXISTS (SELECT 1 FROM documents WHERE id = doc AND tenant_id = app_tenant() AND created_by = app_user()) $$;

CREATE FUNCTION app_groups() RETURNS uuid[] LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT coalesce(array_agg(group_id), '{}') FROM user_groups
   WHERE user_id = app_user() AND tenant_id = app_tenant() $$;

-- ───────────── Row-Level Security ─────────────
ALTER TABLE tenants       ENABLE ROW LEVEL SECURITY;
ALTER TABLE users         ENABLE ROW LEVEL SECURITY;
ALTER TABLE groups        ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_groups   ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents     ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_acl  ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks        ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log     ENABLE ROW LEVEL SECURITY;

CREATE POLICY tenants_sel ON tenants FOR SELECT USING (id = (SELECT app_tenant()));

-- admin-only tables: everyone in the tenant can read, only admins can write
CREATE POLICY users_sel ON users FOR SELECT USING (tenant_id = (SELECT app_tenant()));
CREATE POLICY users_ins ON users FOR INSERT WITH CHECK (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');
CREATE POLICY users_upd ON users FOR UPDATE USING (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');
CREATE POLICY users_del ON users FOR DELETE USING (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');

CREATE POLICY groups_sel ON groups FOR SELECT USING (tenant_id = (SELECT app_tenant()));
CREATE POLICY groups_ins ON groups FOR INSERT WITH CHECK (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');
CREATE POLICY groups_del ON groups FOR DELETE USING (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');

CREATE POLICY ug_sel ON user_groups FOR SELECT USING (tenant_id = (SELECT app_tenant()));
CREATE POLICY ug_ins ON user_groups FOR INSERT WITH CHECK (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');
CREATE POLICY ug_del ON user_groups FOR DELETE USING (tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');

-- documents: visible if tenant-wide, or you are admin / the uploader / in an ACL group
CREATE POLICY docs_sel ON documents FOR SELECT USING (
    tenant_id = (SELECT app_tenant()) AND (
        visibility = 'tenant'
        OR (SELECT app_role()) = 'admin'
        OR created_by = (SELECT app_user())
        OR EXISTS (SELECT 1 FROM document_acl a
                   WHERE a.document_id = documents.id AND a.group_id = ANY ((SELECT app_groups())::uuid[]))
    ));
CREATE POLICY docs_ins ON documents FOR INSERT WITH CHECK (
    tenant_id = (SELECT app_tenant()) AND created_by = (SELECT app_user())
    AND (SELECT app_role()) IN ('admin', 'member'));
CREATE POLICY docs_upd ON documents FOR UPDATE USING (
    tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');
CREATE POLICY docs_del ON documents FOR DELETE USING (
    tenant_id = (SELECT app_tenant())
    AND ((SELECT app_role()) = 'admin' OR created_by = (SELECT app_user())));

-- ACL rows: admins manage any; a member may attach only groups they belong to, on their own doc
CREATE POLICY acl_sel ON document_acl FOR SELECT USING (tenant_id = (SELECT app_tenant()));
CREATE POLICY acl_ins ON document_acl FOR INSERT WITH CHECK (
    tenant_id = (SELECT app_tenant()) AND (
        (SELECT app_role()) = 'admin'
        OR ((SELECT app_role()) = 'member' AND group_id = ANY ((SELECT app_groups())::uuid[])
            AND app_owns_document(document_id))
    ));
CREATE POLICY acl_del ON document_acl FOR DELETE USING (
    tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');

-- THE core policy. No joins: tenant + restricted flag + owner + group overlap, all on the row itself.
CREATE POLICY chunks_sel ON chunks FOR SELECT USING (
    tenant_id = (SELECT app_tenant()) AND (
        NOT restricted
        OR (SELECT app_role()) = 'admin'
        OR owner_id = (SELECT app_user())
        OR acl_group_ids && (SELECT app_groups())
    ));
CREATE POLICY chunks_ins ON chunks FOR INSERT WITH CHECK (
    tenant_id = (SELECT app_tenant()) AND owner_id = (SELECT app_user())
    AND (SELECT app_role()) IN ('admin', 'member'));

CREATE POLICY audit_ins ON audit_log FOR INSERT WITH CHECK (
    tenant_id = (SELECT app_tenant()) AND user_id = (SELECT app_user()));
CREATE POLICY audit_sel ON audit_log FOR SELECT USING (
    tenant_id = (SELECT app_tenant()) AND (SELECT app_role()) = 'admin');

-- ───────────── keep chunk ACLs in sync ─────────────
CREATE FUNCTION trg_sync_chunk_acl() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE doc uuid := coalesce(NEW.document_id, OLD.document_id);
BEGIN
    UPDATE chunks SET acl_group_ids =
        coalesce((SELECT array_agg(group_id) FROM document_acl WHERE document_id = doc), '{}')
    WHERE document_id = doc;
    RETURN NULL;
END $$;
CREATE TRIGGER document_acl_sync AFTER INSERT OR DELETE ON document_acl
    FOR EACH ROW EXECUTE FUNCTION trg_sync_chunk_acl();

CREATE FUNCTION trg_sync_chunk_visibility() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    UPDATE chunks SET restricted = (NEW.visibility = 'restricted') WHERE document_id = NEW.id;
    RETURN NULL;
END $$;
CREATE TRIGGER documents_visibility_sync AFTER UPDATE OF visibility ON documents
    FOR EACH ROW EXECUTE FUNCTION trg_sync_chunk_visibility();

-- ───────────── pre-auth entry points (no tenant context exists yet) ─────────────
CREATE FUNCTION create_tenant(p_name text, p_email text, p_password_hash text)
RETURNS TABLE (tenant_id uuid, user_id uuid) LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE t uuid; u uuid;
BEGIN
    INSERT INTO tenants (name) VALUES (p_name) RETURNING id INTO t;
    INSERT INTO users (tenant_id, email, password_hash, role) VALUES (t, p_email, p_password_hash, 'admin') RETURNING id INTO u;
    RETURN QUERY SELECT t, u;
END $$;

CREATE FUNCTION auth_lookup(p_email text)
RETURNS TABLE (user_id uuid, tenant_id uuid, role text, password_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS
$$ SELECT id, users.tenant_id, users.role, users.password_hash FROM users WHERE email = p_email $$;

REVOKE EXECUTE ON FUNCTION trg_sync_chunk_acl(), trg_sync_chunk_visibility() FROM PUBLIC;

-- ───────────── grants for the application role ─────────────
GRANT USAGE ON SCHEMA public TO rag_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO rag_app;
-- the API must never be able to read password hashes
REVOKE SELECT ON users FROM rag_app;
GRANT SELECT (id, tenant_id, email, role, created_at) ON users TO rag_app;
