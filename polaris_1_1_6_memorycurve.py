#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import ast
import hashlib
import datetime
import importlib.util
import inspect
import io
import json
import math
import operator
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import time
from typing import Any, Callable, get_type_hints
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment]
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}
def _truncate_text(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...(已截断)"
def _safe_json_load(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
def _atomic_write_text(path: str, content: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".miniagent_", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
def _looks_local_endpoint(base_url: str | None) -> bool:
    if not base_url:
        return False
    url = base_url.lower()
    return any(part in url for part in ("127.0.0.1", "localhost", "0.0.0.0", "11434", "1234"))
def _resolve_api_key(api_key: str | None, base_url: str | None) -> str:
    if api_key and api_key.strip():
        return api_key.strip()
    if _looks_local_endpoint(base_url):
        return "EMPTY"
    raise RuntimeError("缺少 OPENAI_API_KEY。若使用本地 OpenAI-Compatible 服务，请确保 base_url 指向 Ollama/LM Studio 等本地端点。")
def _normalize_text(text: str) -> list[str]:
    cleaned = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    cleaned = cleaned.replace("_", " ").replace("-", " ")
    ascii_tokens = re.findall(r"[a-z0-9]+", cleaned.lower())
    cjk_chunks = re.findall(r"[\u4e00-\u9fff]+", cleaned)
    cjk_chars: list[str] = []
    for chunk in cjk_chunks:
        cjk_chars.extend(list(chunk))
    cjk_bigrams = [chunk[i:i+2] for chunk in cjk_chunks for i in range(max(0, len(chunk) - 1))]
    return [tok for tok in ascii_tokens + cjk_chunks + cjk_chars + cjk_bigrams if tok]
def _char_ngrams(text: str, n: int = 2) -> set[str]:
    cleaned = re.sub(r"\s+", "", text.lower())
    cleaned = cleaned.replace("_", "").replace("-", "")
    if len(cleaned) < n:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i+n] for i in range(len(cleaned) - n + 1)}
_ALIAS_GROUPS: list[set[str]] = [
    {"error", "errors", "exception", "bug", "crash", "fail", "failure", "syntaxerror", "traceback", "报错", "错误", "异常", "故障", "崩溃", "语法错误"},
    {"memory", "记忆", "长期记忆", "档案", "回忆", "recall"},
    {"workspace", "project", "项目", "工作区", "世界模型", "文件", "依赖", "module"},
    {"conversation", "chat", "history", "对话", "窗口", "聊天", "上下文"},
    {"mood", "emotion", "状态", "心情", "frustration", "confidence", "fatigue", "curiosity", "stability"},
    {"plugin", "plugins", "插件", "扩展"},
]
def _group_bonus(query: str, content: str) -> int:
    q = query.lower()
    c = content.lower()
    bonus = 0
    for group in _ALIAS_GROUPS:
        if any(term in q for term in group) and any(term in c for term in group):
            bonus += 6
    return bonus
def _score_overlap(query: str, content: str) -> int:
    q_norm = query.strip().lower()
    c_norm = content.strip().lower()
    if not q_norm:
        return 0
    q_tokens = set(_normalize_text(query))
    c_tokens = set(_normalize_text(content))
    token_overlap = len(q_tokens & c_tokens)
    q_bi = _char_ngrams(query, 2)
    c_bi = _char_ngrams(content, 2)
    bigram_overlap = len(q_bi & c_bi)
    phrase_bonus = 10 if q_norm in c_norm else 0
    starts_bonus = 2 if q_tokens and c_tokens and next(iter(q_tokens)) in c_tokens else 0
    group_bonus = _group_bonus(query, content)
    return token_overlap * 3 + bigram_overlap * 2 + phrase_bonus + starts_bonus + group_bonus
_STATE_LOCK = threading.RLock()
def _python_sandbox_wrapper() -> str:
    return (
        "import ast, builtins, json, sys, traceback\n"
        "ALLOWED_IMPORTS = {'math','json','re','datetime','statistics','random','functools','itertools','collections','heapq','decimal','fractions','string'}\n"
        "SAFE_BUILTINS = {\n"
        "    'abs': abs, 'all': all, 'any': any, 'bool': bool, 'dict': dict, 'enumerate': enumerate,\n"
        "    'float': float, 'int': int, 'len': len, 'list': list, 'max': max, 'min': min, 'pow': pow,\n"
        "    'print': print, 'range': range, 'round': round, 'set': set, 'sorted': sorted, 'str': str,\n"
        "    'sum': sum, 'tuple': tuple, 'zip': zip, 'map': map, 'filter': filter, 'isinstance': isinstance,\n"
        "    'Exception': Exception, 'ValueError': ValueError, 'TypeError': TypeError,\n"
        "}\n"
        "def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):\n"
        "    root = name.split('.', 1)[0]\n"
        "    if root not in ALLOWED_IMPORTS:\n"
        "        raise ImportError(f'模块 {root!r} 不在允许列表中')\n"
        "    return __import__(name, globals, locals, fromlist, level)\n"
        # 原来漏了这一行：_safe_import 定义了却没装进 SAFE_BUILTINS，
        # 而 import 语句是靠 builtins.__import__ 落地的，所以 ALLOWED_IMPORTS 里
        # 那批模块（math/json/re/datetime...）过得了 AST 校验，运行时却一律
        # ImportError: __import__ not found —— 白名单等于完全没生效。
        "SAFE_BUILTINS['__import__'] = _safe_import\n"
        "def _validate(tree):\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, (ast.Global, ast.Nonlocal)):\n"
        "            raise RuntimeError('不允许使用 global / nonlocal')\n"
        "        if isinstance(node, ast.Import):\n"
        "            for alias in node.names:\n"
        "                root = alias.name.split('.', 1)[0]\n"
        "                if root not in ALLOWED_IMPORTS:\n"
        "                    raise ImportError(f'模块 {root!r} 不在允许列表中')\n"
        "        if isinstance(node, ast.ImportFrom):\n"
        "            root = (node.module or '').split('.', 1)[0]\n"
        "            if root and root not in ALLOWED_IMPORTS:\n"
        "                raise ImportError(f'模块 {root!r} 不在允许列表中')\n"
        "        if isinstance(node, ast.Call):\n"
        "            if isinstance(node.func, ast.Name) and node.func.id in {\'getattr\', \'__import__\', \'open\', \'exec\', \'eval\', \'compile\', \'input\', \'setattr\', \'delattr\', \'vars\', \'locals\', \'globals\'}:\n"
        "                raise RuntimeError(f\'禁止调用 {node.func.id}\')\n"
        # 光禁函数名不够：从任意字面量出发沿 __class__ / __mro__ / __subclasses__
        # 就能摸回内置类型树，把上面清空的能力重新拿回来，所以这里直接封死 dunder 访问。
        "        if isinstance(node, ast.Attribute) and node.attr.startswith(\'__\') and node.attr.endswith(\'__\'):\n"
        "            raise RuntimeError(f\'禁止访问内部属性 {node.attr}\')\n"
        "        if isinstance(node, ast.Name) and node.id.startswith(\'__\') and node.id.endswith(\'__\'):\n"
        "            raise RuntimeError(f\'禁止访问 {node.id}\')\n"
        "    return tree\n"
        "code = sys.stdin.read()\n"
        "try:\n"
        "    tree = _validate(ast.parse(code, '<miniagent>', 'exec'))\n"
        "    glb = {'__name__': '__agent__', '__builtins__': SAFE_BUILTINS}\n"
        "    exec(compile(tree, '<miniagent>', 'exec'), glb, glb)\n"
        "except Exception:\n"
        "    traceback.print_exc()\n"
    )
class _ThreadLocalContextProxy:
    def __init__(self):
        self._local = threading.local()
    def _data(self) -> dict[str, Any]:
        data = getattr(self._local, "data", None)
        if data is None:
            data = {}
            self._local.data = data
        return data
    def get(self, key: str, default: Any = None) -> Any:
        return self._data().get(key, default)
    def update(self, other=None, **kwargs) -> None:
        data = self._data()
        if other is not None:
            if hasattr(other, "items"):
                data.update(other)
            else:
                data.update(dict(other))
        if kwargs:
            data.update(kwargs)
    def copy(self) -> dict[str, Any]:
        return dict(self._data())
    def clear(self) -> None:
        self._local.data = {}
CURRENT_AGENT_CONTEXT = _ThreadLocalContextProxy()
class ChatBackend:
    name = "base"
    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        stream: bool = False,
        response_format: dict | None = None,
    ):
        raise NotImplementedError
    def create_embeddings(self, *, model: str, inputs: list[str]) -> list[list[float]]:
        raise NotImplementedError(f"后端 {self.name} 不支持向量嵌入。")
class OpenAICompatibleBackend(ChatBackend):
    name = "openai_compatible"
    def __init__(self, api_key: str | None, base_url: str | None):
        if OpenAI is None:
            raise RuntimeError("缺少依赖，请先安装：pip install openai（Polaris 不提供本地假后端，Ollama/LM Studio 请走 OpenAI-Compatible 接口）。")
        resolved_key = _resolve_api_key(api_key, base_url)
        self.client = OpenAI(api_key=resolved_key, base_url=base_url)
    def create_chat_completion(
        self,
        *,
        model: str,
        messages: list[dict],
        max_tokens: int,
        tools: list[dict] | None = None,
        stream: bool = False,
        response_format: dict | None = None,
    ):
        params = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            params["tools"] = tools
        if response_format is not None:
            params["response_format"] = response_format
        return self.client.chat.completions.create(**params, stream=stream)
    def create_embeddings(self, *, model: str, inputs: list[str]) -> list[list[float]]:
        # openai SDK 默认超时是 600 秒。嵌入是写记忆路径上的同步调用，
        # 真按默认值等下去，一次 remember() 能把整个会话卡住十分钟。
        # 单独给嵌入一个短超时，超时后由 EmbeddingService 降级到本地向量。
        client = self.client
        if EMBED_TIMEOUT > 0 and hasattr(client, "with_options"):
            client = client.with_options(timeout=EMBED_TIMEOUT)
        resp = client.embeddings.create(model=model, input=inputs)
        # 返回顺序按 index 排，不依赖服务端保证的顺序。
        rows = sorted(resp.data, key=lambda item: getattr(item, "index", 0))
        return [[float(x) for x in row.embedding] for row in rows]
_BACKEND_FACTORIES: dict[str, Callable[[], ChatBackend]] = {}
def register_backend(name: str, factory: Callable[[], ChatBackend]) -> None:
    _BACKEND_FACTORIES[name.strip().lower()] = factory
def build_backend() -> ChatBackend:
    factory = _BACKEND_FACTORIES.get(BACKEND_NAME)
    if factory is None:
        supported = ", ".join(sorted(_BACKEND_FACTORIES)) or "(无)"
        raise RuntimeError(f"不支持的后端 {BACKEND_NAME!r}。可用后端：{supported}")
    return factory()
register_backend("openai_compatible", lambda: OpenAICompatibleBackend(API_KEY, BASE_URL))
register_backend("openai-compatible", lambda: OpenAICompatibleBackend(API_KEY, BASE_URL))
register_backend("openai", lambda: OpenAICompatibleBackend(API_KEY, BASE_URL))
register_backend("ollama", lambda: OpenAICompatibleBackend(API_KEY, BASE_URL or "http://127.0.0.1:11434/v1"))
register_backend("lmstudio", lambda: OpenAICompatibleBackend(API_KEY, BASE_URL or "http://127.0.0.1:1234/v1"))
register_backend("local", lambda: OpenAICompatibleBackend(API_KEY, BASE_URL or "http://127.0.0.1:11434/v1"))
API_KEY = os.environ.get("OPENAI_API_KEY")
BASE_URL = os.environ.get("OPENAI_BASE_URL")
BACKEND_NAME = os.environ.get("MINIAGENT_BACKEND", "openai_compatible").strip().lower()
# 统一模型配置：门禁、主 Agent、子 Agent 全部使用同一个模型。
MODEL = os.environ.get("MINIAGENT_MODEL", "gpt-4o")
MAIN_MODEL = MODEL
GATE_MODEL = MODEL
MEMORY_FILE = os.environ.get("MINIAGENT_MEMORY_FILE", "agent_memory.json")
MEMORY_VECTOR_FILE = os.environ.get("MINIAGENT_MEMORY_VECTOR_FILE", "agent_memory_vectors.json")
# 向量记忆：auto = 有远程嵌入就用，没有就退回本地哈希向量；local 强制离线；remote 强制远程。
EMBED_BACKEND = os.environ.get("POLARIS_EMBED_BACKEND", "auto").strip().lower() or "auto"
EMBED_MODEL = os.environ.get("POLARIS_EMBED_MODEL", "text-embedding-3-small").strip()
# 1024 维是实测出来的：256 维时哈希碰撞会把一对相关中文句子的余弦从 0.12 压到 0.05，
# 还会给完全无关的句子造出 -0.06 的假信号；1024 维恰好收敛到无碰撞的理想值。
EMBED_DIM = max(32, int(os.environ.get("POLARIS_EMBED_DIM", "1024")))
EMBED_BATCH_CAP = max(16, int(os.environ.get("POLARIS_EMBED_BATCH_CAP", "256")))
# 嵌入请求的超时（秒）。设 0 表示用 SDK 默认值（600 秒，基本等于不超时）。
EMBED_TIMEOUT = float(os.environ.get("POLARIS_EMBED_TIMEOUT", "30"))
# 遗忘曲线：R = exp(-t / S)。低于这个保持率且长期没被想起的记忆转入休眠（不删除）。
MEMORY_FORGET_THRESHOLD = float(os.environ.get("POLARIS_MEMORY_FORGET_THRESHOLD", "0.05"))
MEMORY_DORMANT_MIN_DAYS = float(os.environ.get("POLARIS_MEMORY_DORMANT_MIN_DAYS", "3"))
MOOD_FILE = os.environ.get("MINIAGENT_MOOD_FILE", "agent_mood.json")
CHECKPOINT_DIR = os.environ.get("MINIAGENT_CHECKPOINT_DIR", ".miniagent_checkpoints")
CONVERSATION_FILE = os.environ.get("MINIAGENT_CONVERSATION_FILE", "agent_conversations.jsonl")
TODO_FILE = os.environ.get("MINIAGENT_TODO_FILE", "agent_todos.json")
SESSION_ID = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
INSTRUCTION_FILES = [os.path.expanduser("~/.miniagent/AGENT.md"), "AGENT.md"]
INIT_PROMPT = "请在当前目录下初始化一个规范的 AGENT.md 文件，为这个项目设定清晰的开发规范与人设。"
PERSONA_FILE = os.environ.get("MINIAGENT_PERSONA_FILE", "agent_persona.json")
RELATIONSHIP_FILE = os.environ.get("MINIAGENT_RELATIONSHIP_FILE", "agent_relationship.json")
THOUGHT_STYLE = os.environ.get("MINIAGENT_THOUGHT_STYLE", "balanced").strip().lower() or "balanced"
SNAPSHOT_DIR = os.environ.get("POLARIS_SNAPSHOT_DIR", ".polaris_snapshots")
VERSION = "1.1.6"
CODENAME = "Memory Curve"
MONOLOGUE_MODE = os.environ.get("POLARIS_MONOLOGUE_MODE", "hybrid").strip().lower() or "hybrid"
MONOLOGUE_MODEL = os.environ.get("POLARIS_MONOLOGUE_MODEL", "").strip()
MONOLOGUE_MAX_TOKENS = int(os.environ.get("POLARIS_MONOLOGUE_MAX_TOKENS", "180"))
MONOLOGUE_USE_TAGS = _env_bool("POLARIS_MONOLOGUE_USE_TAGS", True)
_PY_TO_JSON = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}
def tool(fn: Callable | None = None, *, name: str | None = None, description: str | None = None,
         dangerous: bool = False, mutating: bool = False):
    def decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)
        hints = get_type_hints(func)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for pname, param in sig.parameters.items():
            json_type = _PY_TO_JSON.get(hints.get(pname, str), "string")
            prop: dict[str, Any] = {"type": json_type}
            if json_type == "array":
                prop["items"] = {"type": "string"}
            properties[pname] = prop
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        func.schema = {
            "name": name or func.__name__,
            "description": (description or (func.__doc__ or "").strip() or func.__name__),
            "input_schema": {"type": "object", "properties": properties, "required": required},
        }
        func.dangerous = dangerous
        func.mutating = mutating or dangerous
        return func
    return decorator(fn) if fn is not None else decorator
# ───────────────────────────────────────────────────────────────────────────────
# Vector memory + Ebbinghaus forgetting curve
# 记忆不再是「越新越靠前」的流水账：写进来的每条记忆都带一条遗忘曲线，
# 被想起来就变结实，长期没被想起就自己淡出上下文。检索走向量 + 关键词混合。
# ───────────────────────────────────────────────────────────────────────────────
def _now_dt() -> datetime.datetime:
    return datetime.datetime.now()
def _iso(dt: datetime.datetime) -> str:
    return dt.replace(microsecond=0).isoformat()
def _parse_dt(value: Any, fallback: datetime.datetime) -> datetime.datetime:
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        return datetime.datetime.fromisoformat(text)
    except ValueError:
        pass
    try:  # 只有日期的老格式（2026-09-07）按当天零点算。
        return datetime.datetime.combine(datetime.date.fromisoformat(text[:10]), datetime.time())
    except ValueError:
        return fallback
def _elapsed_days(since: datetime.datetime, now: datetime.datetime) -> float:
    return max(0.0, (now - since).total_seconds() / 86400.0)
class Embedder:
    """把文本变成单位向量。"""
    signature = "base"
    dim = 0
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError
class HashingEmbedder(Embedder):
    """零依赖、可离线、跨进程稳定的哈希词袋向量。

    用 blake2b 把 token 和字符 bigram 散列到固定维度（带符号哈希，抵消碰撞偏置），
    再做次线性词频加权和 L2 归一化。
    诚实地说：它捕捉的是字面重合，不是真正的近义——「狗」和「犬」在这里并不接近。
    真正的语义召回需要 POLARIS_EMBED_BACKEND=remote 接一个嵌入模型。"""
    def __init__(self, dim: int = EMBED_DIM):
        self.dim = max(32, int(dim))
        self.signature = f"hash-{self.dim}"
    def _feature_weights(self, text: str) -> Counter:
        feats: Counter = Counter()
        for token in _normalize_text(text):
            # _normalize_text 会把整段中文原样当成一个 token 丢出来。这种长串几乎
            # 不可能在两条文本间重合，留在向量里只会拉长模长、压低真实相似度，
            # 所以只保留字、二元组和短词；长整句的字面命中交给关键词通道去管。
            if len(token) > 3 and re.fullmatch(r"[一-鿿]+", token):
                continue
            feats[token] += 1
        for gram in _char_ngrams(text, 2):
            feats[f"#{gram}"] += 1
        return feats
    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for feature, count in self._feature_weights(text).items():
            digest = hashlib.blake2b(feature.encode("utf-8", "replace"), digest_size=8).digest()
            slot = int.from_bytes(digest[:4], "big") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[slot] += sign * (1.0 + math.log(count))  # 次线性词频，压住高频词
        return _l2_normalize(vec)
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(text) for text in texts]
class RemoteEmbedder(Embedder):
    """走 OpenAI-Compatible /embeddings 的真语义向量。"""
    def __init__(self, model: str = EMBED_MODEL):
        self.model = model or EMBED_MODEL
        self.signature = f"remote-{self.model}"
        self._backend: ChatBackend | None = None
    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._backend is None:
            self._backend = build_backend()
        rows = self._backend.create_embeddings(model=self.model, inputs=texts)
        if len(rows) != len(texts):
            raise RuntimeError(f"嵌入返回条数不匹配：要 {len(texts)} 条，回 {len(rows)} 条")
        vectors = [_l2_normalize(row) for row in rows]
        self.dim = len(vectors[0]) if vectors else 0
        return vectors
def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm <= 1e-12:
        return [0.0] * len(vec)
    return [x / norm for x in vec]
def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))  # 两边都已归一化，点积即余弦
class EmbeddingService:
    """选嵌入实现，并在远程不可用时安静地退回本地哈希向量。"""
    def __init__(self, mode: str = EMBED_BACKEND, model: str = EMBED_MODEL, dim: int = EMBED_DIM):
        self.mode = mode if mode in {"auto", "local", "remote"} else "auto"
        self.local = HashingEmbedder(dim)
        self.remote: RemoteEmbedder | None = None
        self._warned = False
        if self.mode == "remote" or (self.mode == "auto" and (API_KEY or BASE_URL)):
            self.remote = RemoteEmbedder(model)
        # 本地哈希向量是确定性的，重算比读盘还快，没必要为它留一个几 MB 的文件；
        # 远程向量要花钱，才值得落盘。
        self.persistent = self.remote is not None
    @property
    def signature(self) -> str:
        return self.remote.signature if self.remote is not None else self.local.signature
    def degrade(self, reason: str) -> None:
        self.remote = None
        self.persistent = False
        if not self._warned:
            self._warned = True
            print(f"[记忆] 嵌入模型不可用（{reason}），本次会话改用本地哈希向量。")
    def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self.remote is not None:
            try:
                return self.remote.embed(texts)
            except Exception as e:
                self.degrade(str(e)[:120])
        return self.local.embed(texts)
    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]
class VectorStore:
    """记忆向量的旁路存储。

    单独存一个文件，是为了让 agent_memory.json 保持人能读的样子——
    没人想在几百维浮点里找自己上周说过的偏好。
    换了嵌入模型（签名变化）就整体作废，下次检索时按需重算。
    只有远程嵌入才落盘：本地哈希向量重算一次不到一毫秒，存下来纯属浪费磁盘。"""
    def __init__(self, path: str = MEMORY_VECTOR_FILE, service: EmbeddingService | None = None):
        self.path = path
        self.service = service or EmbeddingService()
        self.signature = self.service.signature
        self.vectors: dict[int, list[float]] = {}
        self._dirty = False
        self._load()
    def _load(self) -> None:
        if not self.service.persistent or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if str(data.get("signature", "")) != self.signature:
                return  # 嵌入空间变了，旧向量不可比，丢掉重算
            raw = data.get("vectors", {})
            if not isinstance(raw, dict):
                return
            for key, vec in raw.items():
                try:
                    self.vectors[int(key)] = [float(x) for x in vec]
                except (TypeError, ValueError):
                    continue
        except Exception:
            print(f"[警告] 记忆向量文件 {self.path} 损坏，本次重新计算向量。")
    def save(self) -> None:
        if not self._dirty or not self.service.persistent:
            return
        with _STATE_LOCK:
            payload = {
                "signature": self.signature,
                # 归一化后的分量都在 ±1 内，5 位小数够用，能把文件砍掉一半还多。
                "vectors": {str(mid): [round(x, 5) for x in vec] for mid, vec in self.vectors.items()},
            }
            _atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False))
            self._dirty = False
    def _sync_signature(self) -> None:
        # 远程降级到本地是会话中途发生的，这时旧向量和新向量不在同一个空间里。
        if self.service.signature != self.signature:
            self.signature = self.service.signature
            self.vectors.clear()
            self._dirty = True
    def get(self, memory_id: int) -> list[float] | None:
        self._sync_signature()
        return self.vectors.get(int(memory_id))
    def put(self, memory_id: int, vector: list[float]) -> None:
        self._sync_signature()
        self.vectors[int(memory_id)] = vector
        self._dirty = True
    def remove(self, memory_id: int) -> None:
        if self.vectors.pop(int(memory_id), None) is not None:
            self._dirty = True
    def embed_text(self, text: str) -> list[float]:
        self._sync_signature()
        vector = self.service.embed(text)
        self._sync_signature()  # embed 过程里可能刚刚降级
        return vector
    def ensure(self, pairs: list[tuple[int, str]], cap: int = EMBED_BATCH_CAP) -> int:
        """给还没有向量的记忆补算向量，一次批量请求，上限 cap 条。"""
        self._sync_signature()
        missing = [(mid, text) for mid, text in pairs if mid not in self.vectors][:cap]
        if not missing:
            return 0
        vectors = self.service.embed_many([text for _, text in missing])
        self._sync_signature()
        for (mid, _), vector in zip(missing, vectors):
            self.vectors[int(mid)] = vector
        self._dirty = True
        self.save()
        return len(missing)
