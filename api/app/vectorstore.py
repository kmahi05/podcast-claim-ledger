"""Chroma client, the two collections, and tenant_where(): the ONLY way to build a `where`.

Always pass our own embeddings (embeddings= / query_embeddings=); collections have no
built-in embedding function.
"""

from typing import Any

import chromadb
from chromadb.api import ClientAPI
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings as ChromaSettings
from chromadb.errors import NotFoundError

COLLECTIONS = ("transcript_windows", "claims")
SCHEMA_VERSION = 1


def open_client(path: str) -> ClientAPI:
    return chromadb.PersistentClient(path=path, settings=ChromaSettings(anonymized_telemetry=False))


def ensure_collections(client: ClientAPI, embedding_model: str) -> dict[str, Collection]:
    """Get or create both collections; refuse a collection built with another embedding model."""
    out: dict[str, Collection] = {}
    for name in COLLECTIONS:
        try:
            col = client.get_collection(name, embedding_function=None)
        except NotFoundError:
            col = client.create_collection(
                name,
                configuration={"hnsw": {"space": "cosine"}},
                metadata={"embedding_model": embedding_model, "schema_version": SCHEMA_VERSION},
                embedding_function=None,
            )
        stored = (col.metadata or {}).get("embedding_model")
        if stored != embedding_model:
            raise RuntimeError(
                f"Chroma collection '{name}' was built with embedding model '{stored}', but "
                f"GEMINI_EMBEDDING_MODEL is '{embedding_model}'. Delete CHROMA_PATH and run "
                "POST /api/v1/admin/reindex, or set GEMINI_EMBEDDING_MODEL back."
            )
        out[name] = col
    return out


def tenant_where(workspace_id: str, **conditions: Any) -> dict[str, Any]:
    """Chroma `where` that always includes the workspace filter, ANDed with `conditions`."""
    if not workspace_id:
        raise ValueError("tenant_where needs a workspace_id")
    clauses: list[dict[str, Any]] = [{"workspace_id": str(workspace_id)}]
    clauses += [{key: value} for key, value in conditions.items()]
    # Chroma rejects an $and with fewer than two clauses
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}
