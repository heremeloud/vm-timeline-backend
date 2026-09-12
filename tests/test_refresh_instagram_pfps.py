import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from scripts import refresh_instagram_pfps as refresh


class InstagramLookupTests(unittest.TestCase):
    def test_rate_limit_stops_without_fallback(self):
        with patch.object(refresh, 'request_bytes', side_effect=HTTPError('https://www.instagram.com/', 429, 'Too Many Requests', {}, None)) as request:
            with self.assertRaisesRegex(refresh.InstagramRateLimitError, '429'):
                refresh.fetch_profile_photo_url('example', 10, None)
            self.assertEqual(request.call_count, 1)

    def test_empty_user_has_clear_failure(self):
        with patch.object(refresh, 'request_bytes', side_effect=[(b'{"data":{"user":null}}','application/json'), (b'<html>Login</html>', 'text/html')]):
            with self.assertRaisesRegex(LookupError, 'no matching profile photo'):
                refresh.fetch_profile_photo_url('example', 10, None)

    def test_matching_profile_is_returned(self):
        with patch.object(refresh, 'request_bytes', return_value=(b'{"data":{"user":{"username":"example","profile_pic_url_hd":"https://example.com/photo.jpg"}}}', 'application/json')):
            self.assertEqual(refresh.fetch_profile_photo_url('example', 10, None), 'https://example.com/photo.jpg')

    def test_api_error_survives_fallback_failure(self):
        with patch.object(refresh, 'request_bytes', side_effect=[HTTPError('https://www.instagram.com/', 401, 'Unauthorized', {}, None), (b'<html>Login</html>', 'text/html')]):
            with self.assertRaisesRegex(LookupError, 'API HTTP 401: session rejected'):
                refresh.fetch_profile_photo_url('example', 10, None)
