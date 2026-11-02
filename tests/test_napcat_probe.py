"""Offline tests for the Node/Chromium TLS probe; never contacts the network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "napcat-security"
PROBE = ROOT / "probe.cjs"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required for offline probe tests")
class ProbeTests(unittest.TestCase):
    def evaluate(self, expression):
        env = {k: v for k, v in os.environ.items() if k not in (
            "PROBE_AUTORUN", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS", "NODE_TLS_REJECT_UNAUTHORIZED"
        )}
        script = "const p=require(process.argv[2]);console.log(JSON.stringify(" + expression + "));"
        result = subprocess.run([NODE, "-", str(PROBE)], input=script, text=True,
                                capture_output=True, check=True, timeout=10, env=env)
        return json.loads(result.stdout)

    def test_exact_target_set(self):
        targets = self.evaluate("p.targets()")
        self.assertEqual(len(targets), 14)
        self.assertEqual(len({t["host"] for t in targets}), 14)
        self.assertEqual(sum(t["expected"] == "reject" for t in targets), 12)

    def test_only_explicit_trust_errors_pass(self):
        for error in ["CERT_REVOKED", "UNABLE_TO_GET_ISSUER_CERT_LOCALLY", "net::ERR_CERT_AUTHORITY_INVALID"]:
            self.assertEqual(self.evaluate(f"p.classifyError({json.dumps(error)})"), "trust_rejection")

    def test_timeouts_dns_expiry_and_hostname_errors_are_inconclusive(self):
        for error in ["ETIMEDOUT", "ENOTFOUND", "net::ERR_FAILED", "CERT_HAS_EXPIRED",
                      "ERR_TLS_CERT_ALTNAME_INVALID", "net::ERR_CERT_DATE_INVALID"]:
            self.assertEqual(self.evaluate(f"p.classifyError({json.dumps(error)})"), "inconclusive")

    def test_timeout_wins_over_conflicting_trust_text(self):
        self.assertEqual(self.evaluate("p.classifyError('ETIMEDOUT CERT_REVOKED')"), "inconclusive")

    def test_accepting_bad_chain_is_failure(self):
        result = self.evaluate("p.verdict({expected:'reject'},{kind:'response',status:200})")
        self.assertEqual(result["verdict"], "fail")

    def test_normal_http_error_still_proves_tls_response(self):
        result = self.evaluate("p.verdict({expected:'respond'},{kind:'response',status:403})")
        self.assertEqual(result["verdict"], "pass")

    def test_chromium_env_selection_and_deadline(self):
        result = self.evaluate("p.parseOptions(['--timeout','15000'],{PROBE_STACK:'chromium'})")
        self.assertEqual(result, {"timeout": 15000, "chromium": True})

    def test_bypass_and_custom_roots_are_not_clean_evidence(self):
        result = self.evaluate("p.unsafeEnvironment({NODE_TLS_REJECT_UNAUTHORIZED:'0',NODE_EXTRA_CA_CERTS:'/custom'},['--ignore-certificate-errors'],null)")
        self.assertEqual(len(result), 3)


if __name__ == "__main__":
    unittest.main()
