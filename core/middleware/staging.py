"""
Staging-only middleware.

Add to MIDDLEWARE in staging settings, near the top so it applies to every
response including errors:

    MIDDLEWARE = ['core.middleware.staging.NoIndexMiddleware', *MIDDLEWARE]
"""

from django.conf import settings


class NoIndexMiddleware:
    """
    Tell search engines not to index this deployment.

    robots.txt is not sufficient on its own: it asks crawlers not to *fetch*
    a page, but a URL discovered elsewhere (a shared link, a referrer header)
    can still be indexed without being fetched. X-Robots-Tag on the response
    is the instruction that actually prevents indexing.

    This matters for a marketplace: a staging copy of a professional's profile
    competing with the real one in search results is both an SEO problem and a
    privacy one, since staging may hold copies of real data.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        if not getattr(settings, 'STAGING_NOINDEX', False):
            # Django removes middleware that raises MiddlewareNotUsed, so this
            # costs nothing in production.
            from django.core.exceptions import MiddlewareNotUsed
            raise MiddlewareNotUsed

    def __call__(self, request):
        response = self.get_response(request)
        response['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
        return response
