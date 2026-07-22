# Integrations = thin wrappers around external services (LLM providers, databases, cloud storage).
# No feature-specific logic here — just how to connect/call the external service.

import os
import logging

logger = logging.getLogger(__name__)


def get_llm_client():
    """Example: set up a connection to an external LLM provider."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.warning("OPENAI_API_KEY not found in environment variables")
    # In a real project this would return an actual client, e.g. ChatOpenAI(api_key=api_key)
    return {"provider": "openai", "api_key_set": bool(api_key)}
