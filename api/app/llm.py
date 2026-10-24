"""Gemini factories. Tests inject langchain_core fakes via use_fakes(); never call Gemini."""

from functools import lru_cache

from langchain_core.embeddings import Embeddings
from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings

from app.config import get_settings

_fakes: dict[str, object] = {}


def use_fakes(chat: BaseChatModel | None = None, embeddings: Embeddings | None = None) -> None:
    """Replace the real models (pass nothing to go back to Gemini)."""
    _fakes.clear()
    if chat is not None:
        _fakes["chat"] = chat
    if embeddings is not None:
        _fakes["embeddings"] = embeddings


@lru_cache
def _chat(model: str, api_key: str) -> BaseChatModel:
    # pydantic-settings reads .env without exporting it, so pass the key explicitly
    return ChatGoogleGenerativeAI(model=model, temperature=0, google_api_key=api_key or None)


@lru_cache
def _embeddings(model: str, api_key: str) -> Embeddings:
    return GoogleGenerativeAIEmbeddings(model=model, google_api_key=api_key or None)


def chat_model(strong: bool = False) -> BaseChatModel:
    if "chat" in _fakes:
        return _fakes["chat"]  # type: ignore[return-value]
    s = get_settings()
    return _chat(s.GEMINI_MODEL_STRONG if strong else s.GEMINI_MODEL, s.GOOGLE_API_KEY)


def embeddings() -> Embeddings:
    if "embeddings" in _fakes:
        return _fakes["embeddings"]  # type: ignore[return-value]
    s = get_settings()
    return _embeddings(s.GEMINI_EMBEDDING_MODEL, s.GOOGLE_API_KEY)
