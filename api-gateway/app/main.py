# File: app/main.py
# api-gateway/app/main.py
import os
from fastapi import FastAPI, Request, Depends, HTTPException, status
from typing import Optional, List, Set
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import httpx
import structlog
import uvicorn
import time
import uuid
import logging
import re

# --- Configuración de Logging PRIMERO ---
from app.core.logging_config import setup_logging
setup_logging()

# --- Importaciones Core y DB ---
from app.core.config import settings
from app.db import postgres_client

# --- !!! REMOVE Global HTTP Client Variable !!! ---
# proxy_http_client: Optional[httpx.AsyncClient] = None # REMOVED

# --- Importar Routers ---
from app.routers import gateway_router, user_router

log = structlog.get_logger("atenex_api_gateway.main")

# --- Lifespan Manager (Uses app.state) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Application startup sequence initiated...")
    http_client_instance: Optional[httpx.AsyncClient] = None
    db_pool_ok = False

    # 1. Initialize HTTP Client
    try:
        log.info("Initializing HTTPX client for application state...")
        limits = httpx.Limits(
            max_keepalive_connections=settings.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
            max_connections=settings.HTTP_CLIENT_MAX_CONNECTIONS
        )
        timeout = httpx.Timeout(settings.HTTP_CLIENT_TIMEOUT, connect=10.0)
        http_client_instance = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=False,
            http2=True
        )
        app.state.http_client = http_client_instance # <-- Attach to app.state
        log.info("HTTPX client initialized and attached to app.state successfully.")
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize HTTPX client during startup!", error=str(e))
        app.state.http_client = None # Ensure state is None if init fails

    # 2. Initialize and Verify PostgreSQL Connection
    log.info("Initializing and verifying PostgreSQL connection pool...")
    try:
        pool = await postgres_client.get_db_pool()
        if pool:
            db_pool_ok = await postgres_client.check_db_connection()
            if db_pool_ok:
                log.info("PostgreSQL connection pool initialized and connection verified.")
            else:
                log.critical("PostgreSQL pool initialized BUT connection check failed!")
                await postgres_client.close_db_pool()
        else:
            log.critical("PostgreSQL connection pool initialization returned None!")
    except Exception as e:
        log.exception("CRITICAL: Failed to initialize or verify PostgreSQL connection!", error=str(e))
        db_pool_ok = False # Ensure DB is marked as not OK

    # Log final readiness check
    if getattr(app.state, 'http_client', None) and db_pool_ok:
        log.info("Application startup sequence complete. Dependencies ready.")
    else:
        log.error("Application startup sequence FAILED. Check HTTP Client or DB init.",
                  http_client_ready=bool(getattr(app.state, 'http_client', None)),
                  db_ready=db_pool_ok)
        # Optional: raise an error to prevent startup if critical dependencies failed
        # raise RuntimeError("Critical dependencies failed to initialize during startup.")

    yield # <--- Application runs here

    log.info("Application shutdown sequence initiated...")
    # 1. Close HTTP Client (Retrieve from app.state)
    client_to_close = getattr(app.state, 'http_client', None)
    if client_to_close and not client_to_close.is_closed:
        log.info("Closing HTTPX client from app.state...")
        try:
            await client_to_close.aclose()
            log.info("HTTPX client closed successfully.")
        except Exception as e:
            log.exception("Error closing HTTPX client during shutdown.", error=str(e))
    else:
        log.info("HTTPX client was not initialized or already closed.")

    # 2. Close PostgreSQL Pool
    log.info("Closing PostgreSQL connection pool...")
    try:
        await postgres_client.close_db_pool()
    except Exception as e:
        log.exception("Error closing PostgreSQL connection pool during shutdown.", error=str(e))

    log.info("Application shutdown sequence complete.")


# --- Create FastAPI App Instance ---
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="Atenex API Gateway: Single entry point, JWT auth, routing via explicit HTTP calls.",
    version="1.0.3", # Version bump
    lifespan=lifespan, # Use the updated lifespan manager
)

