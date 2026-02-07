"""
Copyright 2024, Zep Software, Inc.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import logging
from typing import Any

from graphiti_core.driver.driver import GraphDriver, GraphDriverSession, GraphProvider

logger = logging.getLogger(__name__)

# SurrealDB requires explicit schema definitions.
# Unlike Cypher-based databases, SurrealDB uses SurrealQL with RELATE for edges.
# Edge tables are typed relations with in/out fields.
SCHEMA_QUERIES = """
    -- Node tables
    DEFINE TABLE IF NOT EXISTS entity SCHEMAFULL;
    DEFINE FIELD IF NOT EXISTS uuid ON entity TYPE string;
    DEFINE FIELD IF NOT EXISTS name ON entity TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON entity TYPE string;
    DEFINE FIELD IF NOT EXISTS labels ON entity TYPE array<string> DEFAULT [];
    DEFINE FIELD IF NOT EXISTS summary ON entity TYPE string DEFAULT '';
    DEFINE FIELD IF NOT EXISTS created_at ON entity TYPE datetime;
    DEFINE FIELD IF NOT EXISTS name_embedding ON entity TYPE option<array<float>>;

    DEFINE TABLE IF NOT EXISTS episodic SCHEMAFULL;
    DEFINE FIELD IF NOT EXISTS uuid ON episodic TYPE string;
    DEFINE FIELD IF NOT EXISTS name ON episodic TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON episodic TYPE string;
    DEFINE FIELD IF NOT EXISTS labels ON episodic TYPE array<string> DEFAULT [];
    DEFINE FIELD IF NOT EXISTS source ON episodic TYPE string;
    DEFINE FIELD IF NOT EXISTS source_description ON episodic TYPE string;
    DEFINE FIELD IF NOT EXISTS content ON episodic TYPE string;
    DEFINE FIELD IF NOT EXISTS valid_at ON episodic TYPE datetime;
    DEFINE FIELD IF NOT EXISTS created_at ON episodic TYPE datetime;
    DEFINE FIELD IF NOT EXISTS entity_edges ON episodic TYPE array<string> DEFAULT [];

    DEFINE TABLE IF NOT EXISTS community SCHEMAFULL;
    DEFINE FIELD IF NOT EXISTS uuid ON community TYPE string;
    DEFINE FIELD IF NOT EXISTS name ON community TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON community TYPE string;
    DEFINE FIELD IF NOT EXISTS labels ON community TYPE array<string> DEFAULT [];
    DEFINE FIELD IF NOT EXISTS summary ON community TYPE string DEFAULT '';
    DEFINE FIELD IF NOT EXISTS created_at ON community TYPE datetime;
    DEFINE FIELD IF NOT EXISTS name_embedding ON community TYPE option<array<float>>;

    DEFINE TABLE IF NOT EXISTS saga SCHEMAFULL;
    DEFINE FIELD IF NOT EXISTS uuid ON saga TYPE string;
    DEFINE FIELD IF NOT EXISTS name ON saga TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON saga TYPE string;
    DEFINE FIELD IF NOT EXISTS labels ON saga TYPE array<string> DEFAULT [];
    DEFINE FIELD IF NOT EXISTS created_at ON saga TYPE datetime;

    -- Edge tables (graph relations)
    DEFINE TABLE IF NOT EXISTS relates_to SCHEMAFULL TYPE RELATION IN entity OUT entity;
    DEFINE FIELD IF NOT EXISTS uuid ON relates_to TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON relates_to TYPE string;
    DEFINE FIELD IF NOT EXISTS name ON relates_to TYPE string;
    DEFINE FIELD IF NOT EXISTS fact ON relates_to TYPE string;
    DEFINE FIELD IF NOT EXISTS episodes ON relates_to TYPE array<string> DEFAULT [];
    DEFINE FIELD IF NOT EXISTS created_at ON relates_to TYPE datetime;
    DEFINE FIELD IF NOT EXISTS expired_at ON relates_to TYPE option<datetime>;
    DEFINE FIELD IF NOT EXISTS valid_at ON relates_to TYPE option<datetime>;
    DEFINE FIELD IF NOT EXISTS invalid_at ON relates_to TYPE option<datetime>;
    DEFINE FIELD IF NOT EXISTS fact_embedding ON relates_to TYPE option<array<float>>;

    DEFINE TABLE IF NOT EXISTS mentions SCHEMAFULL TYPE RELATION IN episodic OUT entity;
    DEFINE FIELD IF NOT EXISTS uuid ON mentions TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON mentions TYPE string;
    DEFINE FIELD IF NOT EXISTS created_at ON mentions TYPE datetime;

    DEFINE TABLE IF NOT EXISTS has_member TYPE RELATION;
    DEFINE FIELD IF NOT EXISTS uuid ON has_member TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON has_member TYPE string;
    DEFINE FIELD IF NOT EXISTS created_at ON has_member TYPE datetime;

    DEFINE TABLE IF NOT EXISTS has_episode SCHEMAFULL TYPE RELATION IN saga OUT episodic;
    DEFINE FIELD IF NOT EXISTS uuid ON has_episode TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON has_episode TYPE string;
    DEFINE FIELD IF NOT EXISTS created_at ON has_episode TYPE datetime;

    DEFINE TABLE IF NOT EXISTS next_episode SCHEMAFULL TYPE RELATION IN episodic OUT episodic;
    DEFINE FIELD IF NOT EXISTS uuid ON next_episode TYPE string;
    DEFINE FIELD IF NOT EXISTS group_id ON next_episode TYPE string;
    DEFINE FIELD IF NOT EXISTS created_at ON next_episode TYPE datetime;
