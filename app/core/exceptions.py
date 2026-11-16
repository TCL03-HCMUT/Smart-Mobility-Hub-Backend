from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

ERROR_CODES = {
    400: "INVALID_INPUT", 401: "UNAUTHENTICATED", 403: "FORBIDDEN",
    404: "NOT_FOUND", 409: "CONFLICT", 422: "INVALID_INPUT",
    503: "TEMPORARILY_UNAVAILABLE",
}

class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict | None = None


class ErrorEnvelope(BaseModel):
    error: ErrorDetail


ERROR_RESPONSES = {
    code: {"description": name, "model": ErrorEnvelope}
    for code, name in {**ERROR_CODES, 500: "INTERNAL_ERROR"}.items()
}


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 400, *, code: str | None = None, details=None):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.details = details


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: str):
        super().__init__(f"{resource} {resource_id} was not found", 404)


def error_response(status_code: int, message: str, *, code=None, details=None, headers=None):
    error = {
        "code": code or ERROR_CODES.get(status_code, "HTTP_ERROR"),
        "message": message,
        "retryable": status_code == 503,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return error_response(exc.status_code, exc.message, code=exc.code, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Validation input can include passwords or other private fields.
        fields = [{"loc": error["loc"], "type": error["type"]} for error in exc.errors()]
        return error_response(422, "Invalid request.", details={"fields": fields})

    @app.exception_handler(HTTPException)
    async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
        return error_response(exc.status_code, str(exc.detail), headers=exc.headers)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        return error_response(500, "Internal server error.", code="INTERNAL_ERROR")
