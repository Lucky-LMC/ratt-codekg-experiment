// Static parsing only. The caller supplies its installed Babel tooling directory.
const fs = require("node:fs");
const path = require("node:path");
const [mode, modules] = process.argv.slice(2);
const parse = require(path.join(modules, "@babel/parser")).parse;
const traverse = require(path.join(modules, "@babel/traverse")).default;
const input = JSON.parse(fs.readFileSync(0, "utf8"));

function importedPackage(node) {
  return node?.type === "CallExpression" && node.callee.type === "Identifier" &&
    node.callee.name === "require" && node.arguments.length === 1 &&
    node.arguments[0].type === "StringLiteral" ? node.arguments[0].value : null;
}

function propertyName(node) {
  if (!node.computed && node.property.type === "Identifier") return node.property.name;
  if (node.computed && node.property.type === "StringLiteral") return node.property.value;
  return null;
}

function chain(node) {
  if (node.type === "Identifier") return [node.name];
  if (node.type !== "MemberExpression") return null;
  const root = chain(node.object);
  const property = propertyName(node);
  return root && property ? [...root, property] : null;
}

function fence(code) {
  let ast;
  try { ast = parse(code, {sourceType: "unambiguous"}); } catch { return null; }
  const imports = [], calls = [];
  traverse(ast, {
    VariableDeclarator(p) {
      const pkg = importedPackage(p.node.init);
      if (pkg && !p.scope.getBinding("require") && p.node.id.type === "Identifier") {
        imports.push({alias: p.node.id.name, pkg, line: p.node.loc.start.line});
      }
    },
    CallExpression(p) {
      const names = chain(p.node.callee);
      if (!names) return;
      const binding = p.scope.getBinding(names[0]);
      if (binding && (!binding.constant || !binding.path.isVariableDeclarator() ||
          !importedPackage(binding.path.node.init) || binding.path.scope.getBinding("require"))) return;
      calls.push({names, bound_package: binding ? importedPackage(binding.path.node.init) : null,
                  line: p.node.loc.start.line});
    }
  });
  return {imports, calls};
}

function directCalls(source, pkg) {
  let ast;
  try { ast = parse(source, {sourceType: "unambiguous"}); } catch { return []; }
  const calls = [];
  traverse(ast, {VariableDeclarator(p) {
    const id = p.node.id;
    if (id.type !== "Identifier" || importedPackage(p.node.init) !== pkg ||
        p.scope.getBinding("require")) return;
    const binding = p.scope.getBinding(id.name);
    if (!binding || !binding.constant) return;
    const members = binding.referencePaths.map(ref => ref.parentPath).filter(m =>
      m.isMemberExpression() && m.node.object.type === "Identifier" && m.node.object.name === id.name);
    if (members.some(m => m.parentPath.isAssignmentExpression({left: m.node}) ||
        m.parentPath.isUpdateExpression() || m.parentPath.isUnaryExpression({operator: "delete"}))) return;
    for (const m of members) {
      if (!m.parentPath.isCallExpression() || m.parentPath.node.callee !== m.node) continue;
      const name = propertyName(m.node);
      if (name && /^[A-Za-z_$][\w$]*$/.test(name)) {
        calls.push({member: name, alias: id.name, line: m.node.loc.start.line, column: m.node.loc.start.column + 1});
      }
    }
  }});
  return calls;
}

const output = mode === "fences" ? input.map(fence) : directCalls(input.source, input.pkg);
process.stdout.write(JSON.stringify(output));
