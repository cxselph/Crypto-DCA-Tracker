"""What the tracker trusts for Home Auth and the PulseChain node (tracker/tls.py).

Run: python3 -I -m unittest discover -s tests -v  (standard library only)
"""

import ssl
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

from tracker.tls import tls_context  # noqa: E402


class TlsContext(unittest.TestCase):
    def test_checks_certificates_against_the_public_cas(self):
        ctx = tls_context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)


if __name__ == "__main__":
    unittest.main()
