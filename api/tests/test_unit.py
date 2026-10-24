"""Pure unit tests: no database needed."""

import io
import time
import uuid

import chromadb
import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from starlette.datastructures import Headers, UploadFile

from app import llm, storage
from app.errors import AppError
from app.vectorstore import ensure_collections, tenant_where

WS = str(uuid.uuid4())
EP = str(uuid.uuid4())


def test_tenant_where_always_has_workspace():
    assert tenant_where(WS) == {"workspace_id": WS}
    where = tenant_where(WS, episode_id={"$ne": EP}, published_ts={"$lte": 5})
    assert where == {
        "$and": [{"workspace_id": WS}, {"episode_id": {"$ne": EP}}, {"published_ts": {"$lte": 5}}]
    }
    # a caller cannot override the tenant filter through a condition
    with pytest.raises(TypeError):
        tenant_where(WS, workspace_id="other")
    with pytest.raises(ValueError):
        tenant_where("")


def test_collections_check_embedding_model():
    client = chromadb.EphemeralClient()
    cols = ensure_collections(client, "model-a")
    assert set(cols) == {"transcript_windows", "claims"}
    assert cols["claims"].metadata == {"embedding_model": "model-a", "schema_version": 1}
    assert ensure_collections(client, "model-a")["claims"].name == "claims"
    with pytest.raises(RuntimeError, match="embedding model"):
        ensure_collections(client, "model-b")
    for name in cols:
        client.delete_collection(name)


def test_llm_factories_use_fakes():
    assert llm.chat_model().model == "gemini-2.5-flash"
    assert llm.chat_model(strong=True).model == "gemini-2.5-pro"
    assert llm.embeddings().model == "models/gemini-embedding-001"
    fake_chat = FakeListChatModel(responses=["hi"])
    fake_emb = DeterministicFakeEmbedding(size=8)
    llm.use_fakes(chat=fake_chat, embeddings=fake_emb)
    try:
        assert llm.chat_model(strong=True) is fake_chat
        assert llm.embeddings().embed_query("x") == fake_emb.embed_query("x")
    finally:
        llm.use_fakes()


def test_storage_paths():
    rel = storage.object_path(WS, EP, "MP3")
    assert rel == f"{WS}/{EP}.mp3"
    for bad in (
        f"../{EP}.mp3",
        f"not-a-uuid/{EP}.mp3",
        f"{WS.upper()}/{EP}.mp3",
        f"{WS}/{EP}.exe",
        f"{WS}/x/{EP}.mp3",
        f"{WS}/..{EP}.mp3",
    ):
        with pytest.raises(ValueError):
            storage.validate_path(bad)


def test_signed_url_valid_expired_tampered():
    rel = f"{WS}/{EP}.mp3"
    now = time.time()
    url = storage.signed_url(rel, now=now)
    query = dict(p.split("=") for p in url.split("?")[1].split("&"))
    exp, sig = int(query["exp"]), query["sig"]
    assert exp == int(now + 600)
    assert storage.verify_signature(rel, exp, sig, now=now)
    assert not storage.verify_signature(rel, exp, sig, now=exp + 1)  # expired
    assert not storage.verify_signature(rel, exp + 60, sig, now=now)  # exp changed
    assert not storage.verify_signature(rel, exp, "0" * 64, now=now)  # sig changed
    other = f"{uuid.uuid4()}/{EP}.mp3"
    assert not storage.verify_signature(other, exp, sig, now=now)  # other workspace


def _upload(data: bytes, filename="ep.mp3", content_type="audio/mpeg") -> UploadFile:
    return UploadFile(
        io.BytesIO(data), filename=filename, headers=Headers({"content-type": content_type})
    )


async def test_save_upload_enforces_cap_and_type():
    rel = storage.object_path(WS, uuid.uuid4(), "mp3")
    data = b"x" * (storage.CHUNK + 10)
    assert await storage.save_upload(_upload(data), rel) == len(data)
    assert storage.absolute(rel).read_bytes() == data

    rel2 = storage.object_path(WS, uuid.uuid4(), "mp3")
    with pytest.raises(AppError) as err:
        await storage.save_upload(_upload(data), rel2, max_bytes=storage.CHUNK)
    assert err.value.status_code == 413
    assert not storage.absolute(rel2).exists()
    assert not list(storage.absolute(rel2).parent.glob("*.part"))

    with pytest.raises(AppError):
        await storage.save_upload(_upload(b"x", content_type="text/plain"), rel2)
    with pytest.raises(AppError):
        await storage.save_upload(_upload(b"x", filename="ep.wav"), rel2)
