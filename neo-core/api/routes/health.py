"""Health check routes."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "healthy", "service": "neo-core"}


@router.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "service": "Neo Core API",
        "version": "2.0.0",
        "description": "Modular AI Agent orchestration engineeyuuuuuyyyyyyyyeiiiiiiiiiiieee",
    }
