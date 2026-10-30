# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
"""kikolib - small library for kiko-lite: threads (no asyncio), standard library only.

Rule: modules here import only the Python standard library and other kikolib
modules. OS facilities are reached through ctypes or subprocess. Each pluggable
part has an interface plus a factory (see factory.py) that picks an implementation.
http.py is a stdlib http backend; server.py (threaded server) and sse.py are done. See kikolib/README.md.
"""

import sys

if sys.version_info < (3, 11):
    raise RuntimeError("kikolib needs Python 3.11 or newer")

__all__ = [
    "dotenv",
    "factory",
    "http_client",
    "keystore",
    "native_utils",
    "posix_utils",
    "retry",
    "server",
    "sse",
    "tokens",
    "yaml_lite",
]

__version__ = "0.1.0"
