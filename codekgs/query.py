"""
CodeKG 查询接口模块
提供对构建好的 CodeKG 的查询功能
"""

import os
import json
import hashlib
from pathlib import Path
import logging
from typing import Dict, List, Any, Optional
import networkx as nx

logger = logging.getLogger(__name__)

SOURCE_CONTEXT_VERSION = 'linux-source-context-v2'


def verify_context_snapshot(directory):
    """A full run must use the enhanced snapshot, not a valid but bare base graph."""
    directory = Path(directory).resolve(strict=True)
    inventory = json.loads((directory.parent / 'inventory.json').read_text('utf8'))
    expected = {row['file'] for row in inventory}
    if expected != {p.name for p in directory.glob('*_codekg.json')}:
        raise ValueError('Source-context inventory differs')
    for row in inventory:
        raw = (directory / row['file']).read_bytes()
        graph = json.loads(raw)
        if (hashlib.sha256(raw).hexdigest() != row['sha256'] or
                graph.get('source_context', {}).get('version') != SOURCE_CONTEXT_VERSION):
            raise ValueError('Full generation requires verified source-context graphs: ' + row['file'])
        by_path = {api['access_path']: api for api in graph['apis']}
        for api in graph['apis']:
            evidence = api['source_evidence']
            status = evidence.get('implementation_status')
            body = evidence.get('function_text', {}).get('implementation_excerpt')
            if status == 'complete':
                if not body or hashlib.sha256(body.encode()).hexdigest() != api['function_sha256']:
                    raise ValueError('Implementation fingerprint differs: ' + api['access_path'])
            elif status == 'wrapped_implementation':
                origin = evidence['callable_origin']
                target = by_path.get(origin['target_access_path'])
                if (body or not target or origin['runtime_function_sha256'] != api['function_sha256'] or
                        origin['target_function_sha256'] != target['function_sha256'] or
                        origin['target_source_evidence'] != target['source_evidence'] or
                        target['source_evidence']['implementation_status'] != 'complete'):
                    raise ValueError('Wrapped implementation differs: ' + api['access_path'])
                if origin['kind'] == 'bound_function':
                    if (origin['receiver']['kind'] == 'exported_object' and not origin['receiver']['paths'] or
                            any(arg['kind'] == 'unresolved' for arg in origin['arguments'])):
                        raise ValueError('Unresolved bound context: ' + api['access_path'])
                elif origin['kind'] != 'proxy' or origin['revoked'] or not origin['handlers'] or any(
                        not handler['locations'] for handler in origin['handlers']):
                    raise ValueError('Unresolved proxy context: ' + api['access_path'])
            elif status not in {'native_implementation', 'location_unresolved'} or body:
                raise ValueError('Unresolved source extraction: ' + api['access_path'])
    return inventory

