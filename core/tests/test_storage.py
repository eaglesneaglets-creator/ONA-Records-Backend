from unittest.mock import Mock, patch

import pytest
from django.test import override_settings
from rest_framework.exceptions import ValidationError

from core import storage

BUCKET = override_settings(
    S3_BUCKET='ona-bucket', S3_ENDPOINT_URL='https://s3.example.test',
    S3_ACCESS_KEY_ID='key-id', S3_SECRET_ACCESS_KEY='key-secret',
    S3_REGION='auto', S3_KEY_PREFIX='ona-test',
)


@pytest.fixture(autouse=True)
def fresh_client():
    storage._client.cache_clear()
    yield
    storage._client.cache_clear()


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


@override_settings(S3_BUCKET='', S3_ACCESS_KEY_ID='', S3_SECRET_ACCESS_KEY='')
def test_without_a_bucket_uploads_are_refused_and_nothing_else_breaks():
    with pytest.raises(ValidationError, match='not available'):
        storage.upload(upload_file(), context='profile_photo')
    assert storage.url_for('ona/profiles/a.png') == ''
    assert storage.delete('ona/profiles/a.png') is False


@BUCKET
def test_upload_stores_under_the_context_folder_with_a_generated_name():
    file = upload_file()
    client = Mock()
    with patch('core.storage._client', return_value=client):
        result = storage.upload(file, context='profile_photo')
    # The name is generated; nothing from the uploader's filename reaches it.
    assert result['key'].startswith('ona-test/profiles/')
    assert result['key'].endswith('.png')
    assert result == {'key': result['key'], 'bytes': 100, 'content_type': 'image/png'}
    client.upload_fileobj.assert_called_once_with(
        file, 'ona-bucket', result['key'], ExtraArgs={'ContentType': 'image/png'},
    )


@BUCKET
def test_provider_errors_are_hidden_from_the_client():
    client = Mock()
    client.upload_fileobj.side_effect = RuntimeError('secret detail')
    with patch('core.storage._client', return_value=client):
        with pytest.raises(ValidationError) as error:
            storage.upload(upload_file(), context='profile_photo')
    assert 'secret detail' not in str(error.value)
    assert 'upload failed' in str(error.value).lower()


@BUCKET
def test_delete_returns_provider_outcome():
    client = Mock()
    with patch('core.storage._client', return_value=client):
        assert storage.delete('ona-test/profiles/a.png') is True
        client.delete_object.assert_called_once_with(Bucket='ona-bucket', Key='ona-test/profiles/a.png')
        client.delete_object.side_effect = RuntimeError('down')
        assert storage.delete('ona-test/profiles/a.png') is False


@BUCKET
def test_url_for_is_a_signed_link_that_expires():
    """Built by the real client: signing is local, so no network is touched."""
    assert storage.url_for('') == ''
    url = storage.url_for('ona-test/profiles/a.png', expires=900)
    assert url.startswith('https://ona-bucket.s3.example.test/ona-test/profiles/a.png?')
    assert 'X-Amz-Expires=900' in url and 'X-Amz-Signature=' in url
    assert 'key-secret' not in url


@BUCKET
def test_download_name_cannot_inject_a_header():
    url = storage.url_for('ona-test/deliverables/a.zip', download_name='mix"\r\nX: y.zip')
    assert 'filename%3D%22mixX%20y.zip%22' in url
