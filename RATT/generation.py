"""Generate one source-grounded candidate per scenario."""
import json
import re

CODE_GENERATION_SYSTEM = """You are a JavaScript test code generator. STRICT RULES:
1. Output ONLY JavaScript code, no explanation
2. Use describe() with at least one it() block
3. Use assert module only (no expect, no chai, no jest)
4. Every variable must be declared inside the it() block
5. Follow the exact import and call pattern specified in the constraints
6. Never reference undefined variables like logFile, classifier, testFilePath unless defined in it()"""


REVISION_SCENARIOS = (
    "ordinary nontrivial input, checking the exact observable result",
    "an input-dependent boundary or alternate source branch, checking how the result changes; "
    "when a supplied condition distinguishes ordinary from edge inputs, exercise the other side "
    "of that condition rather than another ordinary example; otherwise use a supported boundary",
    "a distinct supported interaction, sequence, side effect, or error behavior",
)


REVISION_ENVIRONMENT = (
    "Execution environment: isolated Node.js and Mocha, no external network or DNS, "
    "read-only installed packages, writable /tmp. Use Node standard-library fixtures. "
    "A local HTTP server on 127.0.0.1 with an OS-assigned port may be used inside the test; "
    "close it after asynchronous completion. Do not use public websites. "
    "Always execute the real target; do not replace or mock the target or its internal implementation. "
    "Only external dependencies may be isolated while preserving the target's behavior. "
    "In the factual contract, is_async records the async declaration keyword only: false does not "
    "mean synchronous completion. Determine Promise or callback completion from source and declarations. "
    "Implementation status records unavailable source; do not invent missing behavior."
)


def _revision_code(response: str) -> str:
    """Keep imports, setup, and template literals in an unfenced answer."""
    fenced = re.findall(r"^```(?:javascript|js)?[ \t]*\r?\n(.*?)^\s*```[ \t]*$", response,
                       re.IGNORECASE | re.MULTILINE | re.DOTALL)
    if len(fenced) > 1:
        import esprima

        def has_test(node):
            if isinstance(node, list):
                return any(has_test(child) for child in node)
            if not isinstance(node, dict):
                return False
            if node.get('type') == 'CallExpression':
                callee = node.get('callee', {})
                if callee.get('type') == 'MemberExpression' and not callee.get('computed'):
                    owner, prop = callee.get('object', {}), callee.get('property', {})
                    callee = prop if owner.get('name') == 'mocha' else owner
                if callee.get('type') == 'Identifier' and callee.get('name') in {'it','test','specify'}:
                    return True
            return any(has_test(child) for child in node.values())

        # Explanations can quote implementation snippets before the complete test.
        for code in fenced:
            try:
                if has_test(esprima.parseScript(code).toDict()):
                    return code.strip()
            except esprima.Error:
                continue
    return (fenced[0] if fenced else response).strip()


def revision_receiver_binding(api_info):
    """Only bind a graph-resolved member of an already exported nested object."""
    info = api_info or {}
    facts = info.get("fact_bundle") or {}
    pkg = info.get("package_name", "")
    owner = facts.get("receiver_path") or ""
    name = facts.get("api_name") or ""
    if (not info.get("revision_exact_path") or facts.get("receiver_kind") != "object_member"
            or not pkg or not owner.startswith(pkg + ".") or facts.get("access_path") != owner + "." + name
            or not re.fullmatch(r"[A-Za-z_$][\w$]*", name)):
        return None
    suffix = owner[len(pkg):]
    if not re.fullmatch(r"(?:\.[A-Za-z_$][\w$]*|\[\d+\])+", suffix):
        return None
    provenance = facts.get("source_evidence") or {}
    if provenance.get("documented_method_transfer"):
        return None
    indexed_instance = bool(re.search(r"\[\d+\]$", suffix)
                            and facts.get("inherited_member") and provenance.get("function_text"))
    if not (indexed_instance or provenance.get("exported_value_construction")):
        return None
    return (f"const receiver = require({json.dumps(pkg)}){suffix};\n"
            f"return Reflect.apply(receiver[{json.dumps(name)}], receiver, args);")


