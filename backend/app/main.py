from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.tasks import router

app = FastAPI(title="Deep Research", version="0.1.0")

# 本地 demo 放开跨域，方便后续接入前端
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
