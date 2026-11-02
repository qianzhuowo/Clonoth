"""Offline regression tests for experimental runtime distrust policy."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "napcat-security"
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node required")
class RuntimePolicyTests(unittest.TestCase):
    def evaluate(self, expression):
        env = dict(os.environ)
        for k in ["PROBE_AUTORUN", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS"]:
            env.pop(k, None)
        script = "const p=require(process.argv[2]);const e=require(process.argv[3]);" + \
            "const tls=require('node:tls');const safe=tls.rootCertificates.find(x=>!p.inspectChain({data:x}).blocked);" + \
            "console.log(JSON.stringify(" + expression + "));"
        r = subprocess.run([NODE, "-", str(ROOT / "runtime-policy.cjs"), str(ROOT / "ca-evidence.json")],
                           input=script, text=True, capture_output=True, check=True, timeout=10, env=env)
        return json.loads(r.stdout)

    def test_all_cross_signed_intermediate_keys_denied(self):
        result = self.evaluate("e['impacted-intermediates'].map(r=>p.decide({certificate:{data:r.pem},verificationResult:'net::OK'}).code)")
        self.assertEqual(result, [-206] * 16)

    def test_all_affected_root_keys_denied(self):
        result = self.evaluate("e.roots.map(r=>p.decide({certificate:{data:r.pem},verificationResult:'OK'}).code)")
        self.assertEqual(result, [-206] * 4)

    def test_safe_chain_uses_default_never_accept_override(self):
        for state in ["net::OK", "net::ERR_CERT_DATE_INVALID", "net::ERR_CERT_COMMON_NAME_INVALID"]:
            code = self.evaluate("p.decide({certificate:{data:safe},validatedCertificate:{data:safe},verificationResult:" + json.dumps(state) + "}).code")
            self.assertEqual(code, -3)

    def test_missing_success_chain_fails_closed(self):
        self.assertEqual(self.evaluate("p.decide({certificate:{data:safe},verificationResult:'OK'}).code"), -202)

    def test_malformed_certificate_fails_closed(self):
        self.assertEqual(self.evaluate("p.decide({certificate:{data:'broken'},verificationResult:'OK'}).code"), -202)

    def test_issuer_chain_is_inspected_not_just_leaf(self):
        code = self.evaluate("p.decide({certificate:{data:safe,issuerCert:{data:e.roots[0].pem}},validatedCertificate:{data:safe},verificationResult:'OK'}).code")
        self.assertEqual(code, -206)

    def test_root_filter_only_subtracts(self):
        result = self.evaluate("(()=>{const r=p.filterRoots([safe,...e.roots.map(x=>x.pem)]);return {removed:r.removed.length,retained:r.retained.length,same:r.retained[0]===safe};})()")
        self.assertEqual(result, {"removed": 4, "retained": 1, "same": True})

    def test_empty_result_rejected(self):
        self.assertTrue(self.evaluate("(()=>{try{p.filterRoots(e.roots.map(x=>x.pem));return false;}catch(e){return true;}})()"))


if __name__ == "__main__":
    unittest.main()
