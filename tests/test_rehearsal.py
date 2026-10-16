import asyncio
import tempfile
import unittest
from tools.generate_comms_pki import generate_pki
from tools.rehearse_comms import rehearse


class RehearsalTests(unittest.TestCase):
    def test_local_tls_bridge_and_direct_subscriber(self):
        with tempfile.TemporaryDirectory(prefix="comms-rehearsal-") as folder:
            generate_pki(folder)
            summary = asyncio.run(rehearse(folder, duration=5, target=10))
        self.assertTrue(summary["passed"], summary)
        self.assertEqual(summary["results"], summary["bridge"]["acked"])
        # One result per accepted sample comes from the server's built-in dummy model.
        self.assertEqual(summary["server"]["model_results"], summary["results"])
        self.assertEqual(summary["server"]["model_errors"], 0)
        self.assertGreaterEqual(summary["results"], 10)
        self.assertEqual(summary["source"], "synthetic")
        self.assertEqual(summary["bridge"]["transport_connections"], 1)
