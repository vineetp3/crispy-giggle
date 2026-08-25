-- Idempotent schema. Re-running must be a no-op.
-- Price and inventory are deliberately absent: they are read live at query time.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS stores (
    id                 serial PRIMARY KEY,
    slug               text UNIQUE NOT NULL,
    domain             text NOT NULL,
    admin_api_version  text,
    coverage_pct       numeric,
    first_ingested_at  timestamptz NOT NULL DEFAULT now(),
    last_ingested_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    id                  bigserial PRIMARY KEY,
    store_id            integer NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    shopify_product_id  text NOT NULL,
    handle              text NOT NULL,
    title               text,
    vendor              text,
    product_type        text,
    status              text,
    tags                text[] NOT NULL DEFAULT '{}',
    online_store_url    text,
    template_suffix     text,
    collection_handles  text[] NOT NULL DEFAULT '{}',
    updated_at          timestamptz,
    UNIQUE (store_id, shopify_product_id)
);

CREATE INDEX IF NOT EXISTS products_store_handle_idx ON products (store_id, handle);

-- Identity only. No price, no inventory_quantity, by design.
CREATE TABLE IF NOT EXISTS variants (
    id                  bigserial PRIMARY KEY,
    product_id          bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    shopify_variant_id  text NOT NULL,
    title               text,
    sku                 text,
    selected_options    jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (product_id, shopify_variant_id)
);

-- One row per (product, field, source). Provenance is the point.
CREATE TABLE IF NOT EXISTS field_assertions (
    id                 bigserial PRIMARY KEY,
    product_id         bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    field              text NOT NULL,
    label              text,
    value              text NOT NULL,
    source             text NOT NULL,
    source_kind        text NOT NULL
        CHECK (source_kind IN ('metafield', 'theme', 'description', 'api')),
    rendered           boolean NOT NULL DEFAULT false,
    trust_class        text NOT NULL
        CHECK (trust_class IN ('retrieval', 'quotable')),
    observed_at        timestamptz NOT NULL DEFAULT now(),
    source_updated_at  timestamptz,
    value_hash         text NOT NULL,
    UNIQUE (product_id, field, source)
);

CREATE INDEX IF NOT EXISTS field_assertions_product_idx ON field_assertions (product_id);
CREATE INDEX IF NOT EXISTS field_assertions_trust_idx ON field_assertions (trust_class);

CREATE TABLE IF NOT EXISTS template_constants (
    id               bigserial PRIMARY KEY,
    store_id         integer NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    template_key     text NOT NULL,
    value            text NOT NULL,
    label            text,
    value_hash       text NOT NULL,
    observed_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (store_id, template_key, value_hash)
);

-- trust_class lives on the chunk, not only on field_assertions: `text` is what an
-- answer layer receives, so the class has to travel with it.
CREATE TABLE IF NOT EXISTS documents (
    id           bigserial PRIMARY KEY,
    product_id   bigint NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    chunk_key    text NOT NULL DEFAULT 'main',
    trust_class  text NOT NULL DEFAULT 'retrieval'
        CHECK (trust_class IN ('retrieval', 'quotable')),
    text         text NOT NULL,
    text_hash    text NOT NULL,
    embedding    vector(1024),
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    UNIQUE (product_id, chunk_key)
);

ALTER TABLE documents ADD COLUMN IF NOT EXISTS trust_class text
    NOT NULL DEFAULT 'retrieval';

CREATE INDEX IF NOT EXISTS documents_tsv_idx ON documents USING gin (tsv);
-- Operator class is chosen after measuring whether OpenAI returns unit-normalised
-- vectors. Cosine is correct either way; inner product would be cheaper if normalised.
CREATE INDEX IF NOT EXISTS documents_embedding_idx
    ON documents USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS edges (
    id         bigserial PRIMARY KEY,
    store_id   integer NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    from_type  text NOT NULL,
    from_id    text NOT NULL,
    relation   text NOT NULL,
    to_type    text NOT NULL,
    to_id      text NOT NULL,
    source     text NOT NULL,
    UNIQUE (store_id, from_type, from_id, relation, to_type, to_id)
);

CREATE INDEX IF NOT EXISTS edges_from_idx ON edges (store_id, from_type, from_id);

CREATE TABLE IF NOT EXISTS rejected_keys (
    id           bigserial PRIMARY KEY,
    store_id     integer NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    namespace    text NOT NULL,
    key          text NOT NULL,
    reason_code  text NOT NULL,
    detail       text,
    UNIQUE (store_id, namespace, key)
);