def revision_native_alias(api_info):
    """Resolve only a runtime-verified native alias, never infer it from a name."""
    info = api_info or {}
    facts = info.get('fact_bundle') or {}
    origin = (facts.get('source_evidence') or {}).get('callable_origin') or {}
    if not info.get('revision_exact_path') or origin.get('kind') != 'native_alias':
        return None
    pkg, target, native = info.get('package_name', ''), info.get('access_path', ''), origin.get('native_identity')
    if (not pkg or not target.startswith(pkg + '.') or target != facts.get('access_path')
            or target != origin.get('access_path') or native != facts.get('native_identity')
            or not facts.get('function_sha256') or facts['function_sha256'] != origin.get('runtime_function_sha256')
            or not isinstance(native, str) or not re.fullmatch(r'[A-Za-z_$][\w$]*', native)):
        raise ValueError('Inconsistent native alias evidence')
    members = target[len(pkg)+1:].split('.')
    if not all(re.fullmatch(r'[A-Za-z_$][\w$]*', member) for member in members):
        raise ValueError('Unsupported native alias path')
    expression = f'require({json.dumps(pkg)})' + ''.join(f'[{json.dumps(member)}]' for member in members)
    return expression, native


def revision_native_contract(api_info):
    alias = revision_native_alias(api_info)
    if not alias:
        return ''
    expression, native = alias
    return (f'\nVerified runtime function identity: {expression} === globalThis[{json.dumps(native)}]. '
            'This is exact object equality, not inheritance. Every property name, including "$", is literal. '
            'The receiver constructor describes the containing object, not this callable. '
            'The harness supplies this exact function as invokeTarget; use invokeTarget(...args), '
            'or new invokeTarget(...args) when construction is supported. '
            'Assert an observable result of that invocation using the verified native identity. '
            'Correct conflicting design or draft expectations; do not substitute another package member '
            'or invoke a global constructor instead. Do not redeclare or shadow invokeTarget.')


def revision_native_test(code, api_info, *, unwrap=False):
    alias = revision_native_alias(api_info)
    if not alias or not code:
        return code
    prefix = '(function (invokeTarget) {\n'
    suffix = '\n}).call(module.exports, ' + alias[0] + ');'
    # Remove only our exact shell; arbitrary model code is never rewritten by regex.
    if code.startswith(prefix) and code.endswith(suffix):
        code = code[len(prefix):-len(suffix)]
    return code if unwrap else prefix + code + suffix


def revision_class_target(api_info):
    if not api_info.get('revision_exact_path') or (api_info.get('fact_bundle') or {}).get('api_type') != 'class':
        return ''
    return ("\nThe exact target is the class constructor itself. Exercise construction with new, or a "
            "source-supported factory that actually constructs this class, and check supported initialized state. "
            "Calling a static member alone does not execute the target constructor. If only an implicit/default "
            "constructor exists, check default construction and instance identity without inventing initialization "
            "logic. Static-member behavior belongs to a different target.")


def revision_generation_instructions(prompt, system):
    """Share environment and assertion requirements across direct and staged generation."""
    prompt += "\n" + REVISION_ENVIRONMENT
    prompt += ("\nInvoke the actual target and assert a result or effect of that invocation. "
               "Do not replace the planned operation with existence, type, or function-identity checks. "
               "Use the recorded package constructor, not a similarly named global constructor. "
               "For a state-changing operation, keep input fixtures separate from output state. "
               "Check that the expected change is absent before the call and present afterwards; "
               "setup must not already satisfy the final assertion.")
    system += (
        "\nFor asynchronous operations, keep fixtures alive until the callback "
        "or awaited Promise has completed and its assertions have run. "
        "Perform cleanup afterwards, including on failure. A synchronous "
        "try/finally around callback scheduling must not remove fixtures before completion. "
        "Propagate assertion failures to Mocha through done(error) or a rejected Promise.")
    prompt += (
        "\n异步清理规则：回调模式的文件清理只能在回调内的 finally 中，或 Mocha afterEach 中；"
        "不能放在发起异步调用的外层 finally 中。回调应先检查错误与结果，再清理，最后调用 done。"
        "Promise 模式必须先 await 完成，再执行 finally 清理。生成前检查代码是否满足这个顺序。")
    return prompt, system