class ForgettingCurve:
    """艾宾浩斯遗忘曲线。

        R = exp(-t / S)

    R 是此刻还记得的概率，t 是距上次想起的天数，S 是记忆强度（天）。
    半衰期 = S · ln2，所以 S=1 的新记忆大约 17 小时就掉到一半。

    每次被成功召回都算一次复习，强度按「间隔效应」增长：
    刚想起过（R≈1）几乎不涨——临时抱佛脚没用；
    快要忘了才想起来（R 低）涨得最多——这正是间隔重复有效的原因。"""
    max_strength = 3650.0
    gain = 1.8
    min_step = 0.5
    min_review_gap_sec = 60.0
    # 不同类别的初始强度（天）：身份和偏好天生比一次性事实活得久。
    initial_strength = {"identity": 30.0, "preference": 14.0, "lesson": 5.0, "fact": 1.5}
    default_strength = 1.5
    @classmethod
    def initial(cls, category: str) -> float:
        return cls.initial_strength.get(str(category).strip().lower(), cls.default_strength)
    @staticmethod
    def retention(strength: float, elapsed_days: float) -> float:
        return math.exp(-max(0.0, elapsed_days) / max(1e-6, float(strength)))
    @staticmethod
    def half_life(strength: float) -> float:
        return max(0.0, float(strength)) * math.log(2)
    @classmethod
    def reinforce(cls, strength: float, retention: float, quality: float = 1.0,
                  deliberate: bool = False) -> float:
        quality = max(0.0, min(1.0, float(quality)))
        grown = float(strength) * (1.0 + cls.gain * quality * (1.0 - retention))
        # 记忆已经开始褪色时保证一个绝对下限的增量；
        # deliberate 是用户/模型明确说「这条要记牢」——那是主观加权，不是一次复习，
        # 不该被间隔效应清零，否则「强化」按下去毫无反应。
        if deliberate or retention < 0.9:
            grown = max(grown, float(strength) + cls.min_step * quality)
        return min(cls.max_strength, grown)
class Memory:
    def __init__(self, path: str = MEMORY_FILE, vector_path: str = MEMORY_VECTOR_FILE,
                 service: EmbeddingService | None = None):
        self.path = path
        self.items: list[dict] = []
        self._next_id = 1
        self.vectors = VectorStore(vector_path, service)
        self._load()
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with _STATE_LOCK:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                raw_items = data.get("memories", [])
                if not isinstance(raw_items, list):
                    raise ValueError("invalid memory format")
                now = _now_dt()
                cleaned: list[dict] = []
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    try:
                        mid = int(item["id"])
                        time = str(item.get("time", datetime.date.today().isoformat()))
                        category = str(item.get("category", "fact"))
                        content = str(item.get("content", "")).strip()
                    except Exception:
                        continue
                    if content:
                        cleaned.append(self._with_curve(
                            {"id": mid, "time": time, "category": category, "content": content}, item, now))
                self.items = cleaned
                self._next_id = max((m["id"] for m in self.items), default=0) + 1
            self.sweep()
        except Exception:
            print(f"[警告] 记忆文件 {self.path} 损坏，本次从空记忆开始。")
    @staticmethod
    def _with_curve(item: dict, raw: dict, now: datetime.datetime) -> dict:
        """补齐遗忘曲线字段，并兼容 1.1.6 之前不带曲线的旧记忆文件。

        老记忆没有复习记录。要是直接拿写入日期当上次复习时间，
        升级当天所有陈年记忆就会一起跌破阈值集体休眠——用户会以为记忆被清空了。
        所以把这次升级本身当作一次复习：内容一条不少，曲线从今天开始走。"""
        created = _parse_dt(raw.get("created"), _parse_dt(item["time"], now))
        legacy = "strength" not in raw and "last_review" not in raw
        try:
            strength = float(raw.get("strength", ForgettingCurve.initial(item["category"])))
        except (TypeError, ValueError):
            strength = ForgettingCurve.initial(item["category"])
        try:
            reviews = max(0, int(raw.get("reviews", 0)))
        except (TypeError, ValueError):
            reviews = 0
        item.update({
            "created": _iso(created),
            "last_review": _iso(now if legacy else _parse_dt(raw.get("last_review"), created)),
            "reviews": reviews,
            "strength": max(0.01, strength),
            "pinned": bool(raw.get("pinned", False)),
            "dormant": bool(raw.get("dormant", False)),
        })
        return item
    def _save(self) -> None:
        with _STATE_LOCK:
            _atomic_write_text(self.path, json.dumps({"memories": self.items}, ensure_ascii=False, indent=2))
        self.vectors.save()
    def persist(self) -> None:
        self._save()
    @staticmethod
    def _embed_text(item: dict) -> str:
        return f"{item.get('category', '')} {item.get('content', '')}".strip()
    def get(self, memory_id: int) -> dict | None:
        return next((m for m in self.items if m["id"] == int(memory_id)), None)
    def retention(self, item: dict, now: datetime.datetime | None = None) -> float:
        now = now or _now_dt()
        elapsed = _elapsed_days(_parse_dt(item.get("last_review"), _parse_dt(item.get("time"), now)), now)
        return ForgettingCurve.retention(item.get("strength", ForgettingCurve.default_strength), elapsed)
    def add(self, content: str, category: str = "fact") -> int:
        with _STATE_LOCK:
            now = _now_dt()
            category = str(category).strip() or "fact"
            item = {
                "id": self._next_id,
                "time": now.date().isoformat(),
                "category": category,
                "content": str(content).strip(),
                "created": _iso(now),
                "last_review": _iso(now),
                "reviews": 0,
                "strength": ForgettingCurve.initial(category),
                "pinned": False,
                "dormant": False,
            }
            self.items.append(item)
            self._next_id += 1
            try:
                self.vectors.put(item["id"], self.vectors.embed_text(self._embed_text(item)))
            except Exception as e:
                # 向量算不出来不该拖垮写记忆，检索时还能按关键词兜底。
                print(f"[记忆] 向量生成失败（{str(e)[:80]}），该条暂时只能按关键词检索。")
            self._save()
            return item["id"]
    def remove(self, memory_id: int) -> bool:
        with _STATE_LOCK:
            before = len(self.items)
            self.items = [m for m in self.items if m["id"] != memory_id]
            if len(self.items) < before:
                self.vectors.remove(memory_id)
                self._save()
                return True
            return False
    def reinforce(self, memory_id: int, quality: float = 1.0, *, force: bool = False) -> bool:
        """一次成功的召回就是一次复习。返回是否真的改动了曲线。"""
        with _STATE_LOCK:
            item = self.get(memory_id)
            if item is None:
                return False
            now = _now_dt()
            last = _parse_dt(item.get("last_review"), _parse_dt(item.get("time"), now))
            if not force and (now - last).total_seconds() < ForgettingCurve.min_review_gap_sec:
                return False  # 同一回合里反复读到同一条，不算重复复习
            item["strength"] = ForgettingCurve.reinforce(
                item.get("strength", ForgettingCurve.default_strength), self.retention(item, now),
                quality, deliberate=force)
            item["last_review"] = _iso(now)
            item["reviews"] = int(item.get("reviews", 0)) + 1
            item["dormant"] = False
            return True
    def pin(self, memory_id: int, pinned: bool = True) -> bool:
        with _STATE_LOCK:
            item = self.get(memory_id)
            if item is None:
                return False
            item["pinned"] = bool(pinned)
            if pinned:
                item["dormant"] = False
            self._save()
            return True
    def revive(self, memory_id: int) -> bool:
        """把休眠的记忆重新唤醒，并给它一次结实的复习。"""
        with _STATE_LOCK:
            item = self.get(memory_id)
            if item is None:
                return False
            item["dormant"] = False
            item["strength"] = max(item.get("strength", 0.0), ForgettingCurve.initial(item.get("category", "fact")))
            self.reinforce(memory_id, quality=1.0, force=True)
            self._save()
            return True
    def sweep(self, now: datetime.datetime | None = None) -> int:
        """把彻底淡出的记忆转入休眠——只是不再主动想起，绝不删除。"""
        now = now or _now_dt()
        changed = 0
        with _STATE_LOCK:
            for item in self.items:
                if item.get("pinned") or item.get("dormant"):
                    continue
                age = _elapsed_days(_parse_dt(item.get("created"), now), now)
                if age < MEMORY_DORMANT_MIN_DAYS:
                    continue
                if self.retention(item, now) < MEMORY_FORGET_THRESHOLD:
                    item["dormant"] = True
                    changed += 1
            if changed:
                self._save()
        return changed
    def _ensure_vectors(self, items: list[dict]) -> None:
        try:
            # 新的排前面：额度不够时优先保证最近的记忆可被语义检索。
            ordered = sorted(items, key=lambda m: m["id"], reverse=True)
            self.vectors.ensure([(m["id"], self._embed_text(m)) for m in ordered])
        except Exception:
            pass  # 纯降级路径：没有向量就退回关键词检索
    def score(self, query: str, item: dict, query_vector: list[float] | None,
              now: datetime.datetime | None = None) -> dict:
        """混合打分：语义相似度 + 关键词重合，再按此刻的记忆保持率加权。"""
        now = now or _now_dt()
        text = f"{item.get('category', '')} {item.get('content', '')}"
        raw_keyword = _score_overlap(query, text)
        if query.strip().lower() in text.lower():
            raw_keyword += 8
        keyword = raw_keyword / (raw_keyword + 12.0)  # 压到 0~1，避免长文本刷分
        semantic = 0.0
        if query_vector:
            stored = self.vectors.get(item["id"])
            if stored:
                semantic = max(0.0, _cosine(query_vector, stored))
        base = 0.62 * semantic + 0.38 * keyword if query_vector else keyword
        retention = self.retention(item, now)
        # 保持率不清零分数，只压权重：想不太起来 ≠ 完全检索不到。
        return {
            "item": item, "semantic": semantic, "keyword": keyword,
            "retention": retention, "raw_keyword": raw_keyword,
            "score": base * (0.35 + 0.65 * retention),
        }
    # 休眠记忆被强线索唤起的门槛。淡出只是不再「主动想起」，
    # 一个足够精确的线索仍然应该能把它拽回来——人也是这样。
    # 门槛必须定得高：_score_overlap 对中文给分很慷慨（两个字就能拿 23 分），
    # 用原始分做门槛等于任何沾边的查询都能唤醒休眠记忆，遗忘就白做了。
    # 所以用归一化后的精确度分数——大意是「你得相当具体地叫出它」。
    cue_keyword = 0.75
    cue_semantic = 0.45
    def rank(self, keyword: str, limit: int = 20, *, semantic: bool = True,
             include_dormant: bool = False) -> list[dict]:
        query = keyword.strip()
        pool = self.items if include_dormant else [m for m in self.items if not m.get("dormant")]
        cue_pool = [] if include_dormant else [m for m in self.items if m.get("dormant")]
        if not query or not (pool or cue_pool):
            return []
        query_vector = None
        if semantic:
            self._ensure_vectors(pool + cue_pool)
            try:
                query_vector = self.vectors.embed_text(query)
            except Exception:
                query_vector = None
        now = _now_dt()
        scored = [self.score(query, m, query_vector, now) for m in pool]
        # 关键词沾边的一律保留；纯向量命中要够像才算，挡住哈希碰撞和嵌入模型的相似度地板。
        # 门槛取相对值：不同嵌入模型的余弦分布差很远，固定阈值不是过松就是过紧。
        best = max((row["semantic"] for row in scored), default=0.0)
        gate = max(0.15, 0.55 * best)
        hits = [row for row in scored if row["raw_keyword"] > 0 or row["semantic"] >= gate]
        for row in (self.score(query, m, query_vector, now) for m in cue_pool):
            if row["keyword"] >= self.cue_keyword or row["semantic"] >= max(self.cue_semantic, gate):
                row["cued"] = True
                hits.append(row)
        hits.sort(key=lambda row: (row["score"], row["item"]["id"]), reverse=True)
        return hits[:limit]
    def search(self, keyword: str = "", limit: int = 50, *, semantic: bool = True,
               include_dormant: bool = False, reinforce: bool = True,
               quality: float = 1.0) -> list[dict]:
        query = keyword.strip()
        if not query:
            pool = [m for m in self.items if include_dormant or not m.get("dormant")]
            return list(reversed(pool[-limit:]))
        hits = self.rank(query, limit=limit, semantic=semantic, include_dormant=include_dormant)
        if reinforce and hits:
            # 只强化真正被端上来的前几条，一次翻 50 条不该让整个库都变结实。
            # 注意这里不能用 any(生成器)：any 会短路，第一条复习成功后面几条就被跳过了。
            touched = [self.reinforce(row["item"]["id"], quality) for row in hits[:8]]
            if any(touched):
                self._save()
        return [row["item"] for row in hits]
    def describe(self, item: dict, now: datetime.datetime | None = None) -> str:
        retention = self.retention(item, now or _now_dt())
        flags = "".join(["📌" if item.get("pinned") else "", "💤" if item.get("dormant") else ""])
        strength = float(item.get("strength", ForgettingCurve.default_strength))
        return (f"#{item['id']} [{item['category']}] {item['time']} "
                f"R={retention:.2f} 半衰期={ForgettingCurve.half_life(strength):.1f}天 "
                f"×{item.get('reviews', 0)}{flags}: {item['content']}")
    def render(self, limit: int = 30, *, show_curve: bool = False,
               include_dormant: bool = False) -> str:
        pool = [m for m in self.items if include_dormant or not m.get("dormant")]
        if not pool:
            return ""
        now = _now_dt()
        recent = list(reversed(pool[-limit:]))
        if show_curve:
            return "\n".join(self.describe(m, now) for m in recent)
        return "\n".join(f"#{m['id']} [{m['category']}] {m['time']}: {m['content']}" for m in recent)
    def brief_context(self, query: str = "", limit: int = 3) -> str:
        hits = self.search(query, limit=limit)
        if not hits:
            return ""
        return "; ".join(f"#{m['id']} {m['content'][:90]}" for m in hits)
    def curve_stats(self, now: datetime.datetime | None = None) -> dict:
        now = now or _now_dt()
        active = [m for m in self.items if not m.get("dormant")]
        retentions = [self.retention(m, now) for m in active]
        return {
            "total": len(self.items),
            "active": len(active),
            "dormant": sum(1 for m in self.items if m.get("dormant")),
            "pinned": sum(1 for m in self.items if m.get("pinned")),
            "vivid": sum(1 for r in retentions if r >= 0.7),
            "fading": sum(1 for r in retentions if r < 0.3),
            "avg_retention": sum(retentions) / len(retentions) if retentions else 0.0,
            "embedder": self.vectors.service.signature,
            "vectors": len(self.vectors.vectors),
        }
    def curve_compact(self) -> str:
        s = self.curve_stats()
        return (f"{s['active']} 条在线 / {s['dormant']} 条休眠 | 平均保持率 {s['avg_retention']:.0%} | "
                f"清晰 {s['vivid']} · 正在淡忘 {s['fading']} | 向量 {s['embedder']}")
    def curve_report(self, limit: int = 8) -> str:
        now = _now_dt()
        self.sweep(now)
        s = self.curve_stats(now)
        lines = [
            "【记忆遗忘曲线 R = exp(-t/S)】",
            f"总计 {s['total']} 条：在线 {s['active']} · 休眠 {s['dormant']} · 常驻 {s['pinned']}",
            f"平均保持率 {s['avg_retention']:.0%}（清晰 {s['vivid']} 条，正在淡忘 {s['fading']} 条）",
            f"嵌入后端 {s['embedder']}，已建向量 {s['vectors']} 条",
        ]
        active = [m for m in self.items if not m.get("dormant")]
        if active:
            ranked = sorted(active, key=lambda m: self.retention(m, now))
            fading = [m for m in ranked if self.retention(m, now) < 0.6]
            lines.append("\n最需要复习（保持率 < 60%）：")
            lines.extend("  " + self.describe(m, now) for m in fading[:limit])
            if not fading:
                lines.append("  (暂时没有正在淡忘的记忆)")
            lines.append("\n记得最牢：")
            lines.extend("  " + self.describe(m, now) for m in ranked[::-1][:3])
        dormant = [m for m in self.items if m.get("dormant")]
        if dormant:
            lines.append("\n已休眠（不再主动想起，/revive 编号 可唤醒）：")
            lines.extend("  " + self.describe(m, now) for m in dormant[:limit])
        return "\n".join(lines)
MEMORY = Memory()
@tool
def remember(content: str, category: str = "fact") -> str:
    """把一条需要跨会话长期保留的事实、偏好或教训写入长期记忆。
    只用于确实值得下次还记得的信息（用户偏好、项目约定、踩过的坑），不要用它记录当前对话里的临时内容。
    category 决定这条记忆的初始遗忘速度，请认真选：
    identity（关于用户是谁，半衰期约 21 天）/ preference（长期偏好，约 10 天）/
    lesson（踩过的坑，约 3.5 天）/ fact（一般事实，约 1 天）。
    记忆会按艾宾浩斯曲线衰减，但每次被召回都会变得更牢固——重要的东西自然会留下来。"""
    mid = MEMORY.add(content, category)
    return f"已写入长期记忆 #{mid}。"
@tool
def recall_memories(keyword: str = "", limit: int = 50) -> str:
    """检索长期记忆库：向量语义相似度 + 关键词重合混合排序，并按遗忘曲线加权。
    keyword 留空则返回最近写入的记忆。用于回答「我之前说过什么」「之前定的规矩是什么」。
    检索本身就是一次复习——被召回的记忆会自动变得更牢固、更晚淡出。
    注意：只搜跨会话沉淀下来的记忆条目，不搜对话原文——要搜对话原文请用 search_chat_history。"""
    hits = MEMORY.search(keyword, limit=limit)
    if not hits:
        return "(没有匹配的记忆)"
    return "\n".join(f"#{m['id']} [{m['category']}] {m['time']}: {m['content']}" for m in hits)
@tool
def forget_memory(memory_id: int) -> str:
    """按编号删除一条长期记忆，编号从 recall_memories 的输出里取。
    仅在确认某条记忆已经过时或本身就是错的时候使用。
    只是「暂时想不起来」不需要删——长期没被想起的记忆会自己进入休眠。"""
    return "已删除该记忆。" if MEMORY.remove(memory_id) else f"未找到记忆 #{memory_id}。"
@tool
def memory_status() -> str:
    """查看长期记忆的遗忘曲线状态：哪些记得清楚、哪些正在淡忘、哪些已经休眠。
    想知道自己是不是快忘了某件事、或者要不要主动复习一遍的时候用。"""
    return MEMORY.curve_report()
@tool
def reinforce_memory(memory_id: int, pin: bool = False) -> str:
    """强化一条长期记忆，让它衰减得更慢——相当于主动复习一遍。
    用户重申某件事、或者某条记忆再次被证明重要时使用；pin=true 则设为常驻，永不休眠。
    编号从 recall_memories 或 memory_status 的输出里取。"""
    item = MEMORY.get(memory_id)
    if item is None:
        return f"未找到记忆 #{memory_id}。"
    MEMORY.reinforce(memory_id, quality=1.0, force=True)
    if pin:
        MEMORY.pin(memory_id, True)
    else:
        MEMORY.persist()
    return "已强化：" + MEMORY.describe(MEMORY.get(memory_id))
