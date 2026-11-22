"""
Turning an admin's upload into a website image.

Every picture is opened with Pillow (so a file that only claims to be an
image is refused), turned the right way up, scaled down to at most
MAX_EDGE pixels on its long side, and saved again as WebP. Saving again is
the point: the stored file carries none of the original's metadata, which
for a phone photo includes the GPS position it was taken at, and it is a
fraction of the original's size, which matters on Ghanaian mobile data.
"""

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageOps, UnidentifiedImageError
from rest_framework.exceptions import ValidationError

from core import storage

#: What an admin may upload (before re-encoding).
ACCEPTED = {'image/jpeg', 'image/png', 'image/webp'}
#: Largest upload accepted, before re-encoding. Phone photos are 3–8 MB.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
#: Long edge of the stored image. A 2400 px photo is sharp full-width on a
#: large laptop screen; Next.js serves smaller sizes from it to phones.
MAX_EDGE = 2400
#: Refuse anything larger than this before decoding it fully: a small file can
#: claim enormous dimensions and exhaust memory ("decompression bomb").
MAX_PIXELS = 60_000_000
WEBP_QUALITY = 82


def process_upload(file) -> dict:
    """
    Validate, re-encode and store an uploaded image.

    Returns {'key', 'bytes', 'width', 'height', 'content_type'}. Raises
    ValidationError (a 400 for the admin) on anything that is not a usable
    image.
    """
    if not file:
        raise ValidationError({'file': 'Choose an image to upload.'})
    if getattr(file, 'size', 0) > MAX_UPLOAD_BYTES:
        raise ValidationError({'file': f'That image is too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.'})
    if getattr(file, 'content_type', None) not in ACCEPTED:
        raise ValidationError({'file': 'Upload a JPEG, PNG or WebP image.'})

    try:
        with Image.open(file) as probe:
            width, height = probe.size
            if width * height > MAX_PIXELS:
                raise ValidationError({'file': 'That image has too many pixels. Use one under 60 megapixels.'})
            probe.verify()  # structural check; the image must be reopened after this
        file.seek(0)
        with Image.open(file) as img:
            img = ImageOps.exif_transpose(img)  # phone photos store "rotate me" in EXIF
            img = img.convert('RGBA' if _has_alpha(img) else 'RGB')
            img.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, 'WEBP', quality=WEBP_QUALITY, method=6)
            width, height = img.size
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise ValidationError({'file': 'That file is not an image that can be read.'}) from None

    data = out.getvalue()
    stored = storage.upload(SimpleUploadedFile('image.webp', data, content_type='image/webp'), context='site_media')
    return {'key': stored['key'], 'bytes': len(data), 'width': width, 'height': height, 'content_type': 'image/webp'}


def _has_alpha(img: Image.Image) -> bool:
    return img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info)
