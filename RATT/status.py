"""Execution verdict and timeout classification."""
import re

def is_test_timeout(result):
    return (result.get('timeout_kind') in {'mocha_test','registered_process'}
            or result.get('error_code') == 'ERR_MOCHA_TIMEOUT'
            or (result.get('status') == 'timeout' and bool(result.get('count')) and bool(result.get('title')))
            or (result.get('status') == 'failed' and bool(result.get('title'))
                and bool(re.search(r'Timeout of \d+ms exceeded', result.get('error') or ''))))


def classify_timeout(result):
    if is_test_timeout(result):
        return {**result, 'raw_status':result.get('raw_status',result.get('status')),
                'status':'timeout', 'timeout_kind':result.get('timeout_kind',
                'registered_process' if result.get('status')=='timeout' else 'mocha_test'),
                'evaluation_status':'pending_timeout'}
    return result


def replay_state(result):
    if is_test_timeout(result):
        return "unknown"
    status = result.get("status")
    if status == "passed":
        return "passed"
    if status in {"failed", "skipped", "no_block"}:
        return "failed"
    if status == "load_error" and result.get("error") and not result["error"].startswith("test process exited"):
        return "failed"
    return "unknown"
