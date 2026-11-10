"""What the tracker trusts for Home Auth and the PulseChain node (tracker/tls.py).

Run: python3 -I -m unittest discover -s tests -v  (standard library only; openssl makes the test CA)
"""

import ssl
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from tracker.tls import tls_context  # noqa: E402


class TlsContext(unittest.TestCase):
    def test_house_ca_is_added_to_the_public_cas(self):
        with tempfile.TemporaryDirectory() as d:
            ca = Path(d, "house-ca.crt")
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(Path(d, "k")),
                            "-out", str(ca), "-days", "2", "-subj", "/CN=Test house CA"], check=True, capture_output=True)
            ctx = tls_context(str(ca))
        names = [dict(x[0] for x in c["subject"]).get("commonName") for c in ctx.get_ca_certs()]
        self.assertIn("Test house CA", names)
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_no_house_ca_still_checks_against_the_public_cas(self):
        for ca in ("", "/nonexistent/house-ca.crt"):
            ctx = tls_context(ca)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