"""

INDEX_QUERIES = """
    -- Range indexes on nodes
    DEFINE INDEX IF NOT EXISTS entity_uuid ON entity FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS entity_group_id ON entity FIELDS group_id;
    DEFINE INDEX IF NOT EXISTS entity_name ON entity FIELDS name;
    DEFINE INDEX IF NOT EXISTS entity_created_at ON entity FIELDS created_at;

    DEFINE INDEX IF NOT EXISTS episodic_uuid ON episodic FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS episodic_group_id ON episodic FIELDS group_id;
    DEFINE INDEX IF NOT EXISTS episodic_created_at ON episodic FIELDS created_at;
    DEFINE INDEX IF NOT EXISTS episodic_valid_at ON episodic FIELDS valid_at;

    DEFINE INDEX IF NOT EXISTS community_uuid ON community FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS community_group_id ON community FIELDS group_id;

    DEFINE INDEX IF NOT EXISTS saga_uuid ON saga FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS saga_group_id ON saga FIELDS group_id;
    DEFINE INDEX IF NOT EXISTS saga_name ON saga FIELDS name;

    -- Range indexes on edges
    DEFINE INDEX IF NOT EXISTS relates_to_uuid ON relates_to FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS relates_to_group_id ON relates_to FIELDS group_id;
    DEFINE INDEX IF NOT EXISTS relates_to_name ON relates_to FIELDS name;
    DEFINE INDEX IF NOT EXISTS relates_to_created_at ON relates_to FIELDS created_at;
    DEFINE INDEX IF NOT EXISTS relates_to_expired_at ON relates_to FIELDS expired_at;
    DEFINE INDEX IF NOT EXISTS relates_to_valid_at ON relates_to FIELDS valid_at;
    DEFINE INDEX IF NOT EXISTS relates_to_invalid_at ON relates_to FIELDS invalid_at;

    DEFINE INDEX IF NOT EXISTS mentions_uuid ON mentions FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS mentions_group_id ON mentions FIELDS group_id;

    DEFINE INDEX IF NOT EXISTS has_member_uuid ON has_member FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS has_episode_uuid ON has_episode FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS has_episode_group_id ON has_episode FIELDS group_id;
    DEFINE INDEX IF NOT EXISTS next_episode_uuid ON next_episode FIELDS uuid UNIQUE;
    DEFINE INDEX IF NOT EXISTS next_episode_group_id ON next_episode FIELDS group_id;

    -- Fulltext indexes (BM25)
    DEFINE ANALYZER IF NOT EXISTS graphiti_analyzer TOKENIZERS class, blank FILTERS lowercase, ascii, snowball(english);

    DEFINE INDEX IF NOT EXISTS episode_content_ft ON episodic FIELDS content SEARCH ANALYZER graphiti_analyzer BM25;
    DEFINE INDEX IF NOT EXISTS episode_source_ft ON episodic FIELDS source SEARCH ANALYZER graphiti_analyzer BM25;
    DEFINE INDEX IF NOT EXISTS episode_source_desc_ft ON episodic FIELDS source_description SEARCH ANALYZER graphiti_analyzer BM25;

    DEFINE INDEX IF NOT EXISTS entity_name_ft ON entity FIELDS name SEARCH ANALYZER graphiti_analyzer BM25;
    DEFINE INDEX IF NOT EXISTS entity_summary_ft ON entity FIELDS summary SEARCH ANALYZER graphiti_analyzer BM25;

    DEFINE INDEX IF NOT EXISTS community_name_ft ON community FIELDS name SEARCH ANALYZER graphiti_analyzer BM25;

    DEFINE INDEX IF NOT EXISTS relates_to_name_ft ON relates_to FIELDS name SEARCH ANALYZER graphiti_analyzer BM25;
    DEFINE INDEX IF NOT EXISTS relates_to_fact_ft ON relates_to FIELDS fact SEARCH ANALYZER graphiti_analyzer BM25;

    -- Vector indexes (HNSW with cosine similarity)
    DEFINE INDEX IF NOT EXISTS entity_name_hnsw ON entity FIELDS name_embedding HNSW DIMENSION 1536 DIST COSINE TYPE F32;
    DEFINE INDEX IF NOT EXISTS community_name_hnsw ON community FIELDS name_embedding HNSW DIMENSION 1536 DIST COSINE TYPE F32;
    DEFINE INDEX IF NOT EXISTS relates_to_fact_hnsw ON relates_to FIELDS fact_embedding HNSW DIMENSION 1536 DIST COSINE TYPE F32;
