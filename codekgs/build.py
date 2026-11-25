"""Build a CodeKG from locally installed package source and callable exports."""
import hashlib
import json
from pathlib import Path
import re
import tempfile

from RATT.runtime import IMAGE, isolated_node

PACKAGE_PATTERN = re.compile(r"(?:@[a-z0-9_.~-]+/)?[a-z0-9_~-][a-z0-9_.~-]*", re.IGNORECASE)


def package_name(value):
    if not PACKAGE_PATTERN.fullmatch(value) or value.startswith("@./") or value.startswith("@../"):
        raise ValueError("Expected an npm package name, optionally scoped")
    return value


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def installed_files(runtime):
    """Fingerprint the installed JavaScript, declarations, and package metadata."""
    modules = (Path(runtime) / "node_modules").resolve(strict=True)
    files = {}
    for path in sorted(modules.rglob("*")):
        if not path.is_file() or ".bin" in path.relative_to(modules).parts:
            continue
        if not path.resolve().is_relative_to(modules):
            raise ValueError(f"Dependency points outside the runtime: {path}")
        if path.suffix in {".js", ".cjs", ".mjs", ".ts", ".json", ".node"}:
            files["node_modules/" + path.relative_to(modules).as_posix()] = file_sha(path)
    return files


def source_digest(files):
    digest = hashlib.sha256()
    for path, sha in sorted(files.items()):
        digest.update(f"{path}\0{sha}\n".encode())
    return digest.hexdigest()


def assemble_graph(inventory, revision):
    package = inventory["package_name"]
    nodes = {package: {"id": package, "type": "package"}}
    edges, apis = {}, []
    for item in sorted(inventory["apis"], key=lambda row: row["access_path"]):
        access = item["access_path"]
        owner = item["receiver_path"]
        name = access[len(owner):].lstrip(".") if owner else package
        info = inventory["functions"][item["function_sha256"]]
        parameters = info["parameters"]
        locations = info["locations"]
        runtime_text = info["source"]
        if hashlib.sha256(runtime_text.encode()).hexdigest() != item["function_sha256"]:
            raise ValueError(f"Function fingerprint mismatch for {access}")
        native = bool(re.fullmatch(r".*?\{\s*\[native code\]\s*\}\s*", runtime_text, re.S))
        evidence = {
            "kind": "installed_source_reflection",
            "runtime_locations": locations,
            "git_locations": [],
            "implementation_status": "native_implementation" if native else "complete",
        }
        if info.get("builtin_locations"):
            evidence["node_builtin_locations"] = info["builtin_locations"]
        if not native:
            evidence["function_text"] = {
                "implementation_excerpt": runtime_text,
                "truncated": False,
                "context_ambiguous": len(locations) != 1,
                "source_location": locations[0] if len(locations) == 1 else None,
            }
        observation = item.get("native_return_observation")
        returns = observation
        if not returns and (info["return_type"] or info["return_expressions"]):
            returns = {
                "type": "parsed_runtime_function",
                "function_sha256": item["function_sha256"],
                "return_expressions": info["return_expressions"],
            }
        api = {
            **{key: value for key, value in item.items() if key != "native_return_observation"},
            "id": access, "name": name, "source_commit": revision,
            "type": "class" if info["class_declaration"] else "function",
            "signature": None if parameters is None else f"{name}({', '.join(parameters)})",
            "parameters": parameters, "is_async": info["is_async"],
            "return_type": (observation or {}).get("return_type") or info["return_type"],
            "return_evidence": returns, "source_evidence": evidence,
            "file_path": locations[0]["file"] if len(locations) == 1 else None,
        }
        apis.append(api)
        nodes[access] = {"id": access, "name": name, "type": api["type"], "access_path": access}
        parent = package
        for part in re.findall(r"\.[^.[\]]+|\[\d+\]", access[len(package):]):
            child = parent + part
            nodes.setdefault(child, {"id": child, "type": "object"})
            edges[parent, child] = {"source": parent, "target": child, "relation": "has_member"}
            parent = child
        if parent != access or (owner is not None and (owner, access) not in edges):
            raise ValueError(f"Cannot bind receiver chain for {access}")
    if len(nodes) < len(apis) or len({api["access_path"] for api in apis}) != len(apis):
        raise ValueError("Duplicate callable paths")
    return {
        "schema_version": "revision-source-graph-v1", "package_name": package,
        "source_version": inventory["version"], "source_commit": revision,
        "entry_sha256": inventory["entry_sha256"], "apis": apis,
        "graph": {"directed": True, "multigraph": False, "graph": {},
                  "nodes": list(nodes.values()), "edges": list(edges.values())},
        "inventory_limits": inventory["problems"],
    }


def build_codekg(package, runtime, tools, directory, *, source_revision=None):
    package = package_name(package)
    runtime = Path(runtime).resolve(strict=True)
    tools_modules = Path(tools) / "node_modules"
    files = installed_files(runtime)
    digest = source_digest(files)
    here = Path(__file__).parent
    result = isolated_node(
        ["node", "/inventory.cjs", "/runtime", package, "/tools/acorn/dist/acorn.js"],
        [(runtime / "node_modules", "/runtime/node_modules"), (tools_modules, "/tools"),
         (here / "source_inventory.cjs", "/inventory.cjs")],
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError("Package inventory failed: " + result.stderr[-1600:].decode("utf8", "replace"))
    inventory = json.loads(result.stdout)
    if inventory["package_name"] != package:
        raise ValueError("Installed package identity differs")
    graph = assemble_graph(inventory, source_revision or f"sha256:{digest}")
    with tempfile.TemporaryDirectory(prefix="ratt-graph-") as temp:
        index = Path(temp) / "index.json"
        index.write_text(json.dumps(files), encoding="utf-8")
        spec = dict(root="/runtime", runtimeIndex="/index.json", parser="/tools/typescript/lib/typescript.js",
                    pkg=package, git={}, docs={}, apis=graph["apis"])
        result = isolated_node(
            ["node", "/neighbors.cjs"],
            [(runtime / "node_modules", "/runtime/node_modules"), (tools_modules, "/tools"),
             (here / "source_neighbors.cjs", "/neighbors.cjs"), (index, "/index.json")],
            stdin=json.dumps(spec).encode(), timeout=120,
        )
    if result.returncode:
        raise RuntimeError("Source-context extraction failed: " + result.stderr[-1600:].decode("utf8", "replace"))
    neighbors = json.loads(result.stdout)
    for api in graph["apis"]:
        definitions = neighbors[api["access_path"]]
        if definitions:
            api["source_evidence"]["referenced_definitions"] = definitions
    if files != installed_files(runtime):
        raise ValueError("Installed source changed while building CodeKG")
    graph["runtime_tree_sha256"] = digest
    graph["execution_environment"] = {k: inventory[k] for k in ("node", "platform", "arch")}
    graph["execution_environment"]["image"] = IMAGE
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    name = package.replace("/", "_").replace("@", "") + "_codekg.json"
    with (directory / name).open("x", encoding="utf-8") as output:
        json.dump(graph, output, ensure_ascii=False, indent=2)
    return directory / name
