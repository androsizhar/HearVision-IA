"""
Tests for the SSRF guard in browser_agent/agent.py.

These are the checks that stop the execution agent from navigating to a
private network address or a cloud metadata endpoint -- whether because the
recorded plan pointed there by mistake, or because on-page content tried to
redirect the agent somewhere it shouldn't go.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from browser_agent.agent import is_url_safe, get_allowed_domains


class TestIsUrlSafe(unittest.TestCase):

    def test_blocks_localhost_by_name(self):
        safe, reason = is_url_safe("http://localhost:8000")
        self.assertFalse(safe)
        self.assertIn("localhost", reason)

    def test_blocks_loopback_ip(self):
        safe, reason = is_url_safe("http://127.0.0.1:8000")
        self.assertFalse(safe)

    def test_blocks_private_class_a(self):
        safe, _ = is_url_safe("http://10.0.0.5/admin")
        self.assertFalse(safe)

    def test_blocks_private_class_b(self):
        safe, _ = is_url_safe("http://172.16.0.5")
        self.assertFalse(safe)

    def test_blocks_private_class_c(self):
        safe, _ = is_url_safe("http://192.168.1.1")
        self.assertFalse(safe)

    def test_blocks_cloud_metadata_endpoint(self):
        # 169.254.169.254 is the AWS/GCP/Azure instance metadata endpoint --
        # a classic SSRF target for exfiltrating cloud credentials.
        safe, reason = is_url_safe("http://169.254.169.254/latest/meta-data/")
        self.assertFalse(safe)

    def test_allows_public_domain(self):
        safe, reason = is_url_safe("https://example.com")
        self.assertTrue(safe)
        self.assertEqual(reason, "")

    def test_rejects_url_with_no_host(self):
        safe, reason = is_url_safe("not a url")
        self.assertFalse(safe)

    def test_accepts_url_without_scheme(self):
        # url_es_segura should tolerate a bare host:port, since plans store
        # navigation targets loosely.
        safe, _ = is_url_safe("example.com")
        self.assertTrue(safe)


class TestGetAllowedDomains(unittest.TestCase):

    def test_collects_portal_url_domain(self):
        plan = {"portal_url": "https://portal.example.com/login", "steps": []}
        domains = get_allowed_domains(plan)
        self.assertIn("portal.example.com", domains)

    def test_collects_navigate_step_domains(self):
        plan = {
            "portal_url": "https://a.example.com",
            "steps": [
                {"action": "navigate", "value": "https://b.example.com/page"},
                {"action": "click", "value": ""},  # non-navigate steps are ignored
            ],
        }
        domains = get_allowed_domains(plan)
        self.assertIn("a.example.com", domains)
        self.assertIn("b.example.com", domains)

    def test_empty_plan_yields_no_domains(self):
        self.assertEqual(get_allowed_domains({}), [])


if __name__ == "__main__":
    unittest.main()
