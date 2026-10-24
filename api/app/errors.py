import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)

DEFAULT_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "file_too_large",
    422: "validation_error",
    429: "rate_limited",
}


class AppError(HTTPException):
    """Raise this from routes: AppError(404, "not_found", "Workspace not found")."""

    def __init__(
        self, status_code: int, code: str, message: str, headers: dict | None = None
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.code = code


def error_body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = getattr(exc, "code", None) or DEFAULT_CODES.get(exc.status_code, "error")
    message = exc.detail if isinstance(exc.detail, str) else code
    return JSONResponse(error_body(code, message), status_code=exc.status_code, headers=exc.headers)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", []) if p != "body")
    message = f"{where}: {first.get('msg', 'invalid input')}" if where else "invalid input"
    return JSONResponse(error_body("validation_error", message), status_code=422)


async def _rate_limited(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(error_body("rate_limited", "Too many requests"), status_code=429)


async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(error_body("internal_error", "Internal server error"), status_code=500)


def install_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(RateLimitExceeded, _rate_limited)
    app.add_exception_handler(Exception, _unexpected)
