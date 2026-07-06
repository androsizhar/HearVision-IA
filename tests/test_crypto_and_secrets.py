"""
Tests for postprocessing/crypto.py and the secrets vault in database/db.py.

Covers the two properties that matter for credentials at rest: they are
actually encrypted (not just base64 or plain text), and they expire and
become unreadable after their TTL -- so a leaked database file is not the
same as a leaked set of live credentials.
"""
import sys
import os
import time
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCrypto(unittest.TestCase):

    def setUp(self):
        # Isolated temp dir so this test never touches a real ~/.hearvision key.
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name
        try:
            from cryptography.fernet import Fernet
            os.environ["HEARVISION_ENC_KEY"] = Fernet.generate_key().decode()
        except ImportError:
            self.skipTest("cryptography package not installed")

        # crypto.py caches its Fernet instance at module load time -- reload
        # so each test gets a fresh instance bound to its own temp key.
        import importlib
        import postprocessing.crypto as crypto
        importlib.reload(crypto)
        self.crypto = crypto

    def tearDown(self):
        self._tmp.cleanup()

    def test_encrypted_value_is_not_plain_text(self):
        token = self.crypto.encrypt_text("super-secret-password")
        self.assertNotIn("super-secret-password", token)
        self.assertTrue(self.crypto.is_encrypted(token))

    def test_round_trip(self):
        original = "a value with spaces and Ñ, emojis are fine too 🔒"
        token = self.crypto.encrypt_text(original)
        self.assertEqual(self.crypto.decrypt_text(token), original)

    def test_strict_mode_raises_without_key(self):
        # Simulate no key being available at all.
        self.crypto._fernet = None
        self.crypto._attempted = True  # skip re-resolving a key
        with self.assertRaises(self.crypto.EncryptionUnavailableError):
            self.crypto.encrypt_text("secret", strict=True)

    def test_non_strict_mode_falls_back_to_plain_text(self):
        self.crypto._fernet = None
        self.crypto._attempted = True
        result = self.crypto.encrypt_text("not so secret", strict=False)
        self.assertEqual(result, "not so secret")

    def test_decrypting_plain_legacy_value_returns_as_is(self):
        # Backward compatibility: a value that was never encrypted should
        # pass through unchanged rather than error out.
        self.assertEqual(self.crypto.decrypt_text("plain-legacy-value"), "plain-legacy-value")


class TestSecretsVault(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = self._tmp.name
        try:
            from cryptography.fernet import Fernet
            os.environ["HEARVISION_ENC_KEY"] = Fernet.generate_key().decode()
        except ImportError:
            self.skipTest("cryptography package not installed")

        self._db_dir = tempfile.TemporaryDirectory()
        import importlib
        import database.db as db
        db.DB_PATH = os.path.join(self._db_dir.name, "test.db")
        importlib.reload(__import__("postprocessing.crypto", fromlist=["_"]))
        db.init_db()
        self.db = db

    def tearDown(self):
        self._tmp.cleanup()
        self._db_dir.cleanup()

    def test_secret_round_trip(self):
        secret_id = self.db.save_secret("my-password-123")
        self.assertEqual(self.db.read_secret(secret_id), "my-password-123")

    def test_secret_not_stored_in_plain_text_in_the_database(self):
        secret_id = self.db.save_secret("findable-plaintext-marker")
        conn = self.db.get_connection()
        row = conn.execute("SELECT encrypted_value FROM secrets WHERE id=?", (secret_id,)).fetchone()
        conn.close()
        self.assertNotIn("findable-plaintext-marker", row["encrypted_value"])

    def test_expired_secret_cannot_be_read(self):
        secret_id = self.db.save_secret("temporary-value", ttl_hours=1)
        # Manually back-date the expiry to simulate time passing.
        conn = self.db.get_connection()
        past = (datetime.now() - timedelta(hours=1)).isoformat()
        conn.execute("UPDATE secrets SET expires_at=? WHERE id=?", (past, secret_id))
        conn.commit()
        conn.close()
        self.assertIsNone(self.db.read_secret(secret_id))

    def test_reading_unknown_secret_id_returns_none(self):
        self.assertIsNone(self.db.read_secret("does-not-exist"))


if __name__ == "__main__":
    unittest.main()
