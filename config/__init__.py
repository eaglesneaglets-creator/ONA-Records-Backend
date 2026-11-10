"""
Load the Celery app whenever Django starts.

Without this, @shared_task binds to Celery's default app, which ignores
every CELERY_* setting: .delay() then tries a local RabbitMQ instead of
Redis, and CELERY_TASK_ALWAYS_EAGER is not honoured. The line was in the
reference project and was lost in the port; Task 11 is the first code to
queue a task, which is how it surfaced.
"""

from .celery import app as celery_app

__all__ = ('celery_app',)
