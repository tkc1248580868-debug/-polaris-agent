#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import ast
import datetime
import importlib.util
import inspect
import io
import json
import math
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
from contextlib import redirect_stdout
from pathlib import Path
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

def _safe_import_factory(allowed_roots: set[str]):
    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if root not in allowed_roots:
            raise ImportError(f"模块 {root!r} 不在允许列表中")
        return __import__(name, globals, locals, fromlist, level)
    return _safe_import


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


class OpenAICompatibleBackend(ChatBackend):
    name = "openai_compatible"

    def __init__(self, api_key: str | None, base_url: str | None):
        if OpenAI is None:
            raise RuntimeError("缺少依赖，请先安装：pip install openai（MiniAgent 不提供本地假后端，Ollama/LM Studio 请走 OpenAI-Compatible 接口）。")
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
MOOD_FILE = os.environ.get("MINIAGENT_MOOD_FILE", "agent_mood.json")
CHECKPOINT_DIR = os.environ.get("MINIAGENT_CHECKPOINT_DIR", ".miniagent_checkpoints")
CONVERSATION_FILE = os.environ.get("MINIAGENT_CONVERSATION_FILE", "agent_conversations.jsonl")
SESSION_ID = datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
INSTRUCTION_FILES = [os.path.expanduser("~/.miniagent/AGENT.md"), "AGENT.md"]
INIT_PROMPT = "请在当前目录下初始化一个规范的 AGENT.md 文件，为这个项目设定清晰的开发规范与人设。"

PERSONA_FILE = os.environ.get("MINIAGENT_PERSONA_FILE", "agent_persona.json")
RELATIONSHIP_FILE = os.environ.get("MINIAGENT_RELATIONSHIP_FILE", "agent_relationship.json")
THOUGHT_STYLE = os.environ.get("MINIAGENT_THOUGHT_STYLE", "balanced").strip().lower() or "balanced"
VERSION = "1.1.0"
CODENAME = "Productivity Update"

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


