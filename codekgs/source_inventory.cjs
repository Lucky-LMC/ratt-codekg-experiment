/* Inventory public callable exports without consulting a target manifest. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const Module = require('node:module');
const [root, pkg, parser] = process.argv.slice(2);
const acorn = require(parser);
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const modules = path.join(root, 'node_modules');
const load = Module.createRequire(path.join(root, 'graph-probe.cjs'));
const inside = (dir, file) => {
  const rel = path.relative(dir, file);
  return !path.isAbsolute(rel) && rel !== '..' && !rel.startsWith('..' + path.sep);
};
const resolver = Module._resolveFilename;
Module._resolveFilename = function(...args) {
  const file = resolver.apply(this, args);
  if (path.isAbsolute(file) && !inside(modules, fs.realpathSync(file))) {
    const err = new Error('Dependency outside the assigned runtime: ' + args[0]);
    err.code = 'MODULE_NOT_FOUND';
    throw err;
  }
  return file;
};
const entry = fs.realpathSync(load.resolve(pkg));
if (!inside(path.join(modules, pkg), entry)) throw Error('Unexpected entry: ' + entry);
const exported = load(pkg);
const apis = [];
const functions = new Map();
const problems = {};
const skip = (why, at) => {
  const item = problems[why] ??= {count: 0, examples: []};
  item.count++;
  if (item.examples.length < 12) item.examples.push(at);
};
const intrinsic = new Map();
for (const cls of [Array, Object, Function, String, Number, Promise, RegExp, Date, Error, Map, Set, Buffer]) {
  for (const [name, obj] of [[cls.name,cls],[cls.name + '.prototype',cls.prototype]]) {
    for (const [k,d] of Object.entries(Object.getOwnPropertyDescriptors(obj))) {
      if (k !== 'constructor' && typeof d.value === 'function' && !intrinsic.has(d.value))
        intrinsic.set(d.value, name + '.' + k);
    }
  }
}
function returnFacts(node, wrapped) {
  if (!node) return {expressions:[],type:null};
  const expressions = [];
  function collect(n) {
    if (!n || typeof n !== 'object' || /Function|MethodDefinition|Class/.test(n.type)) return;
    if (n.type === 'ReturnStatement') { expressions.push(n.argument); return; }
    for (const child of Object.values(n)) {
      if (Array.isArray(child)) child.forEach(collect);
      else if (child && typeof child === 'object') collect(child);
    }
  }
  if (node.body.type === 'BlockStatement') collect(node.body);
  else expressions.push(node.body);
  const excerpts = [...new Set(expressions.map(e=>e ? wrapped.slice(e.start,e.end) : '(bare return)'))];
  const last = node.body.body?.at(-1)?.type;
  const complete = node.body.type !== 'BlockStatement' || ['ReturnStatement','ThrowStatement'].includes(last);
  function literalType(e) {
    if (!e) return 'undefined';
    if (e.type === 'ArrayExpression') return 'Array';
    if (e.type === 'ObjectExpression') return 'object';
    if (e.type === 'Literal') return e.value === null ? 'null' : typeof e.value;
    if (e.type === 'TemplateLiteral') return 'string';
    if (e.type === 'BinaryExpression' && ['===','!==','==','!=','<','>','<=','>=','instanceof','in'].includes(e.operator))
      return 'boolean';
    return null;
  }
  const types = new Set(expressions.map(literalType));
  const type = node.generator ? (node.async ? 'AsyncGenerator' : 'Generator') : node.async ? 'Promise' :
    complete && types.size === 1 ? [...types][0] : null;
  return {type,expressions:excerpts.slice(0,4).map(text=>text.length>200 ? text.slice(0,200)+'…' : text)};
}
function parse(source) {
  for (const [prefix, suffix] of [['(', ')'], ['({','})']]) {
    const wrapped = prefix + source + suffix;
    try {
      let n = acorn.parse(wrapped, {ecmaVersion: 2020}).body[0].expression;
      if (n.type === 'ObjectExpression') n = n.properties[0].value;
      const isClass = n.type === 'ClassExpression';
      if (isClass) n = n.body.body.find(x => x.kind === 'constructor')?.value;
      const params = n?.params?.map(p => wrapped.slice(p.start,p.end)) ?? null;
      const returns = returnFacts(n, wrapped);
      return {parameters:params, is_async:n ? !!n.async : null, class_declaration:isClass,
        return_type:isClass ? null : returns.type, return_expressions:returns.expressions};
    } catch { /* Retry method syntax. */ }
  }
  return {parameters:null,is_async:null,class_declaration:false,return_type:null,return_expressions:[]};
}
function walk(value, at, owner, kind, depth, parents, inherited) {
  const fn = typeof value === 'function';
  if (!fn && (value === null || typeof value !== 'object')) return;
  if (fn) {
    const source = Function.prototype.toString.call(value);
    const sha = hash(source);
    if (!functions.has(sha)) functions.set(sha, {source, ...parse(source)});
    const nativeIdentity = intrinsic.get(value) || null;
    const arrayLikeResult = nativeIdentity === 'Array.prototype.map' ?
      Reflect.apply(value, {0:2,1:4,length:2}, [x=>x+1]) : null;
    apis.push({access_path:at, receiver_path:owner, receiver_kind:kind, inherited_member:inherited,
      function_sha256:sha, native_identity:nativeIdentity, native_return_observation:
        arrayLikeResult && Array.isArray(arrayLikeResult) ? {type:'native_identity_probe',
          receiver:'ordinary array-like object {0:2,1:4,length:2}',mapper:'x => x + 1',value:arrayLikeResult,
          return_type:'Array for an ordinary array-like receiver'} : null});
  }
  if (parents.has(value)) { skip('cycle',at); return; }
  if (depth >= 6) { skip('depth_limit',at); return; }
  if (ArrayBuffer.isView(value) || value === globalThis) { skip('opaque_object',at); return; }
  if (/\{\s*\[native code\]\s*\}/.test(fn ? Function.prototype.toString.call(value) : '') &&
      !Object.hasOwn(value, 'prototype')) return;
  parents.add(value);
  const seen = new Set();
  for (let obj = value; obj && ![Object.prototype,Function.prototype,Array.prototype].includes(obj);
       obj = Object.getPrototypeOf(obj)) {
    for (const [key, desc] of Object.entries(Object.getOwnPropertyDescriptors(obj))) {
      if (seen.has(key)) continue;
      seen.add(key);
      if (['constructor','__proto__','caller','callee','arguments'].includes(key) ||
          key.startsWith('_') || !/^(?:[A-Za-z$][\w$]*|0|[1-9]\d*)$/.test(key)) continue;
      if (fn && ['length','name'].includes(key)) continue;
      const child = /^\d+$/.test(key) ? at + '[' + key + ']' : at + '.' + key;
      let member;
      try { member = 'value' in desc ? desc.value : desc.get?.call(value); }
      catch { skip('getter_threw', child); continue; }
      const receiver = at.endsWith('.prototype') ? 'prototype_method' : (fn ? 'static_member' : 'object_member');
      walk(member,child,at,receiver,depth+1,parents,obj !== value);
      if (apis.length > 100000) throw new Error('Export inventory exceeds 100000 paths: ' + at);
    }
  }
  parents.delete(value);
}
walk(exported,pkg,null,'module_export',0,new Set(),false);
const loaded = Object.keys(require.cache).filter(f => inside(modules,f) && /\.[cm]?js$/.test(f));
const files = new Map(loaded.map(f => [path.relative(root,f).replaceAll('\\','/'),fs.readFileSync(f,'utf8')]));
for (const info of functions.values()) {
  info.locations = [];
  if (/\{\s*\[native code\]\s*\}/.test(info.source)) continue;
  for (const [file,text] of files) {
    let offset = -1;
    while ((offset = text.indexOf(info.source,offset+1)) !== -1) {
      const line = 1 + text.slice(0,offset).split('\n').length - 1;
      info.locations.push({file,line,column:offset-text.lastIndexOf('\n',offset-1),file_sha256:hash(text)});
    }
  }
}
const pkgInfo = JSON.parse(fs.readFileSync(path.join(modules,pkg,'package.json')));
process.stdout.write(JSON.stringify({package_name:pkgInfo.name,version:pkgInfo.version,node:process.version,
  platform:process.platform,arch:process.arch,entry_sha256:hash(fs.readFileSync(entry)), problems,apis,
  functions:Object.fromEntries(functions)}));
