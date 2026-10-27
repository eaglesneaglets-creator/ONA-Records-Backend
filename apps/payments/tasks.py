"""Celery tasks for payments."""

from celery import shared_task


@shared_task(ignore_result=True)
def reconcile_paystack() -> dict:
    """Settle online payments no webhook has settled (apps/payments/checkout.py)."""
    from .checkout import reconcile_pending
    return reconcile_pending()