class CodeKGQuery:
    """CodeKG 查询器"""
    
    def __init__(self, codekg_dir: str = None):
        self.codekg_dir = codekg_dir or './codekgs/codekg'
        self.loaded_graphs: Dict[str, nx.DiGraph] = {}
        self.loaded_data: Dict[str, Dict[str, Any]] = {}
    
    def query_api_context(self, api: Dict[str, Any], codekg: Dict[str, Any]) -> Dict[str, Any]:
        """
        查询API的上下文信息（用于RAG）
        
        Args:
            api: API信息
            codekg: CodeKG数据
            
        Returns:
            上下文信息字典
        """
        context = {
            'api_name': api.get('name', ''),
            'usage_examples': api.get('usage_examples', []),
            'common_errors': api.get('common_errors', []),
            'safety_tips': api.get('safety_tips', []),
            'related_apis': []
        }
        
        # 查找相关API（同一个包中的其他API）
        all_apis = codekg.get('apis', [])
        api_name = api.get('name', '')
        
        for other_api in all_apis[:5]:  # 最多5个相关API
            if other_api.get('name') != api_name:
                context['related_apis'].append({
                    'name': other_api.get('name', ''),
                    'access_path': other_api.get('access_path', ''),
                    'signature': other_api.get('signature', '')
                })
        
        return context

    def build_api_fact_bundle(self, package_name: str, api: Dict[str, Any], *,
                              access_path: Optional[str] = None,
                              source_commit: Optional[str] = None) -> Dict[str, Any]:
        """
        构建统一的 API facts bundle，供前置 grounding 与多代理消费。
        """
        api_name = api.get('name', '')
        loaded = self.load_codekg(package_name)
        if source_commit or api.get("source_commit"):
            if not loaded or self.loaded_data[package_name].get("schema_version") != "revision-source-graph-v1":
                raise KeyError(f"No pinned source graph for {package_name}")
        if loaded:
            data = self.loaded_data[package_name]
            if data.get("schema_version") == "revision-source-graph-v1":
                path = access_path or api.get("access_path")
                commit = source_commit or api.get("source_commit")
                if not commit or commit != data["source_commit"] or data["package_name"] != package_name:
                    raise ValueError(f"Source pin mismatch for {package_name} at {path}")
                record = next((a for a in data["apis"] if a["access_path"] == path), None)
                if not record or record["source_commit"] != commit:
                    raise KeyError(f"No exact source graph record: {package_name}@{commit} {path}")
                graph = self.loaded_graphs[package_name]
                if path not in graph or graph.nodes[path].get("access_path") != path:
                    raise KeyError(f"No callable graph node for {path}")
                receiver = None
                node = path
                while node != package_name:
                    parents = [p for p in graph.predecessors(node) if graph.edges[p,node].get("relation") == "has_member"]
                    if len(parents) != 1 or not node.startswith((parents[0]+".", parents[0]+"[")):
                        raise ValueError(f"Missing or ambiguous member edge for {node}")
                    if node == path:
                        receiver = parents[0]
                    node = parents[0]
                if receiver != record["receiver_path"]:
                    raise ValueError(f"Receiver disagrees with graph for {path}")
                fields = ("access_path", "source_commit", "signature", "parameters", "is_async", "return_type",
                          "receiver_path", "receiver_kind", "inherited_member", "native_identity",
                          "return_evidence", "source_evidence", "file_path", "function_sha256")
                construction = {}
                if receiver and receiver.endswith(".prototype"):
                    constructor_path = receiver[:-len(".prototype")]
                    constructor = next((a for a in data["apis"] if a["access_path"] == constructor_path), None)
                    if constructor and constructor["source_commit"] == commit and constructor_path in graph:
                        construction["receiver_constructor"] = {key: constructor[key] for key in
                            ("access_path", "signature", "parameters", "function_sha256", "source_evidence")}
                    members = sorted((a for a in data["apis"] if a["receiver_path"] == receiver),
                                     key=lambda a: a["access_path"])
                    construction["receiver_methods"] = [{key: a[key] for key in
                        ("access_path", "signature", "native_identity")} for a in members[:24]]
                    construction["receiver_methods_truncated"] = len(members) > 24
                return {**{key:record[key] for key in fields},
                        **construction,
                        "receiver_path":receiver,
                        **({"declaration_evidence": record["declaration_evidence"]}
                           if record.get("declaration_evidence") else {}),
                        "package_name":package_name,
                        "api_name":record["name"], "api_type":record["type"], "exists":True,
                        "parameter_count":len(record["parameters"]) if record["parameters"] is not None else None,
                        "usage_examples":[], "common_errors":[], "safety_tips":[]}
        api_info = self.check_api(api_name, package_name, access_path=access_path) or {}
        node_data = api_info.get('node_data', {})
        parameters = api.get('parameters', api_info.get('parameters', [])) or []
        common_errors = api.get('common_errors', api_info.get('common_errors', [])) or []
        usage_examples = api.get('usage_examples', api_info.get('usage_examples', [])) or []
        safety_tips = api.get('safety_tips', api_info.get('safety_tips', [])) or []

        bundle = {
            'package_name': package_name,
            'api_name': api_name,
            'exists': bool(api_info.get('exists', True)),
            'api_type': api.get('type', api_info.get('type', 'unknown')),
            'access_path': api.get('access_path') or node_data.get('access_path') or api_name,
            'signature': api.get('signature') or node_data.get('signature') or '',
            'parameters': parameters,
            'parameter_count': len(parameters),
            'is_async': bool(
                api.get('is_async',
                        node_data.get('is_async',
                                      'promise' in str(api.get('return_type', '')).lower()))
            ),
            'return_type': api.get('return_type') or node_data.get('return_type') or '',
            'usage_examples': usage_examples[:3],
            'common_errors': common_errors[:5],
            'safety_tips': safety_tips[:5],
            'file_path': api.get('file_path', api_info.get('file_path', '')),
        }
        return bundle
    
    def load_codekg(self, package_name: str) -> bool:
        """
        加载指定包的 CodeKG
        
        Args:
            package_name: 包名
            
        Returns:
            是否加载成功
        """
        if package_name in self.loaded_data:
            return True
        
        # 构建文件路径
        safe_pkg_name = package_name.replace('/', '_').replace('@', '')
        codekg_file = os.path.join(self.codekg_dir, f"{safe_pkg_name}_codekg.json")
        
        if not os.path.exists(codekg_file):
            logger.warning(f"CodeKG file not found: {codekg_file}")
            return False
        
        try:
            with open(codekg_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # 重建图结构
            graph_data = data['graph']
            edge_field = 'edges' if 'edges' in graph_data else 'links'
            graph = nx.node_link_graph(graph_data, edges=edge_field)
            
            self.loaded_graphs[package_name] = graph
            self.loaded_data[package_name] = data
            
            logger.debug(f"Loaded CodeKG for {package_name}: "
                        f"{graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges")
            return True
            
        except Exception as e:
            logger.error(f"Failed to load CodeKG for {package_name}: {e}")
            return False
    
    def check_api(self, api_name: str, package_name: str, *,
                  access_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        检查 API 是否存在并返回详细信息
        
        Args:
            api_name: API 名称
            package_name: 包名
            
        Returns:
            API 信息字典，不存在时返回 None
        """
        if not self.load_codekg(package_name):
            return None
        
        data = self.loaded_data[package_name]
        graph = self.loaded_graphs[package_name]
        if data.get("schema_version") == "revision-source-graph-v1" and access_path is None:
            candidates = [api for api in data["apis"] if api["name"] == api_name]
            if len(candidates) != 1:
                return None
            access_path = candidates[0]["access_path"]
        
        # 在 APIs 列表中查找
        for api in data.get('apis', []):
            if access_path is not None and api.get('access_path') != access_path:
                continue
            if api.get('name') == api_name:
                # 从图中获取更详细的信息
                node_id = api.get('id')
                if node_id and graph.has_node(node_id):
                    node_data = graph.nodes[node_id]
                    
                    result = {
                        'name': api_name,
                        'type': api.get('type', 'unknown'),
                        'exists': True,
                        'access_path': api.get('access_path', ''),
                        'signature': api.get('signature', ''),
                        'is_async': api.get('is_async', node_data.get('is_async', False)),
                        'return_type': api.get('return_type', node_data.get('return_type', '')),
                        'parameters': api.get('parameters', []),
                        'file_path': api.get('file_path', ''),
                        'usage_examples': api.get('usage_examples', []),
                        'common_errors': api.get('common_errors', []),
                        'safety_tips': api.get('safety_tips', []),
                        'node_data': node_data
                    }
                    
                    return result
                else:
                    # 如果没有 node_id 或节点不存在，返回基本信息
                    return {
                        'name': api_name,
                        'type': api.get('type', 'unknown'),
                        'exists': True,
                        'access_path': api.get('access_path', ''),
                        'signature': api.get('signature', ''),
                        'is_async': api.get('is_async', False),
                        'return_type': api.get('return_type', ''),
                        'parameters': api.get('parameters', []),
                        'file_path': api.get('file_path', ''),
                        'usage_examples': api.get('usage_examples', []),
                        'common_errors': api.get('common_errors', []),
                        'safety_tips': api.get('safety_tips', []),
                        'node_data': {}
                    }
        
        # 如果在 APIs 列表中没找到，尝试在图中查找
        if access_path is not None:
            return None
        possible_node_ids = [
            f"{package_name}.{api_name}",
            api_name
        ]
        
        for node_id in possible_node_ids:
            if graph.has_node(node_id):
                node_data = graph.nodes[node_id]
                return {
                    'name': api_name,
                    'type': node_data.get('type', 'unknown'),
                    'exists': True,
                    'access_path': node_data.get('access_path', api_name),
                    'signature': node_data.get('signature', ''),
                    'is_async': node_data.get('is_async', False),
                    'return_type': node_data.get('return_type', ''),
                    'parameters': node_data.get('parameters', []),
                    'file_path': node_data.get('file_path', ''),
                    'usage_examples': [],
                    'common_errors': [],
                    'safety_tips': [],
                    'node_data': node_data
                }
        
        return None
    
    def get_all_apis(self, package_name: str) -> List[str]:
        """
        获取指定包的所有 API 名称
        
        Args:
            package_name: 包名
            
        Returns:
            API 名称列表
        """
        if not self.load_codekg(package_name):
            return []
        
        data = self.loaded_data[package_name]
        return [api['name'] for api in data.get('apis', [])]
    
    def get_api_relationships(self, api_name: str, package_name: str) -> Dict[str, List[str]]:
        """
        获取 API 的关系信息
        
        Args:
            api_name: API 名称
            package_name: 包名
            
        Returns:
            关系信息字典
        """
        if not self.load_codekg(package_name):
            return {}
        
        graph = self.loaded_graphs[package_name]
        node_id = f"{package_name}.{api_name}"
        
        if not graph.has_node(node_id):
            return {}
        
        relationships = {
            'parameters': [],
            'calls': [],
            'called_by': [],
            'extends': [],
            'extended_by': []
        }
        
        # 获取参数
        for successor in graph.successors(node_id):
            edge_data = graph.edges[node_id, successor]
            if edge_data.get('relation') == 'has_parameter':
                param_name = graph.nodes[successor].get('name', successor)
                relationships['parameters'].append(param_name)
        
        # 获取调用关系
        for successor in graph.successors(node_id):
            edge_data = graph.edges[node_id, successor]
            if edge_data.get('relation') == 'calls':
                relationships['calls'].append(successor)
        
        for predecessor in graph.predecessors(node_id):
            edge_data = graph.edges[predecessor, node_id]
            if edge_data.get('relation') == 'calls':
                relationships['called_by'].append(predecessor)
        
        # 获取继承关系
        for successor in graph.successors(node_id):
            edge_data = graph.edges[node_id, successor]
            if edge_data.get('relation') == 'extends':
                relationships['extends'].append(successor)
        
        for predecessor in graph.predecessors(node_id):
            edge_data = graph.edges[predecessor, node_id]
            if edge_data.get('relation') == 'extends':
                relationships['extended_by'].append(predecessor)
        
        return relationships
    
    def search_apis(self, query: str, package_name: str) -> List[Dict[str, Any]]:
        """
        搜索 API
        
        Args:
            query: 搜索查询
            package_name: 包名
            
        Returns:
            匹配的 API 列表
        """
        if not self.load_codekg(package_name):
            return []
        
        data = self.loaded_data[package_name]
        results = []
        
        query_lower = query.lower()
        
        for api in data.get('apis', []):
            # 名称匹配
            if query_lower in api['name'].lower():
                results.append(api)
                continue
            
            # 参数匹配
            for param in api.get('parameters', []):
                if query_lower in param.lower():
                    results.append(api)
                    break
        
        return results
    
    def get_package_stats(self, package_name: str) -> Dict[str, Any]:
        """
        获取包的统计信息
        
        Args:
            package_name: 包名
            
        Returns:
            统计信息
        """
        if not self.load_codekg(package_name):
            return {}
        
        data = self.loaded_data[package_name]
        graph = self.loaded_graphs[package_name]
        
        # 统计不同类型的节点
        node_types = {}
        for node_id, node_data in graph.nodes(data=True):
            node_type = node_data.get('type', 'unknown')
            node_types[node_type] = node_types.get(node_type, 0) + 1
        
        return {
            'package_name': package_name,
            'total_apis': len(data.get('apis', [])),
            'total_nodes': graph.number_of_nodes(),
            'total_edges': graph.number_of_edges(),
            'node_types': node_types,
            'enhanced': data.get('enhanced', False)
        }

# 全局查询器实例
_query_instance: Optional[CodeKGQuery] = None

def init_codekg_query(codekg_dir: str) -> CodeKGQuery:
    """初始化全局 CodeKG 查询器"""
    global _query_instance
    _query_instance = CodeKGQuery(codekg_dir)
    return _query_instance

def get_codekg_query() -> CodeKGQuery:
    """获取全局 CodeKG 查询器"""
    if _query_instance is None:
        raise RuntimeError("CodeKG query not initialized")
    return _query_instance

# 便捷函数
def check_api(api_name: str, package_name: str) -> Optional[Dict[str, Any]]:
    """检查 API 是否存在"""
    query = get_codekg_query()
    return query.check_api(api_name, package_name)

def get_all_apis(package_name: str) -> List[str]:
    """获取所有 API"""
    query = get_codekg_query()
    return query.get_all_apis(package_name)