# --- Middlewares (No changes needed here) ---
# CORS Configuration (using regex)
vercel_pattern = ""
if settings.VERCEL_FRONTEND_URL:
    # Simplified regex derivation (adjust if needed)
    base_vercel_url = settings.VERCEL_FRONTEND_URL.split("://")[1] # Remove scheme
    base_vercel_url = re.sub(r"(-git-[a-z0-9-]+)?(-[a-z0-9]+)?\.vercel\.app", ".vercel.app", base_vercel_url)
    escaped_base = re.escape(base_vercel_url).replace(r"\.vercel\.app", "")
    vercel_pattern = rf"(https://{escaped_base}(-[a-z0-9-]+)*\.vercel\.app)" # Re-add scheme
else:
    log.warning("VERCEL_FRONTEND_URL not set for CORS.")
localhost_pattern = r"(http://localhost:300[0-9])"
allowed_origin_patterns = [localhost_pattern]
if vercel_pattern: allowed_origin_patterns.append(vercel_pattern)
final_regex = rf"^{ '|'.join(allowed_origin_patterns) }$"
log.info("Configuring CORS middleware", allow_origin_regex=final_regex)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=final_regex, allow_credentials=True, allow_methods=["*"],
    allow_headers=["*"], expose_headers=["X-Request-ID", "X-Process-Time"], max_age=600,
)

# Request Context/Timing/Logging Middleware
@app.middleware("http")
async def add_request_context_timing_logging(request: Request, call_next):
    start_time = time.perf_counter()
    request_id = request.headers.get("x-request-id", str(uuid.uuid4()))
    request.state.request_id = request_id # Attach request_id to request state

    request_log = log.bind(request_id=request_id, method=request.method, path=request.url.path,
                           client_ip=request.client.host if request.client else "unknown",
                           origin=request.headers.get("origin", "N/A"))

    if request.method == "OPTIONS": request_log.info("OPTIONS preflight request received")
    else: request_log.info("Request received")

    response = None
    status_code = 500 # Default
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        process_time_ms = (time.perf_counter() - start_time) * 1000
        request_log.exception("Unhandled exception", status_code=500, error=str(e), proc_time=round(process_time_ms,2))
        response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
        # Add CORS headers to manual error responses if needed
        origin = request.headers.get("Origin")
        if origin and re.match(final_regex, origin):
             response.headers["Access-Control-Allow-Origin"] = origin
             response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        if response:
            process_time_ms = (time.perf_counter() - start_time) * 1000
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Process-Time"] = f"{process_time_ms:.2f}ms"
            log_level = "debug" if request.url.path == "/health" else "info"
            log_func = getattr(request_log.bind(status_code=status_code), log_level)
            if request.method != "OPTIONS": log_func("Request completed", proc_time=round(process_time_ms, 2))
    return response


# --- Include Routers ---
log.info("Including application routers...")
app.include_router(user_router.router)
app.include_router(gateway_router.router)
log.info("Routers included successfully.")

# --- Root & Health Endpoints ---
@app.get("/", tags=["General"], summary="Root endpoint")
async def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running!"}

@app.get("/health", tags=["Health"], summary="Health check endpoint")
async def health_check(request: Request): # Inject request to access app.state
    health_status = {"status": "healthy", "service": settings.PROJECT_NAME, "checks": {}}
    db_ok = await postgres_client.check_db_connection()
    health_status["checks"]["database_connection"] = "ok" if db_ok else "failed"

    # Check HTTP Client from app.state
    http_client = getattr(request.app.state, 'http_client', None)
    http_client_ok = http_client is not None and not http_client.is_closed
    health_status["checks"]["http_client"] = "ok" if http_client_ok else "failed"

    if not db_ok or not http_client_ok:
        health_status["status"] = "unhealthy"
        log.warning("Health check determined service unhealthy", checks=health_status["checks"])
        return JSONResponse(content=health_status, status_code=503)

    log.debug("Health check successful", checks=health_status["checks"])
    return health_status


# --- Main Execution (for local development) ---
if __name__ == "__main__":
    print(f"Starting {settings.PROJECT_NAME} using Uvicorn for local dev...")
    uvicorn.run(
        "app.main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)),
        reload=True, log_level=settings.LOG_LEVEL.lower(),
    )