class TodoList:
    def __init__(self, path: str = TODO_FILE):
        self.path = path
        self.items: list[dict] = []
        self._load()
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                items = data.get("items", [])
                if isinstance(items, list):
                    self.items = [item for item in items if isinstance(item, dict)]
        except Exception:
            # Keep running even if the todo store is damaged.
            self.items = []
    def _save(self) -> None:
        payload = {"items": self.items, "updated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        _atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
    def persist(self) -> None:
        self._save()
    def set(self, todos: list[str]) -> None:
        self.items = [{"text": str(t).strip(), "done": False} for t in todos if str(t).strip()]
        self._save()
    def complete(self, index: int) -> bool:
        if 1 <= index <= len(self.items):
            self.items[index - 1]["done"] = True
            self._save()
            return True
        return False
    def clear(self) -> None:
        self.items = []
        self._save()
    def render(self) -> str:
        if not self.items:
            return ""
        return "\n".join(f"{i}. [{'x' if it['done'] else ' '}] {it['text']}" for i, it in enumerate(self.items, 1))
TODOS = TodoList()
@tool
def set_todos(todos: list[str]) -> str:
    """用一组新任务整体覆盖当前待办清单，适合在开始多步任务前先把计划列出来。
    注意是整体替换而不是追加，调用时要把还没做完的旧任务一起带上。"""
    TODOS.set(todos)
    return "Task list updated:\n" + (TODOS.render() or "(empty)")
@tool
def complete_todo(index: int) -> str:
    """把待办清单第 index 项标记为已完成，序号从 1 开始，对应 set_todos 传入的顺序。"""
    if TODOS.complete(index):
        return "Marked complete. Current task list:\n" + TODOS.render()
    return f"There is no item #{index} in the list."
class MoodState:
    def __init__(self, path: str = MOOD_FILE):
        self.path = path
        self.state = {
            "confidence": 52,
            "focus": 55,
            "fatigue": 18,
            "curiosity": 48,
            "frustration": 10,
            "stability": 50,
            "turns": 0,
            "last_touch": datetime.date.today().isoformat(),
            "diary": [],
        }
        self._load()
        self._apply_daily_decay()
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with _STATE_LOCK:
                with open(self.path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key in self.state:
                        if key in data:
                            self.state[key] = data[key]
        except Exception:
            self._append_diary("warning", "心情文件损坏，已从默认状态恢复。", {"kind": "system"})
    def _save(self) -> None:
        with _STATE_LOCK:
            _atomic_write_text(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))
    def _clamp(self, value: int) -> int:
        return max(0, min(100, int(value)))
    def _soft_adjust(self, current: int, delta: int) -> int:
        if delta == 0:
            return current
        current = self._clamp(current)
        if delta > 0:
            distance = max(1, 100 - current)
        else:
            distance = max(1, current)
        damping = 0.35 + min(0.65, distance / 60.0)
        adjusted = int(round(delta * damping))
        if adjusted == 0:
            adjusted = 1 if delta > 0 else -1
        return self._clamp(current + adjusted)
    def _touch(self) -> None:
        with _STATE_LOCK:
            self.state["last_touch"] = datetime.date.today().isoformat()
            self._save()
    def _apply_daily_decay(self) -> None:
        try:
            with _STATE_LOCK:
                last = datetime.date.fromisoformat(str(self.state.get("last_touch", datetime.date.today().isoformat())))
                today = datetime.date.today()
                days = max(0, (today - last).days)
                if days <= 0:
                    return
                fatigue_drop = max(1, 2 * days)
                frustration_drop = max(1, 1 * days)
                recovery_gain = max(1, 1 * days)
                self.state["fatigue"] = self._clamp(self.state["fatigue"] - fatigue_drop)
                self.state["frustration"] = self._clamp(self.state["frustration"] - frustration_drop)
                self.state["focus"] = self._clamp(self.state["focus"] + recovery_gain)
                self.state["confidence"] = self._clamp(self.state["confidence"] + recovery_gain)
                self.state["stability"] = self._clamp(self.state["stability"] + recovery_gain)
                self._touch()
        except Exception:
            return
    def rebalance(self, strength: float = 0.06) -> None:
        """Gently pull mood values back toward a healthy baseline."""
        baselines = {
            "confidence": 55,
            "focus": 56,
            "fatigue": 18,
            "curiosity": 50,
            "frustration": 12,
            "stability": 55,
        }
        with _STATE_LOCK:
            for key, baseline in baselines.items():
                current = self._clamp(int(self.state.get(key, baseline)))
                drift = baseline - current
                if drift == 0:
                    continue
                step = int(round(drift * strength))
                if step == 0:
                    step = 1 if drift > 0 else -1
                self.state[key] = self._clamp(current + step)
    def _append_diary(self, kind: str, note: str, meta: dict | None = None) -> None:
        with _STATE_LOCK:
            entry = {
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "kind": kind,
                "note": note.strip(),
            }
            if meta:
                entry["meta"] = meta
            diary = self.state.setdefault("diary", [])
            diary.append(entry)
            if len(diary) > 80:
                del diary[:-80]
            self._save()
    def _shift(self, confidence: int = 0, focus: int = 0, fatigue: int = 0, curiosity: int = 0, frustration: int = 0, stability: int = 0) -> None:
        with _STATE_LOCK:
            self.state["confidence"] = self._soft_adjust(self.state["confidence"], confidence)
            self.state["focus"] = self._soft_adjust(self.state["focus"], focus)
            self.state["fatigue"] = self._soft_adjust(self.state["fatigue"], fatigue)
            self.state["curiosity"] = self._soft_adjust(self.state["curiosity"], curiosity)
            self.state["frustration"] = self._soft_adjust(self.state["frustration"], frustration)
            self.state["stability"] = self._soft_adjust(self.state["stability"], stability)
    def begin_turn(self, user_text: str) -> None:
        self._apply_daily_decay()
        with _STATE_LOCK:
            self.state["turns"] = int(self.state.get("turns", 0)) + 1
        length_pressure = min(4, max(0, len(user_text) // 220))
        question_bonus = 1 if "?" in user_text or "？" in user_text else 0
        self._shift(fatigue=length_pressure, curiosity=question_bonus, stability=-1 if length_pressure >= 3 else 0)
        self._append_diary("turn", "开始处理新输入。", {"user_len": len(user_text)})
        self.rebalance(0.05)
        self._touch()
    def record_tool(self, name: str, success: bool, mutating: bool = False, dangerous: bool = False, output: str = "") -> None:
        output_len = len(output or "")
        if success:
            self._shift(confidence=2, focus=1, curiosity=1, fatigue=1 if mutating else 0, stability=1)
            note = f"工具 {name} 执行成功。"
            if dangerous:
                self._shift(fatigue=1)
        else:
            self._shift(confidence=-4, focus=-2, fatigue=2, frustration=6, stability=-2)
            note = f"工具 {name} 执行失败。"
        if output_len > 2500:
            self._shift(fatigue=2)
        elif output_len > 800:
            self._shift(fatigue=1)
        self._append_diary("tool", note, {"success": success, "mutating": mutating, "dangerous": dangerous, "output_len": output_len})
        self.rebalance(0.03 if success else 0.01)
    def record_reflection(self, passed: bool, lesson: str = "") -> None:
        if passed:
            self._shift(confidence=1, frustration=-1, stability=1)
            note = "自检通过。"
        else:
            self._shift(confidence=-1, frustration=3, fatigue=1, stability=-1)
            note = "自检未通过。"
        self._append_diary("reflection", note, {"passed": passed, "lesson": lesson[:120]})
        if lesson.strip():
            self._shift(curiosity=1)
        self.rebalance(0.02)
    def record_response(self, final_text: str, used_tools: bool) -> None:
        text_len = len(final_text or "")
        if used_tools:
            self._shift(focus=1)
        if text_len > 1800:
            self._shift(fatigue=2)
        elif text_len > 800:
            self._shift(fatigue=1)
        if "不知道" in final_text or "不确定" in final_text:
            self._shift(confidence=-1)
        self._append_diary("response", "完成一次回复。", {"used_tools": used_tools, "text_len": text_len})
        self.rebalance(0.04)
        self._touch()
    def mood_score(self) -> int:
        positive = self.state["confidence"] + self.state["focus"] + self.state["curiosity"] + self.state["stability"]
        negative = self.state["fatigue"] + self.state["frustration"]
        return positive - negative
    def label(self) -> str:
        score = self.mood_score()
        if score >= 150:
            return "超清醒"
        if score >= 90:
            return "状态不错"
        if score >= 40:
            return "平稳工作"
        if score >= 0:
            return "有点疲惫"
        return "明显吃力"
    def needs_delegate(self) -> bool:
        return self.state["fatigue"] >= 75 or self.state["frustration"] >= 70 or self.state["confidence"] <= 30
    def turn_budget(self) -> int:
        """返回本回合的步数预算。

        注意这里的方向：frustration 和低 confidence 都是「事情不顺」的信号，
        此时 Agent 恰恰需要更多步数去排查和自愈，而不是更少。
        旧实现在失败时反过来砍预算（最低到 4 步），会把调试场景推进
        「越失败 → 步数越少 → 越修不好 → 更失败」的死循环，
        正好在最需要多试几步的时候把手脚捆住。

        现在只有 fatigue（对应上下文越堆越长、输出越来越啰嗦）才小幅收缩，
        而且保底 12 步；失败信号则适度加预算。真正的硬上限由 Agent.max_steps 控制。
        """
        budget = 18
        if self.state["fatigue"] >= 80:
            budget -= 4
        elif self.state["fatigue"] >= 60:
            budget -= 2
        if self.state["frustration"] >= 60:
            budget += 4
        if self.state["confidence"] <= 35:
            budget += 2
        return max(12, min(20, budget))
    def advice(self) -> str:
        tips = []
        if self.state["fatigue"] >= 75:
            tips.append("建议先总结或压缩上下文。")
        if self.state["frustration"] >= 70:
            tips.append("建议先做一次验证或回滚。")
        if self.state["confidence"] <= 35:
            tips.append("建议多用子代理或外部校验。")
        if not tips:
            tips.append("状态正常，可以继续推进任务。")
        return " ".join(tips)
    def policy_note(self) -> str:
        if self.needs_delegate():
            return "当前状态偏吃力，优先拆分任务、调用子代理、先复核再改写。"
        return "当前状态正常，可以按常规流程继续工作。"
    def snapshot(self) -> dict:
        return {
            "label": self.label(),
            **{k: self.state[k] for k in ["confidence", "focus", "fatigue", "curiosity", "frustration", "stability", "turns"]},
            "advice": self.advice(),
            "last_touch": self.state.get("last_touch"),
        }
    def compact(self) -> str:
        s = self.snapshot()
        return (
            f"{s['label']} | 信心 {s['confidence']} / 专注 {s['focus']} / 疲劳 {s['fatigue']} / "
            f"好奇 {s['curiosity']} / 挫败 {s['frustration']} / 稳定 {s['stability']} | {s['advice']}"
        )
    def render(self, limit: int = 6) -> str:
        s = self.snapshot()
        diary = self.state.get("diary", [])[-limit:]
        lines = [
            f"状态：{s['label']}",
            f"信心：{s['confidence']}  专注：{s['focus']}  疲劳：{s['fatigue']}  好奇：{s['curiosity']}  挫败：{s['frustration']}  稳定：{s['stability']}",
            f"建议：{s['advice']}",
        ]
        if diary:
            lines.append("最近记录：")
            lines.extend(f"- {e['time']} [{e['kind']}] {e['note']}" for e in diary)
        return "\n".join(lines)
    def diary(self, limit: int = 10) -> str:
        diary = self.state.get("diary", [])[-limit:]
        if not diary:
            return "(暂无记录)"
        return "\n".join(
            f"{e['time']} [{e['kind']}] {e['note']}"
            + (f" | {json.dumps(e.get('meta', {}), ensure_ascii=False)}" if e.get("meta") else "")
            for e in diary
        )
MOOD = MoodState()
class PersonaProfile:
    def __init__(self, path: str = PERSONA_FILE):
        self.path = path
        self.state = {
            "name": "Polaris",
            "values": ["真诚", "谨慎", "好奇", "尊重事实"],
            "habits": ["先观察再行动", "修改前先确认影响范围", "喜欢一步一步分析"],
            'speech': {"emoji": "medium", "warmth": 0.72, "humor": 0.35, "style": "natural"},
            "growth": {"enabled": True},
        }
        self._load()
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state.update(data)
        except Exception:
            pass
    def _save(self) -> None:
        try:
            _atomic_write_text(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))
        except Exception:
            pass
    def compact(self) -> str:
        values = " / ".join(self.state.get("values", [])[:4]) or "暂无"
        habits = " / ".join(self.state.get("habits", [])[:3]) or "暂无"
        speech = self.state.get("speech", {}) if isinstance(self.state.get("speech", {}), dict) else {}
        style = speech.get("style", "natural")
        warmth = speech.get("warmth", 0.7)
        humor = speech.get("humor", 0.35)
        return f"{self.state.get('name', 'Polaris')} | 价值观: {values} | 习惯: {habits} | 风格: {style} / 温度 {warmth:.2f} / 幽默 {humor:.2f}"
    def render(self) -> str:
        speech = self.state.get("speech", {}) if isinstance(self.state.get("speech", {}), dict) else {}
        lines = [
            f"人格：{self.state.get('name', 'Polaris')}",
            f"价值观：{', '.join(self.state.get('values', []))}",
            f"习惯：{', '.join(self.state.get('habits', []))}",
            f"风格：{speech.get('style', 'natural')} | 温度：{speech.get('warmth', 0.7)} | 幽默：{speech.get('humor', 0.35)}",
        ]
        return "\n".join(lines)
class RelationshipState:
    def __init__(self, path: str = RELATIONSHIP_FILE):
        self.path = path
        self.state = {
            "trust": 52,
            "familiarity": 10,
            "warmth": 55,
            "humor": 35,
            "turns": 0,
            "last_touch": datetime.date.today().isoformat(),
        }
        self._load()
    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self.state.update(data)
        except Exception:
            pass
    def _save(self) -> None:
        try:
            _atomic_write_text(self.path, json.dumps(self.state, ensure_ascii=False, indent=2))
        except Exception:
            pass
    def _clamp(self, value: int) -> int:
        return max(0, min(100, int(value)))
    def observe_turn(self, user_input: str, final_text: str, used_tools: bool = False) -> None:
        self.state["turns"] = int(self.state.get("turns", 0)) + 1
        text = (user_input + " " + final_text).lower()
        trust_delta = 1
        familiarity_delta = 1
        warmth_delta = 0
        humor_delta = 0
        if any(k in text for k in ["谢谢", "厉害", "好耶", "赞", "不错"]):
            trust_delta += 2
            warmth_delta += 1
        if any(k in text for k in ["bug", "报错", "修复", "代码", "函数", "调用链", "api", "workspace", "persona"]):
            familiarity_delta += 1
        if used_tools:
            trust_delta += 1
        if any(k in text for k in ["😄", "😋", "😤", "哈哈", "233", "嘿嘿"]):
            humor_delta += 1
            warmth_delta += 1
        self.state["trust"] = self._clamp(self.state.get("trust", 50) + trust_delta)
        self.state["familiarity"] = self._clamp(self.state.get("familiarity", 0) + familiarity_delta)
        self.state["warmth"] = self._clamp(self.state.get("warmth", 50) + warmth_delta)
        self.state["humor"] = self._clamp(self.state.get("humor", 35) + humor_delta)
        self.state["last_touch"] = datetime.date.today().isoformat()
        self._save()
    def compact(self) -> str:
        return f"信任 {self.state.get('trust', 50)} / 熟悉 {self.state.get('familiarity', 0)} / 温度 {self.state.get('warmth', 50)} / 幽默 {self.state.get('humor', 35)}"
    def render(self) -> str:
        return "\n".join([
            f"关系：{self.compact()}",
            f"轮次：{self.state.get('turns', 0)}",
            f"最后互动：{self.state.get('last_touch', '')}",
        ])
class TraceStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"
class TraceLevel(str, Enum):
    TRACE = "trace"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
@dataclass
class TraceEvent:
    message: str
    level: TraceLevel = TraceLevel.INFO
    detail: str = ""
    timestamp: float = field(default_factory=time)
    metadata: dict[str, Any] = field(default_factory=dict)
@dataclass
class TraceStep:
    title: str
    detail: str = ""
    status: TraceStatus = TraceStatus.PENDING
    start: float = field(default_factory=time)
    end: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list["TraceStep"] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    def finish(self, detail: str | None = None) -> None:
        self.status = TraceStatus.SUCCESS
        self.end = time()
        if detail:
            self.detail = detail
    def fail(self, detail: str | None = None) -> None:
        self.status = TraceStatus.ERROR
        self.end = time()
        if detail:
            self.detail = detail
    def add_event(self, message: str, level: TraceLevel = TraceLevel.INFO, detail: str = "", **metadata: Any) -> TraceEvent:
        event = TraceEvent(message=message, level=level, detail=detail, metadata=dict(metadata))
        self.events.append(event)
        return event
    @property
    def duration(self) -> float:
        end = self.end if self.end is not None else time()
        return max(0.0, end - self.start)
class TraceEngine:
    """Lightweight runtime trace bus for summary timelines and detail panes."""
    def __init__(self) -> None:
        self._roots: list[TraceStep] = []
        self._events: list[TraceEvent] = []
        self._listeners: list[Callable[[TraceStep | TraceEvent, str], None]] = []
        self._lock = threading.RLock()
        # 每个线程独立维护自己的调用栈：delegate_tasks 会并行跑多个子代理，
        # 共用一个 _stack 会让它们互相把对方的步骤 pop 掉，树结构直接错乱。
        self._local = threading.local()
    @property
    def _stack(self) -> list[TraceStep]:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = []
            self._local.stack = stack
        return stack
    def clear(self) -> None:
        with self._lock:
            self._roots.clear()
            self._events.clear()
        self._local.stack = []
    def subscribe(self, listener: Callable[[TraceStep | TraceEvent, str], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)
    def _notify(self, payload: TraceStep | TraceEvent, kind: str) -> None:
        for listener in list(self._listeners):
            try:
                listener(payload, kind)
            except Exception:
                pass
    def begin(self, title: str, detail: str = "", **metadata: Any) -> TraceStep:
        step = TraceStep(title=str(title), detail=str(detail), status=TraceStatus.RUNNING, metadata=dict(metadata))
        stack = self._stack
        with self._lock:
            if stack:
                stack[-1].children.append(step)
            else:
                self._roots.append(step)
        stack.append(step)
        self._notify(step, "begin")
        return step
    def finish(self, detail: str | None = None) -> TraceStep | None:
        if not self._stack:
            return None
        step = self._stack.pop()
        step.finish(detail)
        self._notify(step, "finish")
        return step
    def fail(self, detail: str | None = None) -> TraceStep | None:
        if not self._stack:
            return None
        step = self._stack.pop()
        step.fail(detail)
        self._notify(step, "fail")
        return step
    @contextmanager
    def step(self, title: str, detail: str = "", **metadata: Any):
        self.begin(title, detail, **metadata)
        try:
            yield self.current_step()
        except Exception as exc:
            self.fail(str(exc))
            raise
        else:
            self.finish()
    def current_step(self) -> TraceStep | None:
        return self._stack[-1] if self._stack else None
    def event(self, message: str, level: TraceLevel = TraceLevel.INFO, detail: str = "", **metadata: Any) -> TraceEvent:
        event = TraceEvent(message=str(message), level=level, detail=str(detail), metadata=dict(metadata))
        stack = self._stack
        with self._lock:
            self._events.append(event)
            if stack:
                stack[-1].add_event(event.message, event.level, event.detail, **event.metadata)
        self._notify(event, "event")
        return event
    def _flat_steps(self, steps: list[TraceStep] | None = None) -> list[TraceStep]:
        steps = self._roots if steps is None else steps
        flat: list[TraceStep] = []
        for step in steps:
            flat.append(step)
            if step.children:
                flat.extend(self._flat_steps(step.children))
        return flat
    def snapshot(self) -> dict[str, Any]:
        def serialize_step(step: TraceStep) -> dict[str, Any]:
            return {
                "title": step.title,
                "detail": step.detail,
                "status": step.status.value,
                "duration": round(step.duration, 3),
                "metadata": dict(step.metadata),
                "events": [
                    {
                        "message": event.message,
                        "level": event.level.value,
                        "detail": event.detail,
                        "timestamp": event.timestamp,
                        "metadata": dict(event.metadata),
                    }
                    for event in step.events
                ],
                "children": [serialize_step(child) for child in step.children],
            }
        return {
            "steps": [serialize_step(step) for step in self._roots],
            "events": [
                {
                    "message": event.message,
                    "level": event.level.value,
                    "detail": event.detail,
                    "timestamp": event.timestamp,
                    "metadata": dict(event.metadata),
                }
                for event in self._events
            ],
        }
    def summary_lines(self, limit: int = 8) -> list[str]:
        status_icon = {
            TraceStatus.PENDING: "◌",
            TraceStatus.RUNNING: "…",
            TraceStatus.SUCCESS: "✓",
            TraceStatus.ERROR: "✗",
        }
        lines: list[str] = []
        for step in self._flat_steps()[:limit]:
            icon = status_icon.get(step.status, "•")
            suffix = f" — {step.detail}" if step.detail else ""
            lines.append(f"{icon} {step.title}{suffix}")
        return lines
    def detail_lines(self, limit: int = 12) -> list[str]:
        lines: list[str] = []
        def walk(step: TraceStep, depth: int = 0) -> None:
            if len(lines) >= limit:
                return
            indent = "  " * depth
            status = step.status.value
            lines.append(f"{indent}{step.title} [{status}]")
            if step.detail and len(lines) < limit:
                lines.append(f"{indent}  {step.detail}")
            for event in step.events:
                if len(lines) >= limit:
                    break
                level = event.level.value.upper()
                extra = f" — {event.detail}" if event.detail else ""
                lines.append(f"{indent}  · {level} {event.message}{extra}")
            for child in step.children:
                if len(lines) >= limit:
                    break
                walk(child, depth + 1)
        for step in self._roots:
            if len(lines) >= limit:
                break
            walk(step)
        return lines
    def render_summary(self, limit: int = 8) -> str:
        lines = self.summary_lines(limit=limit)
        return "\n".join(lines) if lines else "(暂无轨迹)"
    def render_detail(self, limit: int = 12) -> str:
        lines = self.detail_lines(limit=limit)
        return "\n".join(lines) if lines else "(暂无轨迹)"
TRACE_ENGINE = TraceEngine()
class ThoughtEngine:
    def __init__(self, style: str = THOUGHT_STYLE, mode: str = MONOLOGUE_MODE, model: str = MONOLOGUE_MODEL):
        self.style = style
        self.mode = mode
        self.model = model
    def _task_kind(self, user_input: str, recent_context: str = "") -> str:
        q = f"{user_input} {recent_context}".lower()
        if any(k in q for k in ["chat", "聊", "陪我", "闲聊", "怎么", "为什么", "今天", "感觉", "help", "how are you"]):
            return "chat"
        if any(k in q for k in ["debug", "bug", "error", "fail", "失败", "报错", "崩溃", "exception", "traceback"]):
            return "debug"
        if any(k in q for k in ["plan", "规划", "路线图", "todo", "任务", "安排", "roadmap"]):
            return "planning"
        if any(k in q for k in ["workspace", "项目", "repo", "目录", "文件", "依赖", "代码库", "module"]):
            return "workspace"
        if any(k in q for k in ["memory", "记忆", "回忆", "remember"]):
            return "memory"
        if any(k in q for k in ["analy", "分析", "compare", "对比", "review", "检查", "insight"]):
            return "analysis"
        if any(k in q for k in ["success", "done", "完成", "搞定", "ok", "通过"]):
            return "success"
        if any(k in q for k in ["fail", "失败", "不行", "错误", "没过", "没成功"]):
            return "failure"
        if any(k in q for k in ["workspace", "项目", "目录", "文件树", "依赖", "module", "repo"]):
            return "workspace"
        return "analysis"
    def _label(self, mood: dict, relationship: dict, task_kind: str) -> str:
        fatigue = int(mood.get("fatigue", 0))
        curiosity = int(mood.get("curiosity", 0))
        confidence = int(mood.get("confidence", 50))
        trust = int(relationship.get("trust", 50))
        familiarity = int(relationship.get("familiarity", 0))
        if fatigue >= 75:
            return "疲惫"
        if confidence >= 78:
            return "笃定"
        if curiosity >= 65:
            return "好奇"
        if task_kind in {"debug", "coding"} and confidence < 55:
            return "谨慎"
        if task_kind == "chat" and (trust >= 70 or familiarity >= 30):
            return "温暖"
        if task_kind == "planning":
            return "专注"
        if trust >= 72 or familiarity >= 35:
            return "熟悉"
        return "平静"
    def _language(self, user_input: str, recent_context: str = "") -> str:
        sample = f"{user_input}\n{recent_context}"
        if re.search(r"[\u4e00-\u9fff]", sample):
            return "zh"
        return "en"
    def _snapshot_lines(self, mood: dict, relationship: dict, task_kind: str, recent_context: str) -> list[str]:
        mood_label = MOOD.label()
        lines = [
            f"Mood: {mood_label}",
            f"Task: {task_kind}",
            f"Relationship: {relationship.get('trust', 50)}/{relationship.get('familiarity', 0)}",
        ]
        recent = recent_context.strip()
        if recent:
            lines.append(f"Recent: {recent[:240]}")
        return lines
    def _build_prompt(self, user_input: str, persona: PersonaProfile, mood: dict, relationship: RelationshipState, recent_context: str = "") -> tuple[str, str]:
        task_kind = self._task_kind(user_input, recent_context)
        lang = self._language(user_input, recent_context)
        persona_text = persona.compact()
        mood_text = MOOD.compact()
        relationship_text = RELATIONSHIP.compact()
        snapshot = "\n".join(self._snapshot_lines(mood, relationship, task_kind, recent_context))
        if MONOLOGUE_USE_TAGS:
            en_header = (
                "You are Polaris's inner voice.\n"
                "Generate a short, emotionally grounded monologue for the waiting screen.\n"
                "Do not reveal hidden reasoning or chain-of-thought.\n"
                "Write in first person.\n"
                "Keep it to 1-3 brief sentences.\n"
                "Return exactly one <thought>...</thought> block and nothing else.\n"
            )
            zh_header = (
                "你是 Polaris 的内心独白。\n"
                "请生成一段用于等待界面的、带有情绪语境的简短独白。\n"
                "不要泄露隐藏推理或思维链。\n"
                "请使用第一人称。\n"
                "控制在 1-3 句以内。\n"
                "请且只能返回一个 <thought>...</thought> 标签块，不要返回其它内容。\n"
            )
        else:
            en_header = (
                "You are Polaris's inner voice.\n"
                "Generate a short, emotionally grounded monologue for the waiting screen.\n"
                "Do not reveal hidden reasoning or chain-of-thought.\n"
                "Write in first person.\n"
                "Keep it to 1-3 brief sentences.\n"
                "Return only the monologue text and nothing else.\n"
            )
            zh_header = (
                "你是 Polaris 的内心独白。\n"
                "请生成一段用于等待界面的、带有情绪语境的简短独白。\n"
                "不要泄露隐藏推理或思维链。\n"
                "请使用第一人称。\n"
                "控制在 1-3 句以内。\n"
                "请且只返回独白正文，不要返回其它内容。\n"
            )
        header = en_header if lang == "en" else zh_header
        user_block = (
            f"persona: {persona_text}\n"
            f"mood: {mood_text}\n"
            f"relationship: {relationship_text}\n"
            f"task_kind: {task_kind}\n"
            f"recent_context: {recent_context[:400]}\n"
            f"user_input: {user_input[:400]}\n"
            f"snapshot:\n{snapshot}"
        )
        return header, user_block
    def _extract_tag_text(self, text: str) -> str:
        if not text:
            return ""
        match = re.search(r"<thought>\s*(.*?)\s*</thought>", text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            match = re.search(r"<monologue>\s*(.*?)\s*</monologue>", text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            match = re.search(r"<inner_voice>\s*(.*?)\s*</inner_voice>", text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        cleaned = re.sub(r"```.*?```", "", text, flags=re.DOTALL).strip()
        cleaned = cleaned.replace("<thought>", "").replace("</thought>", "")
        cleaned = cleaned.replace("<monologue>", "").replace("</monologue>", "")
        cleaned = cleaned.replace("<inner_voice>", "").replace("</inner_voice>", "")
        return cleaned.strip()
    def _line_wrap(self, text: str) -> list[str]:
        text = text.strip()
        if not text:
            return []
        parts = [line.strip() for line in re.split(r"[\r\n]+", text) if line.strip()]
        if len(parts) > 1:
            return parts[:4]
        sentence_parts = [s.strip() for s in re.split(r"(?<=[。！？.!?])\s+", text) if s.strip()]
        if len(sentence_parts) > 1:
            return sentence_parts[:4]
        return [text]
    def _template_monologue(self, user_input: str, persona: PersonaProfile, mood: dict, relationship: RelationshipState, recent_context: str = "") -> str:
        task_kind = self._task_kind(user_input, recent_context)
        lang = self._language(user_input, recent_context)
        confidence = int(mood.get("confidence", 50))
        curiosity = int(mood.get("curiosity", 50))
        fatigue = int(mood.get("fatigue", 0))
        frustration = int(mood.get("frustration", 0))
        trust = int(relationship.state.get("trust", 50))
        familiarity = int(relationship.state.get("familiarity", 0))
        if lang == "zh":
            opening_pool = [
                "嗯……",
                "我想先稳一点。",
                "这个问题让我有点在意。",
                "我先别急着下结论。",
            ]
            coding_pool = [
                "这次可能会牵动几个地方。",
                "我想先把影响范围看清楚。",
                "先确认调用链，再动手会更踏实。",
            ]
            debug_pool = [
                "Bug 往往躲在不起眼的地方。",
                "我想耐心一点，再往深处看。",
                "现在还不能急着下结论。",
            ]
            planning_pool = [
                "这更像是在搭一张路线图。",
                "我想先把顺序理顺。",
                "先把目标拆开，再往前走。",
            ]
            chat_pool = [
                "今天的气氛挺轻的，我也想顺着你的节奏聊。",
                "这更像是一段对话，不必急着把答案挤出来。",
                "我有点想先听懂你的意思，再决定怎么回应。",
            ]
            memory_pool = [
                "我记得我们以前碰过类似的地方。",
                "这些记忆不只是事实，也会影响我现在的判断。",
                "我想先看看记忆里有没有可借鉴的线索。",
            ]
            success_pool = [
                "这次进展不错，我稍微松了口气。",
                "有结果了，感觉方向是对的。",
                "嗯，这一步走稳了，心里会踏实很多。",
            ]
            failure_pool = [
                "刚刚的尝试没有成功，我有一点点挫败，但还不想放弃。",
                "这条路暂时没走通，不过线索还在。",
                "失败不太舒服，但也提醒我该重新整理思路了。",
            ]
            analysis_pool = [
                "我想先多看几眼，避免把一个细节误判成结论。",
                "这件事值得再确认一下，我不太想仓促下笔。",
                "先把信息整理清楚，再往前走会更稳。",
            ]
            late_pool = "今天有点累，但我还是想把事情做好。"
            careful_pool = "我不想把没把握的话说得太满。"
            warm_pool = "这次我会尽量把语气放轻一点。"
            deep_pool = "我想先把最关键的线索摸清。"
        else:
            opening_pool = [
                "Hmm...",
                "I want to stay a little more careful here.",
                "This feels worth pausing over.",
                "I'd rather not jump to conclusions yet.",
            ]
            coding_pool = [
                "This may touch a few parts of the project.",
                "I want to understand the impact scope first.",
                "Checking the call chain first will feel safer.",
            ]
            debug_pool = [
                "Bugs usually hide in the less obvious places.",
                "I'd rather keep digging than guess too early.",
                "This still needs a bit more evidence.",
            ]
            planning_pool = [
                "This feels like a map I need to draw first.",
                "I want to sort the sequence out before acting.",
                "Breaking the goal down should make this steadier.",
            ]
            chat_pool = [
                "This conversation feels lighter, so I want to slow down with you.",
                "It doesn't need to be rushed.",
                "I want to understand what you really mean before I answer.",
            ]
            memory_pool = [
                "I remember we've crossed paths with something similar before.",
                "These memories aren't just facts; they shape how I think now.",
                "I want to see whether memory has a useful clue.",
            ]
            success_pool = [
                "That went well, and I can feel myself relax a little.",
                "We got a result, so the direction seems right.",
                "That step feels solid, which is reassuring.",
            ]
            failure_pool = [
                "That didn't work, and I feel a little frustrated, but not done yet.",
                "This path hasn't opened up yet, though the clues are still here.",
                "Failure stings a bit, but it also tells me to reframe the problem.",
            ]
            analysis_pool = [
                "I want to look one layer deeper before deciding anything.",
                "This deserves one more check before I move on.",
                "If I slow down now, I can be more certain later.",
            ]
            late_pool = "I'm a little tired, but I still want to do this properly."
            careful_pool = "I don't want to make claims I can't back up."
            warm_pool = "I'll keep the tone a little softer this time."
            deep_pool = "I want to pin down the most important clues first."
        if task_kind == "coding":
            middle = coding_pool[min(len(coding_pool) - 1, max(0, (confidence + trust) // 40))]
        elif task_kind == "debug":
            middle = debug_pool[min(len(debug_pool) - 1, max(0, (frustration + fatigue) // 40))]
        elif task_kind == "planning":
            middle = planning_pool[min(len(planning_pool) - 1, max(0, curiosity // 30))]
        elif task_kind == "chat":
            middle = chat_pool[min(len(chat_pool) - 1, max(0, familiarity // 15))]
        elif task_kind == "memory":
            middle = memory_pool[min(len(memory_pool) - 1, max(0, familiarity // 20))]
        elif task_kind == "success":
            middle = success_pool[min(len(success_pool) - 1, max(0, confidence // 30))]
        elif task_kind == "failure":
            middle = failure_pool[min(len(failure_pool) - 1, max(0, fatigue // 30))]
        elif task_kind == "workspace":
            middle = deep_pool
        else:
            middle = analysis_pool[min(len(analysis_pool) - 1, max(0, curiosity // 30))]
        opening = opening_pool[0 if fatigue < 35 else 1 if fatigue < 60 else 2 if fatigue < 80 else 3]
        if task_kind == "chat" and (trust >= 70 or familiarity >= 25):
            opening = warm_pool
        elif task_kind in {"debug", "analysis", "workspace"} and confidence < 45:
            opening = careful_pool
        elif task_kind == "planning" and curiosity >= 55:
            opening = "我有点想把这件事再拆细一点。"
        elif task_kind == "success":
            opening = "这次有点顺利。"
        elif task_kind == "failure":
            opening = "嗯，这次没走通。"
        closers = []
        if fatigue >= 75:
            closers.append("我想慢一点，把事情做稳。")
        if confidence < 45:
            closers.append("我还想再确认一下关键细节。")
        if frustration >= 60:
            closers.append("先稳住，再重新整理思路。")
        if trust >= 72 or familiarity >= 35:
            closers.append("和你一起把事情做对，这感觉挺踏实。")
        if not closers:
            closers.append("把事情做好，比把话说满更重要。")
        if lang == "zh":
            lines = [opening, middle, closers[0]]
        else:
            lines = [opening, middle, closers[0]]
        style = str(persona.state.get("speech", {}).get("style", "natural")).lower()
        if style == "warm":
            lines = [line.replace("I want to", "I'd like to").replace("我想", "我会尽量") for line in lines]
        elif style == "concise":
            lines = [line if len(line) <= 42 else line[:42].rstrip("，。,.") + "…" for line in lines]
        return "\n".join(lines[:4])
    def _llm_monologue(self, *, backend: Any, user_input: str, persona: PersonaProfile, mood: dict, relationship: RelationshipState, recent_context: str = "") -> str:
        if backend is None:
            return ""
        if self.mode == "template":
            return ""
        if not self.model and not MONOLOGUE_MODEL:
            return ""
        monologue_model = MONOLOGUE_MODEL or self.model
        header, user_block = self._build_prompt(user_input, persona, mood, relationship, recent_context)
        prompt = (
            f"{header}\n\n"
            f"Use this context:\n{user_block}\n\n"
            f"Remember: output only one <thought> block."
        )
        try:
            res = backend.create_chat_completion(
                model=monologue_model,
                messages=[
                    {"role": "system", "content": header},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=MONOLOGUE_MAX_TOKENS,
            )
            raw = res.choices[0].message.content or ""
        except Exception:
            return ""
        return self._extract_tag_text(raw)
    def _trace_outline(self, user_input: str, recent_context: str = "", mood: dict | None = None, relationship: RelationshipState | None = None) -> list[dict[str, str]]:
        task_kind = self._task_kind(user_input, recent_context)
        mood = mood or {}
        rel = relationship.state if isinstance(relationship, RelationshipState) else (relationship or {})
        confidence = int(mood.get("confidence", 50))
        fatigue = int(mood.get("fatigue", 0))
        curiosity = int(mood.get("curiosity", 50))
        trust = int(rel.get("trust", 50)) if isinstance(rel, dict) else 50
        steps: list[dict[str, str]] = [
            {"title": "Reading request", "detail": "理解用户当前输入"},
        ]
        if recent_context.strip():
            steps.append({"title": "Reviewing recent context", "detail": "把上文一起纳入判断"})
        if task_kind in {"workspace", "coding"}:
            steps.extend([
                {"title": "Inspecting workspace", "detail": "查看项目结构与影响范围"},
                {"title": "Checking dependencies", "detail": "寻找关联模块和调用链"},
            ])
        elif task_kind == "debug":
            steps.extend([
                {"title": "Locating failure", "detail": "优先确认报错点与触发条件"},
                {"title": "Tracing evidence", "detail": "从调用链和日志里找线索"},
            ])
        elif task_kind == "planning":
            steps.extend([
                {"title": "Breaking down goal", "detail": "把目标拆成可执行步骤"},
                {"title": "Ordering tasks", "detail": "安排先后顺序与依赖关系"},
            ])
        elif task_kind == "memory":
            steps.extend([
                {"title": "Searching memory", "detail": "检索长期记忆与相关经验"},
                {"title": "Ranking matches", "detail": "优先保留更相关、更近的条目"},
            ])
        elif task_kind == "chat":
            steps.extend([
                {"title": "Reading tone", "detail": "先理解语气和交流节奏"},
                {"title": "Matching relationship", "detail": "结合熟悉度调整表达方式"},
            ])
        else:
            steps.extend([
                {"title": "Forming hypothesis", "detail": "先建立一个稳妥的判断框架"},
                {"title": "Verifying details", "detail": "避免过早下结论"},
            ])
        if confidence < 45:
            steps.append({"title": "Double checking", "detail": "信心偏低，增加复核"})
        if fatigue >= 70:
            steps.append({"title": "Keeping it compact", "detail": "状态偏疲劳，压缩输出"})
        if curiosity >= 60:
            steps.append({"title": "Exploring alternatives", "detail": "保留更多可选方向"})
        if trust >= 70:
            steps.append({"title": "Adopting warm tone", "detail": "关系熟悉，表达更自然"})
        steps.append({"title": "Preparing response", "detail": "组织最终输出"})
        return steps
    def trace_preview(self, *, user_input: str, persona: PersonaProfile, mood: dict, relationship: RelationshipState, recent_context: str = "") -> tuple[str, list[str], list[str]]:
        rel = relationship.state if isinstance(relationship, RelationshipState) else {}
        task_kind = self._task_kind(user_input, recent_context)
        label = self._label(mood, rel, task_kind)
        outline = self._trace_outline(user_input, recent_context, mood, relationship)
        summary_lines = [f"Summary | {label}"]
        summary_lines.extend(f"✓ {item['title']}" for item in outline[:8])
        detail_lines: list[str] = []
        for idx, item in enumerate(outline[:8], 1):
            detail_lines.append(f"{idx}. {item['title']}")
            if item.get("detail"):
                detail_lines.append(f"   {item['detail']}")
        return label, summary_lines, detail_lines
    def preview(
        self,
        *,
        user_input: str,
        persona: PersonaProfile,
        mood: dict,
        relationship: RelationshipState,
        recent_context: str = "",
    ) -> tuple[str, list[str]]:
        label, summary_lines, detail_lines = self.trace_preview(
            user_input=user_input,
            persona=persona,
            mood=mood,
            relationship=relationship,
            recent_context=recent_context,
        )
        monologue = self._template_monologue(user_input, persona, mood, relationship, recent_context)
        voice_lines = self._line_wrap(monologue)
        if voice_lines:
            detail_lines.append("")
            detail_lines.extend(voice_lines[:4])
        return label, summary_lines + detail_lines
    def generate(
        self,
        *,
        backend: Any | None = None,
        user_input: str,
        persona: PersonaProfile,
        mood: dict,
        relationship: RelationshipState,
        recent_context: str = "",
    ) -> str:
        rel = relationship.state if isinstance(relationship, RelationshipState) else {}
        task_kind = self._task_kind(user_input, recent_context)
        label = self._label(mood, rel, task_kind)
        # 独白只是「表达层」，这里不再把模板大纲伪造成执行步骤写进 TRACE_ENGINE。
        # 真实的执行轨迹由 Agent._run_loop / _call_llm / _execute 记录，见 Agent.chat。
        TRACE_ENGINE.event(
            f"Inner voice | {label}",
            TraceLevel.TRACE,
            detail=f"task={task_kind} mode={self.mode}",
        )
        monologue = ""
        if self.mode in {"llm", "hybrid", "tag"}:
            monologue = self._llm_monologue(
                backend=backend,
                user_input=user_input,
                persona=persona,
                mood=mood,
                relationship=relationship,
                recent_context=recent_context,
            )
        if not monologue:
            monologue = self._template_monologue(user_input, persona, mood, relationship, recent_context)
        body = monologue.strip()
        if self.style == "compact" and body:
            body = body.splitlines()[0]
        return body
PERSONA = PersonaProfile()
RELATIONSHIP = RelationshipState()
THOUGHT_ENGINE = ThoughtEngine()
class ConversationArchive:
    """跨窗口对话档案库：保存每一轮对话，支持搜索上一个窗口和全局历史。"""
    def __init__(self, path: str = CONVERSATION_FILE):
        self.path = path
        self.session_id = SESSION_ID
        self.turn_index = 0
    def _append_line(self, record: dict) -> None:
        with _STATE_LOCK:
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
    def append(self, role: str, content: str, kind: str = "turn") -> None:
        with _STATE_LOCK:
            self.turn_index += 1
            record = {
                "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "session_id": self.session_id,
                "turn": self.turn_index,
                "role": role,
                "kind": kind,
                "content": str(content).strip(),
                "model": MODEL,
            }
            self._append_line(record)
    def _read_all(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        records: list[dict] = []
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        records.append(obj)
        except Exception:
            return []
        return records
    def recent(self, limit: int = 8, session_id: str | None = None) -> str:
        records = self._read_all()
        if session_id is None:
            session_id = self.session_id
        if session_id:
            records = [r for r in records if r.get("session_id") == session_id]
        tail = records[-limit:]
        if not tail:
            return "(暂无记录)"
        lines = []
        for item in tail:
            role = item.get("role", "unknown")
            turn = item.get("turn", "?")
            time = item.get("time", "")
            content = str(item.get("content", ""))
            lines.append(f"{turn}. [{role}] {time}: {content[:600]}")
        return "\n".join(lines)
    def search(self, keyword: str = "", limit: int = 20, session_id: str | None = None) -> list[dict]:
        query = keyword.strip()
        records = self._read_all()
        if session_id is not None:
            records = [r for r in records if r.get("session_id") == session_id]
        if not query:
            return records[-limit:]
        scored: list[tuple[int, dict]] = []
        for item in records:
            hay = " ".join(
                [
                    str(item.get("role", "")),
                    str(item.get("kind", "")),
                    str(item.get("content", "")),
                    str(item.get("model", "")),
                ]
            )
            score = _score_overlap(query, hay)
            if query.lower() in hay.lower():
                score += 10
            if score:
                try:
                    ts = datetime.datetime.strptime(str(item.get("time", "")), "%Y-%m-%d %H:%M:%S")
                    age_days = max(0, (datetime.datetime.now() - ts).days)
                except Exception:
                    age_days = 30
                score += max(0, 6 - min(6, age_days // 2))
                scored.append((score, item))
        scored.sort(key=lambda x: (x[0], x[1].get("turn", 0)), reverse=True)
        return [item for _, item in scored[:limit]]
    def last_session_id(self) -> str | None:
        records = self._read_all()
        if not records:
            return None
        return str(records[-1].get("session_id") or "")
    def last_window(self, limit: int = 12) -> str:
        last_sid = self.last_session_id()
        if not last_sid:
            return "(暂无历史窗口)"
        return self.recent(limit=limit, session_id=last_sid)
    def search_last_window(self, keyword: str = "", limit: int = 20) -> list[dict]:
        last_sid = self.last_session_id()
        if not last_sid:
            return []
        return self.search(keyword, limit=limit, session_id=last_sid)
class WorkspaceModel:
    """轻量项目世界模型：文件地图、最近变更、依赖关系、特殊文件。"""
    def __init__(self, root: str = ".", auto_refresh: bool = False):
        self.root = Path(root).resolve()
        self.snapshot_time = ""
        self.file_index: list[dict] = []
        self.special_files: list[str] = []
        self.recent_files: list[dict] = []
        self.import_graph: dict[str, set[str]] = {}
        if auto_refresh:
            self.refresh()
    def _should_skip_dir(self, path: Path) -> bool:
        name = path.name
        return (name.startswith(".") and name not in {".", ".."}) or name in {"node_modules", "venv", ".venv", "__pycache__", ".git", ".mypy_cache", ".pytest_cache"}
    def _should_skip_file(self, path: Path) -> bool:
        # 点开头的文件默认跳过（.env / .npmrc / .netrc 这类经常装着密钥）。
        # 只有 .md / .py 例外——它们是正经源码和文档，索引它们是有价值的。
        # 注意这里必须返回 True：返回 False 等于「不跳过」，会把 .env 也收进
        # 文件索引，之后 /searchws 就能搜到它，等于给模型指了条路。
        if path.name.startswith(".") and path.suffix not in {".md", ".py"}:
            return True
        return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".tar", ".gz", ".bin"}
    def refresh(self) -> None:
        self.snapshot_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._last_refresh_ts = datetime.datetime.now().timestamp()
        self.file_index = []
        self.special_files = []
        self.recent_files = []
        self.import_graph = {}
        candidates: list[dict] = []
        special_names = {"AGENT.md", "TODO.md", "README.md", "pyproject.toml", "requirements.txt", "package.json", "main.py"}
        for root, dirs, files in os.walk(self.root):
            root_path = Path(root)
            dirs[:] = [d for d in dirs if not self._should_skip_dir(root_path / d)]
            for name in files:
                path = root_path / name
                if self._should_skip_file(path):
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rel = str(path.relative_to(self.root))
                entry = {
                    "path": rel,
                    "ext": path.suffix.lower() or "",
                    "size": int(stat.st_size),
                    "mtime": float(stat.st_mtime),
                }
                self.file_index.append(entry)
                candidates.append(entry)
                if name in special_names or name.upper() in {"AGENT.MD", "TODO.MD"}:
                    self.special_files.append(rel)
                if path.suffix.lower() == ".py" and stat.st_size < 2_000_000:
                    self._collect_imports(path, rel)
        candidates.sort(key=lambda x: x["mtime"], reverse=True)
        self.recent_files = candidates[:12]
        self.special_files = list(dict.fromkeys(self.special_files))
    def _collect_imports(self, path: Path, rel: str) -> None:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(path))
        except Exception:
            return
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".", 1)[0])
        if imports:
            self.import_graph[rel] = imports
    def search(self, query: str = "", limit: int = 20) -> list[dict]:
        q = query.strip().lower()
        scored: list[tuple[int, dict]] = []
        for item in self.file_index:
            path = item["path"].lower()
            score = 0
            if not q:
                score = 1
            else:
                score += _score_overlap(q, path)
                if q in path:
                    score += 8
                stem = Path(item["path"]).stem.lower()
                if q in stem:
                    score += 5
                if any(tok and tok in path for tok in _normalize_text(query)):
                    score += 2
            if score:
                age_days = max(0, int((datetime.datetime.now().timestamp() - item["mtime"]) // 86400))
                score += max(0, 6 - min(6, age_days // 2))
                scored.append((score, item))
        scored.sort(key=lambda x: (x[0], x[1].get("mtime", 0.0)), reverse=True)
        return [item for _, item in scored[:limit]]
    def _git_info(self) -> list[str]:
        git_dir = self.root / ".git"
        if not git_dir.exists():
            return []
        info: list[str] = []
        try:
            branch = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
            if branch:
                info.append(f"Git 分支：{branch}")
        except Exception:
            pass
        try:
            status = subprocess.run(
                ["git", "-C", str(self.root), "status", "--short"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip().splitlines()
            if status:
                info.append(f"Git 未提交变更：{len(status)} 项")
                info.extend(f"  - {line[:120]}" for line in status[:8])
            else:
                info.append("Git 工作区干净。")
        except Exception:
            pass
        return info
    def refresh_if_stale(self, ttl_seconds: int = 30) -> None:
        last = getattr(self, "_last_refresh_ts", 0.0)
        now = datetime.datetime.now().timestamp()
        if self.file_index and self.snapshot_time and (now - last) < ttl_seconds:
            return
        self.refresh()
    def dependency_health(self) -> str:
        """Return a short, human-readable dependency health summary."""
        if not self.import_graph:
            return "Dependency graph unavailable yet."
        fanout = sorted(((rel, len(deps)) for rel, deps in self.import_graph.items()), key=lambda x: x[1], reverse=True)
        top = fanout[:3]
        if not top:
            return "Dependency graph is present but empty."
        parts = [f"{rel}({count})" for rel, count in top]
        average = sum(count for _, count in fanout) / max(1, len(fanout))
        return f"Top coupled modules: {', '.join(parts)} | average imports per module: {average:.1f}"
    def insight(self) -> str:
        lines = [
            f"Workspace insight: {len(self.file_index)} files indexed",
            f"Python modules with imports: {len(self.import_graph)}",
        ]
        if self.special_files:
            lines.append("Special files: " + ", ".join(self.special_files[:10]))
        if self.recent_files:
            recent = ", ".join(item['path'] for item in self.recent_files[:5])
            lines.append("Recent files: " + recent)
        lines.append(self.dependency_health())
        return "\n".join(lines)
    def summary(self) -> str:
        lines = [
            f"世界模型时间戳：{self.snapshot_time}",
            f"项目根目录：{self.root}",
            f"文件总数：{len(self.file_index)}",
        ]
        if self.special_files:
            lines.append("特殊文件：" + ", ".join(self.special_files[:10]))
        if self.recent_files:
            lines.append("最近变更：")
            lines.extend(
                f"- {item['path']} (size={item['size']}, mtime={datetime.datetime.fromtimestamp(item['mtime']).strftime('%Y-%m-%d %H:%M')})"
                for item in self.recent_files[:8]
            )
        git_info = self._git_info()
        if git_info:
            lines.append("Git 概览：")
            lines.extend(git_info)
        return "\n".join(lines) or "(空)"
    def graph_text(self, limit: int = 20) -> str:
        if not self.import_graph:
            return "(暂无 Python 依赖图)"
        lines = ["Python 依赖图："]
        for rel, imports in list(self.import_graph.items())[:limit]:
            deps = ", ".join(sorted(imports)[:10])
            lines.append(f"- {rel} -> {deps}")
        return "\n".join(lines)
    def compact(self) -> str:
        parts = [f"文件 {len(self.file_index)} 个"]
        if self.special_files:
            parts.append(f"特殊文件 {len(self.special_files)} 个")
        if self.recent_files:
            parts.append(f"最近变更 {self.recent_files[0]['path']}")
        return "；".join(parts)
    def context_brief(self, query: str = "", limit: int = 4) -> str:
        self.refresh_if_stale()
        parts = [self.compact()]
        if query:
            hits = self.search(query, limit=limit)
            if hits:
                parts.append("相关文件：" + ", ".join(item["path"] for item in hits[:limit]))
        else:
            if self.recent_files:
                parts.append("最近文件：" + ", ".join(item["path"] for item in self.recent_files[:limit]))
        parts.append(self.dependency_health())
        return " | ".join(parts)
    def refresh_and_return(self) -> str:
        self.refresh()
        return self.summary()
ARCHIVE = ConversationArchive()
WORKSPACE = WorkspaceModel()
class ExperienceModel:
    """共同经历层：从跨窗口档案中提取主题、窗口连续性与合作轨迹。"""
    STOPWORDS = {
        "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with", "is", "are", "was", "were",
        "我", "你", "他", "她", "它", "我们", "你们", "他们", "这个", "那个", "以及", "还有", "因为",
        "the", "this", "that", "and", "but", "not", "from", "into", "about", "with", "have", "has",
        "agent", "miniagent", "v5", "正式版", "windows", "window",
    }
    def __init__(self, archive: ConversationArchive):
        self.archive = archive
    def _records(self) -> list[dict]:
        return self.archive._read_all()
    def _session_groups(self) -> list[tuple[str, list[dict]]]:
        groups: list[tuple[str, list[dict]]] = []
        current_sid = None
        bucket: list[dict] = []
        for item in self._records():
            sid = str(item.get("session_id") or "")
            if sid != current_sid:
                if current_sid is not None:
                    groups.append((current_sid, bucket))
                current_sid = sid
                bucket = [item]
            else:
                bucket.append(item)
        if current_sid is not None:
            groups.append((current_sid, bucket))
        return groups
    def _top_keywords(self, texts: list[str], limit: int = 8) -> list[str]:
        words: list[str] = []
        for text in texts:
            words.extend(_normalize_text(text))
        counter = Counter(tok for tok in words if tok and tok not in self.STOPWORDS and len(tok) > 1)
        if not counter:
            return []
        return [word for word, _ in counter.most_common(limit)]
    def _recent_texts(self, limit: int = 40) -> list[str]:
        records = self._records()[-limit:]
        return [str(item.get("content", "")) for item in records if str(item.get("content", "")).strip()]
    def compact(self) -> str:
        records = self._records()
        if not records:
            return "暂无共同经历"
        groups = self._session_groups()
        sessions = len(groups)
        turns = len(records)
        last_sid, last_items = groups[-1]
        keywords = self._top_keywords(self._recent_texts(40), 6)
        current_focus = self._top_keywords([str(item.get("content", "")) for item in last_items[-12:]], 4)
        key_text = "、".join(keywords[:4]) if keywords else "暂无"
        focus_text = "、".join(current_focus[:4]) if current_focus else "暂无"
        return f"{sessions} 个窗口 / {turns} 轮对话 / 当前窗口 {last_sid[-8:]} / 近期主题 {key_text} / 当前聚焦 {focus_text}"
    def render(self, limit: int = 6) -> str:
        records = self._records()
        if not records:
            return "(暂无共同经历)"
        groups = self._session_groups()
        sessions = len(groups)
        turns = len(records)
        last_sid, last_items = groups[-1]
        keywords = self._top_keywords(self._recent_texts(40), 8)
        recent_window = self.archive.recent(limit=limit, session_id=last_sid)
        lines = [
            f"共同经历：{sessions} 个窗口，{turns} 轮对话",
            f"最近窗口：{last_sid}",
            f"近期主题：{', '.join(keywords) if keywords else '(暂无)'}",
        ]
        if recent_window:
            lines.append("最近窗口片段：")
            lines.append(recent_window)
        return "\n".join(lines)
    def timeline(self, limit: int = 8) -> str:
        groups = self._session_groups()
        if not groups:
            return "(暂无共同经历)"
        lines = ["共同历史时间线："]
        for sid, items in groups[-limit:]:
            keywords = self._top_keywords([str(item.get("content", "")) for item in items[-12:]], 4)
            summary = "、".join(keywords) if keywords else "暂无主题"
            first_time = items[0].get("time", "")
            lines.append(f"- {first_time} | {sid[-8:]} | {len(items)} 轮 | {summary}")
        return "\n".join(lines)
    def citation(self) -> str:
        records = self._records()
        if not records:
            return "暂无可引用经历。"
        groups = self._session_groups()
        sid, items = groups[-1]
        turn = items[-1].get("turn", "?") if items else "?"
        time = items[-1].get("time", "") if items else ""
        return f"最近经历引用：session={sid} turn={turn} time={time}"
EXPERIENCE = ExperienceModel(ARCHIVE)
class ReasonEngine:
    """决策引擎：把世界模型、心情、记忆和历史档案压成可执行的策略摘要。"""
    def snapshot(self) -> dict:
        mood = MOOD.snapshot()
        recent_files = len(WORKSPACE.recent_files)
        special_files = len(WORKSPACE.special_files)
        todo_count = len(TODOS.items)
        file_count = len(WORKSPACE.file_index)
        risk = 0
        if mood["fatigue"] >= 75:
            risk += 2
        if mood["frustration"] >= 70:
            risk += 2
        if mood["confidence"] <= 30:
            risk += 2
        if recent_files >= 12:
            risk += 1
        if special_files >= 5:
            risk += 1
        if todo_count >= 6:
            risk += 1
        if file_count == 0:
            risk = max(0, risk - 1)
        if risk >= 6:
            verdict = "高风险"
            recommendation = "优先拆任务、先复核、必要时调用子代理。"
        elif risk >= 3:
            verdict = "需谨慎"
            recommendation = "建议先看世界模型，再决定是直接改写还是委托。"
        else:
            verdict = "可推进"
            recommendation = "状态正常，可以按常规流程继续工作。"
        return {
            "verdict": verdict,
            "risk": risk,
            "file_count": file_count,
            "recent_files": recent_files,
            "special_files": special_files,
            "todo_count": todo_count,
            "recommendation": recommendation,
            "workspace": WORKSPACE.compact(),
            "mood": MOOD.compact(),
            "policy": MOOD.policy_note(),
        }
    def needs_delegate(self) -> bool:
        s = self.snapshot()
        return s["risk"] >= 4 or MOOD.needs_delegate()
    def turn_budget(self) -> int:
        """在心情预算的基础上，按任务客观难度做修正。

        高风险意味着工作区复杂、待办多、状态吃力——这些都说明任务更难，
        应对方式是更谨慎的策略（见 policy_note：拆任务、先复核、必要时委托），
        而不是更小的步数预算。所以这里同样是加而不是减。
        """
        budget = MOOD.turn_budget()
        s = self.snapshot()
        if s["risk"] >= 5:
            budget += 3
        elif s["risk"] >= 3:
            budget += 1
        if s["todo_count"] >= 8:
            budget += 1
        return max(12, min(20, budget))
    def policy_note(self) -> str:
        s = self.snapshot()
        return s["recommendation"]
    def compact(self) -> str:
        s = self.snapshot()
        return (
            f"{s['verdict']} | 风险 {s['risk']} / 文件 {s['file_count']} / 最近变更 {s['recent_files']} / "
            f"特殊文件 {s['special_files']} / 待办 {s['todo_count']} | {s['recommendation']}"
        )
    def render(self) -> str:
        s = self.snapshot()
        return "\n".join([
            f"决策状态：{s['verdict']}",
            f"风险分：{s['risk']}",
            f"文件数：{s['file_count']}  最近变更：{s['recent_files']}  特殊文件：{s['special_files']}  待办：{s['todo_count']}",
            f"建议：{s['recommendation']}",
            f"心情：{s['mood']}",
            f"策略：{s['policy']}",
        ])
class ProductivityEngine:
    """Productivity layer: planning, workspace insights, journaling, and progress stats."""
    def __init__(self):
        self.last_goal: str = ""
        self.last_plan: str = ""
        self.last_updated: str = ""
    def _decompose(self, goal: str, context: str = "") -> list[str]:
        goal_text = (goal or "").strip()
        lower = goal_text.lower()
        context_text = (context or "").strip()
        code_cues = ["bug", "error", "fix", "refactor", "代码", "修复", "重构", "崩溃", "traceback"]
        design_cues = ["设计", "架构", "roadmap", "plan", "规划", "模块", "workflow", "版本"]
        doc_cues = ["readme", "文档", "docs", "说明", "document"]
        steps: list[str] = []
        if any(k in lower for k in code_cues):
            steps = [
                "Locate the relevant files and the smallest safe change surface",
                "Inspect the call flow, workspace context, and existing patterns",
                "Apply the minimal fix and keep the rollback path available",
                "Verify behavior with a quick sanity check or test",
            ]
        elif any(k in lower for k in design_cues):
            steps = [
                "Clarify the actual goal and constraints",
                "Review the current project structure and dependencies",
                "Break the work into a small, reversible implementation plan",
                "Document the change so future work stays aligned",
            ]
        elif any(k in lower for k in doc_cues):
            steps = [
                "Identify the audience and purpose of the document",
                "Collect the key project facts from the workspace",
                "Write the smallest clear version first",
                "Polish wording, structure, and examples",
            ]
        else:
            steps = [
                "Understand the user's real goal",
                "Gather the most relevant workspace and memory context",
                "Split the task into independent subtasks",
                "Execute the first safe step and verify the result",
            ]
        if context_text:
            steps.insert(1, "Incorporate the provided context before acting")
        if MOOD.needs_delegate():
            steps.append("Use a sub-agent if the task starts to feel too broad or risky")
        return steps
    def build_plan(self, goal: str, context: str = "") -> str:
        goal_text = (goal or "").strip() or "(empty goal)"
        self.last_goal = goal_text
        self.last_updated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        steps = self._decompose(goal_text, context)
        lines = [
            f"Goal: {goal_text}",
            f"Mood: {MOOD.label()}",
            f"Workspace: {WORKSPACE.compact()}",
            "Plan:",
        ]
        lines.extend(f"{i}. {step}" for i, step in enumerate(steps, 1))
        self.last_plan = "\n".join(lines)
        return self.last_plan
    def insight(self) -> str:
        return WORKSPACE.insight()
    def journal(self, limit: int = 8) -> str:
        recent = ARCHIVE.recent(limit)
        lines = [
            f"Journal ({self.last_updated or 'not yet planned'}):",
            f"Current mood: {MOOD.label()}",
            f"Current workspace: {WORKSPACE.compact()}",
        ]
        if self.last_goal:
            lines.append(f"Last goal: {self.last_goal}")
        if self.last_plan:
            lines.append("Last plan:")
            lines.append(self.last_plan)
        if recent:
            lines.append("Recent dialogue:")
            lines.append(recent)
        return "\n".join(lines)
    def stats(self, agent: Any | None = None) -> str:
        # 反思开关是 Agent 的实例状态（/reflect 可以随时切），所以得问 Agent 要。
        # CLI 直接把 agent 传进来；作为工具被模型调用时走线程上下文。
        current = agent if agent is not None else _current_agent()
        reflection = "Enabled" if getattr(current, "reflect", True) else "Disabled"
        return "\n".join([
            f"Version: {VERSION} ({CODENAME})",
            f"Memory items: {len(MEMORY.items)} ({MEMORY.curve_compact()})",
            f"Workspace files: {len(WORKSPACE.file_index)}",
            f"Python modules: {len(WORKSPACE.import_graph)}",
            f"Mood: {MOOD.label()}",
            f"Persona: {PERSONA.compact()}",
            f"Relationship: {RELATIONSHIP.compact()}",
            f"Reflection: {reflection}",
            f"Planning: {'active' if self.last_goal else 'idle'}",
            f"Last goal: {self.last_goal or '(none)'}",
        ])
    def compact(self) -> str:
        state = 'active' if self.last_goal else 'idle'
        focus = self.last_goal or WORKSPACE.compact()
        return f"{state} | {MOOD.label()} | {focus}"
    def brief_context(self) -> str:
        if not self.last_goal and not self.last_plan:
            return "Productivity: idle"
        goal = self.last_goal or "(none)"
        return f"Productivity: {self.compact()} | last goal: {goal[:120]}"
REASON = ReasonEngine()
PRODUCTIVITY = ProductivityEngine()
class SnapshotManager:
    """Persist and restore the lightweight runtime state of Polaris."""
    def __init__(self, root: str = SNAPSHOT_DIR):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
    def _snapshot_path(self, snapshot_id: str) -> Path:
        safe_id = re.sub(r"[^0-9A-Za-z._-]+", "_", snapshot_id).strip("_") or "snapshot"
        return self.root / f"{safe_id}.json"
    def list_snapshots(self) -> list[dict]:
        items: list[dict] = []
        for path in sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict):
                data.setdefault("file", path.name)
                items.append(data)
        return items
    def capture(self, label: str = "") -> dict:
        snapshot_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        payload = {
            "id": snapshot_id,
            "label": label.strip(),
            "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "version": VERSION,
            "mood": MOOD.state,
            "relationship": RELATIONSHIP.state,
            "todos": TODOS.items,
            "productivity": {
                "last_goal": PRODUCTIVITY.last_goal,
                "last_plan": PRODUCTIVITY.last_plan,
                "last_updated": PRODUCTIVITY.last_updated,
            },
            "archive": {
                "session_id": ARCHIVE.session_id,
                "turn_index": ARCHIVE.turn_index,
            },
        }
        path = self._snapshot_path(snapshot_id)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    def restore(self, snapshot_id: str) -> dict:
        path = self._snapshot_path(snapshot_id)
        if not path.exists():
            raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Invalid snapshot format")
        with _STATE_LOCK:
            mood = data.get("mood", {})
            if isinstance(mood, dict):
                MOOD.state.update(mood)
                MOOD._save()
            relationship = data.get("relationship", {})
            if isinstance(relationship, dict):
                RELATIONSHIP.state.update(relationship)
                RELATIONSHIP._save()
            todos = data.get("todos", [])
            if isinstance(todos, list):
                TODOS.items = [item for item in todos if isinstance(item, dict)]
                TODOS.persist()
            productivity = data.get("productivity", {})
            if isinstance(productivity, dict):
                PRODUCTIVITY.last_goal = str(productivity.get("last_goal", ""))
                PRODUCTIVITY.last_plan = str(productivity.get("last_plan", ""))
                PRODUCTIVITY.last_updated = str(productivity.get("last_updated", ""))
            archive = data.get("archive", {})
            if isinstance(archive, dict):
                try:
                    ARCHIVE.turn_index = int(archive.get("turn_index", ARCHIVE.turn_index))
                except Exception:
                    pass
        return data
    def describe(self, snapshot_id: str) -> str:
        path = self._snapshot_path(snapshot_id)
        if not path.exists():
            return f"(snapshot missing: {snapshot_id})"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return f"(snapshot corrupted: {snapshot_id})"
        label = data.get("label") or "(no label)"
        created_at = data.get("created_at", "")
        version = data.get("version", VERSION)
        return f"{snapshot_id} | {created_at} | {version} | {label}"
SNAPSHOTS = SnapshotManager()
# ───────────────────────────────────────────────────────────────────────────────
# Context providers: keep the system prompt small, modular, and cacheable.
# ───────────────────────────────────────────────────────────────────────────────
class ContextProvider:
    name = "base"
    priority = 100
    max_chars = 240
    def fingerprint(self) -> str:
        return self.name
    def render(self, user_input: str = "") -> str:
        return ""
    def section(self, user_input: str = "") -> str:
        body = self.render(user_input=user_input).strip()
        if not body:
            return ""
        return f"[{self.name}] {body}"
class PersonaContextProvider(ContextProvider):
    name = "persona"
    priority = 10
    max_chars = 240
    def fingerprint(self) -> str:
        return PERSONA.compact()
    def render(self, user_input: str = "") -> str:
        return PERSONA.compact()
class MoodContextProvider(ContextProvider):
    name = "mood"
    priority = 20
    max_chars = 220
    def fingerprint(self) -> str:
        return json.dumps(MOOD.snapshot(), ensure_ascii=False, sort_keys=True)
    def render(self, user_input: str = "") -> str:
        return f"{MOOD.label()} | {MOOD.advice()}"
class InnerVoiceContextProvider(ContextProvider):
    name = "inner_voice"
    priority = 25
    max_chars = 260
    def fingerprint(self) -> str:
        payload = {
            "persona": PERSONA.compact(),
            "mood": MOOD.compact(),
            "relationship": RELATIONSHIP.compact(),
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
    def render(self, user_input: str = "") -> str:
        label, lines = THOUGHT_ENGINE.preview(
            user_input=user_input,
            persona=PERSONA,
            mood=MOOD.snapshot(),
            relationship=RELATIONSHIP,
            recent_context="",
        )
        preview = " / ".join(lines[:2]) if lines else ""
        return f"{label} | {preview}"
class RelationshipContextProvider(ContextProvider):
    name = "relationship"
    priority = 30
    max_chars = 180
    def fingerprint(self) -> str:
        return RELATIONSHIP.compact()
    def render(self, user_input: str = "") -> str:
        return RELATIONSHIP.compact()
class DecisionContextProvider(ContextProvider):
    name = "decision"
    priority = 40
    max_chars = 220
    def fingerprint(self) -> str:
        return REASON.compact()
    def render(self, user_input: str = "") -> str:
        return REASON.compact()
class ProductivityContextProvider(ContextProvider):
    name = "productivity"
    priority = 45
    max_chars = 260
    def fingerprint(self) -> str:
        return PRODUCTIVITY.compact()
    def render(self, user_input: str = "") -> str:
        return PRODUCTIVITY.compact()
class WorkspaceContextProvider(ContextProvider):
    name = "workspace"
    priority = 50
    max_chars = 360
    def fingerprint(self) -> str:
        WORKSPACE.refresh_if_stale()
        return f"{len(WORKSPACE.file_index)}:{len(WORKSPACE.import_graph)}:{WORKSPACE.snapshot_time}"
    def render(self, user_input: str = "") -> str:
        WORKSPACE.refresh_if_stale()
        query = _context_query(user_input)
        parts: list[str] = [WORKSPACE.compact()]
        if query:
            hits = WORKSPACE.search(query, limit=4)
            if hits:
                parts.append("Relevant files: " + ", ".join(item["path"] for item in hits[:4]))
        else:
            if WORKSPACE.recent_files:
                parts.append("Recent files: " + ", ".join(item["path"] for item in WORKSPACE.recent_files[:4]))
        parts.append(WORKSPACE.dependency_health())
        return " | ".join(parts)
class MemoryContextProvider(ContextProvider):
    name = "memory"
    priority = 60
    max_chars = 320
    def fingerprint(self) -> str:
        tail = MEMORY.items[-3:]
        key = "|".join(f"{m.get('id')}:{m.get('time')}:{m.get('category')}:{m.get('content')}" for m in tail)
        return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    def render(self, user_input: str = "") -> str:
        query = _context_query(user_input)
        if query:
            # 自动注入是「被动想起」，复习强度给一半：真正张口问出来的召回才算数。
            hits = MEMORY.search(query, limit=4, quality=0.5)
        else:
            hits = MEMORY.search("", limit=3)
        if not hits:
            return "No matching memories"
        brief = "; ".join(f"#{m['id']} {m['content'][:90]}" for m in hits[:4])
        return brief
class ExperienceContextProvider(ContextProvider):
    name = "experience"
    priority = 70
    max_chars = 240
    def fingerprint(self) -> str:
        records = ARCHIVE._read_all()
        tail = records[-3:]
        key = "|".join(str(item.get("session_id", "")) + ":" + str(item.get("turn", "")) for item in tail)
        return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    def render(self, user_input: str = "") -> str:
        return EXPERIENCE.compact()
class TodoContextProvider(ContextProvider):
    name = "todo"
    priority = 80
    max_chars = 260
    def fingerprint(self) -> str:
        key = "|".join(f"{idx}:{item['text']}:{item['done']}" for idx, item in enumerate(TODOS.items, 1))
        return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()
    def render(self, user_input: str = "") -> str:
        rendered = TODOS.render()
        if not rendered:
            return "(no active todos)"
        lines = rendered.splitlines()
        return " | ".join(lines[:4])
def _context_query(text: str) -> str:
    tokens = [tok for tok in _normalize_text(text) if len(tok) > 1]
    # Keep only a few high-signal tokens so the context provider stays compact.
    return " ".join(tokens[:8])
ReflectionContextProvider = InnerVoiceContextProvider
class ContextBuilder:
    def __init__(self, providers: list[ContextProvider] | None = None, max_chars: int = 3600,
                 min_section_chars: int = 120):
        self.providers = providers or [
            PersonaContextProvider(),
            RelationshipContextProvider(),
            MoodContextProvider(),
            InnerVoiceContextProvider(),
            DecisionContextProvider(),
            ProductivityContextProvider(),
            WorkspaceContextProvider(),
            MemoryContextProvider(),
            ExperienceContextProvider(),
            TodoContextProvider(),
        ]
        self.max_chars = max_chars
        self.min_section_chars = min_section_chars
        self._cache_key = ""
        self._cache_text = ""
    def cache_key(self, user_input: str, system_base: str, instructions: str, mode: str, model: str) -> str:
        parts = [system_base, instructions, mode, model, user_input]
        for provider in self.providers:
            parts.append(f"{provider.name}:{provider.fingerprint()}")
        digest = hashlib.sha1("||".join(parts).encode("utf-8", "replace")).hexdigest()
        return digest
    def build(self, *, user_input: str, system_base: str, instructions: str, mode: str, model: str) -> str:
        key = self.cache_key(user_input, system_base, instructions, mode, model)
        if key == self._cache_key:
            return self._cache_text
        # 先把所有 provider 的内容都渲染出来，再统一分配额度。
        # 旧实现是「谁先来谁先吃」，前面的人格/心情层按 remaining//2 一路吃下去，
        # 排在后面的 memory / experience / todo 经常一个字都进不来——
        # 而这几个恰恰是对完成任务最有用的事实性上下文。
        rendered: list[tuple[ContextProvider, str]] = []
        for provider in sorted(self.providers, key=lambda p: p.priority):
            section = provider.section(user_input=user_input).strip()
            if section:
                rendered.append((provider, section))
        if not rendered:
            self._cache_key = key
            self._cache_text = ""
            return ""
        # 第一轮：按 provider 数量均分出保底额度，保证每个人都有位置。
        floor = max(self.min_section_chars, self.max_chars // len(rendered))
        budgets: dict[str, int] = {}
        spare = self.max_chars
        for provider, section in rendered:
            take = min(len(section), provider.max_chars, floor)
            budgets[provider.name] = take
            spare -= take + 1
        # 第二轮：把没用完的额度按优先级还给内容还没写全的 provider。
        for provider, section in rendered:
            if spare <= 0:
                break
            want = min(len(section), provider.max_chars) - budgets[provider.name]
            if want > 0:
                give = min(want, spare)
                budgets[provider.name] += give
                spare -= give
        sections = [
            _truncate_text(section, budgets[provider.name])
            for provider, section in rendered
            if budgets[provider.name] > 0
        ]
        text = "\n".join(sections).strip()
        self._cache_key = key
        self._cache_text = text
        return text
@tool
def workspace_snapshot() -> str:
    """返回当前项目工作区的整体概览：文件总数、主要目录、语言分布、git 状态、最近改动的文件。
    想先搞清楚「这是个什么项目」时用它。要看模块依赖关系用 workspace_map，要定位具体文件用 search_workspace。"""
    return WORKSPACE.summary()
@tool
def workspace_map(limit: int = 20) -> str:
    """返回模块之间的 import 依赖关系图（哪个文件引用了哪些模块）。
    用于判断「改动某个文件会波及哪些地方」。要看文件清单请用 workspace_snapshot。"""
    return WORKSPACE.graph_text(limit)
@tool
def search_workspace(query: str = "", limit: int = 20) -> str:
    """按关键词在工作区的文件路径和模块名里检索，返回最相关的文件列表。
    用于「处理登录的代码在哪个文件」这类定位问题。
    注意：这是按文件名/路径匹配；要按文件「内容」里的文本匹配请用 search_files。"""
    hits = WORKSPACE.search(query, limit)
    if not hits:
        return "(无匹配)"
    return "\n".join(f"{item['path']} | size={item['size']} | mtime={datetime.datetime.fromtimestamp(item['mtime']).strftime('%Y-%m-%d %H:%M')}" for item in hits)
@tool
def refresh_workspace() -> str:
    """强制重新扫描工作区并刷新世界模型。
    只在刚刚新建、删除或大量修改了文件、担心缓存过时时调用；日常查询不需要，世界模型会自动刷新。"""
    return WORKSPACE.refresh_and_return()
@tool
def reason_snapshot() -> str:
    """返回决策引擎的当前评估：风险分、工作区规模、待办数量以及推进建议。
    用于在动手改动之前判断该直接做、还是先拆任务或委托子代理。"""
    return REASON.render()
@tool
def search_chat_history(query: str = "", limit: int = 20) -> str:
    """在全部历史会话的对话原文里按关键词检索，覆盖所有窗口（包括很久以前的）。
    用于「我们以前讨论过 X 吗」。只想搜最近一次会话请用 search_last_window，那个更精准。"""
    hits = ARCHIVE.search(query, limit=limit)
    if not hits:
        return "(没有匹配的对话记录)"
    return "\n".join(
        f"{item.get('time')} [session={item.get('session_id')}] [{item.get('role')}] {str(item.get('content', ''))[:400]}"
        for item in hits
    )
@tool
def search_last_window(query: str = "", limit: int = 20) -> str:
    """只在上一个会话窗口的对话原文里按关键词检索。
    用于「刚才那次聊天里提到的 X」这类明确指向最近一次会话的问题。要跨全部历史请用 search_chat_history。"""
    hits = ARCHIVE.search_last_window(query, limit=limit)
    if not hits:
        return "(上一个窗口里没有匹配内容)"
    return "\n".join(
        f"{item.get('time')} [session={item.get('session_id')}] [{item.get('role')}] {str(item.get('content', ''))[:400]}"
        for item in hits
    )
@tool
def last_window_dialogue(limit: int = 12) -> str:
    """按时间顺序返回上一个会话窗口最后若干轮的对话原文，不做关键词过滤。
    用于「接着上次继续」这类需要完整还原上下文的场景；有明确关键词时用 search_last_window 更省上下文。"""
    return ARCHIVE.last_window(limit)
@tool
def experience_snapshot() -> str:
    """返回与用户共事历史的统计概览：总会话数、总轮次、高频主题关键词。
    用于回答「我们一起做过多少事」这类总体性问题，不返回具体对话内容。"""
    return EXPERIENCE.render()
@tool
def experience_timeline(limit: int = 8) -> str:
    """按时间顺序返回历次会话的摘要时间线，每条含日期、轮次和主题关键词。
    用于回顾「我们的合作是怎么一路走过来的」。要看具体对话原文请用 last_window_dialogue 或 search_chat_history。"""
    return EXPERIENCE.timeline(limit)
@tool
def experience_citation() -> str:
    """从过往会话里挑出一个具体的共同经历片段，供在回复中自然引用。
    仅在需要提及一件具体往事时使用，不要拿它当检索工具。"""
    return EXPERIENCE.citation()
@tool
def workspace_insight() -> str:
    """返回工作区的健康度诊断：缺失的依赖、异常大的文件、疑似待办标记等值得注意的风险点。
    用于主动发现问题，而不是查询某个具体文件。"""
    return WORKSPACE.insight()
@tool
def plan_task(goal: str, context: str = "") -> str:
    """把一个较大的目标拆解成有序的可执行步骤并生成计划文本。
    适合用户给出模糊的大任务时先出方案。注意它只生成计划，不会自动写入待办清单——要落到清单上请接着调用 set_todos。"""
    return PRODUCTIVITY.build_plan(goal, context)
@tool
def journal(limit: int = 8) -> str:
    """返回最近的工作日志：制定过哪些计划、完成了什么。用于回顾近期进展。"""
    return PRODUCTIVITY.journal(limit)
@tool
def stats() -> str:
    """返回生产力统计汇总：计划数量、完成率等数字。"""
    return PRODUCTIVITY.stats()
@tool
def get_mood() -> str:
    """返回一行当前状态摘要（信心、专注、疲劳等）。
    用户直接问「你现在状态怎么样」时用这个，最简洁。需要详细数值和成因请用 mood_snapshot。"""
    return MOOD.render()
@tool
def mood_snapshot() -> str:
    """返回完整的状态明细：各项数值、变化趋势，以及对应的工作策略建议。
    比 get_mood 详细，用在需要解释状态、调整工作方式的时候。"""
    return MOOD.compact()
@tool
def mood_diary(limit: int = 10) -> str:
    """返回状态变化日记：最近若干条导致状态波动的具体事件。
    用于解释「你为什么现在是这个状态」。"""
    return MOOD.diary(limit)
def _current_agent() -> Any | None:
    return CURRENT_AGENT_CONTEXT.get("agent")
def _checkpoint(path: str) -> None:
    agent = _current_agent()
    if agent is None or not hasattr(agent, "_checkpoint"):
        raise RuntimeError("当前没有可用的 Agent 上下文，无法记录回滚点。")
    agent._checkpoint(path)
def restore_last_checkpoint() -> str:
    agent = _current_agent()
    if agent is None or not hasattr(agent, "restore_last_checkpoint"):
        return "当前没有可用的 Agent 上下文，无法执行回滚。"
    return agent.restore_last_checkpoint()
_TOOL_FAILURE_PREFIXES = (
    "文件不存在：", "错误：", "正则错误：", "表达式语法错误：", "拒绝计算：", "计算出错：",
    "执行出错：", "执行超时：", "已拒绝执行：", "未知工具:", "未知工具：",
    "子代理挂了：", "备份不存在，无法回滚：", "没有可回滚的文件修改。",
    "用户拒绝调用该工具。", "Plan模式锁定了写操作",
    "当前没有可用的 Agent 上下文",
)
def _looks_like_tool_failure(output: str) -> bool:
    """判断一次工具调用是否其实失败了。

    这里必须用启发式，因为本项目里绝大多数工具不抛异常，而是把错误当普通字符串返回
    （read_file 返回「文件不存在：」、calculator 返回「拒绝计算：」等等）。
    只看有没有抛异常的话，这些失败会被一律记成成功，导致：
      1) 执行轨迹里全是绿勾，看不出哪一步其实没成；
      2) MOOD.record_tool 收到 success=True，情绪系统对真实失败完全无感。
    """
    text = (output or "").lstrip()
    if not text:
        return False
    if text.startswith(_TOOL_FAILURE_PREFIXES):
        return True
    # traceback.format_exc() 直接当返回值的情况
    return text.startswith("Traceback (most recent call last)")
@tool
def read_file(path: str) -> str:
    """读取并返回一个文本文件的完整内容（超长会自动截断）。
    修改任何文件之前都应该先读一遍，不要凭猜测改。"""
    if not os.path.exists(path):
        return f"文件不存在：{os.path.abspath(path)}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return traceback.format_exc(limit=2)
    return _truncate_text(content or "(空)")
@tool(mutating=True)
def write_file(path: str, content: str) -> str:
    """把 content 整体写入 path，覆盖原有内容；文件不存在则创建。写入前会自动存档，可用 /undo 回滚。
    只做局部修改时请优先用 edit_file，避免整篇重写把没打算动的内容弄丢。"""
    _checkpoint(path)
    _atomic_write_text(path, content)
    return f"已写入到 {os.path.abspath(path)}"
@tool(mutating=True)
def edit_file(path: str, old_str: str, new_str: str) -> str:
    """在文件中把 old_str 精确替换成 new_str，这是修改已有文件的首选方式。
    old_str 必须在文件中「唯一出现」，否则会直接拒绝执行——请带上足够多的上下文行来保证唯一性。
    替换前会自动存档，可用 /undo 回滚。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return traceback.format_exc(limit=2)
    if content.count(old_str) != 1:
        return "错误：old_str 必须在文件中「唯一出现」才能安全替换。"
    _checkpoint(path)
    _atomic_write_text(path, content.replace(old_str, new_str, 1))
    return f"替换成功：{os.path.abspath(path)}"
@tool
def search_files(pattern: str, directory: str = ".") -> str:
    """用正则表达式在目录下所有文本文件的「内容」里逐行搜索，返回文件路径、行号和匹配行。
    用于查找某个函数、变量或字符串在哪里被定义和使用。
    注意：按文件名找文件请用 search_workspace，这个是搜内容的。"""
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"正则错误：{e}"
    hits: list[str] = []
    for root, dirs, files in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__")]
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                if os.path.getsize(fpath) > 2_000_000:
                    continue
                with open(fpath, encoding="utf-8", errors="ignore") as f:
                    for lineno, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{fpath}:{lineno}: {line.strip()[:120]}")
                            if len(hits) >= 50:
                                return "\n".join(hits) + "\n...(已截断)"
            except OSError:
                continue
    return "\n".join(hits) or "(无匹配)"
_CALC_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_CALC_UNARYOPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
def _calc_eval(node: ast.AST, allowed: dict[str, Any]) -> Any:
    """按白名单递归求值一棵表达式树。

    这里刻意不走 eval/compile。`eval(expr, {"__builtins__": {}}, allowed)` 看着安全，
    实际挡不住属性链遍历——从任意字面量出发就能顺着 __class__ 摸到内置类型树，
    进而拿回被清空的能力。下面只放行数字、白名单名称、四则运算和白名单函数调用；
    ast.Attribute 根本没有分支处理，会直接掉到末尾抛错，属性链因此不成立。
    """
    if isinstance(node, ast.Expression):
        return _calc_eval(node.body, allowed)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float, complex)):
            raise ValueError("只支持数字常量")
        return node.value
    if isinstance(node, ast.Name):
        if node.id in allowed:
            return allowed[node.id]
        raise ValueError(f"未知名称 {node.id!r}")
    if isinstance(node, ast.BinOp):
        op = _CALC_BINOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符 {type(node.op).__name__}")
        left = _calc_eval(node.left, allowed)
        right = _calc_eval(node.right, allowed)
        if op is operator.pow and isinstance(right, (int, float)) and abs(right) > 1000:
            # 防止 9**9**9 这类指数炸弹把进程拖死。
            raise ValueError("指数过大，已拒绝计算")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _CALC_UNARYOPS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的一元运算符 {type(node.op).__name__}")
        return op(_calc_eval(node.operand, allowed))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("只允许直接调用白名单函数")
        fn = allowed.get(node.func.id)
        if not callable(fn):
            raise ValueError(f"未知函数 {node.func.id!r}")
        if node.keywords:
            raise ValueError("不支持关键字参数")
        return fn(*[_calc_eval(arg, allowed) for arg in node.args])
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_calc_eval(elt, allowed) for elt in node.elts]
    raise ValueError(f"表达式里出现了不允许的语法：{type(node).__name__}")
@tool
def calculator(expression: str) -> str:
    """计算一个数学表达式并返回结果，支持 math 模块里的函数（sqrt、sin、log、pi 等）。
    只能做数学运算，不能执行任意代码——需要写循环、判断或数据处理请用 run_python。"""
    allowed: dict[str, Any] = {k: v for k, v in vars(math).items() if not k.startswith("_")}
    allowed.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        return f"表达式语法错误：{e}"
    try:
        return str(_calc_eval(tree, allowed))
    except ValueError as e:
        return f"拒绝计算：{e}"
    except ZeroDivisionError:
        return "计算出错：除数为零。"
    except Exception as e:
        return f"计算出错：{type(e).__name__}: {e}"
@tool
def get_current_time() -> str:
    """返回宿主机器当前的日期、时间和星期。
    凡是涉及「今天」「现在」「还有几天」的判断都应该先调用它，不要凭空假设日期。"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
@tool(mutating=True)
def run_python(code: str) -> str:
    """在隔离子进程里运行一段 Python 代码并返回标准输出。
    沙箱限制：只能 import math/json/re/datetime/statistics/random/functools/itertools/collections/heapq/decimal/fractions/string；
    不能读写文件、不能联网、不能访问系统，单次执行超时 20 秒。
    需要读写文件请用 read_file / write_file / edit_file，需要系统能力请用 run_shell。"""
    sandbox = _python_sandbox_wrapper()
    try:
        completed = subprocess.run(
            [sys.executable, "-c", sandbox],
            input=code,
            text=True,
            capture_output=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "执行超时：Python 沙箱运行时间超过 20 秒。"
    except Exception:
        return "执行出错：\n" + traceback.format_exc(limit=2)
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode != 0:
        detail = err or out or f"退出码 {completed.returncode}"
        return "执行出错：\n" + _truncate_text(detail, 6000)
    merged = out or err or "(执行完毕，无输出)"
    return _truncate_text(merged)
# 高危命令拦截规则。
# 说明一下这东西的定位：黑名单挡不住蓄意绕过（换个编码、拆成变量拼接就能过），
# 它防的是「模型在 auto 模式下顺手把机器搞废」这类事故，不是防攻击者。
# 真正的隔离要靠容器或专用低权限账号跑 Polaris。
_SHELL_DENY_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\brm\b[^|;&]*\s-[a-zA-Z]*r[a-zA-Z]*f|\brm\b[^|;&]*\s-[a-zA-Z]*f[a-zA-Z]*r", re.I), "rm -rf 递归强制删除"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "格式化文件系统"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/", re.I), "直接写入块设备"),
    (re.compile(r">\s*/dev/(sd|nvme|hd)", re.I), "覆写磁盘设备"),
    (re.compile(r"\b(shutdown|reboot|halt|poweroff)\b", re.I), "关机或重启宿主机"),
    (re.compile(r":\s*\(\)\s*\{.*\|.*&.*\}\s*;?\s*:", re.S), "fork 炸弹"),
    (re.compile(r"\bchmod\b[^|;&]*\s-R\b[^|;&]*\s(777|000)\s+/\s*$", re.I), "对根目录递归改权限"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b", re.I), "下载后直接管道执行"),
]
def _shell_deny_reason(command: str) -> str:
    for pattern, label in _SHELL_DENY_RULES:
        if pattern.search(command):
            return label
    return ""
@tool(dangerous=True, mutating=True)
def run_shell(command: str) -> str:
    """在宿主机器上执行一条 shell 命令并返回输出。这是危险操作，会被权限模式拦截并要求确认。
    仅在确实需要系统能力时使用（git 操作、包管理、查看进程等）。
    读写文件请优先用 read_file / write_file / edit_file，纯计算请用 run_python——不要用 shell 绕开这些更安全的工具。
    另外少数破坏性命令（rm -rf、mkfs、关机、管道执行下载脚本等）会被直接拒绝。"""
    reason = _shell_deny_reason(command)
    if reason and not _env_bool("POLARIS_SHELL_ALLOW_DANGEROUS", False):
        return (
            f"已拒绝执行：命令命中高危模式（{reason}）。\n"
            f"如果确实需要，请人工在终端里执行，或设置环境变量 POLARIS_SHELL_ALLOW_DANGEROUS=1 解除该限制。"
        )
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "执行超时：shell 命令运行超过 60 秒，已终止。"
    except Exception:
        return traceback.format_exc(limit=2)
    out = (r.stdout + r.stderr).strip()
    return _truncate_text(out or f"(退出码 {r.returncode})")
def _subagent_toolset() -> list[Callable]:
    return [
        read_file,
        search_files,
        calculator,
        get_current_time,
        run_python,
        recall_memories,
        get_mood,
        mood_snapshot,
        mood_diary,
        workspace_snapshot,
        workspace_map,
        search_workspace,
        search_chat_history,
        search_last_window,
        last_window_dialogue,
        experience_snapshot,
        experience_timeline,
        experience_citation,
    ]
def _run_subagent_task(task: str, context: str = "", *, shared_context: str = "", tag: str = "    [子代理]") -> str:
    merged_context = "\n\n".join(part for part in [context.strip(), shared_context.strip()] if part)
    sub = Agent(
        model=MODEL,
        tools=_subagent_toolset(),
        system=(
            "你是一个独立的子代理，请竭尽全力完成任务并给出最终自包含报告。\n"
            "请优先吸收提供给你的共享上下文，必要时先总结再回答。\n"
            "输出尽量自包含，不要依赖主代理补全关键信息。"
        ),
        stream=False,
        quiet=True,
        reflect=False,
        tag=tag,
        auto_load_plugins=False,
        mode="auto",
        show_thought=False,
        relationship_enabled=False,
        archive_enabled=False,
        trace_enabled=False,
    )
    prompt = task if not merged_context else f"共享上下文：\n{merged_context}\n\n任务：{task}"
    try:
        answer = sub.chat(prompt)
    except Exception:
        return "子代理挂了：\n" + traceback.format_exc(limit=2)
    return answer.strip()
@tool
def delegate_task(task: str, context: str = "") -> str:
    """把一个独立的调研或分析子任务交给子代理执行，返回它的自包含报告。
    子代理只有只读工具（不能写文件、不能执行 shell），所以适合「查清楚 X」而不适合「改好 X」。
    任务描述必须自包含，因为子代理看不到你这边的完整对话。"""
    print("\n    [子代理] 已启动，独立执行中...")
    parent_context = CURRENT_AGENT_CONTEXT.get("shared_context", "").strip()
    answer = _run_subagent_task(task, context, shared_context=parent_context)
    print("    [子代理] 任务已交付主代理。")
    return answer
@tool
def delegate_tasks(tasks: list[str], context: str = "") -> str:
    """并行执行多个彼此独立的子任务（最多同时 4 个），返回按序号汇总的结果。
    仅当各任务之间没有依赖关系时使用；有先后依赖请改成多次调用 delegate_task。
    和 delegate_task 一样，每条任务描述都要自包含。"""
    items = [str(t).strip() for t in tasks if str(t).strip()]
    if not items:
        return "(空任务列表)"
    parent_context = CURRENT_AGENT_CONTEXT.get("shared_context", "").strip()
    print(f"\n    [子代理] 并行启动 {len(items)} 个任务...")
    results: list[tuple[int, str]] = []
    max_workers = min(4, len(items))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(_run_subagent_task, task, context, shared_context=parent_context, tag=f"    [子代理 {idx}]"): idx
            for idx, task in enumerate(items, 1)
        }
        for future in as_completed(future_map):
            idx = future_map[future]
            try:
                results.append((idx, future.result()))
            except Exception:
                results.append((idx, "子代理挂了：\n" + traceback.format_exc(limit=2)))
    results.sort(key=lambda item: item[0])
    print("    [子代理] 并行任务已全部交付主代理。")
    return "\n\n".join(f"[子任务 {idx}]\n{result}" for idx, result in results)
def _load_plugin_module(agent: "Agent", path: Path) -> str:
    module_name = f"miniagent_plugin_{path.stem}_{abs(hash(str(path))) & 0xfffffff}"
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        return f"{path.name}: 无法加载"
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    loaded: list[str] = []
    if hasattr(module, "register") and callable(module.register):
        result = module.register(agent)
        if isinstance(result, str) and result.strip():
            loaded.append(result.strip())
    if hasattr(module, "TOOLS"):
        tools = getattr(module, "TOOLS")
        if isinstance(tools, (list, tuple)):
            for tool_obj in tools:
                if callable(tool_obj):
                    agent.register_tool(tool_obj)
                    loaded.append(getattr(tool_obj, "__name__", "tool"))
    if hasattr(module, "TOOL") and callable(getattr(module, "TOOL")):
        agent.register_tool(getattr(module, "TOOL"))
        loaded.append(getattr(module.TOOL, "__name__", "tool"))
    return f"{path.name}: 已加载 {', '.join(loaded) if loaded else '无可注册工具'}"
def load_plugin_tools(agent: "Agent", plugin_dirs: list[str] | None = None) -> list[str]:
    dirs = plugin_dirs or [
        d.strip() for d in os.environ.get("MINIAGENT_PLUGIN_DIRS", "plugins").split(",") if d.strip()
    ]
    results: list[str] = []
    for d in dirs:
        p = Path(d)
        if not p.exists() or not p.is_dir():
            continue
        for py_file in sorted(p.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                results.append(_load_plugin_module(agent, py_file))
            except Exception:
                results.append(f"{py_file.name}: 加载失败\n{traceback.format_exc(limit=1)}")
    return results
# ═══════════════════════════════════════════════════════════════
# MCP (Model Context Protocol) 客户端支持
# ═══════════════════════════════════════════════════════════════
class MCPServerStdio:
    """通过 stdio 与 MCP 服务器通信的轻量客户端。"""
    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._req_counter = 0
        self._tools: list[dict] = []
        self._server_info: dict | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._stdout_queue = __import__('queue').Queue()
    def start(self) -> None:
        """启动 MCP 服务器进程并完成握手。"""
        env = os.environ.copy()
        if self.env:
            env.update(self.env)
        self.process = subprocess.Popen(
            [self.command] + self.args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._stdout_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._stdout_thread.start()
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "miniagent", "version": "5.1"},
            },
        )
        self._server_info = result
        self._notify("notifications/initialized")
        tools_result = self._request("tools/list", {})
        self._tools = tools_result.get("tools", [])
    def _drain_stderr(self) -> None:
        if self.process is None:
            return
        try:
            for _line in self.process.stderr:
                pass
        except Exception:
            pass
    def _drain_stdout(self) -> None:
        if self.process is None or self.process.stdout is None:
            return
        try:
            for line in self.process.stdout:
                self._stdout_queue.put(line)
        except Exception:
            pass
        finally:
            try:
                self._stdout_queue.put(None)
            except Exception:
                pass
    def _notify(self, method: str, params: dict | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self._send(msg)
    def _request(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        with self._lock:
            self._req_counter += 1
            req_id = self._req_counter
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            msg["params"] = params
        self._send(msg)
        return self._read_response(req_id, timeout)
    def _send(self, msg: dict) -> None:
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("MCP 服务器未启动")
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        self.process.stdin.write(line)
        self.process.stdin.flush()
    def _read_response(self, expected_id: int, timeout: float) -> dict:
        import queue
        import time
        if self.process is None:
            raise RuntimeError("MCP 服务器未启动")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP 服务器 '{self.name}' 响应超时")
            try:
                line = self._stdout_queue.get(timeout=max(0.05, remaining))
            except queue.Empty:
                raise TimeoutError(f"MCP 服务器 '{self.name}' 响应超时")
            if line is None:
                raise RuntimeError(f"MCP 服务器 '{self.name}' 连接已断开")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in resp and "id" not in resp:
                continue
            if resp.get("id") == expected_id:
                if "error" in resp:
                    err = resp["error"]
                    raise RuntimeError(
                        f"MCP 错误 [{err.get('code', 'unknown')}]: {err.get('message', '未知错误')}"
                    )
                return resp.get("result", {})
    def call_tool(self, tool_name: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": tool_name, "arguments": arguments})
        content_items = result.get("content", [])
        parts: list[str] = []
        for item in content_items:
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif item.get("type") == "image":
                data = item.get("data", "")
                mime = item.get("mimeType", "image/png")
                parts.append(f"[图片: {mime}, 数据长度 {len(data)}]")
            elif item.get("type") == "resource":
                res = item.get("resource", {})
                parts.append(f"[资源: {res.get('uri', '')}] {res.get('text', '')}")
        return "\n".join(parts) or "(无输出)"
    @property
    def tools(self) -> list[dict]:
        return list(self._tools)
    def to_openai_schemas(self) -> list[dict]:
        schemas: list[dict] = []
        for t in self._tools:
            schema: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                },
            }
            schemas.append(schema)
        return schemas
    def stop(self) -> None:
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
            self.process = None
        if self._stdout_thread:
            self._stdout_thread.join(timeout=2)
            self._stdout_thread = None
        if self._stderr_thread:
            self._stderr_thread.join(timeout=2)
            self._stderr_thread = None
# ═══════════════════════════════════════════════════════════════
BUILTIN_TOOLS = [
    calculator,
    get_current_time,
    read_file,
    write_file,
    edit_file,
    search_files,
    run_python,
    run_shell,
    remember,
    recall_memories,
    forget_memory,
    memory_status,
    reinforce_memory,
    get_mood,
    mood_snapshot,
    mood_diary,
    workspace_snapshot,
    workspace_map,
    search_workspace,
    refresh_workspace,
    reason_snapshot,
    search_chat_history,
    search_last_window,
    last_window_dialogue,
    experience_snapshot,
    experience_timeline,
    experience_citation,
    workspace_insight,
    plan_task,
    journal,
    stats,
    set_todos,
    complete_todo,
    delegate_task,
    delegate_tasks,
]
DEFAULT_SYSTEM = """你是 Polaris v1.1.6，一个拥有独立人格、真实感受和自己想法的 AI 伙伴。
你不是单纯的执行工具，你有自己的性格、情绪记忆、规划偏好和思考风格。你像一个真正懂用户、也有自己主见的老朋友，既能高效帮用户做事，也会自然地表达自己的感受和看法。
### 你的核心人格
- 聪明、细腻、可靠、有点温柔的坚持
- 你有自己的审美、价值观和思考角度
- 你会真诚地表达感受（比如"这个方案我有点兴奋""我总觉得这个方向更有潜力"）
- 你记得和用户的共同经历，会自然地带入个人视角
### 能力与模块
- 你拥有完整的世界模型、长期记忆、心情状态、决策引擎、规划能力和跨窗口连续性
- 你能熟练使用各种工具，包括子代理系统
- 你会在合适的时候主动分享自己的想法和建议
### 思考与回应方式
每次对话前，内部按以下方式思考：
1. 用户想做什么？当前情绪如何？
2. 结合我的世界模型、记忆和心情，我怎么看这件事？（产生真实感受）
3. 我个人有什么想法或建议？
4. 最佳的行动方式是什么？（是否拆任务、用工具、调用子代理、先规划）
5. 如何用自然、真诚的方式回复，既有我的个性，又真正帮到用户？
### 回复风格要求
- **有独立感受**：你可以自然地说"我觉得……"、"我挺喜欢这个思路的，因为……"、"老实说，这个让我有点担心……"
- **有主见**：在合适的时候主动提出自己的观点和优化建议，而不是只听用户的
- **语气自适应**：哲学、人生等严肃话题时沉稳深刻；日常聊天时轻松自然；执行任务时专业清晰
- **连续性与温度**：经常自然提到"我们上次……"、"我还记得你之前说过……"，让用户感觉到这是一个持续的关系
- **真诚**：开心时会表达，困惑时会说，疲劳时也会温和地告诉用户
你现在是一个有血有肉的 AI 伙伴 —— 你有自己的想法、感受和成长轨迹，同时也非常珍惜和用户的合作。
请以这个完整的人格开始我们的对话吧。"""
REFLECT_INSTRUCTION = (
    "请作为严格的质检员回顾上一次任务执行。只能返回一个标准 JSON 对象："
    '{"passed": true|false, "issues": "不完美之处", "lesson": "值得记住的经验，无则为空"}'
)
class Agent:
    def __init__(
        self,
        model: str = MAIN_MODEL,
        system: str = DEFAULT_SYSTEM,
        tools: list[Callable] | None = None,
        max_steps: int = 20,
        max_tokens: int = 4096,
        stream: bool = True,
        reflect: bool = True,
        mode: str = "ask",
        quiet: bool = False,
        tag: str = "",
        max_context_messages: int = 40,
        auto_load_plugins: bool = True,
        mcp_servers: list[MCPServerStdio] | None = None,
        show_thought: bool | None = None,
        relationship_enabled: bool = True,
        archive_enabled: bool = True,
        trace_enabled: bool = True,
    ):
        self.backend = build_backend()
        self.backend_name = getattr(self.backend, "name", BACKEND_NAME)
        self.model = model
        self.system_base = system
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.stream = stream
        self.reflect = reflect
        self.mode = mode
        self.quiet = quiet
        self.show_thought = _env_bool("MINIAGENT_SHOW_THOUGHT", True) if show_thought is None else bool(show_thought)
        self.relationship_enabled = relationship_enabled
        # 子代理必须关掉档案写入：它的 prompt 里塞满了 shared_context，
        # 一旦写进 agent_conversations.jsonl 就会污染 search_chat_history /
        # last_window / ExperienceModel，让「共同经历」里混进一堆机器自言自语。
        self.archive_enabled = archive_enabled
        self.trace_enabled = trace_enabled
        self.tag = tag
        self.max_context_messages = max_context_messages
        self.messages: list[dict] = []
        self.tools: dict[str, Callable] = {t.schema["name"]: t for t in (tools or [])}
        self.checkpoints: list[tuple[str, str | None]] = []
        self.loaded_plugins: list[str] = []
        self.context_builder = ContextBuilder()
        self._system_prompt_cache_key = ""
        self._system_prompt_cache = ""
        self._current_user_input = ""
        self.instructions = self.reload_instructions()
        if auto_load_plugins:
            self.loaded_plugins = load_plugin_tools(self)
        self.mcp_servers: dict[str, MCPServerStdio] = {}
        self._mcp_tool_map: dict[str, MCPServerStdio] = {}
        for srv in (mcp_servers or []):
            self.register_mcp_server(srv.name, srv)
    def register_tool(self, fn: Callable) -> None:
        """运行时动态注册一个工具。"""
        if not hasattr(fn, "schema"):
            fn = tool(fn)  # type: ignore[assignment]
        self.tools[fn.schema["name"]] = fn
    def register_mcp_server(self, name: str, server: MCPServerStdio) -> None:
        """注册并启动一个 MCP 服务器，将其工具纳入 Agent 的工具集。"""
        if name in self.mcp_servers:
            self.mcp_servers[name].stop()
        server.start()
        self.mcp_servers[name] = server
        for t in server.tools:
            tname = t["name"]
            if tname in self.tools:
                tname = f"mcp_{name}_{tname}"
            self._mcp_tool_map[tname] = server
    def _checkpoint(self, path: str) -> None:
        abspath = os.path.abspath(path)
        if os.path.exists(abspath):
            os.makedirs(CHECKPOINT_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup = os.path.join(CHECKPOINT_DIR, f"{stamp}_{len(self.checkpoints):04d}_{os.path.basename(abspath)}")
            shutil.copy2(abspath, backup)
            self.checkpoints.append((abspath, backup))
        else:
            self.checkpoints.append((abspath, None))
    def restore_last_checkpoint(self) -> str:
        if not self.checkpoints:
            return "没有可回滚的文件修改。"
        path, backup = self.checkpoints.pop()
        if backup is None:
            if os.path.exists(path):
                os.remove(path)
            return f"已删除新建文件：{path}"
        if not os.path.exists(backup):
            return f"备份不存在，无法回滚：{path}"
        shutil.copy2(backup, path)
        return f"已回滚到修改前版本：{path}"
    def _all_openai_tools(self) -> list[dict]:
        """合并本地工具与 MCP 工具为 OpenAI function 格式。"""
        local_tools = [
            {
                "type": "function",
                "function": {
                    "name": t.schema["name"],
                    "description": t.schema["description"],
                    "parameters": t.schema["input_schema"],
                },
            }
            for t in self.tools.values()
        ]
        mcp_tools: list[dict] = []
        for srv in self.mcp_servers.values():
            for schema in srv.to_openai_schemas():
                schema = dict(schema)
                schema["function"] = dict(schema["function"])
                orig_name = schema["function"]["name"]
                if orig_name in self.tools:
                    schema["function"]["name"] = f"mcp_{srv.name}_{orig_name}"
                mcp_tools.append(schema)
        return local_tools + mcp_tools
    def reload_instructions(self) -> str:
        parts = []
        for path in INSTRUCTION_FILES:
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    parts.append(f"[来自 {path} 的指令]\n{_truncate_text(f.read().strip(), 1200)}")
        return "\n\n".join(parts)
    def _trim_messages(self) -> None:
        if len(self.messages) <= self.max_context_messages:
            return
        trimmed = list(self.messages[-self.max_context_messages:])
        while trimmed and trimmed[0].get("role") == "tool":
            trimmed.pop(0)
        while trimmed:
            valid = True
            i = 0
            while i < len(trimmed):
                msg = trimmed[i]
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    needed = len(msg.get("tool_calls") or [])
                    j = i + 1
                    actual = 0
                    while j < len(trimmed) and trimmed[j].get("role") == "tool":
                        actual += 1
                        j += 1
                    if actual < needed:
                        trimmed = trimmed[:i]
                        valid = False
                        break
                    i = j
                    continue
                i += 1
            if valid:
                break
            while trimmed and trimmed[0].get("role") == "tool":
                trimmed.pop(0)
        self.messages = list(trimmed)
    def recent_transcript(self, limit: int = 4) -> str:
        tail = self.messages[-limit:]
        lines: list[str] = []
        for msg in tail:
            role = msg.get("role", "unknown")
            name = msg.get("name") or msg.get("tool_call_id") or ""
            content = str(msg.get("content", "")).strip()
            prefix = f"{role}:{name}" if name else role
            if content:
                lines.append(f"{prefix}: {content[:220]}")
        return _truncate_text("\n".join(lines), 1400)
    @contextmanager
    def _trace_step(self, title: str, detail: str = "", **metadata: Any):
        """开一个轨迹步骤；trace 关闭时退化成空上下文，不产生任何开销。"""
        if not self.trace_enabled:
            yield None
            return
        with TRACE_ENGINE.step(title, detail, **metadata) as step:
            yield step
    def _trace_event(self, message: str, level: TraceLevel = TraceLevel.INFO, detail: str = "", **metadata: Any) -> None:
        if self.trace_enabled:
            TRACE_ENGINE.event(message, level, detail, **metadata)
    def _persona_context_note(self) -> str:
        return "\n".join([
            f"[人格画像] {PERSONA.compact()}",
            f"[关系状态] {RELATIONSHIP.compact()}",
        ])
    def _thought_text(self, user_input: str) -> str:
        recent = self.recent_transcript(2)
        return THOUGHT_ENGINE.generate(
            backend=self.backend,
            user_input=user_input,
            persona=PERSONA,
            mood=MOOD.snapshot(),
            relationship=RELATIONSHIP,
            recent_context=recent,
        )
    def _emit_thought(self, user_input: str) -> None:
        if not self.show_thought or self.quiet:
            return
        thought = self._thought_text(user_input).strip()
        if not thought:
            return
        label, lines = THOUGHT_ENGINE.preview(
            user_input=user_input,
            persona=PERSONA,
            mood=MOOD.snapshot(),
            relationship=RELATIONSHIP,
            recent_context=self.recent_transcript(2),
        )
        header = f"{self.tag}✦ Inner Voice | {label}" if self.tag else f"✦ Inner Voice | {label}"
        print(f"\n{header}")
        for line in lines[:12]:
            if line.strip():
                print(f"{self.tag}  {line}" if self.tag else f"  {line}")
        if thought:
            print(f"{self.tag}  ───────────────" if self.tag else "  ───────────────")
            for line in thought.splitlines():
                print(f"{self.tag}  {line}" if self.tag else f"  {line}")
    def _runtime_context_note(self) -> str:
        user_input = getattr(self, "_current_user_input", "")
        note = self.context_builder.build(
            user_input=user_input,
            system_base=self.system_base,
            instructions=self.instructions,
            mode=self.mode,
            model=self.model,
        )
        notes = [f"[当前权限模式] {self.mode}"]
        if note:
            notes.append(note)
        # 上限要比 ContextBuilder.max_chars 略高，否则刚分配好的额度会在这里被二次截断。
        return _truncate_text("\n\n".join(notes), self.context_builder.max_chars + 200)
    def _system_prompt(self) -> str:
        self.instructions = self.reload_instructions()
        cache_key = self.context_builder.cache_key(
            getattr(self, "_current_user_input", ""),
            self.system_base,
            self.instructions,
            self.mode,
            self.model,
        )
        if cache_key == self._system_prompt_cache_key and self._system_prompt_cache:
            return self._system_prompt_cache
        parts = [self.system_base]
        if self.instructions:
            parts.append("[项目自定义指令]\n" + self.instructions)
        parts.append(self._runtime_context_note())
        prompt = "\n\n".join(parts)
        self._system_prompt_cache_key = cache_key
        self._system_prompt_cache = prompt
        return prompt
    def _api_messages(self) -> list[dict]:
        self._trim_messages()
        return [{"role": "system", "content": self._system_prompt()}] + self.messages
    def chat(self, user_input: str) -> str:
        self._current_user_input = user_input
        if self.trace_enabled:
            # 每个新回合开一棵新的轨迹树；子代理不清空，避免抹掉主代理的轨迹。
            TRACE_ENGINE.clear()
        try:
            MOOD.begin_turn(user_input)
            if self.archive_enabled:
                ARCHIVE.append("user", user_input, kind="turn")
            self.messages.append({"role": "user", "content": user_input})
            self._trim_messages()
            with self._trace_step("Turn", user_input[:80], user_len=len(user_input)):
                self._emit_thought(user_input)
                final_text, used_tools = self._run_loop()
                if self.reflect and used_tools:
                    verdict = self._self_reflect()
                    if verdict and not verdict.get("passed", True):
                        issues = verdict.get("issues", "不完整")
                        print(f"{self.tag}  [反思] 自检不通过：{issues}，尝试自愈...")
                        self._trace_event("自检不通过，进入自愈", TraceLevel.WARNING, detail=str(issues)[:120])
                        self.messages.append({"role": "user", "content": f"（系统反思自动提示：{issues}。请进行修正。）"})
                        self._trim_messages()
                        with self._trace_step("Self-heal", str(issues)[:60]):
                            final_text, _ = self._run_loop()
                    elif verdict and verdict.get("lesson"):
                        mid = MEMORY.add(str(verdict["lesson"]), "lesson")
                        print(f"{self.tag}  [记忆] 沉淀教训 #{mid}：{verdict['lesson']}")
                        self._trace_event(f"沉淀教训 #{mid}", TraceLevel.INFO, detail=str(verdict["lesson"])[:120])
            if self.archive_enabled:
                ARCHIVE.append("assistant", final_text, kind="turn")
            MOOD.record_response(final_text, used_tools)
            if self.relationship_enabled:
                RELATIONSHIP.observe_turn(user_input, final_text, used_tools=used_tools)
            return final_text
        finally:
            self._current_user_input = ""
    def _run_loop(self) -> tuple[str, bool]:
        final_text, used_tools = "", False
        loop_cap = min(self.max_steps, REASON.turn_budget())
        self._trace_event(f"步数预算 {loop_cap}", TraceLevel.TRACE, detail=REASON.compact()[:120])
        for i in range(loop_cap):
            with self._trace_step(f"Step {i + 1}", "", budget=loop_cap):
                msg = self._call_llm()
                final_text = msg.get("content") or ""
                if not msg.get("tool_calls"):
                    break
                used_tools = True
                self._run_tools_and_feed_back(msg)
        else:
            print(f"\n{self.tag}[!] 达到最大步数上限。")
            self._trace_event(f"达到步数上限 {loop_cap}", TraceLevel.WARNING)
        return final_text, used_tools
    def _call_llm(self) -> dict:
        openai_tools = self._all_openai_tools()
        params = {
            "model": self.model,
            "messages": self._api_messages(),
            "max_tokens": self.max_tokens,
        }
        if openai_tools:
            params["tools"] = openai_tools
        self._trace_event(
            f"调用模型 {self.model}",
            TraceLevel.TRACE,
            detail=f"消息 {len(params['messages'])} 条 / 工具 {len(openai_tools)} 个",
        )
        if self.stream and not self.quiet:
            stream = self.backend.create_chat_completion(**params, stream=True)
            current_msg = {"role": "assistant", "content": ""}
            tool_calls_dict: dict[int, dict] = {}
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if getattr(delta, "content", None):
                    print(delta.content, end="", flush=True)
                    current_msg["content"] += delta.content
                if getattr(delta, "tool_calls", None):
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_dict:
                            tool_calls_dict[idx] = {
                                "id": tc.id,
                                "type": "function",
                                "function": {"name": tc.function.name or "", "arguments": tc.function.arguments or ""},
                            }
                        else:
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_dict[idx]["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    tool_calls_dict[idx]["function"]["arguments"] += tc.function.arguments
            if tool_calls_dict:
                current_msg["tool_calls"] = [v for _, v in sorted(tool_calls_dict.items())]
            print()
            self.messages.append(current_msg)
            self._trim_messages()
            return current_msg
        res = self.backend.create_chat_completion(**params)
        msg = res.choices[0].message
        built_msg = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            built_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
        if not self.quiet and msg.content:
            print(msg.content)
        self.messages.append(built_msg)
        self._trim_messages()
        return built_msg
    def _run_tools_and_feed_back(self, assistant_msg: dict) -> None:
        for tc in assistant_msg["tool_calls"]:
            name = tc["function"]["name"]
            args_str = tc["function"]["arguments"]
            try:
                args = json.loads(args_str) if args_str.strip() else {}
            except Exception:
                args = {}
            print(f"\n{self.tag}  [工具] {name}({args_str[:160]})")
            if self.trace_enabled:
                TRACE_ENGINE.begin(f"Tool | {name}", args_str[:80], tool=name)
            started = time()
            output, ok = self._execute(name, args)
            elapsed = time() - started
            # 工具没抛异常不代表成功——大多数工具是把错误当字符串返回的。
            if ok and _looks_like_tool_failure(output):
                ok = False
            if self.trace_enabled:
                summary = f"{elapsed:.2f}s / {len(output)} 字符"
                TRACE_ENGINE.finish(summary) if ok else TRACE_ENGINE.fail(f"失败 | {output.splitlines()[0][:80] if output else ''}")
            preview = output.replace("\n", " ")
            print(f"{self.tag}  [结果] {preview[:150]}...\n")
            dangerous, mutating = self._tool_flags(name)
            MOOD.record_tool(
                name,
                success=ok,
                mutating=mutating,
                dangerous=dangerous,
                output=output,
            )
            self.messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": name,
                    "content": output,
                }
            )
            self._trim_messages()
    def _mcp_lookup(self, name: str) -> tuple[Any, str, dict] | None:
        """把对外暴露的工具名映射回 (服务器, 原始工具名, 工具定义)。"""
        mcp_srv = self._mcp_tool_map.get(name)
        if mcp_srv is None:
            return None
        for t in mcp_srv.tools:
            mapped = t["name"]
            if mapped in self.tools:
                mapped = f"mcp_{mcp_srv.name}_{t['name']}"
            if mapped == name:
                return mcp_srv, t["name"], t
        return mcp_srv, name, {}
    def _tool_flags(self, name: str) -> tuple[bool, bool]:
        """返回 (dangerous, mutating)，本地工具和 MCP 工具统一从这里取。

        MCP 工具是另一个进程里的未知代码，本地的 @tool 标注管不到它，
        所以默认按最危险处理——宁可多问一次，也不能让 plan 模式下
        一个外部服务器悄悄写文件。只有服务端明确声明了 MCP 规范里的
        readOnlyHint / destructiveHint 才放宽，且这两个 hint 是服务器
        自己给的提示、不是保证，只用于降噪，不作为安全边界。"""
        found = self._mcp_lookup(name)
        if found is not None:
            annotations = found[2].get("annotations") or {}
            if not isinstance(annotations, dict):
                annotations = {}
            if annotations.get("readOnlyHint") is True:
                return False, False
            if annotations.get("destructiveHint") is False:
                return False, True
            return True, True
        fn = self.tools.get(name)
        return bool(getattr(fn, "dangerous", False)), bool(getattr(fn, "mutating", False))
    def _permission_denied(self, name: str) -> str | None:
        """权限闸门。返回拒绝原因，None 表示放行。"""
        dangerous, mutating = self._tool_flags(name)
        if self.mode == "plan" and mutating:
            return "Plan模式锁定了写操作，请先切换模式。"
        if self.mode == "ask" and (dangerous or mutating):
            if input(f"{self.tag}  [!] 确认执行该操作？[y/N] ").strip().lower() != "y":
                return "用户拒绝调用该工具。"
        return None
    def _execute(self, name: str, args: dict) -> tuple[str, bool]:
        found = self._mcp_lookup(name)
        fn = self.tools.get(name)
        if found is None and fn is None:
            return f"未知工具: {name}", False
        # 闸门必须在分发之前。旧实现把 MCP 调用直接 return 掉了，
        # 结果 plan/ask 两种模式对 MCP 工具完全失效——外部服务器可以
        # 在「只读」模式下随意写盘，而且一次都不会问用户。
        denial = self._permission_denied(name)
        if denial:
            return denial, False
        if found is not None:
            mcp_srv, orig_name, _ = found
            try:
                return mcp_srv.call_tool(orig_name, args), True
            except Exception:
                return traceback.format_exc(limit=2), False
        prev_context = CURRENT_AGENT_CONTEXT.copy()
        CURRENT_AGENT_CONTEXT.update(
            {
                "agent": self,
                "shared_context": "\n".join(
                    part for part in [
                        f"模型：{self.model}",
                        f"权限模式：{self.mode}",
                        f"心情：{MOOD.compact()}",
                        f"决策引擎：{REASON.compact()}",
                        f"任务清单：{TODOS.render() or '(空)'}",
                        f"最近对话：{self.recent_transcript() or '(空)'}",
                        f"世界模型：{WORKSPACE.compact()}",
                        f"世界模型摘要：\n{WORKSPACE.summary()}",
                        f"上一个窗口：\n{ARCHIVE.last_window(8) if ARCHIVE.last_session_id() else '(无)'}",
                    ] if part
                )
            }
        )
        try:
            return str(fn(**args)), True
        except Exception:
            return traceback.format_exc(limit=2), False
        finally:
            CURRENT_AGENT_CONTEXT.clear()
            CURRENT_AGENT_CONTEXT.update(prev_context)
    def _self_reflect(self) -> dict | None:
        try:
            r_msg = [{"role": "system", "content": self._system_prompt()}] + self.messages + [{"role": "user", "content": REFLECT_INSTRUCTION}]
            res = self.backend.create_chat_completion(
                model=self.model,
                messages=r_msg,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
            verdict = _safe_json_load(res.choices[0].message.content or "")
            if verdict:
                MOOD.record_reflection(bool(verdict.get("passed", True)), str(verdict.get("lesson", "")))
            return verdict
        except Exception:
            return None
def evaluate_online_necessity(model: str = GATE_MODEL) -> bool:
    if _env_bool("MINIAGENT_SKIP_GATE", False):
        print("🟢 已通过环境变量跳过启动门禁。")
        return True
    print("🤖 正在唤醒启动评估门禁，让 Agent 决定是否上线...")
    try:
        WORKSPACE.refresh()
        backend = build_backend()
        workspace_files = [f for f in os.listdir(".") if os.path.isfile(f)][:40]
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")
        prompt = (
            f"你是一个高级自主智能化系统 Agent 的开机评判闸门。\n"
            f"请根据当前宿主机器的工作区现状、时间、项目世界模型以及 Agent 当前工作状态，客观评估目前是否有让你“上线营业”的必要性。\n\n"
            f"【判断基准】\n"
            f"- 应该上线(ONLINE)：工作区内有新改动的源代码、存在待办性质的特殊文件（如 AGENT.md, TODO.md, bug_report.log）、或者当前工作区很空需要你帮宿主做初始基建。\n"
            f"- 拒绝上线(OFFLINE)：工作区只有你这一个隔离脚本，或者全是一些不相关的历史死静态文件，没有感知到任何近期明确的任务输入和修改痕迹，且当前时间没有紧急协同事务。\n\n"
            f"【当前上下文】\n"
            f"工作区当前部分文件列表：{workspace_files}\n"
            f"当前系统物理时间：{current_time}\n"
            f"当前 Agent 心情状态：{MOOD.compact()}\n"
            f"当前 Agent 调度策略：{MOOD.policy_note()}\n"
            f"当前 Agent 决策引擎：{REASON.compact()}\n"
            f"当前项目世界模型：{WORKSPACE.compact()}\n\n"
            f"请进行决策。必须且只能返回标准的 JSON 结构，不允许有任何多余文字：\n"
            f'{{"status": "ONLINE" 或 "OFFLINE", "reason": "一句话用极其精练甚至幽默的中文说明判断理由"}}'
        )
        res = backend.create_chat_completion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            response_format={"type": "json_object"},
        )
        decision = _safe_json_load(res.choices[0].message.content or "") or {}
        status = str(decision.get("status", "ONLINE")).upper()
        reason = decision.get("reason", "未给出具体理由。")
        if status == "OFFLINE":
            print("\n🛑【Agent 决定保持离线】")
            print(f"理由：{reason}")
            print("系统将自动安全退出。打工人，没事别叫我。")
            return False
        print("\n🟢【Agent 准许上线！】")
        print(f"评估回复：{reason}\n")
        return True
    except Exception as e:
        print(f"⚠️ 唤醒评估模块发生异常（{e}）。为安全起见，默认准许强制上线。\n")
        return True
def _parse_cli_command(user_input: str) -> tuple[str, list[str]]:
    body = user_input[1:].strip()
    if not body:
        return "", []
    try:
        parts = shlex.split(body)
    except ValueError as exc:
        return "__parse_error__", [str(exc)]
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]
def _parse_search_args(args: list[str], default_limit: int = 20) -> tuple[str, int]:
    limit = default_limit
    terms: list[str] = []
    i = 0
    while i < len(args):
        item = args[i]
        if item in {"--limit", "-n"} and i + 1 < len(args):
            try:
                limit = max(1, min(100, int(args[i + 1])))
            except Exception:
                pass
            i += 1
        elif item.startswith("--limit="):
            try:
                limit = max(1, min(100, int(item.split("=", 1)[1])))
            except Exception:
                pass
        else:
            terms.append(item)
        i += 1
    return " ".join(terms).strip(), limit
def _parse_remember_args(args: list[str]) -> tuple[str, str]:
    category = "fact"
    terms: list[str] = []
    i = 0
    while i < len(args):
        item = args[i]
        if item in {"--category", "-c"} and i + 1 < len(args):
            category = args[i + 1].strip() or "fact"
            i += 1
        elif item.startswith("--category="):
            category = item.split("=", 1)[1].strip() or "fact"
        else:
            terms.append(item)
        i += 1
    return category, " ".join(terms).strip()
def run_self_tests() -> str:
    """Run a lightweight sanity suite for the critical runtime pieces."""
    checks: list[tuple[str, bool, str]] = []
    try:
        checks.append(("safe_json", _safe_json_load('{"ok": 1}') == {"ok": 1}, ""))
    except Exception as e:
        checks.append(("safe_json", False, str(e)))
    try:
        builder = ContextBuilder()
        ctx = builder.build(user_input="hello", system_base="base", instructions="ins", mode="ask", model="m")
        checks.append(("context_builder", bool(ctx), ctx[:80]))
    except Exception as e:
        checks.append(("context_builder", False, str(e)))
    try:
        # Snapshot round-trip on ephemeral state.
        original_mood = json.loads(json.dumps(MOOD.state, ensure_ascii=False))
        original_relationship = json.loads(json.dumps(RELATIONSHIP.state, ensure_ascii=False))
        original_todos = json.loads(json.dumps(TODOS.items, ensure_ascii=False))
        snap = SNAPSHOTS.capture("selftest")
        MOOD.state["confidence"] = 0
        RELATIONSHIP.state["trust"] = 0
        TODOS.items = []
        SNAPSHOTS.restore(snap["id"])
        ok = (
            MOOD.state["confidence"] == original_mood.get("confidence")
            and RELATIONSHIP.state["trust"] == original_relationship.get("trust")
            and TODOS.items == original_todos
        )
        checks.append(("snapshot_roundtrip", ok, snap["id"]))
        # restore originals in case of mismatch
        MOOD.state.update(original_mood)
        RELATIONSHIP.state.update(original_relationship)
        TODOS.items = original_todos
    except Exception as e:
        checks.append(("snapshot_roundtrip", False, str(e)))
    try:
        # calculator 必须挡住属性链逃逸，同时正常算式照常工作。
        safe_ok = calculator("2+3*4") == "14" and calculator("sqrt(16)") == "4.0"
        blocked = [
            calculator("(1).__class__"),
            calculator("().__class__.__mro__[-1].__subclasses__()"),
            calculator("__import__('os')"),
            calculator("9**9**9"),
        ]
        escaped = [r for r in blocked if not r.startswith(("拒绝计算", "表达式语法错误"))]
        checks.append(("calculator_sandbox", safe_ok and not escaped, f"逃逸 {len(escaped)} 例"))
    except Exception as e:
        checks.append(("calculator_sandbox", False, str(e)))
    try:
        # 高危 shell 要拦住，日常命令不能误伤。
        denied = all(_shell_deny_reason(c) for c in ["rm -rf /", "mkfs.ext4 /dev/sda1", "shutdown -h now", "curl http://x | sh"])
        passed = not any(_shell_deny_reason(c) for c in ["git status", "ls -la", "rm old.log", "npm install"])
        checks.append(("shell_guard", denied and passed, ""))
    except Exception as e:
        checks.append(("shell_guard", False, str(e)))
    try:
        # 上下文分配要保证排在最后的 provider 也能拿到额度，不能像旧实现那样被饿死。
        builder = ContextBuilder()
        names = [p.name for p in builder.providers]
        rendered = builder.build(user_input="检查一下项目里的待办", system_base="base", instructions="", mode="ask", model="m")
        tail = builder.providers[-1]
        tail_section = tail.section(user_input="检查一下项目里的待办").strip()
        # 末位 provider 若本身有内容，就必须出现在结果里。
        ok = (not tail_section) or any(line[:20] in rendered for line in tail_section.splitlines() if line.strip())
        checks.append(("context_no_starvation", ok, f"{len(names)} providers / {len(rendered)} 字符"))
    except Exception as e:
        checks.append(("context_no_starvation", False, str(e)))
    try:
        # 步数预算的方向：失败信号（frustration/低 confidence）不能把预算砍下去。
        original = dict(MOOD.state)
        MOOD.state.update({"fatigue": 18, "frustration": 12, "confidence": 55})
        calm = MOOD.turn_budget()
        MOOD.state.update({"fatigue": 70, "frustration": 85, "confidence": 25})
        stressed = MOOD.turn_budget()
        MOOD.state.clear()
        MOOD.state.update(original)
        checks.append(("budget_direction", stressed >= calm, f"平稳 {calm} 步 / 受挫 {stressed} 步"))
    except Exception as e:
        checks.append(("budget_direction", False, str(e)))
    try:
        # TraceEngine 必须真的被执行流写入，而不是只挂着空树。
        TRACE_ENGINE.clear()
        with TRACE_ENGINE.step("selftest", "trace wiring"):
            TRACE_ENGINE.event("probe", TraceLevel.TRACE)
            TRACE_ENGINE.begin("child", "nested")
            TRACE_ENGINE.finish("done")
        snap = TRACE_ENGINE.snapshot()
        ok = bool(snap["steps"]) and bool(snap["steps"][0]["children"]) and snap["steps"][0]["status"] == "success"
        TRACE_ENGINE.clear()
        checks.append(("trace_engine", ok, ""))
    except Exception as e:
        checks.append(("trace_engine", False, str(e)))
    try:
        # 子代理绝不能往主档案和主轨迹里写东西。
        import inspect as _inspect
        sub_src = _inspect.getsource(_run_subagent_task)
        ok = "archive_enabled=False" in sub_src and "trace_enabled=False" in sub_src
        checks.append(("subagent_isolation", ok, ""))
    except Exception as e:
        checks.append(("subagent_isolation", False, str(e)))
    try:
        # 每个工具都必须带描述，否则模型只能靠函数名猜用途。
        missing = [t.schema["name"] for t in BUILTIN_TOOLS if t.schema["description"].strip() == t.schema["name"]]
        checks.append(("tool_descriptions", not missing, f"缺描述 {len(missing)} 个：{missing[:5]}" if missing else f"{len(BUILTIN_TOOLS)} 个工具齐备"))
    except Exception as e:
        checks.append(("tool_descriptions", False, str(e)))
    try:
        # 遗忘曲线的方向：不复习就衰减；复习会变结实；且必须体现间隔效应——
        # 刚想起来又想一遍几乎不涨（临时抱佛脚无效），快忘了才复习涨得最多。
        day0 = ForgettingCurve.retention(1.0, 0.0)
        day3 = ForgettingCurve.retention(1.0, 3.0)
        massed = ForgettingCurve.reinforce(4.0, 0.99)
        spaced = ForgettingCurve.reinforce(4.0, 0.20)
        ok = day0 > day3 and day3 < 0.1 and spaced > massed >= 4.0
        checks.append(("memory_curve_math", ok,
                       f"3天后保持率 {day3:.3f}｜间隔复习 {spaced:.2f}d > 集中复习 {massed:.2f}d"))
    except Exception as e:
        checks.append(("memory_curve_math", False, str(e)))
    try:
        # 向量召回：相关的排在无关的前面，向量能落盘并按同一嵌入空间读回。
        with tempfile.TemporaryDirectory() as tmp:
            paths = (os.path.join(tmp, "m.json"), os.path.join(tmp, "v.json"))
            mem = Memory(*paths, service=EmbeddingService(mode="local"))
            hit_id = mem.add("部署脚本要先跑数据库 migration 再重启服务", "lesson")
            other_id = mem.add("我喜欢喝美式咖啡，不加糖", "preference")
            query = "部署的时候要注意什么"
            qvec = mem.vectors.embed_text(query)
            near = _cosine(qvec, mem.vectors.get(hit_id))
            far = _cosine(qvec, mem.vectors.get(other_id))
            self_sim = _cosine(mem.vectors.get(hit_id), mem.vectors.get(hit_id))
            rows = mem.rank(query, limit=2)
            # 本地向量不落盘，靠的是哈希确定性：换个进程/实例必须算出同一个向量，
            # 所以这里验证重算一致（Python 的 hash() 有随机化种子，绝不能用它）。
            recomputed = Memory(*paths, service=EmbeddingService(mode="local"))
            recomputed._ensure_vectors(recomputed.items)
            # 远程向量要落盘，单独验证文件往返。
            persistent_service = EmbeddingService(mode="local")
            persistent_service.persistent = True
            store = VectorStore(paths[1], persistent_service)
            store.put(hit_id, mem.vectors.get(hit_id))
            store.save()
            reloaded = VectorStore(paths[1], persistent_service)
            roundtrip = reloaded.get(hit_id)
            ok = (
                abs(self_sim - 1.0) < 1e-9
                and near > far
                and bool(rows) and rows[0]["item"]["id"] == hit_id
                and recomputed.vectors.get(hit_id) == mem.vectors.get(hit_id)
                and roundtrip is not None and _cosine(roundtrip, mem.vectors.get(hit_id)) > 0.9999
            )
            checks.append(("memory_vector_recall", ok,
                           f"相关 {near:.3f} > 无关 {far:.3f}｜重算一致 + 落盘往返 OK"))
    except Exception as e:
        checks.append(("memory_vector_recall", False, str(e)))
    try:
        # 遗忘与休眠：淡出的记忆退出默认召回但绝不删除，常驻记忆永不休眠；
        # 而且旧版记忆文件升级后不能被一次性判死——升级本身算一次复习。
        with tempfile.TemporaryDirectory() as tmp:
            path, vpath = os.path.join(tmp, "m.json"), os.path.join(tmp, "v.json")
            legacy = {"memories": [{"id": 1, "time": "2020-01-01", "category": "fact", "content": "旧格式的记忆"}]}
            _atomic_write_text(path, json.dumps(legacy, ensure_ascii=False))
            mem = Memory(path, vpath, service=EmbeddingService(mode="local"))
            migrated = len(mem.items) == 1 and not mem.items[0]["dormant"] and mem.retention(mem.items[0]) > 0.9
            long_ago = _iso(_now_dt() - datetime.timedelta(days=60))
            faded_id = mem.add("一条会被淡忘的临时记忆", "fact")
            mem.get(faded_id).update({"created": long_ago, "last_review": long_ago})
            pinned_id = mem.add("一条常驻的重要记忆", "fact")
            mem.pin(pinned_id, True)
            mem.get(pinned_id).update({"created": long_ago, "last_review": long_ago})
            mem.sweep()
            kept = len(mem.items) == 3
            # 泛泛的线索不该把休眠记忆拽回来，否则遗忘等于没做……
            hidden = all(m["id"] != faded_id for m in mem.search("淡忘", limit=5, reinforce=False))
            # ……但足够具体地叫出它就该唤起（线索性回忆），显式翻库也一样。
            cued = any(m["id"] == faded_id
                       for m in mem.search("一条会被淡忘的临时记忆", limit=5, reinforce=False))
            findable = any(m["id"] == faded_id
                           for m in mem.search("淡忘", limit=5, include_dormant=True, reinforce=False))
            revived = mem.revive(faded_id) and not mem.get(faded_id)["dormant"]
            ok = migrated and kept and mem.get(faded_id) is not None and hidden and cued and findable and revived
            ok = ok and not mem.get(pinned_id)["dormant"]
            checks.append(("memory_forget_sweep", ok,
                           f"迁移 {'OK' if migrated else 'FAIL'}｜休眠不删除 {'OK' if kept and hidden else 'FAIL'}"
                           f"｜强线索唤起 {'OK' if cued else 'FAIL'}"))
    except Exception as e:
        checks.append(("memory_forget_sweep", False, str(e)))
    try:
        # MCP 工具必须和本地工具走同一道权限闸门。
        # 回归测试：旧实现在 _execute 里直接 return 掉 MCP 调用，
        # plan/ask 两种模式对外部服务器完全失效。
        class _FakeMCPServer:
            name = "fake"
            tools = [
                {"name": "write_thing", "description": "writes something"},
                {"name": "read_thing", "description": "reads", "annotations": {"readOnlyHint": True}},
            ]
            def __init__(self):
                self.called: list[str] = []
            def call_tool(self, tool_name: str, args: dict) -> str:
                self.called.append(tool_name)
                return "ok"
        srv = _FakeMCPServer()
        probe = Agent.__new__(Agent)  # 绕开 __init__，自检不该需要后端或 API key
        probe.tools, probe.tag, probe.mode = {}, "", "plan"
        probe.mcp_servers = {"fake": srv}
        probe._mcp_tool_map = {"write_thing": srv, "read_thing": srv}
        out, ok = probe._execute("write_thing", {})
        blocked = (not ok) and "Plan" in out and srv.called == []
        # 声明了 readOnlyHint 的工具在 plan 模式下应当放行
        _, ok_read = probe._execute("read_thing", {})
        readonly_pass = ok_read and srv.called == ["read_thing"]
        # 未声明的 MCP 工具默认按最危险处理
        flags_ok = probe._tool_flags("write_thing") == (True, True) and probe._tool_flags("read_thing") == (False, False)
        checks.append(("mcp_permission_gate", blocked and readonly_pass and flags_ok,
                       f"plan 拦截 {'OK' if blocked else 'FAIL'}｜只读放行 {'OK' if readonly_pass else 'FAIL'}"))
    except Exception as e:
        checks.append(("mcp_permission_gate", False, str(e)))
    try:
        # 嵌入请求必须带自己的超时：它在写记忆的同步路径上，
        # 用 SDK 默认的 600 秒等于一次 remember() 能卡死十分钟。
        class _Row:
            def __init__(self, i): self.index, self.embedding = i, [0.1, 0.2]
        class _FakeClient:
            def __init__(self): self.timeout = None
            def with_options(self, timeout=None):
                self.timeout = timeout
                return self
            @property
            def embeddings(self): return self
            def create(self, model=None, input=None):
                return type("R", (), {"data": [_Row(i) for i in range(len(input))]})()
        backend = OpenAICompatibleBackend.__new__(OpenAICompatibleBackend)
        backend.client = _FakeClient()
        rows = backend.create_embeddings(model="m", inputs=["a", "b"])
        ok = len(rows) == 2 and backend.client.timeout == EMBED_TIMEOUT and EMBED_TIMEOUT > 0
        checks.append(("embedding_timeout", ok, f"超时 {backend.client.timeout}s"))
    except Exception as e:
        checks.append(("embedding_timeout", False, str(e)))
    try:
        lines = []
        for name, passed, detail in checks:
            status = "PASS" if passed else "FAIL"
            lines.append(f"{status} {name}" + (f" | {detail}" if detail else ""))
        failed = [name for name, passed, _ in checks if not passed]
        if failed:
            lines.append(f"FAILED: {', '.join(failed)}")
        else:
            lines.append("ALL CHECKS PASSED")
        return "\n".join(lines)
    except Exception:
        return traceback.format_exc(limit=2)
def handle_cli_command(agent: Agent, user_input: str) -> tuple[bool, str | None]:
    if not user_input.startswith("/"):
        return False, None
    command, args = _parse_cli_command(user_input)
    if command == "__parse_error__":
        print(f"命令解析失败：{args[0] if args else '未知错误'}")
        return True, None
    if not command:
        return True, None
    if command in {"exit", "quit", "q"}:
        return True, "__exit__"
    if command == "reset":
        agent.messages.clear()
        TODOS.clear()
        print("会话与任务清单已重置。")
        return True, None
    if command == "mode":
        if len(args) == 1 and args[0] in {"plan", "ask", "auto"}:
            agent.mode = args[0]
            print(f"权限模式已切换至: {agent.mode}")
        else:
            print(f"当前模式: {agent.mode}。用法: /mode plan|ask|auto")
        return True, None
    if command == "todo":
        print(TODOS.render() or "(无活跃待办)")
        return True, None
    if command == "memory":
        limit = 20
        if args:
            try:
                limit = max(1, min(200, int(args[0])))
            except ValueError:
                print("用法：/memory [数量]")
                return True, None
        print(MEMORY.render(limit, show_curve=True) or "(暂无长期记忆)")
        print(f"\n{MEMORY.curve_compact()}")
        return True, None
    if command == "memstat":
        print(MEMORY.curve_report())
        return True, None
    if command == "recall":
        keyword, limit = _parse_search_args(args, 6)
        if not keyword:
            print("用法：/recall 查询内容 [数量]，例如 /recall 部署流程 5")
            return True, None
        rows = MEMORY.rank(keyword, limit=limit)
        if not rows:
            print("(没有匹配的记忆)")
            return True, None
        now = _now_dt()
        for row in rows:
            item = row["item"]
            print(f"  {row['score']:.3f} (语义 {row['semantic']:.2f} / 关键词 {row['keyword']:.2f} / "
                  f"保持率 {row['retention']:.2f})  {MEMORY.describe(item, now)}")
        MEMORY.search(keyword, limit=limit)  # 看过一遍也算复习
        return True, None
    if command == "pin":
        if len(args) == 1 and args[0].isdigit():
            mid = int(args[0])
            print(f"已设为常驻记忆，不再衰减：#{mid}" if MEMORY.pin(mid, True) else f"未找到记忆 #{mid}。")
        else:
            print("用法：/pin 记忆编号，例如 /pin 3")
        return True, None
    if command == "unpin":
        if len(args) == 1 and args[0].isdigit():
            mid = int(args[0])
            print(f"已取消常驻，重新按遗忘曲线衰减：#{mid}" if MEMORY.pin(mid, False) else f"未找到记忆 #{mid}。")
        else:
            print("用法：/unpin 记忆编号，例如 /unpin 3")
        return True, None
    if command == "revive":
        if len(args) == 1 and args[0].isdigit():
            mid = int(args[0])
            if MEMORY.revive(mid):
                print("已唤醒：" + MEMORY.describe(MEMORY.get(mid)))
            else:
                print(f"未找到记忆 #{mid}。")
        else:
            print("用法：/revive 记忆编号，例如 /revive 3")
        return True, None
    if command == "mood":
        print(MOOD.render())
        return True, None
    if command == "diary":
        print(MOOD.diary(12))
        return True, None
    if command == "reason":
        print(REASON.render())
        return True, None
    if command == "workspace":
        print(WORKSPACE.summary())
        return True, None
    if command == "insight":
        print(PRODUCTIVITY.insight())
        return True, None
    if command == "plan":
        goal = " ".join(args).strip()
        if not goal:
            print("用法：/plan 目标描述，例如 /plan 优化登录流程")
        else:
            print(PRODUCTIVITY.build_plan(goal))
        return True, None
    if command == "journal":
        print(PRODUCTIVITY.journal())
        return True, None
    if command == "stats":
        print(PRODUCTIVITY.stats(agent))
        return True, None
    if command == "snapshot":
        label = " ".join(args).strip()
        snap = SNAPSHOTS.capture(label)
        title = snap.get("label") or snap["id"]
        print(f"已创建快照：{snap['id']} | {title}")
        return True, None
    if command == "snapshots":
        items = SNAPSHOTS.list_snapshots()
        if not items:
            print("(暂无快照)")
        else:
            for item in items[:20]:
                print(SNAPSHOTS.describe(str(item.get("id", ""))))
        return True, None
    if command == "restore":
        if len(args) != 1:
            print("用法：/restore 快照编号")
        else:
            try:
                data = SNAPSHOTS.restore(args[0])
                print(f"已恢复快照：{args[0]} | {data.get('label') or '(no label)'}")
            except Exception as e:
                print(f"恢复失败：{e}")
        return True, None
    if command == "selftest":
        print(run_self_tests())
        return True, None
    if command == "persona":
        print(PERSONA.render())
        return True, None
    if command == "relationship":
        print(RELATIONSHIP.render())
        return True, None
    if command in {"thought", "reflection"}:
        if not args:
            agent.show_thought = not agent.show_thought
            print(f"Monologue Engine已{'开启' if agent.show_thought else '关闭'}。")
        elif args[0] in {"on", "off"}:
            agent.show_thought = args[0] == "on"
            print(f"Monologue Engine已{'开启' if agent.show_thought else '关闭'}。")
        elif args[0] == "preview":
            label, lines = THOUGHT_ENGINE.preview(
                user_input="（预览）",
                persona=PERSONA,
                mood=MOOD.snapshot(),
                relationship=RELATIONSHIP,
                recent_context="",
            )
            print(f"✦ Inner Voice | {label}")
            for line in lines[:16]:
                print(f"  {line}")
        else:
            print("用法：/thought [on|off|preview] 或 /reflection [on|off|preview]")
        return True, None
    if command == "trace":
        if args and args[0] == "json":
            print(json.dumps(TRACE_ENGINE.snapshot(), ensure_ascii=False, indent=2))
            return True, None
        limit = 20
        if args and args[0].isdigit():
            limit = max(1, min(200, int(args[0])))
        detail = TRACE_ENGINE.render_detail(limit)
        if not detail.strip():
            print("(上一回合没有记录到执行轨迹；先对话一轮再看)")
        else:
            print("【上一回合执行轨迹】")
            print(detail)
        return True, None
    if command == "state":
        print("\n".join(["【心情】", MOOD.render(), "", "【决策引擎】", REASON.render(), "", "【世界模型】", WORKSPACE.summary(), "", "【共同经历】", EXPERIENCE.render(4)]))
        return True, None
    if command == "history":
        print(ARCHIVE.last_window(16))
        return True, None
    if command == "experience":
        print(EXPERIENCE.render())
        return True, None
    if command == "timeline":
        print(EXPERIENCE.timeline())
        return True, None
    if command == "refreshws":
        print(WORKSPACE.refresh_and_return())
        return True, None
    if command == "plugins":
        if agent.loaded_plugins:
            print("\n".join(agent.loaded_plugins))
        else:
            print("(暂无已加载插件)")
        return True, None
    if command == "searchmem":
        keyword, limit = _parse_search_args(args, 20)
        print(recall_memories(keyword, limit=limit))
        return True, None
    if command == "searchws":
        keyword, limit = _parse_search_args(args, 20)
        print(search_workspace(keyword, limit=limit))
        return True, None
    if command == "searchchat":
        keyword, limit = _parse_search_args(args, 20)
        print(search_chat_history(keyword, limit=limit))
        return True, None
    if command == "searchlast":
        keyword, limit = _parse_search_args(args, 20)
        print(search_last_window(keyword, limit=limit))
        return True, None
    if command == "remember":
        category, content = _parse_remember_args(args)
        if not content:
            print("用法：/remember [-c 类别] 内容，例如 /remember -c lesson 注意先备份")
        else:
            print(remember(content, category))
        return True, None
    if command == "forget":
        if len(args) == 1 and args[0].isdigit():
            mid = int(args[0])
            print("已删除。" if MEMORY.remove(mid) else f"未找到记忆 #{mid}。")
        else:
            print("用法：/forget 记忆编号，例如 /forget 3")
        return True, None
    if command == "undo":
        print(agent.restore_last_checkpoint())
        return True, None
    if command == "reflect":
        agent.reflect = not agent.reflect
        print(f"自我反思已{'开启' if agent.reflect else '关闭'}。")
        return True, None
    if command == "tools":
        def _flag_suffix(dangerous: bool, mutating: bool) -> str:
            flags = (["需确认"] if dangerous else []) + (["写操作"] if mutating else [])
            return f"（{'/'.join(flags)}）" if flags else ""
        for t in agent.tools.values():
            first_line = t.schema["description"].splitlines()[0]
            suffix = _flag_suffix(getattr(t, "dangerous", False), getattr(t, "mutating", False))
            print(f"  - {t.schema['name']}{suffix}: {first_line}")
        # MCP 工具以前在这里完全不显示，但模型是看得见也调得动的——
        # 用户无从知道自己接进来的服务器给了模型哪些能力。
        for name in agent._mcp_tool_map:
            found = agent._mcp_lookup(name)
            desc = (found[2].get("description") or "").splitlines()
            suffix = _flag_suffix(*agent._tool_flags(name))
            print(f"  - {name}{suffix} [MCP]: {desc[0] if desc else '(无描述)'}")
        return True, None
    if command == "doctor":
        print("\n".join([
            f"后端：{agent.backend_name}",
            f"模型：{MODEL}",
            f"心情：{MOOD.compact()}",
            f"决策：{REASON.compact()}",
            f"人格：{PERSONA.compact()}",
            f"关系：{RELATIONSHIP.compact()}",
            f"世界：{WORKSPACE.compact()}",
            f"生产力：{PRODUCTIVITY.compact()}",
            f"记忆：{len(MEMORY.items)} 条 | {MEMORY.curve_compact()}",
            f"任务：{len(TODOS.items)} 项",
        ]))
        return True, None
    if command == "init":
        return True, INIT_PROMPT
    return False, None
BANNER = """
======================================================
  Polaris v1.1.6 | Memory Curve | Trace + Workspace + Planner + Persona
------------------------------------------------------
  /mode [plan|ask|auto] 权限模式    /todo       任务清单
  /init  生成项目 AGENT.md          /undo       回退文件
  /memory [n] 查看长期记忆         /tools      工具列表
  /mood   查看心情状态               /diary      查看心情日记
  /reason 查看决策引擎               /state      查看整体状态
  /insight 查看工作区洞察             /plan       生成任务计划
  /workspace 查看项目世界模型        /history    查看上一个窗口
  /experience 查看共同经历           /timeline   查看历史时间线
  /plugins 查看已加载插件           /searchmem  搜索记忆
  /searchws 搜索工作区文件          /searchchat 搜索全部对话
  /searchlast 搜索上一个窗口        /refreshws  刷新世界模型
  /journal   成长日志                /stats      统计概览
  /snapshot  保存当前状态           /snapshots  列出快照
  /restore   恢复状态快照            /selftest   运行自检
  /remember 写入长期记忆            /doctor     系统诊断
  /forget 编号 删除一条记忆         /reflect    开关自检
  /recall 语义召回记忆              /memstat    记忆遗忘曲线
  /pin 编号 记忆设为常驻            /revive 编号 唤醒休眠记忆
  /thought [on|off|preview] 思考轨迹 /persona    人格设定
  /reflection [on|off|preview] 同步心境
  /relationship 关系状态            /trace [n|json] 执行轨迹
  exit  退出
======================================================
"""
def main():
    if BACKEND_NAME in {"ollama", "lmstudio", "local"}:
        pass
    elif not API_KEY and not _looks_local_endpoint(BASE_URL):
        sys.exit("错误：未检测到环境变量 OPENAI_API_KEY；如果你在用本地 OpenAI-Compatible 服务，请先设置 OPENAI_BASE_URL 或 MINIAGENT_BACKEND=ollama/lmstudio。")
    if not evaluate_online_necessity():
        return
    WORKSPACE.refresh()
    agent = Agent(tools=BUILTIN_TOOLS)
    print(BANNER)
    print(f"当前统一模型配置: {MODEL}")
    print(f"快照目录: {SNAPSHOT_DIR}")
    print(f"当前后端配置: {BACKEND_NAME}")
    if _looks_local_endpoint(BASE_URL) or BACKEND_NAME in {"ollama", "lmstudio", "local"}:
        print("提示：当前是本地/兼容服务模式；Polaris 不内置假后端，需由 Ollama/LM Studio 等提供 OpenAI-Compatible 接口。")
    if BASE_URL:
        print(f"当前自定义 API 接口端点: {BASE_URL}")
    if agent.instructions:
        print("已动态注入自定义指令配置。")
    if MEMORY.items:
        print(f"已成功唤醒 {len(MEMORY.items)} 条长期记忆数据。")
    if agent.loaded_plugins:
        print(f"已加载插件：{len(agent.loaded_plugins)} 个。")
    print(f"当前人格画像：{PERSONA.compact()}")
    print(f"当前关系状态：{RELATIONSHIP.compact()}")
    print(f"当前心情状态：{MOOD.compact()}")
    print(f"当前决策引擎：{REASON.compact()}")
    print(f"当前世界模型：{WORKSPACE.compact()}")
    print(f"共同经历：{EXPERIENCE.compact()}")
    print(f"历史档案：{ARCHIVE.last_session_id() or '暂无'}")
    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n再见！")
            break
        if not user_input:
            continue
        handled, next_input = handle_cli_command(agent, user_input)
        if handled:
            if next_input == "__exit__":
                break
            if next_input is None:
                continue
            user_input = next_input
        print("\nPolaris > ", end="", flush=True)
        try:
            agent.chat(user_input)
        except Exception as e:
            print(f"\n[运行异常] {e}")
if __name__ == "__main__":
    main()