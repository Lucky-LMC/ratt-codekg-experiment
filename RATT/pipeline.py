"""Generate, check, repair, and deliver three CodeKG-grounded test candidates."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from codekgs.build import installed_files, package_name, source_digest
from codekgs.query import CodeKGQuery

from .generation import REVISION_SCENARIOS, generate_candidate
from .repair import repair_candidate
from .runtime import IMAGE, run_first
from .status import replay_state


def repairable(result):
    status = result["status"]
    return (status in {"failed", "no_block", "skipped"}
            or (status == "load_error" and replay_state(result) == "failed")
            or (status == "timeout" and result.get("count") and result.get("title")))


def delivery(slots):
    states = [None if slot is None else replay_state(slot[1]) for slot in slots]
    fallback = states == ["failed"] * 3
    chosen = [1] if fallback else [i for i, state in enumerate(states, 1) if state == "passed"]
    pending = not chosen and "unknown" in states
    # First occurrence wins at final delivery. Repair stays in its original slot.
    unique = {}
    for slot in chosen:
        unique.setdefault(slots[slot - 1][0], slot)
    return list(unique.items()), fallback, pending


def generate_tests(package, target, graph_dir, runtime, tools, client, out):
    """Deliver the distinct passing drafts or repairs, with a first-slot fallback."""
    package = package_name(package)
    query = CodeKGQuery(str(Path(graph_dir).resolve(strict=True)))
    if not query.load_codekg(package):
        raise ValueError(f"Missing CodeKG for {package}")
    data = query.loaded_data[package]
    if (data.get("execution_environment", {}).get("image") != IMAGE
            or data.get("runtime_tree_sha256") != source_digest(installed_files(runtime))):
        raise ValueError("Installed runtime and CodeKG differ. Rebuild the graph.")
    facts = query.build_api_fact_bundle(package, {"access_path": target},
                                        access_path=target, source_commit=data["source_commit"])
    info = {"package_name": package, "access_path": target, "name": facts["api_name"],
            "type": facts["api_type"], "fact_bundle": facts}
    out = Path(out)
    if out.exists():
        raise FileExistsError(out)
    with TemporaryDirectory(prefix="ratt-slot-") as temp:
        workspace = Path(temp)
        control = workspace / "control.js"
        control.write_text(f"require({json.dumps(package)}); it('runtime health', () => {{}});", encoding="utf8")
        health = run_first(control, runtime, tools)
        if health["status"] != "passed":
            raise RuntimeError(f"Target package or Mocha unavailable: {health}")

        def run_slot(number_scenario):
            number, scenario = number_scenario
            code = generate_candidate(client, info, scenario)
            if not code.strip():
                return None
            file = workspace / f"slot-{number}.js"
            file.write_text(code, encoding="utf8", newline="\n")
            verdict = run_first(file, runtime, tools)
            if repairable(verdict):
                code = repair_candidate(client, code, info, [verdict], query, tools)
                if not code.strip():
                    return None
                file.write_text(code, encoding="utf8", newline="\n")
                verdict = run_first(file, runtime, tools)
            return code, verdict

        with ThreadPoolExecutor(max_workers=len(REVISION_SCENARIOS)) as pool:
            slots = list(pool.map(run_slot, enumerate(REVISION_SCENARIOS, 1)))

    chosen, fallback, pending = delivery(slots)
    out.mkdir(parents=True, exist_ok=False)
    written = []
    for code, slot in chosen:
        file = out / f"slot-{slot}.js"
        file.write_text(code, encoding="utf-8", newline="\n")
        written.append(str(file))
    return {"tests": written, "fallback": fallback, "pending": pending,
            "slots": [None if slot is None else slot[1]["status"] for slot in slots]}
