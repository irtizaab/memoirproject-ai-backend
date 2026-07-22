# Pydantic models = the request/response "shape" for a feature.
# One models file per feature, matching the name used in api/ and domain/.

from pydantic import BaseModel, Field
from typing import Optional


class GreetingRequest(BaseModel):
    name: str = Field(..., description="Name of the person to greet")
    excited: Optional[bool] = Field(default=False, description="Whether to add an exclamation mark")


class GreetingResponse(BaseModel):
    message: str
    success: bool
    error: Optional[str] = None
