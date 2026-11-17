"""Which database a Vercel deployment connects to (dashawy_backend.settings)."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = (
    'import json, django;'
    'from django.conf import settings;'
    'db = settings.DATABASES["default"];'
    'print(json.dumps({"engine": db["ENGINE"], "host": db.get("HOST", ""), "name": str(db["NAME"])}))'
)


def load_settings(**env):
    """Import the settings in a fresh process (they are read once, at import)."""
    clean = {k: v for k, v in os.environ.items() if not k.startswith(('VERCEL', 'DATABASE', 'PREVIEW_'))}
    clean.update(
        DJANGO_SETTINGS_MODULE='dashawy_backend.settings',
        DJANGO_SECRET_KEY='x' * 50,
        DJANGO_DEBUG='False',
        **env,
    )
    # "test" must not be in argv, or settings treat the run as a test run.
    return subprocess.run(
        [sys.executable, '-c', PROBE], cwd=ROOT, env=clean, capture_output=True, text=True
    )


PROD = 'postgres://owner:pw@prod-host.neon.tech/neondb'
PREVIEW = 'postgres://owner:pw@preview-host.neon.tech/neondb'


class DeploymentDatabaseTests(unittest.TestCase):
    def test_production_uses_database_url(self):
        out = load_settings(VERCEL_ENV='production', DATABASE_URL=PROD)
        self.assertEqual(json.loads(out.stdout)['host'], 'prod-host.neon.tech', out.stderr)

    def test_preview_never_uses_the_production_database(self):
        out = load_settings(VERCEL_ENV='preview', DATABASE_URL=PROD)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn('PREVIEW_DATABASE_URL', out.stderr)
        self.assertNotIn('prod-host', out.stdout)

    def test_preview_uses_its_own_database(self):
        out = load_settings(VERCEL_ENV='preview', DATABASE_URL=PROD, PREVIEW_DATABASE_URL=PREVIEW)
        self.assertEqual(json.loads(out.stdout)['host'], 'preview-host.neon.tech', out.stderr)

    def test_local_development_keeps_sqlite(self):
        out = load_settings()
        self.assertEqual(json.loads(out.stdout)['engine'], 'django.db.backends.sqlite3', out.stderr)
