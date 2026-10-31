# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""kiko-lite: small standard-library-only Anthropic-to-provider proxy."""

from . import db
from . import resolver
from . import providers
from . import app
from . import health

# Expose kikolib
try:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent / "kikolib"))
    import kikolib
except ImportError:
    kikolib = None
