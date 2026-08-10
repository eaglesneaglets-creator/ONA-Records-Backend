"""
Cloudinary storage.

Adapted from the Eagles & Eaglets implementation. The valuable idea carried
over is the upload CONTEXT: a caller must name what kind of upload this is,
and the allowed MIME types and size ceiling are looked up from that name
rather than taken from whatever the client claimed.

Trusting `file.content_type` alone means a caller can label an executable as
an image. The context list is the allow-list, and it lives here rather than
being passed in at the call site.

WHY THIS IS BEHIND AN INTERFACE
    The plan commits to Cloudinary now and a possible S3 move later. Every
    caller goes through upload()/delete()/url_for() so that swap is a change
    to this file, not to every app that stores a portfolio track.
"""

import logging

from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


# Folders are prefixed per environment so staging uploads never appear in the
# production media library, and clearing staging can never touch real files.
def _prefix() -> str:
    return getattr(settings, 'CLOUDINARY_FOLDER_PREFIX', 'ona')


FOLDERS = {
    'profile_photo': 'profiles',
    'portfolio_audio': 'portfolio/audio',
    'portfolio_video': 'portfolio/video',
    'portfolio_image': 'portfolio/images',
    'portfolio_doc': 'portfolio/documents',
    'deliverable': 'deliverables',
    'message_attachment': 'messages',
    'studio_photo': 'studio',
    'misc': 'misc',
}


# Each context names an allow-list and a ceiling. Adding an upload surface
# means adding a context here, which is a deliberate, reviewable act.
UPLOAD_CONTEXTS = {
    # Public-facing, so kept small and image-only.
    'profile_photo': {
        'folder': 'profile_photo',
        'max_bytes': 5 * 1024 * 1024,
        'allowed_mime': {'image/jpeg', 'image/png', 'image/webp'},
        'resource_type': 'image',
    },

    # A professional's portfolio. Audio is the primary case for a record
    # label — generous, because a WAV master is large by nature.
    'portfolio_audio': {
        'folder': 'portfolio_audio',
        'max_bytes': 200 * 1024 * 1024,
        'allowed_mime': {
            'audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/aiff',
            'audio/x-aiff', 'audio/flac', 'audio/mp4', 'audio/aac',
        },
        'resource_type': 'video',   # Cloudinary handles audio under 'video'
    },
    'portfolio_video': {
        'folder': 'portfolio_video',
        'max_bytes': 500 * 1024 * 1024,
        'allowed_mime': {'video/mp4', 'video/quicktime', 'video/webm'},
        'resource_type': 'video',
    },
    'portfolio_image': {
        'folder': 'portfolio_image',
        'max_bytes': 10 * 1024 * 1024,
        'allowed_mime': {'image/jpeg', 'image/png', 'image/webp'},
        'resource_type': 'image',
    },

    # Work delivered on a project. The customer approves against this, and
    # approval releases money — so the file must be exactly what was
    # uploaded, hence no transformation on retrieval.
    'deliverable': {
        'folder': 'deliverable',
        'max_bytes': 500 * 1024 * 1024,
        'allowed_mime': {
            'audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/flac',
            'video/mp4', 'video/quicktime',
            'image/jpeg', 'image/png',
            'application/pdf', 'application/zip',
        },
        'resource_type': 'auto',
    },

    # Attachments inside masked conversations. Deliberately restrictive:
    # a document is a way to pass a phone number past the contact filter,
    # so only briefs and references belong here.
    'message_attachment': {
        'folder': 'message_attachment',
        'max_bytes': 25 * 1024 * 1024,
        'allowed_mime': {
            'image/jpeg', 'image/png', 'image/webp',
            'application/pdf',
            'audio/mpeg',
        },
        'resource_type': 'auto',
    },

    'studio_photo': {
        'folder': 'studio_photo',
        'max_bytes': 10 * 1024 * 1024,
        'allowed_mime': {'image/jpeg', 'image/png', 'image/webp'},
        'resource_type': 'image',
    },
}


IMAGE_PRESETS = {
    'thumbnail': {'width': 150, 'height': 150, 'crop': 'fill'},
    'avatar': {'width': 200, 'height': 200, 'crop': 'fill', 'gravity': 'face'},
    'card': {'width': 600, 'height': 400, 'crop': 'fill'},
    'cover': {'width': 1200, 'height': 400, 'crop': 'fill'},
    'content': {'width': 800, 'crop': 'limit'},
}


