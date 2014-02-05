# -*- coding: utf-8 -*-
"""
    silo.storages.amazon_s3
    ~~~~~~~~~~~~~~~~~~~~~~~

    :copyright: (c) 2014 by Janne Vanhala.
    :license: MIT, see LICENSE for more details.
"""
from datetime import date, datetime, timedelta
import hashlib
import hmac

from libcloud.storage.types import Provider
from libcloud.storage.providers import get_driver

from .._compat import force_bytes, quote, urlunparse
from ..exceptions import ArgumentError
from .apache_libcloud import ApacheLibcloudStorage


class AmazonS3Storage(ApacheLibcloudStorage):
    LIBCLOUD_S3_PROVIDERS_BY_REGION = {
        'ap-northeast-1': Provider.S3_AP_NORTHEAST,
        'ap-southeast-1': Provider.S3_AP_SOUTHEAST,
        'eu-west-1': Provider.S3_EU_WEST,
        'us-east-1': Provider.S3,
        'us-west-1': Provider.S3_US_WEST,
        'us-west-2': Provider.S3_US_WEST_OREGON,
    }

    def __init__(self, access_key_id, secret_access_key, bucket,
                 region='us-east-1', url_expires=timedelta(hours=1),
                 use_https=True, use_path_style=False,
                 use_query_string_auth=False):
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._region = region

        self.url_expires = url_expires
        self.use_https = use_https
        self.use_path_style = use_path_style
        self.use_query_string_auth = use_query_string_auth

        container = self._driver.get_container(bucket)
        super(AmazonS3Storage, self).__init__(container)

    @property
    def _driver(self):
        driver_cls = get_driver(self._provider)
        return driver_cls(self._access_key_id, self._secret_access_key)

    @property
    def _provider(self):
        try:
            return self.LIBCLOUD_S3_PROVIDERS_BY_REGION[self._region]
        except KeyError:
            raise ArgumentError(
                'Invalid value {invalid_region!r} for region. Valid Amazon S3 '
                'regions are {valid_regions}'.format(
                    invalid_region=self._region,
                    valid_regions=', '.join(
                        sorted(self.LIBCLOUD_S3_PROVIDERS_BY_REGION.keys())
                    )
                )
            )

    def url(self, name):
        return self._build_request(key=name).uri

    def _build_request(self, key):
        if self.use_query_string_auth:
            return self._build_presigned_request(key)
        else:
            return self._build_unsigned_request(key)

    def _build_presigned_request(self, key):
        request = self._build_unsigned_request(key)
        self._presigner.presign(request, expires=self.url_expires)
        return request

    def _build_unsigned_request(self, key):
        return _S3Request(
            method='GET',
            bucket=self.container.name,
            key=key,
            endpoint=self.container.driver.connection.host,
            use_https=self.use_https,
            use_path_style=self.use_path_style,
        )

    @property
    def _presigner(self):
        return _PresignerV4(self._signer)

    @property
    def _signer(self):
        return _SignerV4(
            access_key_id=self._access_key_id,
            secret_access_key=self._secret_access_key,
            region=self._region,
            service_name='s3'
        )

    def __repr__(self):
        return '<AmazonS3Storage bucket={bucket!r}>'.format(
            bucket=self.container.name
        )


class _S3Request(object):
    def __init__(self, method, endpoint, bucket, key, headers=None,
                 params=None, use_https=True, use_path_style=False):
        self.method = method
        self.endpoint = endpoint
        self.bucket = bucket
        self.key = key
        self.headers = {} if headers is None else headers
        self.params = {} if params is None else params
        self.use_https = use_https
        self.use_path_style = use_path_style

    @property
    def scheme(self):
        return 'https' if self.use_https else 'http'

    @property
    def host(self):
        if self.use_path_style:
            return self.endpoint
        else:
            return '{bucket}.{host}'.format(
                bucket=self.bucket,
                host=self.endpoint
            )

    @property
    def path(self):
        if self.use_path_style:
            return u'/{bucket}/{key}'.format(
                bucket=self.bucket,
                key=self.key
            )
        else:
            return u'/{key}'.format(key=self.key)

    @property
    def canonical_path(self):
        return _uri_encode(force_bytes(self.path), encode_slash=False)

    @property
    def canonical_query_string(self):
        return '&'.join(
            '{param}={value}'.format(
                param=_uri_encode(param),
                value=_uri_encode(value)
            )
            for param, value in sorted(self.params.items())
        )

    @property
    def canonical_headers(self):
        return ''.join(
            '{header_name}:{value}\n'.format(
                header_name=header_name.lower(),
                value=value.strip()
            )
            for header_name, value in sorted(self.headers.items())
        )

    @property
    def signed_headers(self):
        return ';'.join(header.lower() for header in sorted(self.headers))

    @property
    def uri(self):
        return urlunparse((
            self.scheme,
            self.host,
            self.canonical_path,
            '',
            self.canonical_query_string,
            ''
        ))