def _constraints(api_info):
    pkg = api_info["package_name"]
    access_path = api_info["access_path"]
    alias = re.sub(r'\W+', '_', pkg).strip('_')
    if not alias or not alias[0].isalpha():
        alias = 'targetPackage'
    suffix = access_path.removeprefix(pkg)
    if suffix and not suffix.startswith(('.', '[')):
        raise ValueError(f"Target path {access_path} does not start with package {pkg}")
    call = f"Call {alias}{suffix} with concrete test inputs."
    if revision_native_alias(api_info):
        call = revision_native_contract(api_info)
    elif revision_class_target(api_info):
        call = f"Construct {alias}{suffix} with source-supported inputs." + revision_class_target(api_info)
    elif '.prototype.' in suffix:
        owner, member = suffix.split('.prototype.', 1)
        call = (f"Create a valid instance of {alias}{owner} using the supplied API usage "
                f"examples, then call instance.{member} with concrete test inputs.")
    call += " Use the verified receiver or factory and completion mode from the contract when available."
    return (f"Require the target with const {alias} = require('{pkg}');\n"
            f"Exact target access path: {access_path}\n{call}\n"
            "Check the observable result using Node assert. Keep each test self-contained.")


def generate_candidate(client, api_info, scenario):
    key_constraints = _constraints(api_info)
    fact_bundle = json.dumps(api_info["fact_bundle"], ensure_ascii=False)
    preventive_prompt = (
        "Generate one self-contained Mocha test of the exact target with one justified scenario. "
        "Use concrete Node assert comparisons on its observed behavior. Await asynchronous work. "
        "Use only the supplied behavior evidence. Choose an observable expectation that can be justified. "
        f"\nExact target access path: {api_info['access_path']}\n"
        f"\nUse this scenario focus if supported by the supplied facts: {scenario}.")
    prompt = f"""你是 JavaScript 测试专家。请严格按照以下约束生成 Mocha 单元测试。

{key_constraints}

## Grounded API facts
{fact_bundle}

## 测试设计方案


## 详细指导
{preventive_prompt}

## 输出要求
- 只输出 JavaScript 代码，不要任何解释
- 必须有 describe() 和至少一个 it() 测试用例
- 使用 assert 模块断言，不用 expect()
- 所有变量在 it() 块内定义
- 代码可直接用 mocha 运行"""
    binding = revision_receiver_binding(api_info)
    native_contract = revision_native_contract(api_info)
    system = CODE_GENERATION_SYSTEM
    prompt, system = revision_generation_instructions(prompt, system)
    system += "\nRegister exactly one runnable Mocha it test. Multiple assertions in this test are allowed."
    prompt = prompt.replace("至少一个 it()", "恰好一个 it()") + "\nOutput exactly one registered it() for the single designed scenario."
    if binding:
        system += ("\nThe harness supplies a lexical function named invokeTarget(...args). "
                   "Every scenario must exercise that function to invoke the exact exported method. "
                   "Use it in the test exactly as supplied. Do not declare it or replace its receiver. "
                   "This binding takes precedence over receiver setup proposed in the design.")
        prompt += ("\nThe complete standalone test will receive this function as an argument:\n"
                   f"function invokeTarget(...args) {{\n{binding}\n}}\n"
                   "Output the Mocha suite body that uses invokeTarget(...inputs) directly.")
    if native_contract:
        system += native_contract + '\nOutput only the Mocha suite body; the harness adds the fixed binding.'
    response = client.call_with_system_prompt(
        system,
        prompt,
        log_io=False
    )
    code = _revision_code(response) if response else ""
    if native_contract and code:
        return revision_native_test(code, api_info)
    if binding and code:
        return ("(function (invokeTarget) {\n" + code + "\n}).call(module.exports, "
                "function (...args) {\n" + binding + "\n});")
    return code
