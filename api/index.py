"""Vercel entry point: the whole site is one Django WSGI function.

vercel.json rewrites every path here; Django then routes the API, the rendered
pages and the static assets itself, exactly as it does under runserver.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashawy_backend.wsgi import application  # noqa: E402

app = application
