# Responses semantic cache persistence

The deterministic Responses semantic cache remains disabled unless
`WORKAMA_RESPONSES_SEMANTIC_CACHE` and the existing semantic-cache workspace
allowlist are enabled. Optional pgvector persistence is additionally gated by:

- `WORKAMA_RESPONSES_SEMANTIC_CACHE_PGVECTOR=true`
- `WORKAMA_RESPONSES_SEMANTIC_CACHE_PGVECTOR_WORKSPACES=ws_a,ws_b`

The Gateway exposes `SetResponseSemanticCacheRepository` as a pluggable
injection point. The repository contract is deliberately driver-neutral;
`ResponseSemanticCacheSQLExecutor` is available for an adapter backed by a
PostgreSQL driver supplied by the deployment. This repository is not wired by
the current Gateway binary because its module does not ship a reliable
PostgreSQL driver.

Repository errors, timeouts, missing tables, and missing pgvector are ignored
for request correctness. Lookup then falls back to the existing exact and
deterministic in-memory cosine candidate path. Writes are best-effort. The
repository is only used for eligible Responses requests: temperature must be
exactly zero, tools and side effects are absent, and data classification is
C0-C2. Scope matching retains workspace, model, provider/channel, capability,
prompt id/version/checksum, guard policy, output signature, and region.

Migration `032_gateway_response_semantic_cache_pgvector.sql` creates the
optional `gw_response_semantic_cache` table and a cosine index when pgvector is
available. It intentionally skips the table when the extension cannot be
installed. The current boundary does not include production connection wiring,
cache invalidation events, or repository metrics; these must be supplied before
enabling the feature for production workspaces.
