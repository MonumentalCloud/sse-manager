"""Deploy entrypoint.

GenOS code serving builds the repo with a build command and starts it with a
start command — no Dockerfile, no package install step guaranteed. This file
makes both commands trivial regardless of how the platform runs them:

    start command:  python main.py
                    (or: uvicorn main:app --host 0.0.0.0 --port 8080)

It puts src/ on the path so the app imports without `pip install .`, and when
run directly it reads the port from $PORT so the platform's choice wins.
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))

from sse_relay.app import app  # noqa: E402  (path setup must come first)

__all__ = ["app"]

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
