# Feature-specific helper functions used only by this feature's service logic.

from src.domain.example_feature.constants import EXCITED_SUFFIX, NORMAL_SUFFIX


def format_greeting(name: str, excited: bool) -> str:
    suffix = EXCITED_SUFFIX if excited else NORMAL_SUFFIX
    return f"Hello, {name}{suffix}"
