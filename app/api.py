import logging
from contextlib import ExitStack, asynccontextmanager
from typing import Annotated
from uuid import UUID

import jwt
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
from redis.exceptions import RedisError

from app.config import Settings
from app.feed import FeedCache, redis_client
from app.models import (
    ErrorResponse,
    LoginRequest,
    LoginResponse,
    Post,
    PostCreate,
    PostUpdate,
    Profile,
    RegisterRequest,
    RegisterResponse,
    SearchParams,
)
from app.posts import PostRepository
from app.repository import UserRepository
from app.routing import ReadRouter
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
                "application_name": "social-writer",
                "options": "-c statement_timeout=5000",
            },
            open=False,
        ) as pool:
            pool.wait(timeout=10)
            app.state.pool = pool
            with ExitStack() as stack:
                readers = stack.enter_context(ReadRouter(settings)) if settings.read_hosts else None
                app.state.users = UserRepository(pool, readers)
                app.state.posts = PostRepository(pool)
                cache = stack.enter_context(redis_client(settings.redis_url.get_secret_value()))
                app.state.feed = FeedCache(pool, cache)
                app.state.security = Security(settings)
                yield

    app = FastAPI(
        title="social network",
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

    @app.exception_handler(RedisError)
    async def feed_unavailable(_request: Request, _exc: RedisError):
        logger.warning("Feed cache temporarily unavailable")
        return JSONResponse(
            status_code=503,
            content={"message": "Кэш ленты временно недоступен"},
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, exc: Exception):
        logger.error("Internal server error (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"message": "Внутренняя ошибка сервера"})

    def users(request: Request) -> UserRepository:
        return request.app.state.users

    def security(request: Request) -> Security:
        return request.app.state.security

    def posts(request: Request) -> PostRepository:
        return request.app.state.posts

    def feed_cache(request: Request) -> FeedCache:
        return request.app.state.feed

    Users = Annotated[UserRepository, Depends(users)]
    Auth = Annotated[Security, Depends(security)]
    Posts = Annotated[PostRepository, Depends(posts)]
    Feed = Annotated[FeedCache, Depends(feed_cache)]

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

    CurrentUser = Annotated[UUID, Depends(current_user)]
    authenticated = {401: {"model": ErrorResponse}}

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

    @app.get("/user/search", response_model=list[Profile], tags=["users"])
    def search_users(params: Annotated[SearchParams, Query()], repo: Users):
        return repo.search(params.first_name, params.last_name)

    @app.get(
        "/user/me",
        response_model=Profile,
        tags=["users"],
        responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    )
    def get_me(user_id: CurrentUser, repo: Users):
        return find_profile(repo, user_id)

    @app.put("/friend/add/{user_id}", tags=["friends"], responses=authenticated)
    @app.put("/friend/set/{user_id}", tags=["friends"], responses=authenticated)
    def add_friend(user_id: UUID, viewer: CurrentUser, repo: Posts) -> Response:
        if user_id == viewer:
            raise HTTPException(status_code=400, detail="Нельзя добавить себя в друзья")
        try:
            repo.add_friend(viewer, user_id)
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(status_code=400, detail="Пользователь не найден") from None
        return Response()

    @app.put("/friend/delete/{user_id}", tags=["friends"], responses=authenticated)
    def delete_friend(user_id: UUID, viewer: CurrentUser, repo: Posts) -> Response:
        repo.delete_friend(viewer, user_id)
        return Response()

    @app.post("/post/create", tags=["posts"], responses=authenticated)
    def create_post(body: PostCreate, author: CurrentUser, repo: Posts) -> UUID:
        try:
            return repo.create(author, body.text)
        except psycopg.errors.ForeignKeyViolation:
            raise HTTPException(status_code=400, detail="Пользователь не найден") from None

    @app.put("/post/update", tags=["posts"], responses=authenticated)
    def update_post(body: PostUpdate, author: CurrentUser, repo: Posts) -> Response:
        if not repo.update(author, body.id, body.text):
            raise HTTPException(status_code=400, detail="Пост автора не найден")
        return Response()

    @app.put("/post/delete/{id}", tags=["posts"], responses=authenticated)
    def delete_post(id: UUID, author: CurrentUser, repo: Posts) -> Response:
        if not repo.delete(author, id):
            raise HTTPException(status_code=400, detail="Пост автора не найден")
        return Response()

    @app.get("/post/get/{id}", response_model=Post, tags=["posts"])
    def get_post(id: UUID, repo: Posts):
        post = repo.get(id)
        if post is None:
            raise HTTPException(status_code=400, detail="Пост не найден")
        return post

    @app.get("/post/feed", response_model=list[Post], tags=["posts"], responses=authenticated)
    def get_feed(
        user_id: CurrentUser,
        feed: Feed,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1)] = 10,
    ):
        return feed.page(user_id, offset, limit)

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