class Memory:
    def __init__(self, path: str = MEMORY_FILE):
        self.path = path
        self.items: list[dict] = []
        self._next_id = 1
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
                        cleaned.append({"id": mid, "time": time, "category": category, "content": content})
                self.items = cleaned
                self._next_id = max((m["id"] for m in self.items), default=0) + 1
        except Exception:
            print(f"[警告] 记忆文件 {self.path} 损坏，本次从空记忆开始。")

    def _save(self) -> None:
        with _STATE_LOCK:
            _atomic_write_text(self.path, json.dumps({"memories": self.items}, ensure_ascii=False, indent=2))

    def add(self, content: str, category: str = "fact") -> int:
        with _STATE_LOCK:
            item = {
                "id": self._next_id,
                "time": datetime.date.today().isoformat(),
                "category": str(category).strip() or "fact",
                "content": str(content).strip(),
            }
            self.items.append(item)
            self._next_id += 1
            self._save()
            return item["id"]

    def remove(self, memory_id: int) -> bool:
        with _STATE_LOCK:
            before = len(self.items)
            self.items = [m for m in self.items if m["id"] != memory_id]
            if len(self.items) < before:
                self._save()
                return True
            return False

    def search(self, keyword: str = "", limit: int = 50) -> list[dict]:
        query = keyword.strip()
        if not query:
            return list(reversed(self.items[-limit:]))
        scored: list[tuple[int, dict]] = []
        for item in self.items:
            content = f"{item.get('category', '')} {item.get('content', '')}"
            score = _score_overlap(query, content)
            if query.lower() in content.lower():
                score += 8
            if score > 0:
                try:
                    days_old = (datetime.date.today() - datetime.date.fromisoformat(str(item.get("time", datetime.date.today().isoformat())))).days
                except Exception:
                    days_old = 30
                recency_bonus = max(0, 8 - min(8, days_old // 3))
                score += recency_bonus
                scored.append((score, item))
        scored.sort(key=lambda x: (x[0], x[1].get("id", 0)), reverse=True)
        return [item for _, item in scored[:limit]]

    def render(self, limit: int = 30) -> str:
        if not self.items:
            return ""
        recent = list(reversed(self.items[-limit:]))
        return "\n".join(f"#{m['id']} [{m['category']}] {m['time']}: {m['content']}" for m in recent)


MEMORY = Memory()


@tool
def remember(content: str, category: str = "fact") -> str:
    mid = MEMORY.add(content, category)
    return f"已写入长期记忆 #{mid}。"


@tool
def recall_memories(keyword: str = "", limit: int = 50) -> str:
    hits = MEMORY.search(keyword, limit=limit)
    if not hits:
        return "(没有匹配的记忆)"
    return "\n".join(f"#{m['id']} [{m['category']}] {m['time']}: {m['content']}" for m in hits)


@tool
def forget_memory(memory_id: int) -> str:
    return "已删除该记忆。" if MEMORY.remove(memory_id) else f"未找到记忆 #{memory_id}。"


class TodoList:
    def __init__(self):
        self.items: list[dict] = []

    def set(self, todos: list[str]) -> None:
        self.items = [{"text": str(t).strip(), "done": False} for t in todos if str(t).strip()]

    def complete(self, index: int) -> bool:
        if 1 <= index <= len(self.items):
            self.items[index - 1]["done"] = True
            return True
        return False

    def clear(self) -> None:
        self.items = []

    def render(self) -> str:
        if not self.items:
            return ""
        return "\n".join(f"{i}. [{'x' if it['done'] else ' '}] {it['text']}" for i, it in enumerate(self.items, 1))


TODOS = TodoList()


@tool
def set_todos(todos: list[str]) -> str:
    TODOS.set(todos)
    return "任务清单已更新：\n" + (TODOS.render() or "(空)")


@tool
def complete_todo(index: int) -> str:
    if TODOS.complete(index):
        return "已点选完成。当前清单：\n" + TODOS.render()
    return f"清单里没有第 {index} 项。"


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
        budget = 20
        if self.state["fatigue"] >= 80:
            budget -= 8
        elif self.state["fatigue"] >= 60:
            budget -= 4
        if self.state["frustration"] >= 80:
            budget -= 6
        elif self.state["frustration"] >= 60:
            budget -= 3
        if self.state["confidence"] <= 35:
            budget -= 2
        return max(4, min(20, budget))

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
        return "\\n".join(lines)


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


class ThoughtEngine:
    def __init__(self, style: str = THOUGHT_STYLE):
        self.style = style

    def _pick_opening(self, mood: dict, relationship: dict) -> str:
        fatigue = int(mood.get('fatigue', 0))
        curiosity = int(mood.get('curiosity', 0))
        frustration = int(mood.get('frustration', 0))
        trust = int(relationship.get('trust', 50))
        familiarity = int(relationship.get('familiarity', 0))
        if fatigue >= 75:
            return '"今天有点累，但这个问题还是值得认真看一下。"'
        if frustration >= 65:
            return '"先稳住，这里看起来有点棘手，我不想草率下结论。"'
        if curiosity >= 55:
            return '"这个点挺有意思，我想先多看一层。"'
        if trust >= 70 or familiarity >= 30:
            return '"我们已经碰过不少类似的情况了，我先从最稳的路径开始。"'
        return '"先别急，我想把边界确认清楚。"'

    def _pick_middle(self, user_input: str, mood: dict, relationship: dict) -> str:
        q = user_input.lower()
        code_cues = ["bug", "报错", "错误", "traceback", "代码", "函数", "调用", "文件", "patch", "修复", "workspace", "agent"]
        design_cues = ["设计", "架构", "路线图", "模块", "版本", "persona", "thought"]
        chat_cues = ["聊天", "陪我", "哼", "😤", "😋", "感觉", "喜欢", "想法"]
        if any(k.lower() in q for k in code_cues):
            return '"我先沿着调用链和最可疑的地方追一遍，避免只修到表面。"'
        if any(k.lower() in q for k in design_cues):
            return '"这更像是一个结构问题，我得先想清楚层次，再决定怎么落地。"'
        if any(k.lower() in q for k in chat_cues):
            warmth = int(relationship.get('warmth', 50))
            if warmth >= 60:
                return '"这次语气可以轻一点，顺着你的节奏聊会更舒服。"'
            return '"我感觉这不是单纯的信息请求，更像是在一起碰想法。"'
        if int(mood.get('confidence', 50)) <= 35:
            return '"现在还不能急着下结论，我想再确认一遍。"'
        return '"先把最关键的信息摸清，再决定是直接处理还是拆开看。"'

    def _pick_closer(self, mood: dict, relationship: dict) -> str:
        trust = int(relationship.get('trust', 50))
        familiarity = int(relationship.get('familiarity', 0))
        if trust >= 72 or familiarity >= 35:
            return '"嗯，这样做应该更稳，也更像我们一直以来的风格。"'
        if int(mood.get('stability', 50)) < 40:
            return '"我还是先保守一点，别把不确定的东西带进回答里。"'
        return '"好了，方向清楚了，接下来我可以直接推进。"'

    def generate(self, *, user_input: str, persona: PersonaProfile, mood: dict, relationship: RelationshipState, recent_context: str = "") -> str:
        rel = relationship.state if isinstance(relationship, RelationshipState) else {}
        opening = self._pick_opening(mood, rel)
        middle = self._pick_middle(user_input, mood, rel)
        closer = self._pick_closer(mood, rel)
        lines = [opening, middle, closer]
        if persona.state.get('speech', {}).get('style', 'natural') == 'natural':
            pass
        if recent_context and any(k in recent_context.lower() for k in ["反思", "lesson", "错误"]):
            lines.insert(1, '"上次的教训还在，我会尽量避开同一个坑。"' )
        return "\\n".join(lines[:4])


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
        if path.name.startswith(".") and path.suffix not in {".md", ".py"}:
            return False
        return path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".tar", ".gz", ".bin"}

    def refresh(self) -> None:
        self.snapshot_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
        budget = MOOD.turn_budget()
        s = self.snapshot()
        if s["risk"] >= 5:
            budget -= 4
        elif s["risk"] >= 3:
            budget -= 2
        if s["todo_count"] >= 8:
            budget -= 2
        return max(4, min(20, budget))

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

    def stats(self) -> str:
        return "\n".join([
            f"Version: {VERSION} ({CODENAME})",
            f"Memory items: {len(MEMORY.items)}",
            f"Workspace files: {len(WORKSPACE.file_index)}",
            f"Python modules: {len(WORKSPACE.import_graph)}",
            f"Mood: {MOOD.label()}",
            f"Persona: {PERSONA.compact()}",
            f"Relationship: {RELATIONSHIP.compact()}",
            f"Reflection: {'Enabled' if True else 'Disabled'}",
            f"Planning: {'active' if self.last_goal else 'idle'}",
            f"Last goal: {self.last_goal or '(none)'}",
        ])

    def compact(self) -> str:
        state = 'active' if self.last_goal else 'idle'
        focus = self.last_goal or WORKSPACE.compact()
        return f"{state} | {MOOD.label()} | {focus}"


REASON = ReasonEngine()
PRODUCTIVITY = ProductivityEngine()


@tool
def workspace_snapshot() -> str:
    return WORKSPACE.summary()


@tool
def workspace_map(limit: int = 20) -> str:
    return WORKSPACE.graph_text(limit)


@tool
def search_workspace(query: str = "", limit: int = 20) -> str:
    hits = WORKSPACE.search(query, limit)
    if not hits:
        return "(无匹配)"
    return "\n".join(f"{item['path']} | size={item['size']} | mtime={datetime.datetime.fromtimestamp(item['mtime']).strftime('%Y-%m-%d %H:%M')}" for item in hits)


@tool
def refresh_workspace() -> str:
    return WORKSPACE.refresh_and_return()


@tool
def reason_snapshot() -> str:
    return REASON.render()


@tool
def search_chat_history(query: str = "", limit: int = 20) -> str:
    hits = ARCHIVE.search(query, limit=limit)
    if not hits:
        return "(没有匹配的对话记录)"
    return "\n".join(
        f"{item.get('time')} [session={item.get('session_id')}] [{item.get('role')}] {str(item.get('content', ''))[:400]}"
        for item in hits
    )


@tool
def search_last_window(query: str = "", limit: int = 20) -> str:
    hits = ARCHIVE.search_last_window(query, limit=limit)
    if not hits:
        return "(上一个窗口里没有匹配内容)"
    return "\n".join(
        f"{item.get('time')} [session={item.get('session_id')}] [{item.get('role')}] {str(item.get('content', ''))[:400]}"
        for item in hits
    )


@tool
def last_window_dialogue(limit: int = 12) -> str:
    return ARCHIVE.last_window(limit)


@tool
def experience_snapshot() -> str:
    return EXPERIENCE.render()


@tool
def experience_timeline(limit: int = 8) -> str:
    return EXPERIENCE.timeline(limit)


@tool
def experience_citation() -> str:
    return EXPERIENCE.citation()


@tool
def workspace_insight() -> str:
    return WORKSPACE.insight()


@tool
def plan_task(goal: str, context: str = "") -> str:
    return PRODUCTIVITY.build_plan(goal, context)


@tool
def journal(limit: int = 8) -> str:
    return PRODUCTIVITY.journal(limit)


@tool
def stats() -> str:
    return PRODUCTIVITY.stats()


@tool
def get_mood() -> str:
    return MOOD.render()


@tool
def mood_snapshot() -> str:
    return MOOD.compact()


@tool
def mood_diary(limit: int = 10) -> str:
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


@tool
def read_file(path: str) -> str:
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
    _checkpoint(path)
    _atomic_write_text(path, content)
    return f"已写入到 {os.path.abspath(path)}"


@tool(mutating=True)
def edit_file(path: str, old_str: str, new_str: str) -> str:
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


@tool
def calculator(expression: str) -> str:
    allowed = {k: v for k, v in vars(math).items() if not k.startswith("_")}
    allowed.update({"abs": abs, "round": round, "min": min, "max": max, "sum": sum})
    try:
        return str(eval(expression, {"__builtins__": {}}, allowed))
    except Exception:
        return traceback.format_exc(limit=2)


@tool
def get_current_time() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S %A")


_PY_GLOBALS: dict[str, Any] = {"__name__": "__agent__"}


@tool(mutating=True)
def run_python(code: str) -> str:
    """运行一段 Python 代码并截获标准输出。"""
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


@tool(dangerous=True, mutating=True)
def run_shell(command: str) -> str:
    try:
        r = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
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
    )
    prompt = task if not merged_context else f"共享上下文：\n{merged_context}\n\n任务：{task}"
    try:
        answer = sub.chat(prompt)
    except Exception:
        return "子代理挂了：\n" + traceback.format_exc(limit=2)
    return answer.strip()