"""


class SurrealDBDriver(GraphDriver):
    provider: GraphProvider = GraphProvider.SURREALDB

    def __init__(
        self,
        url: str = 'ws://localhost:8000/rpc',
        namespace: str = 'graphiti',
        database: str = 'default',
        username: str = 'root',
        password: str = 'root',
    ):
        super().__init__()
        self._url = url
        self._namespace = namespace
        self._database = database
        self._username = username
        self._password = password
        self._client: Any = None

    async def _ensure_client(self):
        if self._client is None:
            from surrealdb import AsyncSurreal  # pyright: ignore[reportMissingImports]

            self._client = AsyncSurreal(self._url)
            await self._client.signin({'username': self._username, 'password': self._password})
            await self._client.use(self._namespace, self._database)

    async def execute_query(
        self, query: str, **kwargs: Any
    ) -> tuple[list[dict[str, Any]], None, None]:
        await self._ensure_client()

        # Strip internal routing params that don't apply to SurrealDB
        params = {k: v for k, v in kwargs.items() if k not in ('database_', 'routing_')}

        try:
            results = await self._client.query(query, params)
        except Exception as e:
            truncated_params = {k: (v[:5] if isinstance(v, list) else v) for k, v in params.items()}
            logger.error(f'Error executing SurrealDB query: {e}\n{query}\n{truncated_params}')
            raise

        if results is None:
            return [], None, None

        # SurrealDB query() returns a list of results for the first statement
        if isinstance(results, list):
            # Convert any SurrealDB record objects to plain dicts
            dict_results = []
            for r in results:
                if isinstance(r, dict):
                    dict_results.append(r)
                else:
                    dict_results.append(dict(r))
            return dict_results, None, None

        return [], None, None

    def session(self, _database: str | None = None) -> 'SurrealDBDriverSession':
        return SurrealDBDriverSession(self)

    async def close(self):
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def delete_all_indexes(self):
        await self._ensure_client()
        # SurrealDB: Remove all index definitions
        tables = [
            'entity',
            'episodic',
            'community',
            'saga',
            'relates_to',
            'mentions',
            'has_member',
            'has_episode',
            'next_episode',
        ]
        for table in tables:
            try:
                info = await self._client.query(f'INFO FOR TABLE {table}')
                if info and isinstance(info, list) and len(info) > 0:
                    table_info = info[0]
                    if isinstance(table_info, dict) and 'indexes' in table_info:
                        for idx_name in table_info['indexes']:
                            await self._client.query(
                                f'REMOVE INDEX IF EXISTS {idx_name} ON TABLE {table}'
                            )
            except Exception as e:
                logger.warning(f'Error removing indexes from {table}: {e}')

    async def build_indices_and_constraints(self, delete_existing: bool = False):
        await self._ensure_client()
        if delete_existing:
            await self.delete_all_indexes()

        # Create schema (tables + fields)
        try:
            await self._client.query(SCHEMA_QUERIES)
        except Exception as e:
            logger.warning(f'Error creating SurrealDB schema (may already exist): {e}')

        # Create indexes
        try:
            await self._client.query(INDEX_QUERIES)
        except Exception as e:
            logger.warning(f'Error creating SurrealDB indexes (may already exist): {e}')

    def clone(self, database: str) -> 'SurrealDBDriver':
        return SurrealDBDriver(
            url=self._url,
            namespace=self._namespace,
            database=database,
            username=self._username,
            password=self._password,
        )


class SurrealDBDriverSession(GraphDriverSession):
    provider = GraphProvider.SURREALDB

    def __init__(self, driver: SurrealDBDriver):
        self.driver = driver

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass

    async def close(self):
        pass

    async def execute_write(self, func, *args, **kwargs):
        return await func(self, *args, **kwargs)

    async def run(self, query: str | list, **kwargs: Any) -> Any:
        if isinstance(query, list):
            for q, params in query:
                await self.driver.execute_query(q, **params)
        else:
            await self.driver.execute_query(query, **kwargs)
        return None
