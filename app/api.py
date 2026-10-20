import logging
from contextlib import asynccontextmanager
from typing import Annotated
from uuid import UUID

import jwt
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.config import Settings
from app.models import (
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    Profile,
    RegisterRequest,
    RegisterResponse,
)
from app.repository import UserRepository
from app.security import Security

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        with ConnectionPool(
            settings.database_url.get_secret_value(),
            min_size=1,
            max_size=10,
            timeout=5,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 5,
                "options": "-c statement_timeout=5000",
            },
            open=False,
        ) as pool:
            pool.wait(timeout=10)
            app.state.pool = pool
            app.state.users = UserRepository(pool)
            app.state.security = Security(settings)
            yield

    app = FastAPI(
        title="social network hw1",
        lifespan=lifespan,
        responses={400: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"message": "Невалидные данные запроса"})

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code, content={"message": exc.detail}, headers=exc.headers
        )

    async def database_unavailable(_request: Request, _exc: Exception):
        logger.warning("Database temporarily unavailable")
        return JSONResponse(
            status_code=503,
            content={"message": "База данных временно недоступна"},
            headers={"Retry-After": "5"},
        )

    for error in (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout):
        app.add_exception_handler(error, database_unavailable)

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception):
        logger.error("Internal server error (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"message": "Внутренняя ошибка сервера"})

    def users(request: Request) -> UserRepository:
        return request.app.state.users

    def security(request: Request) -> Security:
        return request.app.state.security

    Users = Annotated[UserRepository, Depends(users)]
    Auth = Annotated[Security, Depends(security)]

    def current_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        auth: Auth,
    ) -> UUID:
        unauthorized = HTTPException(
            status_code=401,
            detail="Требуется действительный Bearer-токен",
            headers={"WWW-Authenticate": "Bearer"},
        )
        if credentials is None:
            raise unauthorized
        try:
            return auth.token_subject(credentials.credentials)
        except jwt.InvalidTokenError, ValueError, TypeError, AttributeError:
            raise unauthorized from None

    def find_profile(repo: UserRepository, user_id: UUID) -> dict:
        profile = repo.get_profile(user_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Анкета не найдена")
        return profile

    @app.post("/user/register", response_model=RegisterResponse, tags=["users"])
    def register(body: RegisterRequest, repo: Users, auth: Auth):
        password_hash = auth.hash_password(body.password.get_secret_value())
        return {"user_id": repo.create(body, password_hash)}

    @app.post(
        "/login",
        response_model=LoginResponse,
        tags=["auth"],
        responses={404: {"model": ErrorResponse}},
    )
    def login(body: LoginRequest, repo: Users, auth: Auth):
        password_hash = repo.get_password_hash(body.id)
        valid = auth.verify_password(
            body.password.get_secret_value(), password_hash or auth.dummy_hash
        )
        if password_hash is None or not valid:
            raise HTTPException(status_code=404, detail="Неверный идентификатор или пароль")
        return {"token": auth.issue_token(body.id)}

    @app.get(
        "/user/get/{id}",
        response_model=Profile,
        tags=["users"],
        responses={404: {"model": ErrorResponse}},
    )
    def get_user(id: UUID, repo: Users):
        return find_profile(repo, id)

    @app.get(
        "/user/me",
        response_model=Profile,
        tags=["users"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    def get_me(user_id: Annotated[UUID, Depends(current_user)], repo: Users):
        return find_profile(repo, user_id)

    @app.get("/health", tags=["system"])
    def health(request: Request):
        with request.app.state.pool.connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "ok"}

    # в OpenAPI не должно быть 422: валидация отдаёт 400
    schema = app.openapi()
    for path in schema["paths"].values():
        for operation in path.values():
            operation.get("responses", {}).pop("422", None)

    return app
