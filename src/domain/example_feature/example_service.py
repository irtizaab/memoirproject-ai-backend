# Domain layer = the actual business logic for a feature.
# The API layer calls into functions here; this layer can call integrations/ for external services.

import logging
from src.domain.example_feature.utils import format_greeting
from src.domain.example_feature.constants import DEFAULT_GREETING

logger = logging.getLogger(__name__)


def create_greeting(name: str, excited: bool = False) -> str:
    """Example business logic: build a greeting string for a given name."""
    logger.info(f"Creating greeting for name={name}")
    if not name:
        name = DEFAULT_GREETING
    return format_greeting(name, excited)
