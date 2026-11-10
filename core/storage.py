"""
File storage: an S3-compatible bucket (Railway Storage Buckets).

The valuable idea here is the upload CONTEXT: a caller must name what kind of
upload this is, and the declared MIME type and size ceiling are validated
against that name. The context list is the allow-list, and it lives here
rather than being passed in at the call site. This is an ingress guard, not
content inspection; surfaces accepting untrusted files still need signature
and malware checks.

WHY THIS IS BEHIND AN INTERFACE
    Every caller goes through upload()/delete()/url_for(), so changing the
    provider is a change to this file, not to every app that stores a file.
    It has been done once already: this module used to talk to Cloudinary.

WHAT A BUCKET DOES NOT DO
    Railway buckets are private and store bytes exactly as uploaded. There is
    no public URL and no on-the-fly resizing: url_for() returns a presigned
    link that expires, and an image is served at the size it was uploaded.
"""

import logging
import uuid
from functools import lru_cache

from django.conf import settings
from rest_framework.exceptions import ValidationError

logger = logging.getLogger(__name__)


# Keys are prefixed per environment so staging uploads never sit beside
# production files, even if both were ever pointed at one bucket.
def _prefix() -> str:
    return getattr(settings, 'S3_KEY_PREFIX', 'ona')


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
    },
    'portfolio_video': {
        'folder': 'portfolio_video',
        'max_bytes': 500 * 1024 * 1024,
        'allowed_mime': {'video/mp4', 'video/quicktime', 'video/webm'},
    },
    'portfolio_image': {
        'folder': 'portfolio_image',
        'max_bytes': 10 * 1024 * 1024,
        'allowed_mime': {'image/jpeg', 'image/png', 'image/webp'},
    },

    # Work delivered on a project. The customer approves against this, and
    # approval releases money — so the file must be exactly what was
    # uploaded. A bucket never alters what it stores.
    'deliverable': {
        'folder': 'deliverable',
        'max_bytes': 500 * 1024 * 1024,
        'allowed_mime': {
            'audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/flac',
            'video/mp4', 'video/quicktime',
            'image/jpeg', 'image/png',
            'application/pdf', 'application/zip',
        },
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
    },

    'studio_photo': {
        'folder': 'studio_photo',
        'max_bytes': 10 * 1024 * 1024,
        'allowed_mime': {'image/jpeg', 'image/png', 'image/webp'},
    },
}


# The stored extension comes from the validated MIME type, never from the
# uploader's filename: a name is attacker-controlled text, the type has
# already passed the allow-list.
EXTENSIONS = {
    'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp',
    'audio/mpeg': '.mp3', 'audio/wav': '.wav', 'audio/x-wav': '.wav',
    'audio/aiff': '.aiff', 'audio/x-aiff': '.aiff', 'audio/flac': '.flac',
    'audio/mp4': '.m4a', 'audio/aac': '.aac',
    'video/mp4': '.mp4', 'video/quicktime': '.mov', 'video/webm': '.webm',
    'application/pdf': '.pdf', 'application/zip': '.zip',
}


@lru_cache(maxsize=1)
def _client():
    """
    The S3 client, or None when no bucket is configured.

    Built once per process. Checksums are sent only when the API requires
    them: newer boto3 adds them to every request by default, which
    S3-compatible providers do not all accept.
    """
    if not (settings.S3_BUCKET and settings.S3_ACCESS_KEY_ID and settings.S3_SECRET_ACCESS_KEY):
        return None
    import boto3
    from botocore.config import Config

    return boto3.client(
        's3',
        endpoint_url=settings.S3_ENDPOINT_URL or None,
        aws_access_key_id=settings.S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
        region_name=settings.S3_REGION,
        config=Config(
            signature_version='s3v4',
            s3={'addressing_style': settings.S3_ADDRESSING_STYLE},
            request_checksum_calculation='when_required',
            response_checksum_validation='when_required',
        ),
    )


def folder_for(kind: str) -> str:
    """Key prefix for a kind of upload, environment-prefixed."""
    return '%s/%s' % (_prefix(), FOLDERS.get(kind, FOLDERS['misc']))


def upload(file, *, context: str) -> dict:
    """
    Validate against a named context, then store.

    There is no unvalidated variant on purpose: a generic upload() is what
    new code reaches for, and then the allow-list protects nothing.

    Returns {'key', 'bytes', 'content_type'}. Keep the key: it is what
    url_for() and delete() take. Raises ValidationError on a rejected file so
    DRF renders it as a 400 rather than a 500.
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
    if not mime:
        raise ValidationError({'file': 'The file type could not be determined.'})
    if mime not in rules['allowed_mime']:
        raise ValidationError({
            'file': '%s files are not accepted here. Allowed: %s.'
                    % (mime, ', '.join(sorted(rules['allowed_mime']))),
        })

    client = _client()
    if client is None:
        logger.error('No storage bucket is configured; upload rejected')
        raise ValidationError({'file': 'File uploads are not available right now.'})

    # A random name: two uploads never collide, and one key cannot be guessed
    # from another.
    key = '%s/%s%s' % (folder_for(rules['folder']), uuid.uuid4().hex, EXTENSIONS.get(mime, ''))
    try:
        client.upload_fileobj(file, settings.S3_BUCKET, key, ExtraArgs={'ContentType': mime})
    except Exception:
        # Never surface the provider's error text: it can contain the bucket
        # name, the endpoint and request signatures.
        logger.exception('Bucket upload failed for context %s', context)
        raise ValidationError({'file': 'The upload failed. Please try again.'}) from None
    return {'key': key, 'bytes': size, 'content_type': mime}


def delete(key: str) -> bool:
    """
    Remove a stored file.

    Returns False rather than raising: a failed delete should not roll back
    the database change that prompted it, or a user cannot remove a
    portfolio item because the provider is briefly down. Orphaned files are
    a cleanup job, not a blocked request.
    """
    try:
        client = _client()
        if client is None or not key:
            return False
        client.delete_object(Bucket=settings.S3_BUCKET, Key=key)
        return True
    except Exception:
        logger.exception('Bucket delete failed for %s', key)
        return False


def url_for(key: str, *, expires: int = None, download_name: str = None) -> str:
    """
    A temporary link to a stored file, or '' when there is none.

    The bucket is private, so every link is signed and expires (default
    S3_URL_EXPIRY_SECONDS). Build it when the response is rendered and never
    store it. `download_name` makes the browser save the file under that
    name instead of showing it.
    """
    if not key:
        return ''
    try:
        client = _client()
        if client is None:
            return ''
        params = {'Bucket': settings.S3_BUCKET, 'Key': key}
        if download_name:
            safe = ''.join(c for c in download_name if c.isalnum() or c in ' ._-').strip()
            params['ResponseContentDisposition'] = 'attachment; filename="%s"' % (safe or 'download')
        return client.generate_presigned_url(
            'get_object', Params=params, ExpiresIn=expires or settings.S3_URL_EXPIRY_SECONDS,
        )
    except Exception:
        logger.exception('Could not build a URL for %s', key)
        return ''
