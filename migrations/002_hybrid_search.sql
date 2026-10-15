-- Keyword (full-text) search next to vector search.
-- `tsv` is GENERATED: Postgres computes it from `content` on every insert/update, so the app never
-- has to remember to fill it. 'english' lowercases, drops stopwords and stems ("salaries" -> "salari").
ALTER TABLE chunks
    ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED;

-- GIN = an "index card per word" structure, so @@ word matching doesn't scan every chunk.
CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv);

-- No new policy needed: RLS policies are per row, so keyword search is filtered exactly like vector search.
