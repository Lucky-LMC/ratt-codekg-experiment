"""Repair an execution failure using the original contract and diagnostic."""
import hashlib
import json
from pathlib import Path
import re
import subprocess

from .generation import REVISION_ENVIRONMENT, _revision_code, revision_native_contract, revision_native_test


def _revision_missing_member_facts(source, info, errors, query, tools):
    """Supply pinned alternatives only after a failed static package-member call."""
    messages = '\n'.join(row.get('error', '') for row in errors)
    if 'is not a function' not in messages or not (info.get('fact_bundle') or {}).get('source_commit'):
        return None
    parser = Path(__file__).with_name("call_parser.cjs")
    modules = Path(tools) / "node_modules"
    parsed = subprocess.run(['node', str(parser), 'direct', str(modules)],
        input=json.dumps(dict(source=source, pkg=info['package_name'])),
        text=True, encoding='utf-8', capture_output=True, check=True, timeout=30)
    calls = [c for c in json.loads(parsed.stdout)
             if f"{c['alias']}.{c['member']} is not a function" in messages]
    if not calls:
        return None
    pkg = info["package_name"]
    if not query.load_codekg(pkg):
        raise ValueError('Missing pinned graph for repair')
    data = query.loaded_data[pkg];commit = info['fact_bundle']['source_commit']
    if data.get('schema_version') != 'revision-source-graph-v1' or data.get('source_commit') != commit:
        raise ValueError('Repair graph identity differs from target')
    indexed = {a['access_path']:a for a in data['apis']}
    missing = [pkg+'.'+c['member'] for c in calls if pkg+'.'+c['member'] not in indexed]
    if not missing:
        return None
    # shortcut: direct root members only; deeper receivers need the scoped parser extended.
    members = sorted((a for a in data['apis'] if a['receiver_path'] == pkg), key=lambda a:a['access_path'])
    graph = Path(query.codekg_dir)/(pkg.replace('/','_').replace('@','')+'_codekg.json')
    return dict(source_commit=commit, graph_sha256=hashlib.sha256(graph.read_bytes()).hexdigest(),
        parser_sha256=hashlib.sha256(parser.read_bytes()).hexdigest(),
        missing_from_graph=missing, receiver=pkg, truncated=len(members)>32,
        available=[{k:a[k] for k in ('access_path','signature','parameters','file_path','function_sha256')}
                   for a in members[:32]],
        instruction='Use a supplied signature only when it supports the intended fixture. '
                    'Missing from this graph is not proof of global nonexistence. '
                    'Do not rename the same invalid call or invent an unlisted helper.')


def repair_candidate(client, original, info, errors, query, tools):
    alternatives = _revision_missing_member_facts(original, info, errors, query, tools)
    prompt = (f"Fix the observed Mocha failures for exact target {info['access_path']}. "
              "Preserve the intended target behavior and meaningful assertions. "
              "The design is a proposal: correct its import or calling mistakes using the supplied facts. "
              "Change an expected value only when supplied source justifies it, not merely to match an observed failure. "
              "Output ONLY complete JavaScript test code, without explanations or reasoning.\n"
              f"Import the target package using require({json.dumps(info['package_name'])}). "
              "Source file paths identify evidence locations, not module import specifiers. "
              "Null or empty factual fields mean unknown, not a JavaScript return value.\n"
              "Do not remove or weaken assertions, skip tests, or swallow target/assertion errors. "
              "Do not pre-create the expected output or change setup so it already satisfies the "
              "assertion; preserve the distinct input and pre-call output state. "
              "Keep asynchronous fixtures alive until the callback or awaited Promise completes. "
              "Callback cleanup belongs inside the callback or afterEach, never in a finally "
              "surrounding the scheduling call. Report assertion errors through done(error).\n"
              f"{REVISION_ENVIRONMENT}\n"
              f"API factual contract: {json.dumps(info['fact_bundle'], ensure_ascii=False)}\n"
              f"Design: \nObserved: {json.dumps([{k: row[k] for k in ('status', 'count', 'title', 'error') if k in row} for row in errors], ensure_ascii=False)}\n"
              f"Draft:\n{original}")
    native_contract = revision_native_contract(info)
    if native_contract:
        prompt = (f'Repair the Mocha suite for exact target {info["access_path"]}. '
                  f'{native_contract}\n{REVISION_ENVIRONMENT}\n'
                  'Return only complete JavaScript suite code using the supplied invokeTarget. '
                  'The harness adds its fixed binding; do not output or edit the binding shell. '
                  'Fix false draft expectations using the verified identity and facts below. '
                  'Keep meaningful result assertions; do not skip tests, swallow errors, or infer '
                  'expected values solely from observed failures. Propagate asynchronous errors '
                  'through done(error), and call done() on successful callback completion.\n'
                  f'API factual contract: {json.dumps(info["fact_bundle"], ensure_ascii=False)}\n'
                  f'Observed: {json.dumps([{k: row[k] for k in ("status", "count", "title", "error") if k in row} for row in errors], ensure_ascii=False)}\n'
                  f'Draft suite:\n{revision_native_test(original, info, unwrap=True)}')
    if alternatives:
        prompt += '\nPinned receiver alternatives:\n' + json.dumps(alternatives, ensure_ascii=False)
    if any('Invalid URL' in row.get('error', '') or
           re.search(r'\b(?:Response code|status(?: code)?|HTTP(?:Error)?:?)\s+404\b', row.get('error', ''), re.I)
           for row in errors):
        prompt += ('\nRe-derive the local HTTP fixture from the supplied constructor and path-building '
                   'source. Check positional versus options-object arguments, URL scheme, base path, '
                   'parent context and identifier. The draft/design may have invented these. '
                   'Configure the real client explicitly as its constructor requires. Match the exact '
                   'method/path that this construction should request; do not make the fixture accept '
                   'every URL or replace the target to hide a wrong request. Preserve response assertions.')
    prompt += "\nPreserve the first registered scenario; return exactly one runnable it()."
    response = client.call_simple(prompt, log_io=False)
    code = _revision_code(response) if response else ""
    return revision_native_test(code, info) if native_contract and code else code
