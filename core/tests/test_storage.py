from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from rest_framework.exceptions import ValidationError

from core import storage


def upload_file(*, size=100, content_type='image/png'):
    return Mock(size=size, content_type=content_type)


def test_upload_requires_a_known_context():
    with pytest.raises(ValidationError, match='Unknown upload context'):
        storage.upload(upload_file(), context='unknown')


def test_upload_rejects_missing_or_disallowed_mime_type():
    with pytest.raises(ValidationError, match='could not be determined'):
        storage.upload(upload_file(content_type=None), context='profile_photo')
    with pytest.raises(ValidationError, match='not accepted'):
        storage.upload(upload_file(content_type='application/x-msdownload'),
                       context='profile_photo')


def test_upload_rejects_oversized_file():
    with pytest.raises(ValidationError, match='too large'):
        storage.upload(upload_file(size=6 * 1024 * 1024), context='profile_photo')


@override_settings(CLOUDINARY_FOLDER_PREFIX='ona-test')
def test_upload_uses_context_owned_provider_options():
    file = upload_file()
    response = {'public_id': 'ona-test/profiles/avatar'}
    with patch('cloudinary.uploader.upload', return_value=response) as upload:
        result = storage.upload(file, context='profile_photo', overwrite=True)
    assert result == response
    upload.assert_called_once_with(
        file, folder='ona-test/profiles', resource_type='image', overwrite=True,
    )


def test_provider_errors_are_hidden_from_the_client():
    with patch('cloudinary.uploader.upload', side_effect=RuntimeError('secret detail')):
        with pytest.raises(ValidationError) as error:
            storage.upload(upload_file(), context='profile_photo')
    assert 'secret detail' not in str(error.value)
    assert 'upload failed' in str(error.value).lower()


def test_delete_returns_provider_outcome():
    with patch('cloudinary.uploader.destroy') as destroy:
        assert storage.delete('asset', resource_type='video') is True
    destroy.assert_called_once_with('asset', resource_type='video')
    with patch('cloudinary.uploader.destroy', side_effect=RuntimeError('down')):
        assert storage.delete('asset') is False


def test_url_helpers_are_defensive_and_preserve_overrides():
    assert storage.url_for('') == ''
    with patch('cloudinary.utils.cloudinary_url', return_value=('https://cdn/asset', {})) as url:
        assert storage.url_for('asset', preset='thumbnail', width=320) == 'https://cdn/asset'
    url.assert_called_once_with(
        'asset', width=320, height=150, crop='fill', secure=True,
        fetch_format='auto', quality='auto',
    )
    with patch('core.storage.url_for', side_effect=lambda public_id, **opts: opts['width']):
        assert storage.responsive_urls('asset', widths=[320, 640]) == {320: 320, 640: 640}
