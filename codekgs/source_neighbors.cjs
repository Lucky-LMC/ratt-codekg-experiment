/* Read verified source only: resolve one receiver and its directly referenced definitions. */
const fs = require('node:fs');
const path = require('node:path');
const {SourceMap} = require('node:module');
const {createHash} = require('node:crypto');
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
const ts = require(spec.parser);
const hash = text => createHash('sha256').update(text).digest('hex');
const hashIndex = JSON.parse(fs.readFileSync(spec.runtimeIndex, 'utf8'));
const permitted = file => !/(?:^|\/)(?:tests?|__tests__|benchmarks?|performance|fixtures)(?:\/|$)|\.(?:test|spec)\.[^.]+$/i.test(file);

function checked(file) {
  if (!permitted(file) || !file.startsWith('node_modules/' + spec.pkg + '/') || !hashIndex[file]) return;
  const raw = fs.readFileSync(path.join(spec.root, file));
  if (hash(raw) !== hashIndex[file]) throw Error('Runtime source digest mismatch: ' + file);
  return raw.toString();
}
const cache = new Map();
function unit(file, text) {
  if (cache.has(file)) return cache.get(file);
  const tree = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const u = {file, text, digest:hash(text), tree, definitions:new Map(), methods:new Map(), imports:new Map()};
  const add = (map, k, v) => map.set(k, [...(map.get(k) || []), v]);
  function visit(n) {
    if ((ts.isFunctionDeclaration(n) || ts.isClassDeclaration(n)) && n.name) add(u.definitions,n.name.text,n);
    if (ts.isBinaryExpression(n) && n.operatorToken.kind===ts.SyntaxKind.EqualsToken) {
      const m=/^(\w+)\.prototype\.(\w+)$/.exec(n.left.getText(tree));
      if(m) add(u.methods,m[1],n);
    }
    if(ts.isVariableDeclaration(n) && n.initializer && ts.isIdentifier(n.name)) {
      const m=/^require\(["'](\.[^"']+)["']\)$/.exec(n.initializer.getText(tree));
      if(m) u.imports.set(n.name.text,m[1]);
    }
    ts.forEachChild(n,visit);
  }
  visit(tree); cache.set(file,u);return u;
}
function singleton(list) { return list?.length===1 ? list[0] : null; }
function reference(u, name) {
  const [base,member]=name.split('.');
  if(!member) {
    const node=singleton(u.definitions.get(base));
    return node ? [u,node] : null;
  }
  if(!u.imports.has(base)) return null;
  const stem=path.posix.normalize(path.posix.join(path.posix.dirname(u.file),u.imports.get(base)));
  for(const file of [stem,stem+'.js',stem+'/index.js']) {
    const src=checked(file);
    if(src){ const lib=unit(file,src);const node=singleton(lib.definitions.get(member));return node ? [lib,node] : null; }
  }
}
function evidence(api) {
  const loc=api.source_evidence.runtime_locations;
  if(loc.length!==1) return [];
  const l=loc[0], raw=checked(l.file), existing=api.source_evidence.function_text?.implementation_excerpt;
  if(!raw || hash(raw)!==l.file_sha256 || !existing) return [];
  const data=[]; let chars=0;
  function add(kind,u,node,limit=650,docs=false) {
    const start=node.getStart(u.tree,docs), src=u.text.slice(start,node.end), excerpt=src.slice(0,limit);
    const item={kind,owner:node.parent && ts.isClassDeclaration(node.parent) ? node.parent.name?.text : undefined,
                file:u.file,file_sha256:u.digest,line:u.tree.getLineAndCharacterOfPosition(start).line+1,
                text:excerpt,truncated:src.length>excerpt.length};
    if(data.some(x=>x.file===item.file && x.line===item.line && x.text===item.text) || chars+excerpt.length>5200) return;
    data.push(item); chars+=excerpt.length;
  }
  let u=unit(l.file,raw), focus=existing,owner,fn;
  const directive=raw.match(/\/\/[#@]\s*sourceMappingURL=([^\r\n]+)\s*$/);
  if(directive && /^[\w.-]+\.map$/.test(directive[1].trim())) {
    const mf=path.posix.join(path.posix.dirname(l.file),directive[1].trim()), map=checked(mf);
    if(map) {
      const m=JSON.parse(map), entry=new SourceMap(m).findEntry(l.line-1,l.column-1);
      if(entry.originalSource) {
        const origin=path.posix.normalize(path.posix.join(path.posix.dirname(l.file.slice(('node_modules/'+spec.pkg+'/').length)),entry.originalSource));
        const original=spec.git[origin], idx=m.sources.indexOf(entry.originalSource);
        if(original && idx>=0 && original===m.sourcesContent?.[idx] && permitted(origin)) {
          const mapped=unit('git/'+origin,original), cls=singleton(mapped.definitions.get((api.receiver_path || '').split('.').at(-2)));
          const method=cls && ts.isClassDeclaration(cls) && singleton(cls.members.filter(n=>n.name?.getText(mapped.tree)===api.name));
          // Transpilers can map private wrapper lines to the enclosing class declaration.
          const wrapper = /^function\s+([\w$]+)\s*\(/.exec(existing);
          const anchor = method && wrapper?.[1] === api.name ? cls : method;
          if(anchor && entry.originalLine >= mapped.tree.getLineAndCharacterOfPosition(anchor.getStart(mapped.tree)).line-1
                    && entry.originalLine <= mapped.tree.getLineAndCharacterOfPosition(anchor.end).line+1) {
            u=mapped;owner=cls;fn=method;focus=method.getText(u.tree);
            add('source_map_method',u,method);
            data.at(-1).map_sha256=hash(map);
          }
        }
      }
    }
  }
  const parent=(ut,c)=> c.heritageClauses?.[0]?.types[0] && reference(ut,c.heritageClauses[0].types[0].expression.getText(ut.tree));
  if(owner) {
    let pair=[u,owner],ctor=false;
    const members=new Map(), dependencies=new Map(), inspected=[fn];
    for(let depth=0;pair && depth<3;depth++) {
      const [ut,c]=pair;
      if(!ts.isClassDeclaration(c)) break;
      for(const m of c.members) {
        if(ts.isConstructorDeclaration(m) && !ctor){
          add('receiver_constructor',ut,m,850);ctor=true;
          for(const parameter of m.parameters) {
            if(parameter.modifiers?.length && ts.isIdentifier(parameter.name) &&
               parameter.type && ts.isTypeReferenceNode(parameter.type)) {
              const type=reference(ut,parameter.type.typeName.getText(ut.tree));
              if(type && ts.isClassDeclaration(type[1])) dependencies.set(parameter.name.text,type);
            }
          }
        }
        const isStatic=m.modifiers?.some(x=>x.kind===ts.SyntaxKind.StaticKeyword);
        if(isStatic && ts.isPropertyDeclaration(m)) add('receiver_static_field',ut,m,180);
        if(m.name && !isStatic) {
          const name=m.name.getText(ut.tree);
          if(!members.has(name)) {
            const own=c.members.filter(x=>x.name?.getText(ut.tree)===name &&
              !x.modifiers?.some(y=>y.kind===ts.SyntaxKind.StaticKeyword));
            const resolved=singleton(own.filter(x=>ts.isMethodDeclaration(x)||ts.isGetAccessor(x)));
            members.set(name,resolved ? [ut,resolved] : null);
          }
        }
      }
      pair=parent(ut,c);
    }
    function receiverNames(node) {
      const names=new Set();
      function walk(n) {
        if(ts.isFunctionLike(n) && !ts.isArrowFunction(n)) return;
        if(ts.isPropertyAccessExpression(n) && n.expression.kind===ts.SyntaxKind.ThisKeyword) names.add(n.name.text);
        ts.forEachChild(n,walk);
      }
      if(node?.body) walk(node.body);
      return names;
    }
    let pending=receiverNames(fn);
    const visited=new Set();
    // Follow only this-receiver methods/getters; lexical functions have another receiver.
    for(let depth=0;depth<4 && pending.size;depth++) {
      const next=new Set();
      for(const name of pending) {
        if(visited.has(name)) continue;
        visited.add(name);
        const resolved=members.get(name); if(!resolved) continue;
        const [ut,node]=resolved;
        add(ts.isGetAccessor(node)?'receiver_getter':'receiver_method',ut,node,1000);
        inspected.push(node);
        for(const dependency of receiverNames(node)) next.add(dependency);
      }
      pending=next;
    }
    const calls=new Map();
    function dependencyCalls(n) {
      if(ts.isFunctionLike(n) && !ts.isArrowFunction(n)) return;
      if(ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression)) {
        const receiver=n.expression.expression;
        if(ts.isPropertyAccessExpression(receiver) && receiver.expression.kind===ts.SyntaxKind.ThisKeyword &&
           dependencies.has(receiver.name.text)) {
          if(!calls.has(receiver.name.text)) calls.set(receiver.name.text,new Set());
          calls.get(receiver.name.text).add(n.expression.name.text);
        }
      }
      ts.forEachChild(n,dependencyCalls);
    }
    for(const node of inspected) if(node?.body) dependencyCalls(node.body);
    for(const [name,used] of [...calls].slice(0,2)) {
      const [ut,type]=dependencies.get(name);
      const constructor=singleton(type.members.filter(ts.isConstructorDeclaration));
      if(constructor) add('typed_dependency_constructor',ut,constructor,1600);
      for(const member of [...used].slice(0,2)) {
        const method=singleton(type.members.filter(n=>ts.isMethodDeclaration(n)&&n.name.getText(ut.tree)===member));
        if(method) add('typed_dependency_method',ut,method,650);
      }
    }
  }
  const names=[...new Set(focus.match(/\b[A-Z][a-zA-Z0-9_$]*\b/g)||[])];
  for(const name of names) {
    let pair=reference(u,name);
    for(let depth=0;pair && depth<2;depth++){
      const [ut,n]=pair;
      if(ts.isClassDeclaration(n)) {
        for(const m of n.members){
          if(ts.isConstructorDeclaration(m)) add('referenced_type_constructor',ut,m,480);
          else if(ts.isMethodDeclaration(m)||ts.isGetAccessor(m)||ts.isPropertyDeclaration(m))add('referenced_type_member',ut,m,260);
        }
        pair=parent(ut,n);
      } else {
        add('referenced_function',ut,n,320);
        for(const m of (ut.methods.get(n.name.text)||[]).slice(0,7))add('referenced_prototype_member',ut,m,570);
        pair=null;
      }
    }
  }
  // Only direct calls to unshadowed, top-level relative CommonJS imports qualify.
  const targetTree=ts.createSourceFile('target.js','('+focus+')',ts.ScriptTarget.Latest,true);
  const shadowed=new Set(), called=new Set();
  function bind(n) { if(ts.isIdentifier(n)) shadowed.add(n.text); else ts.forEachChild(n,bind); }
  function calls(n) {
    if ((ts.isVariableDeclaration(n)||ts.isParameter(n)||ts.isFunctionDeclaration(n)||ts.isClassDeclaration(n)) && n.name) bind(n.name);
    if (ts.isCallExpression(n) && ts.isIdentifier(n.expression)) called.add(n.expression.text);
    ts.forEachChild(n,calls);
  }
  calls(targetTree);
  let imported=0;
  for(const name of [...called].sort()) {
    if(shadowed.has(name)||!u.imports.has(name)||imported>=2) continue;
    const declarations=[];
    for(const statement of u.tree.statements) {
      if(ts.isVariableStatement(statement)) for(const decl of statement.declarationList.declarations)
        if(ts.isIdentifier(decl.name)&&decl.name.text===name) declarations.push(decl);
    }
    const decl=singleton(declarations);
    const importedPath=decl?.initializer && /^require\(["'](\.[^"']+)["']\)$/.exec(decl.initializer.getText(u.tree));
    if(!importedPath || !(decl.parent.flags & ts.NodeFlags.Const)) continue;
    const stem=path.posix.normalize(path.posix.join(path.posix.dirname(u.file),importedPath[1]));
    for(const file of [stem,stem+'.js',stem+'/index.js']) {
      const src=checked(file);if(!src) continue;
      const lib=unit(file,src), exports=lib.tree.statements.filter(n=>ts.isExpressionStatement(n)&&
        ts.isBinaryExpression(n.expression)&&n.expression.operatorToken.kind===ts.SyntaxKind.EqualsToken&&
        n.expression.left.getText(lib.tree)==='module.exports');
      const assignment=singleton(exports)?.expression;
      let fn=assignment?.right;
      if(fn&&ts.isIdentifier(fn)) fn=singleton(lib.definitions.get(fn.text));
      if(fn&&(ts.isArrowFunction(fn)||ts.isFunctionExpression(fn)||ts.isFunctionDeclaration(fn))) {
        add('direct_local_callee',lib,fn,1800);imported++;
      }
      break;
    }
  }
  const symbols = new Set(data.flatMap(row=>row.text.match(/\b[A-Z][a-zA-Z0-9_$]*\b/g)||[]));
  for(const symbol of symbols) {
    const matches=Object.keys(spec.docs).filter(f=>path.posix.basename(f,'.md').toLowerCase()===symbol.toLowerCase());
    if(matches.length!==1) continue;
    const file=matches[0], text=spec.docs[file], block=/```(?:js|javascript|ts|typescript)\s*\n([^]*?)\n```/.exec(text);
    if(block && block[1].includes(symbol) && chars+block[1].length<=5800){
      data.push({kind:'named_public_api_documentation',file:'git/'+file,file_sha256:hash(text),
                 line:text.slice(0,block.index).split('\n').length+1,text:block[1].slice(0,700)});
      chars+=Math.min(700,block[1].length);
    }
  }
  // Short modules can initialize lookup tables outside the target's function body.
  if(raw.length>existing.length && chars+raw.length<=5200) {
    data.push({kind:'module_source_context',file:l.file,file_sha256:hash(raw),line:1,
               text:raw,truncated:false,complete:true});
    chars+=raw.length;
  }
  // Follow explicit same-version declaration links, never a same-named method guess.
  const runtime=unit(l.file,raw), exported=n=>n.modifiers?.some(m=>m.kind===ts.SyntaxKind.ExportKeyword);
  const target=singleton(runtime.tree.statements.filter(n=>ts.isFunctionDeclaration(n)&&n.name?.text===api.name));
  const position=target && runtime.tree.getLineAndCharacterOfPosition(target.getStart(runtime.tree));
  if(target?.getText(runtime.tree)===existing && position.line===l.line-1 && position.character===l.column-1 && l.file.endsWith('.js')) {
    const file=l.file.slice(0,-3)+'.d.ts', text=checked(file);
    if(text) {
      const decl=unit(file,text);
      const fn=singleton(decl.tree.statements.filter(n=>ts.isFunctionDeclaration(n)&&n.name?.text===api.name));
      if(fn && exported(fn) && fn.parameters.map(p=>p.name.getText(decl.tree)).join(',')===
          target.parameters.map(p=>p.name.getText(runtime.tree)).join(',')) {
        add('adjacent_type_declaration',decl,fn,1000,true);
        const doc=(fn.jsDoc||[]).map(n=>n.getText(decl.tree)).join('\n');
        const links=[...new Set([...doc.matchAll(/\[\[([\w$]+\.[\w$]+)\]\]/g)].map(m=>m[1]))];
        for(const link of links.slice(0,2)) {
          const [type,member]=link.split('.'), bindings=[];
          for(const n of decl.tree.statements) {
            if(!ts.isImportDeclaration(n)) continue;
            const named=n.importClause?.namedBindings;
            if(named && ts.isNamedImports(named)) for(const b of named.elements)
              if(b.name.text===type) bindings.push({name:(b.propertyName||b.name).text,from:n.moduleSpecifier.text});
          }
          const binding=singleton(bindings);
          if(!binding?.from?.startsWith('.')) continue;
          const stem=path.posix.normalize(path.posix.join(path.posix.dirname(file),binding.from));
          const matches=[stem+'.d.ts',stem+'/index.d.ts'].map(f=>[f,checked(f)]).filter(p=>p[1]);
          const resolved=singleton(matches);if(!resolved) continue;
          const lib=unit(...resolved);
          const owner=singleton(lib.tree.statements.filter(n=>(ts.isInterfaceDeclaration(n)||ts.isClassDeclaration(n))&&n.name?.text===binding.name));
          if(!owner || !exported(owner)) continue;
          const method=singleton(owner.members.filter(n=>n.name?.getText(lib.tree)===member));
          if(method && (ts.isMethodSignature(method)||ts.isMethodDeclaration(method)))
            add('documented_delegate_contract',lib,method,1000,true);
        }
      }
    }
  }
  return data;
}
process.stdout.write(JSON.stringify(Object.fromEntries(spec.apis.map(a=>[a.access_path,evidence(a)]))));
