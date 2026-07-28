"""GenOS code serving native entrypoint.

The platform's container harness (gunicorn + UvicornWorker) has its own scaffold
main module that does `from service import service`. This file satisfies it:
`service` is our ASGI app. main.py covers the harness's other path, where a
repo-provided main.py is used directly.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from sse_relay.app import app  # noqa: E402  (path setup must come first)

service = app