def folder_for(kind: str) -> str:
    """Full Cloudinary folder path, environment-prefixed."""
    return '%s/%s' % (_prefix(), FOLDERS.get(kind, FOLDERS['misc']))


def upload(file, *, context: str, **options) -> dict:
    """
    Validate against a named context, then store.

    There is no unvalidated variant on purpose. The reference kept a generic
    upload() for backward compatibility and new code kept reaching for it;
    starting without one avoids that.

    Returns the provider's response. Raises ValidationError on a rejected
    file so DRF renders it as a 400 rather than a 500.
    """
    if not file:
        raise ValidationError({'file': 'A file is required.'})

    rules = UPLOAD_CONTEXTS.get(context)
    if rules is None:
        raise ValidationError({'context': 'Unknown upload context: %r' % context})

    size = getattr(file, 'size', None)
    if size is not None and size > rules['max_bytes']:
        raise ValidationError({
            'file': 'That file is too large. The limit for this upload is %d MB.'
                    % (rules['max_bytes'] // (1024 * 1024)),
        })

    mime = getattr(file, 'content_type', None)
    if mime and mime not in rules['allowed_mime']:
        raise ValidationError({
            'file': '%s files are not accepted here. Allowed: %s.'
                    % (mime, ', '.join(sorted(rules['allowed_mime']))),
        })

    try:
        import cloudinary.uploader
    except ImportError:
        logger.error('cloudinary is not installed; upload rejected')
        raise ValidationError({'file': 'File uploads are not available right now.'})

    try:
        return cloudinary.uploader.upload(
            file,
            folder=folder_for(rules['folder']),
            resource_type=rules.get('resource_type', 'auto'),
            **options,
        )
    except Exception:
        # Never surface the provider's error text: it can contain the API
        # key and internal paths.
        logger.exception('Cloudinary upload failed for context %s', context)
        raise ValidationError({'file': 'The upload failed. Please try again.'})


def delete(public_id: str, resource_type: str = 'image') -> bool:
    """
    Remove a stored file.

    Returns False rather than raising: a failed delete should not roll back
    the database change that prompted it, or a user cannot remove a
    portfolio item because the provider is briefly down. Orphaned files are
    a cleanup job, not a blocked request.
    """
    try:
        import cloudinary.uploader
        cloudinary.uploader.destroy(public_id, resource_type=resource_type)
        return True
    except Exception:
        logger.exception('Cloudinary delete failed for %s', public_id)
        return False


def url_for(public_id: str, preset: str = None, **transforms) -> str:
    """
    Build a delivery URL, optionally transformed.

    Deliverables must never be transformed — the customer approves the file
    as delivered, and approval releases money. Pass no preset for those.
    """
    if not public_id:
        return ''

    try:
        import cloudinary.utils
    except ImportError:
        return ''

    params = dict(IMAGE_PRESETS.get(preset, {}) if preset else {})
    params.update(transforms)
    params.setdefault('secure', True)
    params.setdefault('fetch_format', 'auto')
    params.setdefault('quality', 'auto')

    try:
        url, _ = cloudinary.utils.cloudinary_url(public_id, **params)
        return url
    except Exception:
        logger.exception('Could not build a URL for %s', public_id)
        return ''


def responsive_urls(public_id: str, widths=None) -> dict:
    """Width -> URL, for a srcset."""
    widths = widths or [400, 800, 1200]
    return {w: url_for(public_id, width=w, crop='limit') for w in widths}


def signed_download_url(public_id: str, resource_type: str = 'auto',
                        expires_in: int = 3600) -> str:
    """
    A time-limited URL for something that is not public.

    Deliverables and message attachments belong to two named parties, not to
    anyone holding the link. A permanent public URL for a customer's unreleased
    master is a leak that never closes.
    """
    try:
        import time
        import cloudinary.utils
        url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=resource_type,
            type='authenticated',
            sign_url=True,
            expires_at=int(time.time()) + expires_in,
            secure=True,
        )
        return url
    except Exception:
        logger.exception('Could not sign a URL for %s', public_id)
        return ''
