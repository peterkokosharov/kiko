# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
# AI models: claude-sonnet-4-6
"""tray/dialogs/about.py — about dialog data."""
from dataclasses import dataclass


@dataclass
class AboutData:
    version: str
    author: str
    license_name: str
    litellm_credit: str


def get_about() -> AboutData:
    """Return static about data for the about dialog."""
    return AboutData(
        version="0.3.0",
        author="Peter Kokosharov",
        license_name="MIT",
        litellm_credit="Built on LiteLLM by BerriAI (MIT)",
    )
