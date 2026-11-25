"""Execute one registered test in an offline Node.js container."""
import json
from pathlib import Path
import subprocess
import uuid

from .status import classify_timeout

IMAGE = "node@sha256:b21fe589dfbe5cc39365d0544b9be3f1f33f55f3c86c87a76ff65a02f8f5848e"


def isolated_node(args, mounts, *, timeout, environment=None, stdin=None):
    name = f"ratt-{uuid.uuid4().hex}"
    command = [
        "docker", "run", "--pull=never", "--rm", "--name", name, "--network=none",
        "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--user=1000:1000", "--cpus=1", "--memory=384m", "--pids-limit=64",
        "--tmpfs=/tmp:rw,nosuid,nodev,size=64m,mode=1777", "--workdir=/tmp",
    ]
    if stdin is not None:
        command.append("--interactive")
    for key, value in (environment or {}).items():
        command.extend(["--env", f"{key}={value}"])
    for source, destination in mounts:
        source = Path(source).resolve(strict=True)
        if "," in str(source):
            raise ValueError(f"Docker bind source contains a comma: {source}")
        command.extend(["--mount", f"type=bind,source={source},target={destination},readonly"])
    command.extend([IMAGE, *args])
    try:
        return subprocess.run(command, input=stdin, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)
        raise RuntimeError("Docker did not finish within the infrastructure deadline") from exc


def run_first(source, runtime, tools):
    """Run the first registered it with the original setup and hooks intact."""
    modules = Path(tools) / "node_modules"
    version = json.loads((modules / "mocha/package.json").read_text("utf-8"))["version"]
    if version != "11.7.5":
        raise ValueError("Expected Mocha 11.7.5; run npm ci in the method repository")
    here = Path(__file__).parent
    script = r"""
        cp /input/candidate.js /tmp/candidate.js || exit 125
        timeout -k 1 15 node --require=/first_test.cjs /mocha_runner.cjs \
            /tools/mocha /tmp/candidate.js 0 /tmp/result.json 5000 >/tmp/log 2>&1
        rc=$?
        if test -s /tmp/result.json; then head -c 131072 /tmp/result.json; fi
        exit "$rc"
    """
    proc = isolated_node(["sh", "-c", script], [
        (Path(runtime) / "node_modules", "/deps"),
        (modules, "/tools"),
        (source, "/input/candidate.js"),
        (here / "mocha_runner.cjs", "/mocha_runner.cjs"),
        (here / "first_test.cjs", "/first_test.cjs"),
    ], timeout=55, environment={"NODE_PATH": "/deps:/tools", "MOCHA_PATH": "/tools/mocha"})
    if proc.returncode == 125:
        raise RuntimeError("Docker setup failed: " + proc.stderr[-1200:].decode("utf8", "replace"))
    try:
        result = json.loads(proc.stdout) if proc.stdout else {}
    except ValueError:
        result = {}
    if proc.returncode in {124, 137}:
        return classify_timeout({**result, "status": "timeout" if result.get("title") else "load_timeout"})
    if proc.returncode or result.get("status") not in {"passed", "failed", "no_block", "load_error", "skipped"}:
        return {"status": "load_error", "error": f"test process exited {proc.returncode} without a complete Mocha report"}
    return classify_timeout(result)
