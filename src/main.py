# App entrypoint: creates the FastAPI app, registers routers, runs setup on startup.

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.example import router as example_router
from src.core.logging_config import setup_logging
from src.core.app_lifespan import lifespan

setup_logging()

app = FastAPI(title="Project Backend Template", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(example_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
