"""Cross-file configuration contracts that are easy to break silently."""

from pathlib import Path


SETTINGS_DIR = Path(__file__).resolve().parents[1] / 'settings'
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_local_database_default_matches_compose_host_port():
    local_settings = (SETTINGS_DIR / 'local.py').read_text(encoding='utf-8')
    compose = (PROJECT_ROOT / 'docker-compose.yml').read_text(encoding='utf-8')

    assert 'localhost:5433/ona_dev' in local_settings
    assert '"5433:5432"' in compose


def test_staging_installs_privacy_guards():
    staging_settings = (SETTINGS_DIR / 'staging.py').read_text(encoding='utf-8')

    assert "'core.middleware.staging.NoIndexMiddleware'" in staging_settings
    assert "EMAIL_BACKEND = 'core.email_backends.RedirectingEmailBackend'" in staging_settings
