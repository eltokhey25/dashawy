"""Google ID token verification against a real RS256 signature.

Production once failed here because PyJWT's RSA support needs the
``cryptography`` package; this test signs a token with a freshly generated key
so a missing dependency fails the suite instead of the live sign-in.
"""

import io
import json
import time
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, override_settings

from . import google_auth

CLIENT_ID = 'test-client.apps.googleusercontent.com'


def _jwks_response(public_key, kid):
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update(kid=kid, alg='RS256', use='sig')
    body = json.dumps({'keys': [jwk]}).encode('utf-8')

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Response(body)


@override_settings(GOOGLE_CLIENT_ID=CLIENT_ID)
class VerifyIdTokenTests(SimpleTestCase):
    def setUp(self):
        google_auth._certs_cache.update(fetched_at=0.0, keys={})
        self.addCleanup(google_auth._certs_cache.update, fetched_at=0.0, keys={})
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _token(self, **overrides):
        now = int(time.time())
        claims = {
            'iss': 'https://accounts.google.com',
            'aud': CLIENT_ID,
            'sub': '1234567890',
            'email': 'Customer@Example.com',
            'email_verified': True,
            'iat': now,
            'exp': now + 600,
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm='RS256', headers={'kid': 'k1'})

    def _verify(self, token):
        response = _jwks_response(self.private_key.public_key(), 'k1')
        with patch.object(google_auth.urllib.request, 'urlopen', return_value=response):
            return google_auth.verify_id_token(token)

    def test_valid_token_is_accepted(self):
        claims = self._verify(self._token())
        self.assertEqual(claims['sub'], '1234567890')
        self.assertEqual(claims['email'], 'customer@example.com')

    def test_wrong_audience_is_rejected(self):
        with self.assertRaises(google_auth.GoogleTokenError):
            self._verify(self._token(aud='someone-else'))

    def test_unverified_email_is_rejected(self):
        with self.assertRaises(google_auth.GoogleTokenError):
            self._verify(self._token(email_verified=False))
