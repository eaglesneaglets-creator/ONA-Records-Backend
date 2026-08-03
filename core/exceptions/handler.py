"""
Consistent API error shape.

Every error returns the same envelope so the frontend has one thing to parse:

    {"error": {"code": "validation_error", "detail": {...}}}
"""

import logging

from rest_framework.views import exception_handler as drf_handler

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    response = drf_handler(exc, context)
    if response is None:
        # Unhandled — let Django's 500 handling and Sentry take it.
        return None

    code = getattr(exc, 'default_code', 'error')
    response.data = {'error': {'code': code, 'detail': response.data}}
    return response
