"""
Shared pytest fixtures.

The throttle reset below is not optional. DRF's rate limiter counts requests
in the cache, and the cache is process-wide — it does not reset between tests.
Registration and login are throttled at 30/hour for anonymous callers, so once
the suite has exercised those endpoints thirty times, every later test gets a
429 regardless of what it is asserting.

That failure is order-dependent, which makes it worse than a plain bug: the
same code passes when a module runs alone and fails in the full suite, and CI
may or may not reproduce it depending on how tests are distributed.
"""

import pytest
from django.core.cache import cache


@pytest.fixture(autouse=True)
def reset_throttles():
    """
    Clear rate-limit counters before every test.

    autouse, because a test that silently gets throttled reports a confusing
    failure (KeyError on a missing response key) rather than an obvious one,
    and remembering to opt in per test is exactly the kind of thing that gets
    forgotten.
    """
    cache.clear()
    yield
    cache.clear()
