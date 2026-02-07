# Graphiti Episode Linking & Knowledge Graph Construction — Complete Analysis

> **Purpose:** Deep technical analysis of how Graphiti processes raw episodes into linked knowledge graph data, plus assessment of porting to SurrealDB.

---

## Table of Contents

1. [The Big Picture](#the-big-picture)
2. [The 12-Phase Pipeline](#the-12-phase-pipeline)
3. [Data Models](#data-models)
4. [The Search System](#the-search-system)
5. [LLM Prompt Architecture](#llm-prompt-architecture)
6. [Database Abstraction Layer](#database-abstraction-layer)
7. [SurrealDB: Add vs Rewrite](#surrealdb-add-vs-rewrite)

---

## The Big Picture

Graphiti takes raw text (an "episode" — a chat message, JSON document, or text blob) and incrementally builds a temporally-aware knowledge graph through a 12-phase LLM-powered pipeline. The key design principle: **no batch recomputation required**. Each episode is processed against the existing graph state.

### Graph Structure

```
NODES                          EDGES
─────                          ─────
EpisodicNode  (raw source)     EpisodicEdge    (Episode -MENTIONS-> Entity)
EntityNode    (entities)       EntityEdge      (Entity -RELATES_TO-> Entity)
CommunityNode (clusters)       CommunityEdge   (Community -HAS_MEMBER-> Entity)
SagaNode      (sequences)      HasEpisodeEdge  (Saga -HAS_EPISODE-> Episode)
                               NextEpisodeEdge (Episode -NEXT_EPISODE-> Episode)
```

### Example Flow

Input: `"Alice started working at Acme Corp on January 15, 2024"`

Output:
- **Nodes:** `Alice` (Person), `Acme Corp` (Organization)
- **Edge:** `Alice -[WORKS_AT]-> Acme Corp` with `valid_at=2024-01-15`, `fact="Alice started working at Acme Corp"`
- **Episodic links:** Episode -[MENTIONS]-> Alice, Episode -[MENTIONS]-> Acme Corp

---

## The 12-Phase Pipeline

### Phase 1: Initialization

**Entry:** `graphiti_core/graphiti.py:759` — `add_episode()`

**Inputs:**
| Parameter | Type | Purpose |
|-----------|------|---------|
| `name` | str | Episode name |
| `episode_body` | str | Raw content |
| `source_description` | str | Description of data source |
| `reference_time` | datetime | When the content was created |
| `source` | EpisodeType | `message`, `json`, or `text` |
| `group_id` | str | Multi-tenant partition key |
| `entity_types` | dict[str, BaseModel] | Custom Pydantic entity definitions |
| `edge_types` | dict[str, BaseModel] | Custom edge type definitions |
| `edge_type_map` | dict[tuple, list] | Valid (source_type, target_type) → edge_type mappings |
| `custom_extraction_instructions` | str | Additional LLM guidance |

**Operations:**
- Validate entity types (no field name conflicts with base EntityNode)
- Set default group_id
- Clone driver if different database needed
- Build default edge type map: `{('Entity', 'Entity'): [edge_type_names]}`

---

### Phase 2: Context Retrieval

**File:** `graphiti_core/graphiti.py:865-891`

Fetches the N most recent `EpisodicNode`s before `reference_time` for the same `group_id`. This historical context is passed to the LLM so it can:
- Resolve pronouns ("he", "they", "this")
- Understand conversation continuity
- Avoid re-extracting entities from old messages

Creates the new `EpisodicNode`:
```python
EpisodicNode(
    name=name,
    group_id=group_id,
    source=source,
    source_description=source_description,
    content=episode_body,        # Full raw text
    valid_at=reference_time,     # When content was created
    created_at=utc_now(),        # When ingested
)
```

---

### Phase 3A: Entity Extraction (LLM)

**File:** `graphiti_core/utils/maintenance/node_operations.py:66-113`

Sends episode content + previous episodes + entity type definitions to the LLM. Three prompt variants:

| Source Type | Strategy |
|-------------|----------|
| **message** | Extract speaker first (before `:` in dialogue), then other entities. Resolve pronouns. |
| **json** | Extract entities the JSON represents and entities in properties. Skip date fields. |
| **text** | General entity extraction from unstructured text. |

**Chunking for large content:**
- `should_chunk(content, source)` checks content density
- If chunking needed: split by type (json → `chunk_json_content()`, etc.)
- Extract from each chunk in parallel
- Merge results with `_merge_extracted_entities()` (dedupe by normalized name)

**LLM Response Model:**
```python
class ExtractedEntity(BaseModel):
    name: str              # Entity name (unambiguous, explicit)
    entity_type_id: int    # Index into provided entity_types list
```

**Output:** List of `EntityNode` objects with names and type labels, but no embeddings or summaries yet.

---

### Phase 3B: Entity Resolution (Hybrid Search + LLM)

**File:** `graphiti_core/utils/maintenance/node_operations.py:469-519`

This prevents duplicate entities across episodes. Three-step process:

**Step 1 — Search for candidates:**
For each extracted entity, run hybrid search (BM25 + vector similarity + RRF reranking) against the existing graph. Uses `NODE_HYBRID_SEARCH_RRF` search config.

**Step 2 — Deterministic matching:**
- Exact name match (case-insensitive, collapsed whitespace)
- Fuzzy match using MinHash/Jaccard similarity on 3-gram shingles (90% threshold)

**Step 3 — LLM escalation:**
Remaining ambiguous cases go to an LLM dedup prompt:
```python
class NodeDuplicate(BaseModel):
    id: int              # Index of extracted entity
    name: str            # Best complete name
    duplicate_name: str  # Name from EXISTING ENTITIES, or "" if new
```

**Key rules the LLM follows:**
- Duplicates only if they refer to the **same real-world object/concept**
- Semantic equivalence counts (descriptive label → named entity)
- Related but distinct entities are NOT duplicates

**Output:**
- Canonical node list (existing nodes reused, new nodes for genuinely new entities)
- `uuid_map: dict[str, str]` (extracted UUID → canonical UUID) for rewriting edge pointers

---

### Phase 4: Edge Extraction (LLM)

**File:** `graphiti_core/utils/maintenance/edge_operations.py:91-302`

Extracts relationships between resolved entities.

**Covering Chunks Algorithm:**
Since all entity pairs can't fit in one LLM prompt when there are many entities:
- Greedy partition of nodes into chunks of ≤15
- Each node pair assigned to exactly one chunk (no duplicate extraction)
- Parallel LLM calls per chunk

**LLM Response Model:**
```python
class Edge(BaseModel):
    source_entity_name: str   # Must match a name from ENTITIES list
    target_entity_name: str   # Must match a name from ENTITIES list
    relation_type: str        # SCREAMING_SNAKE_CASE (WORKS_AT, LIVES_IN)
    fact: str                 # Natural language, paraphrased (not verbatim)
    valid_at: str | None      # ISO 8601 datetime or None
    invalid_at: str | None    # ISO 8601 datetime or None
```

**Temporal resolution rules:**
| Scenario | valid_at | invalid_at |
|----------|----------|------------|
| Ongoing (present tense) | `reference_time` | `None` |
| Started at specific time | Parsed datetime | `None` |
| Ended at specific time | Start datetime | Parsed datetime |
| No temporal info | `None` | `None` |
| Relative time ("yesterday") | Resolved against `reference_time` | — |
| Year only ("2023") | January 1, 00:00:00 UTC | — |

---

### Phase 5: Edge Pointer Resolution

**File:** `graphiti_core/utils/bulk_utils.py`

Applies the `uuid_map` from Phase 3B to rewrite `source_node_uuid` and `target_node_uuid` on all extracted edges, pointing them to canonical (deduplicated) entities.

```python
for edge in extracted_edges:
    edge.source_node_uuid = uuid_map.get(edge.source_node_uuid, edge.source_node_uuid)
    edge.target_node_uuid = uuid_map.get(edge.target_node_uuid, edge.target_node_uuid)
```

---

### Phase 6: Edge Resolution & Invalidation (LLM)

**File:** `graphiti_core/utils/maintenance/edge_operations.py:305-464`

The most complex phase. For each extracted edge:

1. **Generate embedding** of the `fact` text
2. **Find existing edges** between same source/target nodes (exact match candidates)
3. **Find invalidation candidates** via hybrid search on the fact text (broader)
4. **LLM resolution:**
   - Fast path: fact text exists verbatim → reuse existing edge (add episode to its `episodes` list)
   - LLM dedup: "Is this new fact a duplicate of any existing fact?"
   - LLM contradiction: "Does this new fact contradict any existing fact?"

**LLM Response Model:**
```python
class EdgeDuplicate(BaseModel):
    duplicate_facts: list[int]      # Indices into EXISTING FACTS
    contradicted_facts: list[int]   # Indices into FACT INVALIDATION CANDIDATES
```

5. **Handle contradictions:** Set `invalid_at` and `expired_at` on contradicted edges

**This is where bi-temporal consistency happens.** When you say "Alice no longer works at Acme", the existing "Alice WORKS_AT Acme" edge gets `invalid_at` set to the current reference time.

6. **Extract edge attributes** (if custom edge type has Pydantic fields)

---

### Phase 7: Node Attribute & Summary Extraction (LLM)

**File:** `graphiti_core/utils/maintenance/node_operations.py:537-573`

For each resolved node (in parallel):
1. If entity type has custom Pydantic fields → LLM extracts structured attributes
2. LLM generates concise summary (≤250 chars) from episode context + related edge facts
3. Generate vector embedding of entity name

**Summary guidelines:**
```
BAD:  "Based on the messages provided, the user attended a meeting."
GOOD: "User attended Q3 planning meeting with sales team on March 15."
```

---

### Phase 8: Build Episodic Edges

**File:** `graphiti_core/utils/maintenance/edge_operations.py:53-70`

Creates `EpisodicEdge` (MENTIONS) linking the episode to each entity it references:

```
Episode ─[MENTIONS]─> Alice
Episode ─[MENTIONS]─> Acme Corp
```

This is the **provenance chain** — you can always trace which episode mentioned which entity.

---

### Phase 9: Persist to Database

**File:** `graphiti_core/utils/bulk_utils.py:128-149`

All nodes and edges saved atomically via `add_nodes_and_edges_bulk()`:
- MERGE/upsert semantics (existing nodes updated, new nodes created)
- Single transaction for consistency
- Provider-specific serialization (vectors, datetimes, attributes)

**Batch order:**
1. Save episodic nodes
2. Save entity nodes (with embeddings)
3. Save episodic edges (MENTIONS)
4. Save entity edges (RELATES_TO, with embeddings)

---

### Phase 10: Saga Association (Optional)

**File:** `graphiti_core/graphiti.py:468-562`

If a `saga` is provided:
1. Get or create `SagaNode`
2. Create `HasEpisodeEdge` (Saga → Episode)
3. Create `NextEpisodeEdge` (Previous Episode → Current Episode)

```
Saga ─[HAS_EPISODE]─> Episode1
Saga ─[HAS_EPISODE]─> Episode2
Episode1 ─[NEXT_EPISODE]─> Episode2
```

---

### Phase 11: Community Updates (Optional)

Clusters related entities and generates community summaries via LLM. Creates `CommunityNode` and `CommunityEdge` (HAS_MEMBER).

---

### Phase 12: Return Results

```python
AddEpisodeResults(
    episode=episode,                 # The EpisodicNode
    episodic_edges=episodic_edges,   # MENTIONS edges
    nodes=hydrated_nodes,            # EntityNodes with embeddings & summaries
    edges=entity_edges,              # RELATES_TO edges (resolved + invalidated)
    communities=communities,         # Optional
    community_edges=community_edges, # Optional
)
```

---

## Data Models

### Bi-Temporal Design

Every piece of data has two temporal dimensions:

| Field | Meaning | Applies To |
|-------|---------|------------|
| `created_at` | When the record was created in the database | All nodes/edges |
| `valid_at` | When the fact became true in the real world | Episodes, EntityEdges |
| `invalid_at` | When the fact stopped being true | EntityEdges |
| `expired_at` | When the database record was superseded | EntityEdges |

**Key distinction:**
- `created_at` / `expired_at` = **database record lifecycle**
- `valid_at` / `invalid_at` = **real-world fact lifecycle**

### EpisodicNode

```python
uuid: str                  # UUID v4
name: str                  # Episode name
group_id: str              # Multi-tenant partition
labels: list[str]          # Node labels
source: EpisodeType        # 'message' | 'json' | 'text'
source_description: str    # Human-readable source description
content: str               # Full raw episode text
valid_at: datetime         # When content was created (NOT ingestion time)
created_at: datetime       # When ingested into graph
entity_edges: list[str]    # UUIDs of EntityEdges from this episode
```

### EntityNode

```python
uuid: str                  # UUID v4
name: str                  # Entity name
group_id: str              # Multi-tenant partition
labels: list[str]          # ["Entity", "Person"], ["Entity", "Company"], etc.
summary: str               # LLM-generated ≤250 char summary
attributes: dict[str, Any] # Custom typed attributes from Pydantic entity types
name_embedding: list[float] | None  # Vector embedding (lazy-loaded)
created_at: datetime       # UTC timestamp
```

**Custom Entity Types:**
```python
class Person(BaseModel):
    age: int = Field(description="Person's age")
    occupation: str = Field(description="Person's job")

entity_types = {"Person": Person}
# Node gets labels=["Entity", "Person"], attributes={"age": 30, "occupation": "Engineer"}
```

### EntityEdge (RELATES_TO)

```python
uuid: str                  # UUID v4
source_node_uuid: str      # From entity
target_node_uuid: str      # To entity
name: str                  # Relation type (WORKS_AT, LIVES_IN)
fact: str                  # Natural language fact
group_id: str              # Multi-tenant partition
episodes: list[str]        # Episode UUIDs that support this fact
fact_embedding: list[float] | None  # Vector embedding (lazy-loaded)
valid_at: datetime | None  # When fact became true
invalid_at: datetime | None # When fact stopped being true
expired_at: datetime | None # When record was superseded
attributes: dict[str, Any] # Custom edge attributes
created_at: datetime       # UTC timestamp
```

### SagaNode

```python
uuid: str         # UUID v4
name: str         # Saga name
group_id: str     # Multi-tenant partition
labels: list[str] # Node labels
created_at: datetime
```

### CommunityNode

```python
uuid: str                  # UUID v4
name: str                  # Community name
group_id: str              # Multi-tenant partition
labels: list[str]          # Node labels
summary: str               # Community summary
name_embedding: list[float] | None
created_at: datetime
```

### Simple Edge Types (no extra fields beyond base)

| Type | Pattern | Purpose |
|------|---------|---------|
| EpisodicEdge | Episode -[MENTIONS]-> Entity | Provenance |
| CommunityEdge | Community -[HAS_MEMBER]-> Entity/Community | Clustering |
| HasEpisodeEdge | Saga -[HAS_EPISODE]-> Episode | Grouping |
| NextEpisodeEdge | Episode -[NEXT_EPISODE]-> Episode | Temporal sequence |

---

## The Search System

### Architecture

Hybrid retrieval combining three methods, used both during ingestion (deduplication) and user-facing queries.

| Method | How It Works | Used For |
|--------|-------------|----------|
| **BM25 Full-Text** | Keyword matching on names, facts, summaries via database fulltext index | Finding entities/facts by keywords |
| **Vector Similarity** | Cosine similarity on embeddings (min score 0.6 default) | Semantic matching |
| **BFS Traversal** | Breadth-first from origin nodes, max depth 3 | Graph neighborhood exploration |

### Reranking Strategies

| Strategy | Algorithm | Use Case |
|----------|-----------|----------|
| **RRF** (Reciprocal Rank Fusion) | `score = Σ(1 / (rank + k))` across methods | Default — combines rankings |
| **MMR** (Maximal Marginal Relevance) | `λ·sim(query) - (1-λ)·max_sim(selected)` | Diversity-aware results |
| **Cross-Encoder** | Separate neural model rescoring | Highest accuracy (expensive) |
| **Node Distance** | `1 / shortest_path_distance` from center | Proximity to reference node |
| **Episode Mentions** | Count of MENTIONS edges | Frequency-based importance |

### Search During Episode Processing

**For node deduplication:**
1. `NODE_HYBRID_SEARCH_RRF` (BM25 + cosine + RRF) finds candidates
2. Deterministic matching (exact + MinHash fuzzy at 90% Jaccard)
3. LLM escalation for remaining ambiguous cases

**For edge deduplication:**
1. `EntityEdge.get_between_nodes()` finds edges with same endpoints
2. `EDGE_HYBRID_SEARCH_RRF` finds semantically similar edges (broader)
3. LLM determines duplicates vs contradictions

### Search Configuration

Pre-built recipes in `search_config_recipes.py`:
- `COMBINED_HYBRID_SEARCH_RRF` — Default for all entity types
- `NODE_HYBRID_SEARCH_RRF` — Nodes only
- `EDGE_HYBRID_SEARCH_RRF` — Edges only
- `COMBINED_HYBRID_SEARCH_MMR` — Diversity-optimized
- `NODE_HYBRID_SEARCH_NODE_DISTANCE` — Proximity-based

### Search Filters

Fine-grained filtering:
- **Node labels** — Filter by entity type
- **Edge types** — Filter by relationship type
- **Temporal** — `valid_at`, `invalid_at`, `created_at`, `expired_at` ranges
- **Property filters** — Custom attribute matching with operators (`=`, `<>`, `>`, `<`, `IS NULL`)
- **Logical combinators** — AND/OR for complex temporal queries

---

## LLM Prompt Architecture

### Prompt Files

| File | Purpose | When Called |
|------|---------|------------|
| `extract_nodes.py` | Entity extraction from episodes | Phase 3A |
| `dedupe_nodes.py` | Entity deduplication | Phase 3B |
| `extract_edges.py` | Relationship extraction | Phase 4 |
| `dedupe_edges.py` | Edge duplicate/contradiction detection | Phase 6 |
| `summarize_nodes.py` | Entity summary generation | Phase 7 |

### Context Provided to Each LLM Call

**Entity Extraction:**
- `ENTITY TYPES` — Type definitions with IDs and descriptions (from Pydantic models)
- `PREVIOUS MESSAGES` — Content from earlier episodes
- `CURRENT MESSAGE` — The episode to extract from
- `custom_extraction_instructions` — User-defined rules

**Edge Extraction:**
- `ENTITIES` — List of extracted entities (name + type)
- `REFERENCE_TIME` — ISO 8601 for resolving relative dates
- `FACT_TYPES` — Custom edge type definitions with source/target signatures
- Same previous/current message context

**Node Deduplication:**
- `NEW ENTITY` — The extracted entity with attributes
- `EXISTING ENTITIES` — Candidates from graph with summaries
- `ENTITY TYPE DESCRIPTION` — Type context
- Same message context

**Edge Deduplication:**
- `NEW FACT` — The extracted edge
- `EXISTING FACTS` — Edges between same endpoints
- `FACT INVALIDATION CANDIDATES` — Broader semantically similar edges

### Key Prompt Rules

**Entity extraction:**
- Extract speaker first in conversations
- Disambiguate pronouns to actual entity names
- Only extract from CURRENT MESSAGE (use previous for context only)
- Do NOT extract temporal info, relationships, or actions as entities

**Edge extraction:**
- Entity names MUST match exactly from provided list
- Facts must involve TWO DISTINCT entities
- Paraphrase, don't verbatim quote
- Use SCREAMING_SNAKE_CASE for relation types

**Deduplication:**
- "Same real-world object" = duplicate
- "Related but distinct" ≠ duplicate
- Respond with exact IDs from provided lists

---

## Database Abstraction Layer

### Driver Interface

```python
class GraphDriver(ABC):
    provider: DriverProvider          # NEO4J | FALKORDB | KUZU | NEPTUNE

    async execute_query(query, **kwargs)                    # Run a query
    async session(database) -> GraphDriverSession           # Get transaction session
    async close()                                           # Cleanup
    async build_indices_and_constraints(delete_existing)     # Create indexes
    async delete_all_indexes()                              # Drop indexes
    def clone(database) -> GraphDriver                      # New driver for different DB
    def with_database(database) -> GraphDriver              # Shallow copy
```

### Required Indexes

**Range indexes (18 total):**
- UUID on all node/edge types
- `group_id` on all types
- `name` on Entity, Saga
- `created_at` on Entity, Episodic
- `valid_at` on Episodic, RELATES_TO
- `expired_at`, `invalid_at` on RELATES_TO

**Fulltext indexes (4 total):**
```
episode_content:      Episodic.content, .source, .source_description, .group_id
node_name_and_summary: Entity.name, .summary, .group_id
community_name:       Community.name, .group_id
edge_name_and_fact:   RELATES_TO.name, .fact, .group_id
```

**Vector indexes:**
- `name_embedding` on EntityNode, CommunityNode
- `fact_embedding` on EntityEdge (RELATES_TO)

### Provider-Specific Query Patterns

**Node save (UPSERT):**
```cypher
-- Neo4j/FalkorDB
MERGE (n:Entity {uuid: $uuid})
SET n = $properties
SET n:$dynamic_labels

-- Kuzu
MERGE (n:Entity {uuid: $uuid})
SET n.name = $name, n.summary = $summary, ...

-- Neptune
MERGE (n:Entity {uuid: $uuid})
SET n = removeKeyFromMap($properties, 'embedding')
```

**Edge save:**
```cypher
-- Neo4j/FalkorDB
MATCH (source:Entity {uuid: $source_uuid})
MATCH (target:Entity {uuid: $target_uuid})
MERGE (source)-[e:RELATES_TO {uuid: $uuid}]->(target)
SET e = $properties

-- Kuzu (intermediate node workaround)
MERGE (source:Entity {uuid: $source_uuid})-[:RELATES_TO]->(e:RelatesToNode_ {uuid: $uuid})-[:RELATES_TO]->(target:Entity {uuid: $target_uuid})
SET e = $properties
```

**Vector similarity:**
```
Neo4j:    vector.similarity.cosine(v1, v2)
FalkorDB: (2 - vec.cosineDistance(v1, vecf32(v2)))/2
Kuzu:     array_cosine_similarity(v1, v2)
Neptune:  External OpenSearch
```

### Files with Provider-Specific Logic

| File | Provider Branches | Purpose |
|------|-------------------|---------|
| `search/search_utils.py` | ~31 | Dynamic query building for all search types |
| `nodes.py` | ~15 | Result parsing, tuple unpacking |
| `edges.py` | ~11 | Result parsing, attribute handling |
| `models/nodes/node_db_queries.py` | ~10 | Node save/bulk queries |
| `models/edges/edge_db_queries.py` | ~10 | Edge save/bulk queries |
| `graph_queries.py` | ~10 | Index creation, fulltext/vector queries |
| Various utils | ~17 | Bulk ops, maintenance |
| **Total** | **~110+** | |

---

## SurrealDB: Add vs Rewrite

### The Fundamental Challenge

SurrealDB uses **SurrealQL** (SQL-based), not **Cypher** (graph-pattern-based). Every other backend Graphiti supports (Neo4j, FalkorDB, Kuzu, Neptune) uses a Cypher dialect. SurrealQL is a completely different query paradigm:

| Operation | Cypher | SurrealQL |
|-----------|--------|-----------|
| Create node | `CREATE (n:Label {props})` | `CREATE table:id SET ...` |
| Upsert | `MERGE (n {uuid: $id}) SET ...` | `UPSERT table:id MERGE {...}` |
| Create edge | `(a)-[r:TYPE]->(b)` | `RELATE a->type->b SET ...` |
| Traverse | `MATCH (n)-[:TYPE]->(m)` | `SELECT ->type->*.* FROM n` |
| Vector search | `vector.similarity.cosine()` | Native array operations |
| Fulltext | `CALL db.index.fulltext.queryNodes()` | `DEFINE INDEX ... SEARCH ANALYZER` |

### Effort Breakdown

**Option A: Add SurrealDB to existing codebase (~12-18 weeks)**

| Component | Effort | Notes |
|-----------|--------|-------|
| New SurrealDB driver | 2-3 weeks | Connection, schema, indexes |
| Node query builders | 1-2 weeks | 6 functions, complete rewrite |
| Edge query builders | 1-2 weeks | 5 functions, RELATE syntax |
| Index/fulltext generation | 1 week | DEFINE INDEX/ANALYZER syntax |
| Search implementation | 3-4 weeks | 31 branches, biggest effort |
| Node/edge model updates | 1 week | 26 provider checks |
| Bulk operations | 1 week | Different batch patterns |
| Testing | 2-3 weeks | Integration test suite |

**Option B: Full rewrite targeting SurrealDB (~16-24 weeks)**

| Component | Effort | Notes |
|-----------|--------|-------|
| Architecture design | 2-3 weeks | SurrealDB-native patterns |
| Core driver | 2-3 weeks | Connection, schema |
| Data models | 1-2 weeks | SurrealDB record types |
| Query builders | 4-6 weeks | All CRUD in SurrealQL |
| Search/retrieval | 3-4 weeks | Hybrid search |
| LLM pipeline | 2-3 weeks | Largely reusable |
| Testing | 3-4 weeks | Full test suite |
| Performance tuning | 2-3 weeks | SurrealDB-specific |

### What SurrealDB Handles Well

- **Multi-tenancy:** Namespace/database isolation or `group_id` filtering
- **Graph edges:** Native `RELATE` with properties on edges
- **Vector search:** `vector::similarity::cosine()` with native array support
- **Full-text search:** BM25 available via `DEFINE INDEX ... SEARCH ANALYZER`
- **Temporal fields:** Arbitrary datetime fields on any record
- **Transactions:** Native transaction support
- **Schema:** Optional schema with `DEFINE TABLE`, `DEFINE FIELD`
- **JSON properties:** Native document store, no JSON serialization needed

### What Requires New Patterns

- **No Cypher** — Every query must be rewritten
- **No atomic MERGE** — Must use `UPSERT ... MERGE` (slightly different semantics)
- **Graph traversal syntax** — Arrow notation (`->type->`) vs MATCH patterns
- **Bulk operations** — Different batch patterns than UNWIND
- **Index definitions** — `DEFINE INDEX` vs `CREATE INDEX`

### Recommendation

**For SurrealDB specifically: Rewrite the database layer only.**

The core value of Graphiti is split into two independent layers:

1. **Intelligence Layer** (keep as-is or port directly):
   - LLM extraction pipeline (Phases 3A, 4, 7)
   - Deduplication logic (Phases 3B, 6)
   - Temporal resolution
   - Covering chunks algorithm
   - RRF/MMR reranking
   - MinHash fuzzy matching

2. **Database Layer** (rewrite for SurrealDB):
   - Driver implementation
   - Query builders (`node_db_queries.py`, `edge_db_queries.py`)
   - Index creation (`graph_queries.py`)
   - Search queries (`search_utils.py`)
   - Bulk persistence (`bulk_utils.py`)

The intelligence layer is ~70% of the complexity but database-agnostic. The database layer is ~30% of the complexity but entirely Cypher-dependent.

**Best approach:** Fork the codebase, strip out all 4 existing drivers, implement a single SurrealDB driver, and rewrite the ~110 provider-specific code branches as SurrealQL. This gives you the battle-tested extraction pipeline without the multi-provider complexity overhead.

---

## Appendix: Key File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `graphiti_core/graphiti.py` | ~1000 | Main orchestrator, `add_episode()` |
| `graphiti_core/nodes.py` | ~1060 | All node types + DB record parsing |
| `graphiti_core/edges.py` | ~1030 | All edge types + DB record parsing |
| `graphiti_core/utils/maintenance/node_operations.py` | ~575 | Node extraction & resolution |
| `graphiti_core/utils/maintenance/edge_operations.py` | ~465 | Edge extraction & resolution |
| `graphiti_core/utils/bulk_utils.py` | ~500 | Bulk persistence & cross-batch dedup |
| `graphiti_core/search/search_utils.py` | ~2060 | All search implementations |
| `graphiti_core/search/search_config.py` | ~200 | Search configuration models |
| `graphiti_core/search/search_filters.py` | ~260 | Filter logic & query builders |
| `graphiti_core/models/nodes/node_db_queries.py` | ~370 | Node save/bulk queries |
| `graphiti_core/models/edges/edge_db_queries.py` | ~320 | Edge save/bulk queries |
| `graphiti_core/graph_queries.py` | ~175 | Index creation, fulltext/vector queries |
| `graphiti_core/prompts/extract_nodes.py` | ~300 | Entity extraction prompts |
| `graphiti_core/prompts/extract_edges.py` | ~250 | Relationship extraction prompts |
| `graphiti_core/prompts/dedupe_nodes.py` | ~200 | Node deduplication prompts |
| `graphiti_core/prompts/dedupe_edges.py` | ~150 | Edge dedup/contradiction prompts |
| `graphiti_core/driver/neo4j_driver.py` | ~126 | Neo4j driver |
| `graphiti_core/driver/falkordb_driver.py` | ~368 | FalkorDB driver |
| `graphiti_core/driver/kuzu_driver.py` | ~182 | Kuzu driver |
| `graphiti_core/driver/neptune_driver.py` | ~306 | Neptune driver |
