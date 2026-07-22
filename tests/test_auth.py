from __future__ import annotations

import unittest

from score_fourfold.auth import hash_password, valid_password_hash, verify_password


class PasswordHashTests(unittest.TestCase):
    def test_hash_and_verify_correct_and_wrong_passwords(self):
        encoded = hash_password("correct horse battery staple")

        self.assertTrue(valid_password_hash(encoded))
        self.assertTrue(verify_password("correct horse battery staple", encoded))
        self.assertFalse(verify_password("wrong horse battery staple", encoded))

    def test_fixed_salt_produces_stable_hash(self):
        encoded = hash_password(
            "correct horse battery staple",
            salt=bytes.fromhex("00112233445566778899aabbccddeeff"),
        )

        self.assertEqual(
            encoded,
            "scrypt:16384:8:1:00112233445566778899aabbccddeeff:"
            "fcd5a58d5301bbc44e90fc9a53f156134baee795eb7735ed6473da86e34ba930",
        )

    def test_rejects_short_password_nul_and_wrong_salt_size(self):
        with self.assertRaises(ValueError):
            hash_password("too-short")
        with self.assertRaises(ValueError):
            hash_password("sixteen-characters\x00")
        with self.assertRaises(ValueError):
            hash_password("correct horse battery staple", salt=b"short")

    def test_bad_hash_formats_are_rejected_without_raising(self):
        malformed = (
            "",
            "not-scrypt",
            "scrypt:16384:8:1:not-hex:not-hex",
            "scrypt:32768:8:1:00112233445566778899aabbccddeeff:" + "00" * 32,
            "scrypt:16384:8:1:0011:" + "00" * 32,
            "scrypt:16384:8:1:00112233445566778899aabbccddeeff:00",
        )

        for encoded in malformed:
            with self.subTest(encoded=encoded):
                self.assertFalse(valid_password_hash(encoded))
                self.assertFalse(verify_password("correct horse battery staple", encoded))


if __name__ == "__main__":
    unittest.main()
