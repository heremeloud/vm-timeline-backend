import unittest

from scripts import refresh_instagram_pfps as refresh


class InstagramRefreshTests(unittest.TestCase):
    def test_normalizes_profile_url(self):
        self.assertEqual(
            refresh.normalize_instagram_username("https://www.instagram.com/jaoyng/?hl=en"),
            "jaoyng",
        )

    def test_rejects_non_profile_and_unsafe_usernames(self):
        self.assertIsNone(refresh.normalize_instagram_username("https://instagram.com/reel/abc"))
        self.assertIsNone(refresh.normalize_instagram_username("../../tmp/avatar"))
        self.assertIsNone(refresh.normalize_instagram_username("https://example.com/jaoyng"))

    def test_cookie_header_requires_no_manual_reassembly(self):
        cookies = refresh.parse_cookie_header(
            "csrftoken=abc; sessionid=user%3Atoken; rur=\"CCO\""
        )
        self.assertEqual([cookie["name"] for cookie in cookies], ["csrftoken", "sessionid", "rur"])
        self.assertTrue(cookies[1]["httpOnly"])

    def test_finds_photo_only_for_matching_json_user(self):
        payload = {
            "data": {
                "user": {
                    "username": "jaoyng",
                    "profile_pic_url_hd": "https://cdn.example/jaoyng.jpg",
                },
                "suggested": {
                    "username": "someone_else",
                    "profile_pic_url_hd": "https://cdn.example/wrong.jpg",
                },
            }
        }
        self.assertEqual(
            refresh.find_matching_profile_photo(payload, "JAOYNG"),
            "https://cdn.example/jaoyng.jpg",
        )
        self.assertIsNone(refresh.find_matching_profile_photo(payload, "missing"))

    def test_dom_candidate_must_name_requested_profile(self):
        candidates = [
            refresh.AvatarCandidate("https://cdn.example/me.jpg", "My profile picture", 320, 320),
            refresh.AvatarCandidate("https://cdn.example/wrong.jpg", "Another user's profile picture", 640, 640),
            refresh.AvatarCandidate("https://cdn.example/right.jpg", "jaoyng's profile picture", 150, 150),
        ]
        self.assertEqual(
            refresh.choose_verified_avatar(candidates, "jaoyng"),
            "https://cdn.example/right.jpg",
        )

    def test_dom_candidate_does_not_accept_username_substrings(self):
        candidates = [
            refresh.AvatarCandidate(
                "https://cdn.example/wrong.jpg",
                "notjaoyng's profile picture",
                640,
                640,
            )
        ]
        self.assertIsNone(refresh.choose_verified_avatar(candidates, "jaoyng"))

    def test_image_type_and_signature_must_agree(self):
        self.assertEqual(refresh.image_extension("image/jpeg", b"\xff\xd8\xffmore"), ".jpg")
        with self.assertRaisesRegex(refresh.InstagramLookupError, "invalid bytes"):
            refresh.image_extension("image/jpeg", b"<html>login</html>")
        with self.assertRaisesRegex(refresh.InstagramLookupError, "not an image"):
            refresh.image_extension("text/html", b"<html></html>")


if __name__ == "__main__":
    unittest.main()
