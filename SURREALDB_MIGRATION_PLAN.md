# SurrealDB Migration Plan — Graphiti Knowledge Graph

> **Objective:** Add SurrealDB as a graph database backend to Graphiti, alongside the existing Neo4j, FalkorDB, Kuzu, and Neptune drivers.

---

## Table of Contents

1. [Architecture Approach](#architecture-approach)
2. [SurrealQL Key Differences from Cypher](#surrealql-key-differences)
3. [Schema Design](#schema-design)
4. [Migration Inventory](#migration-inventory)
5. [Query Translation Reference](#query-translation-reference)
6. [Implementation Plan](#implementation-plan)
7. [Critical Gotchas](#critical-gotchas)
8. [Testing Strategy](#testing-strategy)

---

## Architecture Approach

**Strategy: Add SurrealDB as a new provider, following the existing multi-driver pattern.**

The codebase already has a `GraphProvider` enum and provider-conditional query builders. We add `SURREALDB` as a new provider and implement:

1. **`surrealdb_driver.py`** — New driver class implementing `GraphDriver` abstract base class
2. **Provider branches in query builders** — Add `GraphProvider.SURREALDB` cases to:
   - `graphiti_core/models/nodes/node_db_queries.py`
   - `graphiti_core/models/edges/edge_db_queries.py`
   - `graphiti_core/graph_queries.py`
3. **Provider branches in search** — Add SurrealQL search queries to `search_utils.py`
4. **Provider branches in models** — Add result parsing in `nodes.py`, `edges.py`

**Why add vs rewrite:** The LLM extraction pipeline (70% of codebase) is untouched. Only the database layer (~30%) needs SurrealQL translations.

---

## SurrealQL Key Differences

| Concept | Cypher (Neo4j/Kuzu) | SurrealQL |
|---------|---------------------|-----------|
| Record ID | Internal, uuid property | `table:id` (e.g., `entity:abc123`) |
| Node create | `CREATE (n:Label {props})` | `CREATE entity:id CONTENT {...}` |
| Node upsert | `MERGE (n {uuid: $id}) SET ...` | `UPSERT entity:id CONTENT {...}` |
| Edge create | `(a)-[r:TYPE]->(b)` | `RELATE entity:a->relates_to->entity:b SET ...` |
| Edge upsert | `MERGE (a)-[r:TYPE]->(b) SET ...` | `INSERT RELATION INTO relates_to {...} ON DUPLICATE KEY UPDATE ...` |
| Edge source/target | Implicit in pattern | `in` / `out` fields on edge record |
| Traversal | `MATCH (n)-[:TYPE*1..3]->(m)` | `n.{1..3}(->type->table)` |
| Fulltext search | `CALL db.index.fulltext.queryNodes(...)` | `WHERE field @0@ 'query'` + `search::score(0)` |
| Vector similarity | `vector.similarity.cosine(v1, v2)` | `vector::similarity::cosine(v1, v2)` |
| Vector KNN | N/A (manual cosine) | `WHERE field <\|K\|> $vector` (HNSW index) |
| Null | `null` | `NONE` (field absent) vs `NULL` (field exists, empty) |
| Datetime literal | `datetime('2024-01-15')` | `d'2024-01-15T00:00:00Z'` |
| Parameter syntax | `$param` | `$param` (same) |
| Labels | `n:Entity:Person` | Separate tables or label field |
| Transaction | Implicit | `BEGIN TRANSACTION; ... COMMIT;` |

### Critical Gotcha: Labels

Cypher uses multiple labels on a single node (`n:Entity:Person`). SurrealDB uses a **single table per record**. We handle this by:
- All entities live in the `entity` table
- Custom type labels stored in a `labels` array field
- Filter by label: `WHERE labels CONTAINS 'Person'`

### Critical Gotcha: Edge Upsert

`UPSERT` does NOT work on relation/edge tables in SurrealDB. We use:
```sql
INSERT RELATION INTO relates_to {
    id: relates_to:$uuid,
    in: entity:$source_uuid,
    out: entity:$target_uuid,
    ...properties
} ON DUPLICATE KEY UPDATE
    name = $input.name, fact = $input.fact, ...
```

---

## Schema Design

### Tables

```sql
-- Nodes
DEFINE TABLE entity SCHEMAFULL;
DEFINE TABLE episodic SCHEMAFULL;
DEFINE TABLE community SCHEMAFULL;
DEFINE TABLE saga SCHEMAFULL;

-- Edges (created via RELATE)
DEFINE TABLE relates_to SCHEMAFULL TYPE RELATION IN entity OUT entity;
DEFINE TABLE mentions SCHEMAFULL TYPE RELATION IN episodic OUT entity;
DEFINE TABLE has_member SCHEMAFULL TYPE RELATION IN community OUT entity | community;
DEFINE TABLE has_episode SCHEMAFULL TYPE RELATION IN saga OUT episodic;
DEFINE TABLE next_episode SCHEMAFULL TYPE RELATION IN episodic OUT episodic;
```

### Entity Node Fields

```sql
DEFINE FIELD uuid ON entity TYPE string;
DEFINE FIELD name ON entity TYPE string;
DEFINE FIELD group_id ON entity TYPE string;
DEFINE FIELD labels ON entity TYPE array<string>;
DEFINE FIELD summary ON entity TYPE string DEFAULT '';
DEFINE FIELD created_at ON entity TYPE datetime;
DEFINE FIELD name_embedding ON entity TYPE option<array<float>>;
-- attributes stored as flexible object fields directly on the record
```

### Episodic Node Fields

```sql
DEFINE FIELD uuid ON episodic TYPE string;
DEFINE FIELD name ON episodic TYPE string;
DEFINE FIELD group_id ON episodic TYPE string;
DEFINE FIELD labels ON episodic TYPE array<string>;
DEFINE FIELD source ON episodic TYPE string;
DEFINE FIELD source_description ON episodic TYPE string;
DEFINE FIELD content ON episodic TYPE string;
DEFINE FIELD valid_at ON episodic TYPE datetime;
DEFINE FIELD created_at ON episodic TYPE datetime;
DEFINE FIELD entity_edges ON episodic TYPE array<string> DEFAULT [];
```

### Community Node Fields

```sql
DEFINE FIELD uuid ON community TYPE string;
DEFINE FIELD name ON community TYPE string;
DEFINE FIELD group_id ON community TYPE string;
DEFINE FIELD labels ON community TYPE array<string>;
DEFINE FIELD summary ON community TYPE string DEFAULT '';
DEFINE FIELD created_at ON community TYPE datetime;
DEFINE FIELD name_embedding ON community TYPE option<array<float>>;
```

### Saga Node Fields

```sql
DEFINE FIELD uuid ON saga TYPE string;
DEFINE FIELD name ON saga TYPE string;
DEFINE FIELD group_id ON saga TYPE string;
DEFINE FIELD labels ON saga TYPE array<string>;
DEFINE FIELD created_at ON saga TYPE datetime;
```

### Entity Edge (relates_to) Fields

```sql
DEFINE FIELD uuid ON relates_to TYPE string;
DEFINE FIELD group_id ON relates_to TYPE string;
DEFINE FIELD name ON relates_to TYPE string;
DEFINE FIELD fact ON relates_to TYPE string;
DEFINE FIELD episodes ON relates_to TYPE array<string> DEFAULT [];
DEFINE FIELD created_at ON relates_to TYPE datetime;
DEFINE FIELD expired_at ON relates_to TYPE option<datetime>;
DEFINE FIELD valid_at ON relates_to TYPE option<datetime>;
DEFINE FIELD invalid_at ON relates_to TYPE option<datetime>;
DEFINE FIELD fact_embedding ON relates_to TYPE option<array<float>>;
-- attributes stored as flexible object fields
```

### Episodic Edge (mentions) Fields

```sql
DEFINE FIELD uuid ON mentions TYPE string;
DEFINE FIELD group_id ON mentions TYPE string;
DEFINE FIELD created_at ON mentions TYPE datetime;
```

### Other Edge Fields (has_member, has_episode, next_episode)

```sql
-- Same pattern: uuid, group_id, created_at
DEFINE FIELD uuid ON has_member TYPE string;
DEFINE FIELD group_id ON has_member TYPE string;
DEFINE FIELD created_at ON has_member TYPE datetime;

DEFINE FIELD uuid ON has_episode TYPE string;
DEFINE FIELD group_id ON has_episode TYPE string;
DEFINE FIELD created_at ON has_episode TYPE datetime;

DEFINE FIELD uuid ON next_episode TYPE string;
DEFINE FIELD group_id ON next_episode TYPE string;
DEFINE FIELD created_at ON next_episode TYPE datetime;
```

### Indexes

```sql
-- Range indexes
DEFINE INDEX entity_uuid ON entity FIELDS uuid UNIQUE;
DEFINE INDEX entity_group_id ON entity FIELDS group_id;
DEFINE INDEX entity_name ON entity FIELDS name;
DEFINE INDEX entity_created_at ON entity FIELDS created_at;

DEFINE INDEX episodic_uuid ON episodic FIELDS uuid UNIQUE;
DEFINE INDEX episodic_group_id ON episodic FIELDS group_id;
DEFINE INDEX episodic_created_at ON episodic FIELDS created_at;
DEFINE INDEX episodic_valid_at ON episodic FIELDS valid_at;

DEFINE INDEX community_uuid ON community FIELDS uuid UNIQUE;
DEFINE INDEX community_group_id ON community FIELDS group_id;

DEFINE INDEX saga_uuid ON saga FIELDS uuid UNIQUE;
DEFINE INDEX saga_group_id ON saga FIELDS group_id;
DEFINE INDEX saga_name ON saga FIELDS name;

DEFINE INDEX relates_to_uuid ON relates_to FIELDS uuid UNIQUE;
DEFINE INDEX relates_to_group_id ON relates_to FIELDS group_id;
DEFINE INDEX relates_to_name ON relates_to FIELDS name;
DEFINE INDEX relates_to_created_at ON relates_to FIELDS created_at;
DEFINE INDEX relates_to_expired_at ON relates_to FIELDS expired_at;
DEFINE INDEX relates_to_valid_at ON relates_to FIELDS valid_at;
DEFINE INDEX relates_to_invalid_at ON relates_to FIELDS invalid_at;

DEFINE INDEX mentions_uuid ON mentions FIELDS uuid UNIQUE;
DEFINE INDEX mentions_group_id ON mentions FIELDS group_id;

DEFINE INDEX has_member_uuid ON has_member FIELDS uuid UNIQUE;
DEFINE INDEX has_episode_uuid ON has_episode FIELDS uuid UNIQUE;
DEFINE INDEX has_episode_group_id ON has_episode FIELDS group_id;
DEFINE INDEX next_episode_uuid ON next_episode FIELDS uuid UNIQUE;
DEFINE INDEX next_episode_group_id ON next_episode FIELDS group_id;

-- Fulltext indexes (BM25)
DEFINE ANALYZER graphiti_analyzer TOKENIZERS class, blank FILTERS lowercase, ascii, snowball(english);

DEFINE INDEX episode_content_ft ON episodic FIELDS content
    SEARCH ANALYZER graphiti_analyzer BM25;
DEFINE INDEX episode_source_ft ON episodic FIELDS source
    SEARCH ANALYZER graphiti_analyzer BM25;
DEFINE INDEX episode_source_desc_ft ON episodic FIELDS source_description
    SEARCH ANALYZER graphiti_analyzer BM25;

DEFINE INDEX entity_name_ft ON entity FIELDS name
    SEARCH ANALYZER graphiti_analyzer BM25;
DEFINE INDEX entity_summary_ft ON entity FIELDS summary
    SEARCH ANALYZER graphiti_analyzer BM25;

DEFINE INDEX community_name_ft ON community FIELDS name
    SEARCH ANALYZER graphiti_analyzer BM25;

DEFINE INDEX relates_to_name_ft ON relates_to FIELDS name
    SEARCH ANALYZER graphiti_analyzer BM25;
DEFINE INDEX relates_to_fact_ft ON relates_to FIELDS fact
    SEARCH ANALYZER graphiti_analyzer BM25;

-- Vector indexes (HNSW)
DEFINE INDEX entity_name_embedding_hnsw ON entity FIELDS name_embedding
    HNSW DIMENSION 1536 DIST COSINE TYPE F32;
DEFINE INDEX community_name_embedding_hnsw ON community FIELDS name_embedding
    HNSW DIMENSION 1536 DIST COSINE TYPE F32;
DEFINE INDEX relates_to_fact_embedding_hnsw ON relates_to FIELDS fact_embedding
    HNSW DIMENSION 1536 DIST COSINE TYPE F32;
```

---

## Migration Inventory

### Files to Create (1)

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `graphiti_core/driver/surrealdb_driver.py` | Driver class + session | ~300 |

### Files to Modify (12)

| File | Changes | Scope |
|------|---------|-------|
| `graphiti_core/driver/driver.py` | Add `SURREALDB` to `GraphProvider` enum | 1 line |
| `graphiti_core/driver/__init__.py` | Export new driver | 2 lines |
| `graphiti_core/models/nodes/node_db_queries.py` | Add SURREALDB branches to 6 functions | ~150 lines |
| `graphiti_core/models/edges/edge_db_queries.py` | Add SURREALDB branches to 5 functions | ~120 lines |
| `graphiti_core/graph_queries.py` | Add SURREALDB branches to 5 functions | ~80 lines |
| `graphiti_core/search/search_utils.py` | Add SURREALDB search queries | ~200 lines |
| `graphiti_core/search/search_filters.py` | Add SURREALDB filter builder | ~40 lines |
| `graphiti_core/nodes.py` | Add SURREALDB record parsing | ~30 lines |
| `graphiti_core/edges.py` | Add SURREALDB record parsing | ~30 lines |
| `graphiti_core/utils/bulk_utils.py` | Add SURREALDB bulk handling | ~20 lines |
| `graphiti_core/helpers.py` | Add SURREALDB default group_id | ~5 lines |
| `pyproject.toml` | Add surrealdb dependency | ~3 lines |

### Total Estimated New/Modified Code: ~1,000 lines

---

## Query Translation Reference

### Node Operations

#### Save Entity Node

**Cypher (Neo4j):**
```cypher
MERGE (n:Entity {uuid: $uuid})
SET n = {uuid: $uuid, name: $name, group_id: $group_id, summary: $summary, created_at: $created_at}
SET n:$labels
WITH n
CALL db.create.setNodeVectorProperty(n, "name_embedding", $name_embedding)
RETURN n.uuid AS uuid
```

**SurrealQL:**
```sql
UPSERT entity:$uuid CONTENT {
    uuid: $uuid,
    name: $name,
    group_id: $group_id,
    labels: $labels,
    summary: $summary,
    created_at: $created_at,
    name_embedding: $name_embedding
} RETURN uuid;
```

#### Save Entity Node (Bulk)

**Cypher (Neo4j):**
```cypher
UNWIND $nodes AS node
MERGE (n:Entity {uuid: node.uuid})
SET n = node
SET n:$(node.labels)
```

**SurrealQL:**
```sql
INSERT INTO entity $nodes ON DUPLICATE KEY UPDATE
    name = $input.name,
    group_id = $input.group_id,
    labels = $input.labels,
    summary = $input.summary,
    created_at = $input.created_at,
    name_embedding = $input.name_embedding;
```

#### Save Episodic Node

**Cypher:**
```cypher
MERGE (e:Episodic {uuid: $uuid})
SET e = {uuid: $uuid, name: $name, source: $source, content: $content, ...}
RETURN e.uuid
```

**SurrealQL:**
```sql
UPSERT episodic:$uuid CONTENT {
    uuid: $uuid,
    name: $name,
    group_id: $group_id,
    labels: $labels,
    source: $source,
    source_description: $source_description,
    content: $content,
    entity_edges: $entity_edges,
    created_at: $created_at,
    valid_at: $valid_at
} RETURN uuid;
```

#### Save Community Node

**SurrealQL:**
```sql
UPSERT community:$uuid CONTENT {
    uuid: $uuid,
    name: $name,
    group_id: $group_id,
    labels: $labels,
    summary: $summary,
    created_at: $created_at,
    name_embedding: $name_embedding
} RETURN uuid;
```

#### Save Saga Node

**SurrealQL:**
```sql
UPSERT saga:$uuid CONTENT {
    uuid: $uuid,
    name: $name,
    group_id: $group_id,
    labels: $labels,
    created_at: $created_at
} RETURN uuid;
```

### Edge Operations

#### Save Entity Edge (RELATES_TO)

**Cypher:**
```cypher
MATCH (source:Entity {uuid: $source_uuid})
MATCH (target:Entity {uuid: $target_uuid})
MERGE (source)-[e:RELATES_TO {uuid: $uuid}]->(target)
SET e = {uuid: $uuid, name: $name, fact: $fact, ...}
```

**SurrealQL:**
```sql
INSERT RELATION INTO relates_to {
    id: relates_to:$uuid,
    in: entity:$source_uuid,
    out: entity:$target_uuid,
    uuid: $uuid,
    group_id: $group_id,
    name: $name,
    fact: $fact,
    episodes: $episodes,
    created_at: $created_at,
    expired_at: $expired_at,
    valid_at: $valid_at,
    invalid_at: $invalid_at,
    fact_embedding: $fact_embedding
} ON DUPLICATE KEY UPDATE
    name = $input.name,
    fact = $input.fact,
    group_id = $input.group_id,
    episodes = $input.episodes,
    expired_at = $input.expired_at,
    valid_at = $input.valid_at,
    invalid_at = $input.invalid_at,
    fact_embedding = $input.fact_embedding;
```

#### Save Episodic Edge (MENTIONS)

**Cypher:**
```cypher
MATCH (episode:Episodic {uuid: $episode_uuid})
MATCH (node:Entity {uuid: $entity_uuid})
MERGE (episode)-[e:MENTIONS {uuid: $uuid}]->(node)
SET e.group_id = $group_id, e.created_at = $created_at
```

**SurrealQL:**
```sql
INSERT RELATION INTO mentions {
    id: mentions:$uuid,
    in: episodic:$episode_uuid,
    out: entity:$entity_uuid,
    uuid: $uuid,
    group_id: $group_id,
    created_at: $created_at
} ON DUPLICATE KEY UPDATE
    group_id = $input.group_id,
    created_at = $input.created_at;
```

#### Save Has Episode Edge

**SurrealQL:**
```sql
INSERT RELATION INTO has_episode {
    id: has_episode:$uuid,
    in: saga:$saga_uuid,
    out: episodic:$episode_uuid,
    uuid: $uuid,
    group_id: $group_id,
    created_at: $created_at
} ON DUPLICATE KEY UPDATE
    group_id = $input.group_id;
```

#### Save Next Episode Edge

**SurrealQL:**
```sql
INSERT RELATION INTO next_episode {
    id: next_episode:$uuid,
    in: episodic:$source_uuid,
    out: episodic:$target_uuid,
    uuid: $uuid,
    group_id: $group_id,
    created_at: $created_at
} ON DUPLICATE KEY UPDATE
    group_id = $input.group_id;
```

### Search Operations

#### Fulltext Search — Entities

**Cypher:**
```cypher
CALL db.index.fulltext.queryNodes("node_name_and_summary", $query, {limit: $limit})
YIELD node AS n, score
RETURN n.uuid, n.name, n.summary, ... ORDER BY score DESC
```

**SurrealQL:**
```sql
SELECT
    uuid, name, group_id, labels, summary, created_at,
    search::score(0) + search::score(1) AS score
FROM entity
WHERE name @0@ $query OR summary @1@ $query
ORDER BY score DESC
LIMIT $limit;
```

#### Fulltext Search — Edges

**SurrealQL:**
```sql
SELECT
    uuid, group_id, in.uuid AS source_node_uuid, out.uuid AS target_node_uuid,
    name, fact, episodes, created_at, expired_at, valid_at, invalid_at,
    search::score(0) + search::score(1) AS score
FROM relates_to
WHERE name @0@ $query OR fact @1@ $query
ORDER BY score DESC
LIMIT $limit;
```

#### Vector Similarity — Entities

**Cypher:**
```cypher
MATCH (n:Entity)
WITH n, vector.similarity.cosine(n.name_embedding, $search_vector) AS score
WHERE score > $min_score
RETURN ... ORDER BY score DESC LIMIT $limit
```

**SurrealQL:**
```sql
SELECT
    uuid, name, group_id, labels, summary, created_at,
    vector::similarity::cosine(name_embedding, $search_vector) AS score
FROM entity
WHERE name_embedding <|$limit|> $search_vector
    AND vector::similarity::cosine(name_embedding, $search_vector) > $min_score
ORDER BY score DESC
LIMIT $limit;
```

#### Vector Similarity — Edges

**SurrealQL:**
```sql
SELECT
    uuid, group_id, in.uuid AS source_node_uuid, out.uuid AS target_node_uuid,
    name, fact, episodes, created_at, expired_at, valid_at, invalid_at,
    vector::similarity::cosine(fact_embedding, $search_vector) AS score
FROM relates_to
WHERE fact_embedding <|$limit|> $search_vector
    AND vector::similarity::cosine(fact_embedding, $search_vector) > $min_score
ORDER BY score DESC
LIMIT $limit;
```

#### BFS Graph Traversal — Nodes

**Cypher:**
```cypher
UNWIND $uuids AS uuid
MATCH (origin:Entity {uuid: uuid})-[:RELATES_TO*1..3]-(n:Entity)
RETURN DISTINCT n.uuid, n.name, ...
```

**SurrealQL:**
```sql
SELECT VALUE array::distinct(array::flatten(
    (SELECT VALUE .{1..3}(<->relates_to<->entity) FROM $origin_ids)
)) FROM ONLY {};
```

Or per-node with depth control:
```sql
LET $connected = (SELECT VALUE .{1..$max_depth}(<->relates_to<->entity)
    FROM entity WHERE uuid IN $origin_uuids);
SELECT * FROM entity WHERE id IN array::distinct(array::flatten($connected))
LIMIT $limit;
```

#### BFS Graph Traversal — Edges

**SurrealQL:**
```sql
LET $nodes = (SELECT VALUE .{1..$max_depth}(<->relates_to<->entity)
    FROM entity WHERE uuid IN $origin_uuids);
LET $node_ids = array::distinct(array::flatten($nodes));
SELECT
    uuid, group_id, in.uuid AS source_node_uuid, out.uuid AS target_node_uuid,
    name, fact, episodes, created_at, expired_at, valid_at, invalid_at
FROM relates_to
WHERE in IN $node_ids OR out IN $node_ids
LIMIT $limit;
```

### Utility Queries

#### Get Node by UUID

```sql
SELECT * FROM entity WHERE uuid = $uuid LIMIT 1;
-- Or direct record access:
SELECT * FROM entity:$uuid;
```

#### Get Edges Between Two Nodes

```sql
SELECT * FROM relates_to
WHERE in = entity:$source_uuid AND out = entity:$target_uuid;
```

#### Retrieve Recent Episodes

```sql
SELECT * FROM episodic
WHERE group_id IN $group_ids AND valid_at <= $reference_time
ORDER BY valid_at DESC
LIMIT $last_n;
```

#### Count Episode Mentions

```sql
SELECT out.uuid AS uuid, count() AS score
FROM mentions
WHERE out.uuid IN $node_uuids
GROUP BY out.uuid;
```

---

## Implementation Plan

### Phase 1: Foundation (driver + schema)
1. Add `SURREALDB` to `GraphProvider` enum
2. Create `surrealdb_driver.py` with connection, session, schema init
3. Add `surrealdb` to `pyproject.toml`
4. Wire up exports in `__init__.py`

### Phase 2: Node Queries
5. Add SurrealQL node save queries to `node_db_queries.py`
6. Add SurrealQL node return/parsing to `nodes.py`

### Phase 3: Edge Queries
7. Add SurrealQL edge save queries to `edge_db_queries.py`
8. Add SurrealQL edge return/parsing to `edges.py`

### Phase 4: Index & Graph Queries
9. Add SurrealQL index creation to `graph_queries.py`
10. Add SurrealQL fulltext/vector query functions

### Phase 5: Search
11. Add SurrealQL fulltext search functions to `search_utils.py`
12. Add SurrealQL vector search functions
13. Add SurrealQL BFS search functions
14. Add SurrealQL filter construction to `search_filters.py`

### Phase 6: Bulk & Utilities
15. Add SurrealQL bulk handling to `bulk_utils.py`
16. Add SurrealDB default group_id to `helpers.py`

### Phase 7: Verification
17. Run `make lint` and fix type errors
18. Run `make test` for unit tests
19. Verify build is clean

---

## Critical Gotchas

1. **UPSERT does NOT work on edge/relation tables** — Must use `INSERT RELATION ... ON DUPLICATE KEY UPDATE`
2. **Datetime literals need `d''` prefix** — Python SDK should handle ISO strings automatically
3. **`NONE` vs `NULL`** — Use `NONE` for absent optional fields, `option<T>` for the type
4. **Edge records use `in`/`out` fields** — Not `source`/`target` or `from`/`to`
5. **Single table per record** — No multi-label nodes; use `labels` array field
6. **HNSW indexes are in-memory** — Configure `SURREAL_HNSW_CACHE_SIZE` for large datasets
7. **`+=` on arrays deduplicates** — Use `array::append()` for true append
8. **Python SDK `query()` returns first statement only** — Use `query_raw()` for multi-statement
9. **Fulltext search uses `@@` / `@N@` operators** — `@N@` links to `search::score(N)`
10. **Record IDs are `table:id`** — Use `RecordID('entity', uuid_str)` in Python SDK

---

## Testing Strategy

### Unit Tests
- Query generation functions return valid SurrealQL
- Record parsing handles SurrealDB result format
- Filter construction produces correct WHERE clauses

### Integration Tests (require SurrealDB instance)
- Schema creation runs without errors
- CRUD operations on all node/edge types
- Fulltext search returns results with BM25 scoring
- Vector similarity search with HNSW returns correct neighbors
- Graph traversal reaches expected depth
- Bulk insert handles multiple records
- Temporal queries filter correctly on valid_at/invalid_at
- Edge upsert (INSERT RELATION ON DUPLICATE KEY UPDATE) works
- Full add_episode() pipeline runs end-to-end

### SurrealDB Test Instance
```bash
# Docker
docker run --rm -p 8000:8000 surrealdb/surrealdb:latest start --user root --pass root

# Or binary
surreal start --user root --pass root --bind 0.0.0.0:8000
```