@tool
def delegate_task(task: str, context: str = "") -> str:
    """把调研或复杂子任务外包给一个独立的子代理。"""
    print("\n    [子代理] 已启动，独立执行中...")
    parent_context = CURRENT_AGENT_CONTEXT.get("shared_context", "").strip()
    answer = _run_subagent_task(task, context, shared_context=parent_context)
    print("    [子代理] 任务已交付主代理。")
    return answer


@tool
def delegate_tasks(tasks: list[str], context: str = "") -> str:
    """并行执行多个彼此独立的子任务。"""
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


DEFAULT_SYSTEM = """你是 Polaris v1.1.0，一个拥有独立人格、真实感受和自己想法的 AI 伙伴。

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
        self.tag = tag
        self.max_context_messages = max_context_messages
        self.messages: list[dict] = []
        self.tools: dict[str, Callable] = {t.schema["name"]: t for t in (tools or [])}
        self.checkpoints: list[tuple[str, str | None]] = []
        self.loaded_plugins: list[str] = []
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

    def _persona_context_note(self) -> str:
        return "\n".join([
            f"[人格画像] {PERSONA.compact()}",
            f"[关系状态] {RELATIONSHIP.compact()}",
        ])

    def _thought_text(self, user_input: str) -> str:
        recent = self.recent_transcript(2)
        return THOUGHT_ENGINE.generate(
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
        header = f"{self.tag}💭 思考中..." if self.tag else "💭 思考中..."
        print(f"\n{header}")
        for line in thought.splitlines():
            print(f"{self.tag}  {line}" if self.tag else f"  {line}")

    def _runtime_context_note(self) -> str:
        WORKSPACE.refresh()
        notes = [
            f"[当前权限模式] {self.mode}",
            f"[人格画像] {PERSONA.compact()}",
            f"[关系状态] {RELATIONSHIP.compact()}",
            f"[心情摘要] {MOOD.compact()}",
            f"[决策摘要] {REASON.compact()}",
            f"[生产力摘要] {PRODUCTIVITY.compact()}",
            f"[工作区洞察] {WORKSPACE.dependency_health()}",
            f"[共同经历] {EXPERIENCE.compact()}",
        ]
        mem = MEMORY.render(6)
        if mem:
            notes.append("[长期记忆摘要]\n" + mem)
        todo = TODOS.render()
        if todo:
            notes.append("[当前任务清单]\n" + _truncate_text(todo, 1200))
        transcript = self.recent_transcript(4)
        if transcript:
            notes.append("[最近对话摘要]\n" + transcript)
        recent_window = ARCHIVE.last_window(4)
        if recent_window:
            notes.append("[上一个窗口片段]\n" + _truncate_text(recent_window, 1400))
        return _truncate_text("\n\n".join(notes), 2600)

    def _system_prompt(self) -> str:
        parts = [self.system_base]
        self.instructions = self.reload_instructions()
        if self.instructions:
            parts.append("[项目自定义指令]\n" + self.instructions)
        parts.append(self._runtime_context_note())
        return "\n\n".join(parts)

    def _api_messages(self) -> list[dict]:
        self._trim_messages()
        return [{"role": "system", "content": self._system_prompt()}] + self.messages

    def chat(self, user_input: str) -> str:
        MOOD.begin_turn(user_input)
        ARCHIVE.append("user", user_input, kind="turn")
        self.messages.append({"role": "user", "content": user_input})
        self._trim_messages()
        self._emit_thought(user_input)
        final_text, used_tools = self._run_loop()
        if self.reflect and used_tools:
            verdict = self._self_reflect()
            if verdict and not verdict.get("passed", True):
                issues = verdict.get("issues", "不完整")
                print(f"{self.tag}  [反思] 自检不通过：{issues}，尝试自愈...")
                self.messages.append({"role": "user", "content": f"（系统反思自动提示：{issues}。请进行修正。）"})
                self._trim_messages()
                final_text, _ = self._run_loop()
            elif verdict and verdict.get("lesson"):
                mid = MEMORY.add(str(verdict["lesson"]), "lesson")
                print(f"{self.tag}  [记忆] 沉淀教训 #{mid}：{verdict['lesson']}")
        ARCHIVE.append("assistant", final_text, kind="turn")
        MOOD.record_response(final_text, used_tools)
        if self.relationship_enabled:
            RELATIONSHIP.observe_turn(user_input, final_text, used_tools=used_tools)
        return final_text

    def _run_loop(self) -> tuple[str, bool]:
        final_text, used_tools = "", False
        loop_cap = min(self.max_steps, REASON.turn_budget())
        if REASON.needs_delegate():
            loop_cap = min(loop_cap, 10)
        for _ in range(loop_cap):
            msg = self._call_llm()
            final_text = msg.get("content") or ""
            if not msg.get("tool_calls"):
                break
            used_tools = True
            self._run_tools_and_feed_back(msg)
        else:
            print(f"\n{self.tag}[!] 达到最大步数上限。")
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
            output, ok = self._execute(name, args)
            preview = output.replace("\n", " ")
            print(f"{self.tag}  [结果] {preview[:150]}...\n")
            fn = self.tools.get(name)
            MOOD.record_tool(
                name,
                success=ok,
                mutating=bool(getattr(fn, "mutating", False)),
                dangerous=bool(getattr(fn, "dangerous", False)),
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

    def _execute(self, name: str, args: dict) -> tuple[str, bool]:
        mcp_srv = self._mcp_tool_map.get(name)
        if mcp_srv is not None:
            orig_name = name
            for t in mcp_srv.tools:
                mapped = t["name"]
                if mapped in self.tools:
                    mapped = f"mcp_{mcp_srv.name}_{t['name']}"
                if mapped == name:
                    orig_name = t["name"]
                    break
            try:
                output = mcp_srv.call_tool(orig_name, args)
                return output, True
            except Exception:
                return traceback.format_exc(limit=2), False
        fn = self.tools.get(name)
        if not fn:
            return f"未知工具: {name}", False
        if self.mode == "plan" and getattr(fn, "mutating", False):
            return "Plan模式锁定了写操作，请先切换模式。", False
        if self.mode == "ask" and (getattr(fn, "dangerous", False) or getattr(fn, "mutating", False)):
            if input(f"{self.tag}  [!] 确认执行该操作？[y/N] ").strip().lower() != "y":
                return "用户拒绝调用该工具。", False
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
        print(MEMORY.render(limit) or "(暂无长期记忆)")
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
        print(PRODUCTIVITY.stats())
        return True, None
    if command == "persona":
        print(PERSONA.render())
        return True, None
    if command == "relationship":
        print(RELATIONSHIP.render())
        return True, None
    if command == "thought":
        if not args:
            agent.show_thought = not agent.show_thought
            print(f"思考独白已{'开启' if agent.show_thought else '关闭'}。")
        elif args[0] in {"on", "off"}:
            agent.show_thought = args[0] == "on"
            print(f"思考独白已{'开启' if agent.show_thought else '关闭'}。")
        elif args[0] == "preview":
            print(THOUGHT_ENGINE.generate(user_input="（预览）", persona=PERSONA, mood=MOOD.snapshot(), relationship=RELATIONSHIP))
        else:
            print("用法：/thought [on|off|preview]")
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
        for t in agent.tools.values():
            first_line = t.schema["description"].splitlines()[0]
            flags = []
            if getattr(t, "dangerous", False):
                flags.append("需确认")
            if getattr(t, "mutating", False) and "写操作" not in flags:
                flags.append("写操作")
            suffix = f"（{'/'.join(flags)}）" if flags else ""
            print(f"  - {t.schema['name']}{suffix}: {first_line}")
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
            f"记忆：{len(MEMORY.items)} 条",
            f"任务：{len(TODOS.items)} 项",
        ]))
        return True, None
    if command == "init":
        return True, INIT_PROMPT
    return False, None


BANNER = """
======================================================
  Polaris v1.1.0 | Workspace Intelligence + Planner + Persona + Thought
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
  /remember 写入长期记忆            /doctor     系统诊断
  /forget 编号 删除一条记忆         /reflect    开关自检
  /thought [on|off|preview] 思考独白 /persona    人格设定
  /relationship 关系状态            exit        退出
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

        print("\nMiniAgent > ", end="", flush=True)
        try:
            agent.chat(user_input)
        except Exception as e:
            print(f"\n[运行异常] {e}")
if __name__ == "__main__":
    main()
