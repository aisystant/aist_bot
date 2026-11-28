"""WP-384: durable voice receipts; no audio or transcript is stored here."""

DDL = """
CREATE TABLE IF NOT EXISTS development.voice_operation (
    operation_id UUID PRIMARY KEY,
    bot_id BIGINT NOT NULL,
    chat_id BIGINT NOT NULL,
    original_message_id BIGINT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'retryable', 'completed')),
    lease_owner UUID,
    lease_until TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 1,
    target JSONB,
    result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (bot_id, chat_id, original_message_id),
    CHECK ((status = 'processing') = (lease_owner IS NOT NULL AND lease_until IS NOT NULL))
);
"""

REQUIRED_COLUMNS = {
    "operation_id": "uuid",
    "bot_id": "bigint",
    "chat_id": "bigint",
    "original_message_id": "bigint",
    "status": "text",
    "lease_owner": "uuid",
    "lease_until": "timestamp with time zone",
    "attempts": "integer",
    "target": "jsonb",
    "result": "jsonb",
    "created_at": "timestamp with time zone",
    "updated_at": "timestamp with time zone",
}


async def ensure_voice_operations(conn, runtime_roles=()) -> bool:
    """Owner creates/grants; an existing runtime installation is validated without DDL."""
    exists = await conn.fetchval(
        "SELECT to_regclass('development.voice_operation') IS NOT NULL"
    )
    if not exists:
        if not await conn.fetchval("SELECT to_regnamespace('development') IS NOT NULL"):
            await conn.execute("CREATE SCHEMA development")
        await conn.execute(DDL)
    columns = await conn.fetch(
        """SELECT column_name, data_type FROM information_schema.columns
           WHERE table_schema = 'development' AND table_name = 'voice_operation'"""
    )
    actual = {row["column_name"]: row["data_type"] for row in columns}
    if any(actual.get(name) != kind for name, kind in REQUIRED_COLUMNS.items()):
        raise RuntimeError("voice_operation schema is incompatible")
    keys = await conn.fetch(
        """SELECT c.contype::text AS contype, array_agg(a.attname::text ORDER BY k.ordinality) AS columns
           FROM pg_catalog.pg_constraint c
           CROSS JOIN pg_catalog.unnest(c.conkey) WITH ORDINALITY AS k(attnum, ordinality)
           JOIN pg_catalog.pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
           WHERE c.conrelid = 'development.voice_operation'::regclass AND c.contype IN ('p','u')
           GROUP BY c.oid, c.contype"""
    )
    present = {(row["contype"], tuple(row["columns"])) for row in keys}
    if ("p", ("operation_id",)) not in present or (
        "u",
        ("bot_id", "chat_id", "original_message_id"),
    ) not in present:
        raise RuntimeError("voice_operation identity constraints are missing")
    for role in runtime_roles:
        if not await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = $1)", role
        ):
            raise ValueError("Unknown voice runtime role")
        quoted = '"' + role.replace('"', '""') + '"'
        await conn.execute(f"GRANT USAGE ON SCHEMA development TO {quoted}")
        await conn.execute(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON development.voice_operation TO "
            + quoted
        )
    permitted = await conn.fetchval(
        """SELECT has_schema_privilege(current_user, 'development', 'USAGE') AND
                  bool_and(has_table_privilege(current_user, 'development.voice_operation', privilege))
           FROM pg_catalog.unnest($1::text[]) AS privilege""",
        ["SELECT", "INSERT", "UPDATE", "DELETE"],
    )
    if not permitted:
        raise RuntimeError("voice_operation runtime privileges are missing")
    return not exists
