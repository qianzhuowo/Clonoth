"""Gate an immutable NapCat image using account-free disposable containers.

Requires Python 3.11+, Docker and Linux. Never mounts QQ accounts or config.
The report attests only system curl, embedded Node HTTPS and Chromium net;
it does not cover Tencent private native networking or future explicit CA overrides.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import re
import subprocess
import uuid

logger = logging.getLogger(__name__)
BAD_HOSTS = [
    f"{kind}-g3{region}{cross}-emudhra.we-have-a-problem.com"
    for kind in ("dv", "ov", "ev")
    for region in ("b", "c")
    for cross in ("", "-aaa")
]
GOOD_HOSTS = ["github.com", "pypi.org"]


def isolated(image: str, args: list[str], command: list[str], timeout: int) -> subprocess.CompletedProcess:
    name = "clonoth-tls-probe-" + uuid.uuid4().hex[:12]
    # Anonymous upstream VOLUMEs contain only disposable, empty test profiles.
    cmd = ["docker", "run", "--name", name, "--ulimit", "core=0", "--memory", "768m",
           "--pids-limit", "128", "--entrypoint", "/bin/sh", *args, image, *command]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "inconclusive: container deadline exceeded")
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=30)


def system_script() -> str:
    lines = ["failed=0"]
    for host in BAD_HOSTS + GOOD_HOSTS:
        expected = 60 if host in BAD_HOSTS else 0
        lines.extend([
            f"curl -q --noproxy '*' -sS -I --connect-timeout 8 --max-time 15 https://{host}/ >/dev/null 2>/tmp/tls-error; code=$?",
            "message=$(cat /tmp/tls-error)",
            f"printf '%s\\n' '{host}'\" exit=$code $message\"",
            f"if [ \"$code\" != {expected} ]; then failed=1; fi",
        ])
        if expected == 60:
            # Reject expiry/hostname errors as inconclusive rather than a fix.
            lines.append("case \"$message\" in *'unable to get local issuer'*|*'unable to verify the first certificate'*|*'certificate revoked'*|*'self-signed certificate in certificate chain'*) ;; *) failed=1;; esac")
    lines.append("exit $failed")
    return "\n".join(lines)


def json_lines(text: str) -> list[dict]:
    values = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                values.append(value)
        except ValueError:
            pass
    return values


def summary_passed(records: list[dict]) -> bool:
    summaries = [record for record in records if record.get("kind") == "summary"]
    if len(summaries) != 1 or summaries[0].get("ok") is not True:
        return False
    results = summaries[0].get("results", [])
    expected = set(BAD_HOSTS + GOOD_HOSTS)
    return (len(results) == len(expected)
            and {result.get("host") for result in results} == expected
            and all(result.get("verdict") == "pass" for result in results))


def verify(image: str, output: Path) -> dict:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Use an immutable local sha256 image ID, not a mutable tag")
    output.mkdir(parents=True, exist_ok=True)
    probe = Path(__file__).resolve().with_name("probe.cjs")
    if not probe.is_file():
        raise FileNotFoundError(probe)
    results = {}
    for stack in ("system", "node", "chromium"):
        if stack == "system":
            args, command, deadline = [], ["-c", system_script()], 250
        else:
            args = ["-v", f"{probe}:/opt/QQ/resources/app/loadNapCat.js:ro",
                    "-e", "PROBE_AUTORUN=1", "-e", f"PROBE_STACK={stack}"]
            command = ["-c", "Xvfb :98 -screen 0 640x480x16 >/dev/null 2>&1 & "
                       "export DISPLAY=:98; sleep 1; exec timeout 100 /opt/QQ/qq --no-sandbox"]
            deadline = 120
        result = isolated(image, args, command, deadline)
        (output / f"{stack}.stdout.log").write_text(result.stdout, encoding="utf-8")
        (output / f"{stack}.stderr.log").write_text(result.stderr, encoding="utf-8")
        records = json_lines(result.stdout)
        ok = result.returncode == 0
        if stack != "system":
            ok = ok and summary_passed(records)
        results[stack] = {"ok": ok, "exit_code": result.returncode, "records": records}
        logger.info("%s probe ok=%s exit=%s", stack, ok, result.returncode)
    report = {
        "image_id": image,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "probe_sha256": hashlib.sha256(probe.read_bytes()).hexdigest(),
        "all_passed": all(r["ok"] for r in results.values()),
        "scope": "system curl + embedded Node HTTPS + isolated Chromium net; not Tencent private native networking",
        "results": results,
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True, help="Use a root-only directory outside Git")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = verify(args.image, args.output)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