def _uri_encode(string, encode_slash=True):
    safe = '~'
    if not encode_slash:
        safe += '/'
    return quote(string, safe)


def _expires_in_seconds(input_):
    if isinstance(input_, date):
        if not isinstance(input_, datetime):
            input_ = datetime.combine(input_, datetime.min.time())
        now = datetime.utcnow()
        input_ = input_ - now
    if isinstance(input_, timedelta):
        return int(input_.total_seconds())
    return input_


class _SignerV4(object):
    def __init__(self, access_key_id, secret_access_key, region, service_name):
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region = region
        self.service_name = service_name

    def get_credential(self, timestamp):
        return '/'.join([
            self.access_key_id,
            self._get_scope(timestamp)
        ])

    def get_signature(self, request, timestamp, payload_sha256):
        string = self._get_string_to_sign(request, timestamp, payload_sha256)
        key = self._get_signing_key(timestamp)
        return self._hex_hmac(key, force_bytes(string, 'ascii'))

    def _get_string_to_sign(self, request, timestamp, payload_sha256):
        canonical_request_sha256 = self._get_canonical_request_sha256(
            request,
            payload_sha256
        )

        return '\n'.join([
            'AWS4-HMAC-SHA256',
            timestamp,
            self._get_scope(timestamp),
            canonical_request_sha256,
        ])

    def _get_canonical_request_sha256(self, request, payload_sha256):
        canonical_request = self._get_canonical_request(
            request,
            payload_sha256
        )
        canonical_request = force_bytes(canonical_request, 'ascii')
        return hashlib.sha256(canonical_request).hexdigest()

    def _get_canonical_request(self, request, payload_sha256):
        return '\n'.join([
            request.method,
            request.canonical_path,
            request.canonical_query_string,
            request.canonical_headers,
            request.signed_headers,
            payload_sha256,
        ])

    def _get_scope(self, timestamp):
        return '/'.join([
            timestamp[:8],
            self.region,
            self.service_name,
            'aws4_request'
        ])

    def _get_signing_key(self, timestamp):
        date_key = self._hmac(
            b'AWS4' + force_bytes(self.secret_access_key, 'ascii'),
            force_bytes(timestamp[:8], 'ascii')
        )
        date_region_key = self._hmac(
            date_key,
            force_bytes(self.region, 'ascii')
        )
        date_region_service_key = self._hmac(
            date_region_key,
            force_bytes(self.service_name, 'ascii')
        )
        signing_key = self._hmac(date_region_service_key, b'aws4_request')
        return signing_key

    @staticmethod
    def _hmac(key, value):
        return hmac.new(key, value, digestmod=hashlib.sha256).digest()

    @staticmethod
    def _hex_hmac(key, value):
        return hmac.new(key, value, digestmod=hashlib.sha256).hexdigest()


class _PresignerV4(object):
    def __init__(self, signer):
        self.signer = signer

    def presign(self, request, expires):
        timestamp = self._get_timestamp()

        request.headers['Host'] = request.host
        request.params = {
            'X-Amz-Algorithm': 'AWS4-HMAC-SHA256',
            'X-Amz-Date': timestamp,
            'X-Amz-SignedHeaders': request.signed_headers,
            'X-Amz-Expires': str(_expires_in_seconds(expires)),
            'X-Amz-Credential': self.signer.get_credential(timestamp),
        }
        signature = self._get_signature(request, timestamp)
        request.params['X-Amz-Signature'] = signature

    def _get_timestamp(self):
        return datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')

    def _get_signature(self, request, timestamp):
        return self.signer.get_signature(
            request,
            timestamp,
            payload_sha256='UNSIGNED-PAYLOAD'
        )
