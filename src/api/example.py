# API layer = FastAPI routes. Keep this thin: validate request, call domain/, return response.

import logging
from fastapi import APIRouter, HTTPException
from src.models.example_models import GreetingRequest, GreetingResponse
from src.domain.example_feature.example_service import create_greeting

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/example", tags=["example"])


@router.post("/greet", response_model=GreetingResponse)
async def greet(request: GreetingRequest):
    try:
        message = create_greeting(request.name, request.excited)
        return GreetingResponse(message=message, success=True)
    except Exception as e:
        logger.error(f"Error creating greeting: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
