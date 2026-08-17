---
name: persistence-change
description: Scope SQLite schema, durable job/selection/receipt semantics, migrations, reconciliation, analytics, idempotency, or Redis versus durable-state changes. Use before modifying persistence contracts or stored data.
---

# Persistence change

Use `graphify explain "SqliteJobRepository"` and a bounded query for reverse dependencies, ports,
workers, handlers, analytics, and tests. Inspect `application/ports/*repository.py`,
`infrastructure/persistence/`, SQLite integration tests, and affected worker/queue tests. Read T009,
the persistence/recovery architecture sections, and ADR-007, ADR-008, ADR-017, ADR-023.

Treat SQLite/WAL as durable truth and Redis/ARQ as transient. Preserve backward-readable rows and
non-destructive migrations, atomic transition/usage semantics, cancellation authority, delivery
uncertainty, stable URL-free media identities, concurrency behavior, and restart idempotency. Add
migration and rollback/recovery coverage; never solve a schema change by deleting operator state.

