"""Offline regression tests for the account-free image verification gate."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1] / "adapters" / "onebot" / "napcat-security"
spec = importlib.util.spec_from_file_location("napcat_verify_image", ROOT / "verify_image.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class VerifyImageTests(unittest.TestCase):
    def test_all_twelve_bad_chains_are_covered(self):
        self.assertEqual(len(module.BAD_HOSTS), 12)
        self.assertEqual(len(set(module.BAD_HOSTS)), 12)
        self.assertEqual(len(module.GOOD_HOSTS), 2)

    def test_mutable_tags_are_rejected_before_running_docker(self):
        with patch.object(module.subprocess, "run") as run:
            with self.assertRaises(ValueError):
                module.verify("clonoth-napcat:latest", Path("not-created"))
            run.assert_not_called()

    def test_timeout_is_not_a_security_pass_and_container_is_removed(self):
        with patch.object(module.subprocess, "run", side_effect=[
            subprocess.TimeoutExpired("docker", 1),
            subprocess.CompletedProcess("docker", 0),
        ]) as run:
            result = module.isolated("sha256:" + "a" * 64, [], ["-c", "exit 0"], 1)
        self.assertEqual(result.returncode, 124)
        cleanup = run.call_args_list[-1].args[0]
        self.assertEqual(cleanup[:4], ["docker", "rm", "-f", "-v"])
        self.assertTrue(cleanup[-1].startswith("clonoth-tls-probe-"))
        command = run.call_args_list[0].args[0]
        self.assertNotIn("/opt/napcat/qq", command)
        self.assertNotIn("/opt/napcat/config", command)

    def test_probe_requires_specific_curl_errors(self):
        script = module.system_script()
        self.assertIn("unable to get local issuer", script)
        self.assertNotIn("--insecure", script)
        self.assertNotIn("certificate has expired", script)
        self.assertIn("exit $failed", script)

    def test_summary_requires_all_expected_hosts(self):
        report = {"kind": "summary", "ok": True, "results": []}
        self.assertFalse(module.summary_passed([report]))
        report["results"] = [{"host": host, "verdict": "pass"}
                             for host in module.BAD_HOSTS + module.GOOD_HOSTS]
        self.assertTrue(module.summary_passed([report]))
        report["results"][0]["verdict"] = "inconclusive"
        self.assertFalse(module.summary_passed([report]))

    def test_noisy_electron_output_is_not_a_success_report(self):
        self.assertEqual(module.json_lines("[preload] ready\nnull\n[]\n{}"), [{}])


if __name__ == "__main__":
    unittest.main()
