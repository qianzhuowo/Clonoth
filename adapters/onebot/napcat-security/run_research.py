"""Run baseline and hardened QQ experiments without loading accounts or NapCat."""
from pathlib import Path
import argparse
import hashlib
import json
import logging
import subprocess
from verify_image import isolated, json_lines

logger = logging.getLogger(__name__)
IMAGE = "sha256:3a0c7ea39963d47206caa532787795b41367d975e5910025bd554190c51f4c89"
FILES = ["research-main.cjs", "runtime-policy.cjs", "node-preload.cjs", "worker-guard.cjs", "probe.cjs", "ca-evidence.json"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    args.output.mkdir(parents=True, exist_ok=True)
    # Resolve and verify the original candidate rather than trusting a mutable tag.
    meta = json.loads(subprocess.check_output(["docker", "image", "inspect", IMAGE]))[0]
    if meta["Id"] != IMAGE:
        raise RuntimeError("Image ID mismatch")
    result = {"image": IMAGE, "production_ready": False,
              "sha256": {f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in FILES}, "modes": {}}
    for mode in ["baseline", "hardened"]:
        mounts = []
        for name in FILES:
            mounts.extend(["-v", f"{root / name}:/opt/QQ/resources/app/{name}:ro"])
        mounts.extend(["-v", f"{root / 'research-main.cjs'}:/opt/QQ/resources/app/loadNapCat.js:ro",
                       "-e", f"RESEARCH_MODE={mode}"])
        proc = isolated(IMAGE, mounts, ["-c", "Xvfb :98 -screen 0 640x480x16 >/dev/null 2>&1 & "
                        "export DISPLAY=:98; sleep 1; exec timeout 150 /opt/QQ/qq --no-sandbox"], 170)
        (args.output / f"{mode}.stdout.log").write_text(proc.stdout)
        (args.output / f"{mode}.stderr.log").write_text(proc.stderr)
        reports = [r for r in json_lines(proc.stdout) if r.get("kind") == "runtime-research"]
        result["modes"][mode] = {"exit_code": proc.returncode, "reports": reports}
        for report in reports:
            logger.info("%s covered=%s fatal=%s", mode, report.get("covered_paths_passed"), report.get("fatal"))
            for group in report["groups"]:
                logger.info("%s %s: passed=%s; nonpass=%s", mode, group["name"], group["ok"],
                            [(r["host"], r["verdict"], r.get("code"), r.get("error"))
                             for r in group["results"] if r["verdict"] != "pass"])
    (args.output / "research.json").write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
