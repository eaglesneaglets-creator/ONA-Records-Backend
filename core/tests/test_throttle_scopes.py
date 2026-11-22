"""
Every throttle scope a view uses has a rate.

DRF only looks a scope's rate up when a request arrives, so a scope with no
rate is not caught at startup: every request to that view is a 500
(ImproperlyConfigured). That shipped once, to staging, when 'media' was
added to base's rates but staging replaced the whole table.
"""

import re
from pathlib import Path

from django.conf import settings
from django.urls import URLPattern, URLResolver, get_resolver

ROOT = Path(settings.BASE_DIR)


def _views():
    def walk(patterns):
        for p in patterns:
            if isinstance(p, URLResolver):
                yield from walk(p.url_patterns)
            elif isinstance(p, URLPattern):
                cls = getattr(p.callback, 'cls', None) or getattr(p.callback, 'view_class', None)
                if cls is not None:
                    yield cls
    return set(walk(get_resolver().url_patterns))


def test_every_scope_a_view_uses_has_a_rate():
    rates = settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']
    missing = {
        f'{v.__module__}.{v.__name__}: {v.throttle_scope}'
        for v in _views()
        if getattr(v, 'throttle_scope', None) and v.throttle_scope not in rates
    }
    assert not missing, missing


def test_environment_settings_add_to_base_rates_rather_than_replace_them():
    # staging.py and local.py tune the rates; each must start from base's.
    for name in ('staging.py', 'local.py'):
        src = (ROOT / 'config' / 'settings' / name).read_text(encoding='utf-8')
        if 'DEFAULT_THROTTLE_RATES' in src:
            assert re.search(r"\*\*REST_FRAMEWORK\['DEFAULT_THROTTLE_RATES'\]", src), name
