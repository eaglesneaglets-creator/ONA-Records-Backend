"""
Background work for the website's content: enhancing (upscaling) a photo.

The admin's request returns at once (202) with the photo marked pending; the
worker does the slow part here. On success the upscaled copy becomes a new
media asset and takes the original's place on the site (hero and gallery).
The original stays in the library, so going back is choosing it again.
"""

import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import transaction

from core.models import AuditLog

from .enhance import EnhanceError, upscale
from .models import GalleryImage, MediaAsset, SiteContent

logger = logging.getLogger(__name__)


def _fail(asset: MediaAsset, message: str) -> str:
    asset.enhance_status = MediaAsset.Enhance.FAILED
    asset.enhance_error = message[:200]
    asset.save(update_fields=['enhance_status', 'enhance_error', 'updated_at'])
    return 'failed'


@shared_task(ignore_result=True)
def enhance_media(asset_id: str, actor_id: str | None = None) -> str:
    asset = MediaAsset.objects.filter(pk=asset_id).first()
    if asset is None:
        return 'missing'
    if asset.enhance_status != MediaAsset.Enhance.PENDING:
        return 'not-pending'  # a duplicate delivery, or the request was superseded
    try:
        stored = upscale(asset)
    except EnhanceError as e:
        return _fail(asset, str(e))
    except Exception:  # noqa: BLE001 — the admin must never be left on "Enhancing…"
        logger.exception('Enhancing media %s failed', asset_id)
        return _fail(asset, 'Something went wrong while enhancing. Try again.')

    actor = get_user_model().objects.filter(pk=actor_id).first() if actor_id else None
    with transaction.atomic():
        new = MediaAsset.objects.create(
            alt_text=asset.alt_text, uploaded_by=actor, enhanced_from=asset, **stored,
        )
        hero = SiteContent.objects.filter(hero_image=asset).update(hero_image=new)
        gallery = GalleryImage.objects.filter(media=asset).update(media=new)
        asset.enhance_status = MediaAsset.Enhance.DONE
        asset.enhance_error = ''
        asset.save(update_fields=['enhance_status', 'enhance_error', 'updated_at'])
        AuditLog.record(
            'content.media_enhanced', actor=actor, target=new, enhanced_from=str(asset.pk),
            before=f'{asset.width}x{asset.height}', after=f'{new.width}x{new.height}',
            replaced_hero=bool(hero), replaced_in_gallery=gallery,
        )
    return 'done'
