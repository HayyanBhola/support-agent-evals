-- GlowCare Database Schema (FICTIONAL DEMO DATABASE)
-- This schema holds everything the agent must NEVER guess at:
-- prices, stock, order status. RAG never touches this data.

CREATE TABLE IF NOT EXISTS customers (
    customer_id     SERIAL PRIMARY KEY,
    name            VARCHAR(150) NOT NULL,
    email           VARCHAR(150) UNIQUE NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS products (
    product_id      VARCHAR(20) PRIMARY KEY,   -- e.g. GC-P001, matches products.json
    name            VARCHAR(150) NOT NULL,
    description     TEXT,
    price           NUMERIC(10, 2) NOT NULL,
    currency        VARCHAR(5) NOT NULL DEFAULT 'USD',
    stock_quantity  INTEGER NOT NULL DEFAULT 0,
    availability    VARCHAR(20) NOT NULL DEFAULT 'in_stock', -- in_stock | out_of_stock
    ingredients     TEXT[] NOT NULL DEFAULT '{}',  -- full list, added in Phase 5 (migration 001)
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

-- Inventory is intentionally NOT a separate table for this MVP.
-- stock_quantity + availability on products IS the inventory system.
-- If a client later needs multi-warehouse tracking, split this out then.

CREATE TABLE IF NOT EXISTS orders (
    order_id        VARCHAR(20) PRIMARY KEY,   -- e.g. GC10241
    customer_id     INTEGER NOT NULL REFERENCES customers(customer_id),
    status          VARCHAR(30) NOT NULL,      -- placed | processing | shipped | delivered | cancelled
    total_amount    NUMERIC(10, 2) NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS order_items (
    order_item_id       SERIAL PRIMARY KEY,
    order_id            VARCHAR(20) NOT NULL REFERENCES orders(order_id),
    product_id          VARCHAR(20) NOT NULL REFERENCES products(product_id),
    quantity            INTEGER NOT NULL,
    price_at_purchase   NUMERIC(10, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    conversation_id     SERIAL PRIMARY KEY,
    customer_id         INTEGER REFERENCES customers(customer_id), -- nullable: anonymous chats allowed
    started_at          TIMESTAMP NOT NULL DEFAULT NOW(),
    status               VARCHAR(20) NOT NULL DEFAULT 'open', -- open | closed | escalated
    channel              VARCHAR(20) NOT NULL DEFAULT 'web',  -- web | demo | api | n8n | (whatsapp later); Phase 9
    external_id          VARCHAR(100)                         -- sender id on an outside channel; Phase 11
);

CREATE TABLE IF NOT EXISTS messages (
    message_id          SERIAL PRIMARY KEY,
    conversation_id      INTEGER NOT NULL REFERENCES conversations(conversation_id),
    sender                VARCHAR(10) NOT NULL,  -- customer | assistant
    content                TEXT NOT NULL,
    intent                 VARCHAR(40),            -- the route chosen (Phase 5 routing)
    created_at             TIMESTAMP NOT NULL DEFAULT NOW(),
    -- added in Phase 8 (migration 003):
    source                 VARCHAR(20),            -- database | knowledge_base | fixed_reply
    answer_type            VARCHAR(20),            -- llm | quoted_source | not_found
    details                JSONB,                  -- audit trail: route, facts/chunks, checker result
    escalation_id          INTEGER                 -- FK added after escalations exists (below)
);

CREATE TABLE IF NOT EXISTS escalations (
    escalation_id        SERIAL PRIMARY KEY,
    conversation_id       INTEGER NOT NULL REFERENCES conversations(conversation_id),
    reason                  TEXT NOT NULL,
    status                   VARCHAR(20) NOT NULL DEFAULT 'pending', -- pending | in_progress | resolved
    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    -- added in Phase 7 (migration 002):
    priority                 VARCHAR(10) NOT NULL DEFAULT 'normal', -- high | normal | low
    customer_message         TEXT,
    staff_notes              TEXT,
    resolved_at              TIMESTAMP,
    -- added in Phase 11 (migration 005): n8n alerts
    alerted_at               TIMESTAMP,
    alerted_priority         VARCHAR(10),
    reminded_at              TIMESTAMP
);

-- Phase 8 (migration 003): messages.escalation_id -> escalations, and a
-- fast "recent messages of a chat" lookup.
DO $$ BEGIN
    ALTER TABLE messages ADD CONSTRAINT messages_escalation_id_fkey
        FOREIGN KEY (escalation_id) REFERENCES escalations(escalation_id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages (conversation_id, message_id);

-- Phase 9 (migration 004): dashboard filters by date and channel.
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages (created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_channel ON conversations (channel);

-- Phase 11 (migration 005): outside channels (n8n, later WhatsApp).
CREATE INDEX IF NOT EXISTS idx_conversations_external ON conversations (channel, external_id);
