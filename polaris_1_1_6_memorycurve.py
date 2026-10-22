#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
import ast
import atexit
import hashlib
import datetime
import importlib.util
import inspect
import io
import json
import math
import operator
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import traceback
import types
import unicodedata
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from time import time
from typing import Any, Callable, get_type_hints
from urllib.parse import urlparse
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
# ───────────────────────────────────────────────────────────────────────────────
# 终端呈现：颜色与显示宽度
#
# 不引 rich 之类的库——「单文件 + 一个依赖」是这个项目的卖点，
# 为了好看牺牲掉它不划算。纯 ANSI 转义码够用，连 import 都省了。
# ───────────────────────────────────────────────────────────────────────────────
_STYLES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m", "italic": "\033[3m", "underline": "\033[4m",
    "accent": "\033[38;5;111m",   # Polaris 的主色，一整套界面只用这一个彩色
    # 主色的两档浅色，只给等待动画里那道扫过文字的光用——还是同一个蓝，不算新颜色
    "glow": "\033[38;5;153m", "shine": "\033[38;5;189m",
    "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m", "gray": "\033[90m",
}
def _color_enabled() -> bool:
    """NO_COLOR 约定：这个变量只要存在（哪怕是空值）就关掉颜色。
    非 TTY（管道、重定向、CI）也自动关——那些地方转义码只会变成乱码。"""
    if "NO_COLOR" in os.environ:
        return False
    forced = os.environ.get("POLARIS_COLOR", "").strip().lower()
    if forced in {"0", "off", "never"}:
        return False
    if forced in {"1", "on", "always"}:
        return True
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False
_COLOR = _color_enabled()
def paint(text: str, *styles: str) -> str:
    """给文本上色。关掉颜色时原样返回，所以调用处不需要写 if。"""
    if not _COLOR or not styles:
        return text
    prefix = "".join(_STYLES.get(s, "") for s in styles)
    return f"{prefix}{text}{_STYLES['reset']}" if prefix else text
def _display_width(text: str) -> int:
    """终端里的实际显示宽度：中日韩字符占两格。

    按 len() 补空格是对不齐的——横幅那两列以前就歪在这上面，
    第二列散落在第 35/36/37/38 格四个位置。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in "WF" else 1
    return width
def _pad_display(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))
def _fit_display(text: str, width: int) -> str:
    """按显示宽度截断，超出部分用省略号，不会把中文截成半个。"""
    if _display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _display_width(out) + _display_width(ch) > width - 1:
            break
        out += ch
    return out + "…"
# ───────────────────────────────────────────────────────────────────────────────
# 终端呈现：等待动画、Markdown 排版、一回合的版面
#
# 等模型的时候屏幕不能是死的：一颗会呼吸的星、一道扫过文字的光、一个秒表，
# 让人一眼看出它在干活、干了多久、怎么打断——这套是跟 Claude Code 学的。
# 回答按 Markdown 排好再上屏，不把 ** 和 ## 原样甩给用户。
#
# 依旧只靠标准库和 ANSI 转义码。不是终端（管道、重定向、CI）时动画和排版全关、
# 原样输出：日志里要的是能 grep 的原文，不是一堆光标控制码。
# ───────────────────────────────────────────────────────────────────────────────
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
def _is_tty() -> bool:
    try:
        return bool(sys.stdout.isatty()) and os.environ.get("TERM", "") != "dumb"
    except Exception:
        return False
_TTY = _is_tty()
# 动画要靠「把光标挪回去重画」实现。POLARIS_ANIMATE=0 只保留排版，不动光标。
_ANIMATE = _TTY and _env_bool("POLARIS_ANIMATE", True)
if _ANIMATE:
    # 动画期间会藏起光标。万一进程从奇怪的地方退出，至少把光标还回去。
    atexit.register(lambda: (sys.stdout.write("\x1b[?25h"), sys.stdout.flush()))
def _visible_width(text: str) -> int:
    """去掉转义码之后的显示宽度。"""
    return _display_width(_ANSI_RE.sub("", text))
def _term_width() -> int:
    """排版宽度：跟着终端走，但超过 100 列就不再变宽，太长的行读着累。
    最后一列空着不用：写满整行时各家终端的折行行为不一致，擦除重画会算错行数。"""
    cols = shutil.get_terminal_size((80, 24)).columns
    return max(20, min(cols, 101) - 1)
class Spinner:
    """等待动画：呼吸的星 + 扫过文字的光 + 本回合已用时。

    跑在后台线程里，只管自己那一行，每一帧回到行首重画。
    往屏幕写任何东西之前都必须先 stop()——它会把这一行擦干净，动画不在输出里留痕迹。
    等待很短（读个小文件）时一帧都不画，免得一闪一闪。

    gap=True 时动画和上面的内容隔一个空行（画在下一行），停下时退回原来那一行：
    光标回到哪儿，后面的输出就从哪儿接着写，版面和没转过一样。"""
    FRAMES = "·✢✳✶✻✽✻✶✳✢"
    VERBS = ("思索", "琢磨", "推敲", "斟酌", "观星", "校准航向", "整理思路", "翻阅记忆")
    INTERVAL = 0.08
    DELAY = 0.15
    def __init__(self, label: str = "", *, since: float | None = None, enabled: bool | None = None,
                 gap: bool = False, interval: float | None = None, sweep: int = 1, faint: bool = False):
        self.label = label or random.choice(self.VERBS)
        self.since = since
        self.gap = gap
        self.enabled = _ANIMATE if enabled is None else enabled
        # 节奏、扫光速度、明暗由心情决定（见 spinner_mood）；默认值就是平常的样子
        self.interval = interval or self.INTERVAL
        self.sweep = max(1, int(sweep))
        self.faint = bool(faint)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._drawn = False
        self._out: Any = None
    @property
    def running(self) -> bool:
        return self._thread is not None
    def frame(self, tick: int, elapsed: float) -> str:
        text = self.label + "…"
        hint_text = f"（{int(elapsed)}s · Ctrl+C 打断）"
        # 一帧必须比终端窄：折到第二行的话，停下时只擦得掉一行，会留下半截动画。
        # _term_width() 最小只到 20，比它还窄的终端得看真实列数
        width = min(_term_width(), shutil.get_terminal_size((80, 24)).columns - 1)
        star = paint(self.FRAMES[tick % len(self.FRAMES)], *(("accent", "dim") if self.faint else ("accent",)))
        if 2 + _display_width(text) + 1 + _display_width(hint_text) > width:
            hint_text = f"（{int(elapsed)}s）"
        room = width - 3 - _display_width(hint_text)
        if room < 4:
            return star  # 连「x…（Ns）」都放不下：只剩一颗星在呼吸
        if _display_width(text) > room:
            text = _fit_display(text, max(2, room))
        # 光从左边外面扫进来、从右边扫出去，然后从头再来；正中最亮，两侧渐暗。
        # faint（累了）时整体压暗一档，最亮也只到 glow——同一个蓝，只是没精神
        center = (tick * self.sweep) % (len(text) + 6) - 3
        lit = []
        for i, ch in enumerate(text):
            near = abs(i - center)
            if self.faint:
                styles = ("glow",) if near == 0 else ("accent",) if near == 1 else ("accent", "dim")
            else:
                styles = ("shine", "bold") if near == 0 else ("glow",) if near == 1 else ("accent",)
            lit.append(paint(ch, *styles))
        return f"{star} {''.join(lit)} {paint(hint_text, 'dim')}"
    def start(self) -> "Spinner":
        if not self.enabled or self._thread is not None:
            return self
        self._out = sys.stdout
        self._stop.clear()
        self._drawn = False
        self.since = self.since or time()
        self._thread = threading.Thread(target=self._run, name="polaris-spinner", daemon=True)
        self._thread.start()
        return self
    def _run(self) -> None:
        if self._stop.wait(self.DELAY):
            return
        tick = 0
        while True:
            try:
                # 藏光标：不然它会在动画后面跟着闪
                lead = "\n" if self.gap and not self._drawn else ""
                self._out.write(lead + "\x1b[?25l\r" + self.frame(tick, time() - self.since) + "\x1b[K")
                self._out.flush()
                self._drawn = True
            except Exception:
                return
            if self._stop.wait(self.interval):
                return
            tick += 1
    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join()
        self._thread = None
        if self._drawn:
            try:
                self._out.write("\r\x1b[K" + ("\x1b[1A" if self.gap else "") + "\x1b[?25h")
                self._out.flush()
            except Exception:
                pass
    def __enter__(self) -> "Spinner":
        return self.start()
    def __exit__(self, *exc: Any) -> None:
        self.stop()
# ───────────────────────────────────────────────────────────────────────────────
# 有心情的等待动画
#
# 心情的六个维度都是 0~100。阈值是照着真实的取值范围定的，不是拍脑袋：
# rebalance() 会把每个维度往基线拉，好奇心、专注度的 +1 几乎都会被抵消掉，
# 所以「高」要按「比基线高出一截」算，而不是借用 ThoughtEngine 里那种 65 的绝对值——那永远到不了。
# ───────────────────────────────────────────────────────────────────────────────
_MOOD_BASELINE = {"confidence": 55, "focus": 56, "fatigue": 18, "curiosity": 50, "frustration": 12, "stability": 55}
@dataclass(frozen=True)
class SpinnerMood:
    key: str
    verb: str
    interval: float = Spinner.INTERVAL
    sweep: int = 1
    faint: bool = False
def _mood_int(state: dict, key: str) -> int:
    """心情文件是手改得动的，读进来不做类型校验——这里兜底，坏值按基线算。"""
    try:
        return int(state.get(key, _MOOD_BASELINE[key]))
    except (TypeError, ValueError):
        return _MOOD_BASELINE[key]
def spinner_mood(state: dict | None) -> SpinnerMood | None:
    """按此刻的心情挑等待动画的样子；返回 None 表示心情平常，用普通动词。

    先到先得的顺序就是轻重缓急：
    - 受挫 ≥ 35：基线 12，一次失败的工具净涨 5 左右，35 大约是「最近接连失败四五次还没缓过来」。
      光扫得快一倍——这是唯一会在一回合里跳变的维度，紧跟在红色的 ⎿ 后面。
    - 疲劳 ≥ 60：和 turn_budget 第一次砍步数的线是同一条，动画变暗变慢的时候，步数预算也真的少了。
    - 自信 ≥ 72 且受挫 < 20：大约连续成功 18~20 次工具才到得了，叫它「兴冲冲」不算夸张。
    - 专注 ≥ 62：只有歇了一周左右才会涨到这儿。
    - 好奇 ≥ 56：几乎碰不到（每次 +1 都会被 rebalance 抵消），留着是为了恢复快照或手改状态时也说得通。

    动词只说心情，不说它没在做的事：受挫、疲劳这两档上，没有任何代码会让模型「换个思路」
    或「再检查一遍」（MoodState.advice 要到 70、75 才给模型提醒），写成那样就是在骗人。"""
    if not isinstance(state, dict) or not state:
        return None
    if _mood_int(state, "frustration") >= 35:
        return SpinnerMood("frustrated", "有点受挫，接着找", sweep=2)
    if _mood_int(state, "fatigue") >= 60:
        return SpinnerMood("tired", "有点累，慢慢来", interval=0.14, faint=True)
    if _mood_int(state, "confidence") >= 72 and _mood_int(state, "frustration") < 20:
        return SpinnerMood("on_a_roll", "兴冲冲地翻找")
    if _mood_int(state, "focus") >= 62:
        return SpinnerMood("steady", "稳稳地推敲")
    if _mood_int(state, "curiosity") >= 56:
        return SpinnerMood("curious", "好奇地东翻西找")
    return None
_MD_INLINE = re.compile(
    r"(`+)(.+?)\1"                                          # `代码`
    r"|\*\*(?=\S)(.+?)(?<=\S)\*\*"                          # **加粗**
    # *斜体*。两边只排除英文字母数字（防 2*3*4 这种）——不能用 \w，
    # Python 里中文也算 \w，那样「会*成长*的」就永远认不出来了
    r"|(?<![A-Za-z0-9*])\*(?=[^\s*])([^*]+?)(?<=\S)\*(?![A-Za-z0-9*])"
    r"|\[([^\]]+)\]\(([^)\s]+)\)"                           # [链接](url)
)
def _md_inline(text: str, base: tuple[str, ...] = ()) -> list[tuple[str, tuple[str, ...]]]:
    """行内 Markdown → (文字, 样式) 片段。只认最常见的四种：代码、加粗、斜体、链接。

    下划线写法（__x__、_x_）故意不认：回答里到处是 __init__ 和 snake_case，
    认了误伤的远比漏掉的多。"""
    segs: list[tuple[str, tuple[str, ...]]] = []
    pos = 0
    for m in _MD_INLINE.finditer(text):
        if m.start() > pos:
            segs.append((text[pos:m.start()], base))
        if m.group(1):
            segs.append((m.group(2), base + ("accent",)))
        elif m.group(3) is not None:
            segs.extend(_md_inline(m.group(3), base + ("bold",)))
        elif m.group(4) is not None:
            segs.append((m.group(4), base + ("italic",)))
        else:
            label, url = m.group(5), m.group(6)
            segs.append((label, base + ("underline",)))
            if url != label:
                segs.append((f" ({url})", base + ("dim",)))
        pos = m.end()
    if pos < len(text):
        segs.append((text[pos:], base))
    return segs
def _paint_segments(segs: list[tuple[str, tuple[str, ...]]]) -> str:
    """把样式片段拼成带转义码的字符串；相邻同样式的合并，少发几个转义码。"""
    out, run, style = [], "", None
    for text, st in segs:
        if st == style:
            run += text
            continue
        if run:
            out.append(paint(run, *style))
        run, style = text, st
    if run:
        out.append(paint(run, *(style or ())))
    return "".join(out)
def _wrap_segments(segs: list[tuple[str, tuple[str, ...]]], width: int,
                   first_prefix: str = "", rest_prefix: str = "", keep_indent: bool = False) -> list[str]:
    """按显示宽度折行，样式跟着字走。英文按词折、中文按字折，续行用 rest_prefix 悬挂缩进。

    自己折而不交给终端有两个原因：终端折出来的续行会顶到第 0 列，缩进全没了；
    而且流式预览要擦掉重画，必须确切知道一行占了几排——每一排都不超过 width，
    终端就不会自己再折，排数才算得准。keep_indent 保留第一排开头的空格（代码缩进）。"""
    units: list[tuple[str, tuple[str, ...]]] = []
    for text, st in segs:
        word = ""
        for ch in text.replace("\t", "    "):
            if ch.isspace() or _display_width(ch) != 1:
                if word:
                    units.append((word, st))
                    word = ""
                units.append((" " if ch.isspace() else ch, st))
            else:
                word += ch
        if word:
            units.append((word, st))
    rows: list[str] = []
    cur: list[tuple[str, tuple[str, ...]]] = []
    prefix, col = first_prefix, 0
    avail = max(1, width - _visible_width(prefix))
    def newline() -> None:
        nonlocal cur, col, prefix, avail
        while cur and cur[-1][0] == " ":
            cur.pop()
        rows.append(prefix + _paint_segments(cur))
        cur, col, prefix = [], 0, rest_prefix
        avail = max(1, width - _visible_width(prefix))
    for text, st in units:
        w = _display_width(text)
        if text == " ":
            if col == 0 and not (keep_indent and not rows):
                continue  # 折行后的行首不留空格
            if col + 1 > avail:
                newline()
                continue
            cur.append((text, st))
            col += 1
            continue
        if col and col + w > avail:
            newline()
        while w > avail:  # 一个词比整行还宽（长网址、长路径）：只能硬切
            head, hw = "", 0
            for ch in text:
                cw = _display_width(ch)
                if hw + cw > avail:
                    break
                head, hw = head + ch, hw + cw
            head = head or text[0]
            cur.append((head, st))
            newline()
            text = text[len(head):]
            w = _display_width(text)
        if text:
            cur.append((text, st))
            col += w
    if cur or not rows:
        newline()
    return rows
class MarkdownRenderer:
    """逐行把 Markdown 排成终端文本。

    流式输出时一行收全了才排这一行，所以跨行只需要记一个状态：是不是在代码块里。
    表格之类排不好的写法原样输出，不瞎猜。"""
    _FENCE = re.compile(r"^\s*(```|~~~)")
    _HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
    _RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
    _BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
    _ORDERED = re.compile(r"^(\s*)(\d{1,3}[.)])\s+(.*)$")
    _QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
    _TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")
    def __init__(self, width: int, *, preview: bool = False):
        self.width = width
        self.in_code = False
        # 预览排的是还没写完的半行：**加 这种开了头还没收尾的记号先藏起来，
        # 等收尾的那半来了再变粗。定稿时照原文排，不会误删正文里真有的星号。
        self.preview = preview
    def _inline(self, text: str, base: tuple[str, ...] = ()) -> list[tuple[str, tuple[str, ...]]]:
        segs = _md_inline(text, base)
        if not self.preview:
            return segs
        # 只清没被任何记号认领的原文片段（样式就是 base 的那些）；代码片段里的星号原样留着
        return [(t.replace("**", "").replace("`", "") if st == base else t, st) for t, st in segs]
    def render(self, line: str, first: str = "", rest: str = "") -> list[str]:
        """排一行。first 是这一行头一排的前缀（回答的第一行是 ✦），rest 是折行后的前缀。
        返回空列表表示这一行不上屏（代码块的围栏）。"""
        if self._FENCE.match(line):
            self.in_code = not self.in_code
            return []
        if self.in_code:
            bar = paint("│ ", "dim")
            return _wrap_segments([(line, ("accent",))], self.width, first + bar, rest + bar, keep_indent=True)
        if not line.strip():
            return [""]
        m = self._HEADING.match(line)
        if m:
            base = ("bold", "accent") if len(m.group(1)) <= 2 else ("bold",)
            return _wrap_segments(self._inline(m.group(2), base), self.width, first, rest)
        if self._RULE.match(line):
            return [first + paint("─" * max(3, min(40, self.width - _visible_width(first))), "dim")]
        m = self._BULLET.match(line)
        if m:
            pad = " " * len(m.group(1).expandtabs(4))
            body, mark = m.group(2), "•"
            task = self._TASK.match(body)
            if task:
                mark, body = ("✔" if task.group(1) != " " else "○"), task.group(2)
            return _wrap_segments(self._inline(body), self.width,
                                  first + pad + paint(mark, "accent") + " ", rest + pad + "  ")
        m = self._ORDERED.match(line)
        if m:
            pad = " " * len(m.group(1).expandtabs(4))
            return _wrap_segments(self._inline(m.group(3)), self.width,
                                  first + pad + paint(m.group(2), "accent") + " ",
                                  rest + pad + " " * (len(m.group(2)) + 1))
        m = self._QUOTE.match(line)
        if m:
            bar = paint("│ ", "dim")
            return _wrap_segments(self._inline(m.group(1), ("italic",)), self.width, first + bar, rest + bar)
        if line.lstrip().startswith("|"):
            cells = line.strip()
            if re.fullmatch(r"[|:\-\s]+", cells):
                return _wrap_segments([(cells.replace("-", "─"), ("dim",))], self.width, first, rest)
            return _wrap_segments([(part, ("dim",)) if part == "|" else (part, ())
                                   for part in re.split(r"(\|)", cells) if part], self.width, first, rest)
        pad = " " * (len(line) - len(line.lstrip(" ")))
        return _wrap_segments(self._inline(line.strip()), self.width, first + pad, rest + pad)
def _format_tool_args(args_str: str) -> str:
    """工具参数的短写法：read_file("README.md", limit=30)，而不是一整坨 JSON。"""
    try:
        args = json.loads(args_str) if args_str.strip() else {}
    except Exception:
        return args_str
    if not isinstance(args, dict):
        return args_str
    parts = []
    for i, (key, value) in enumerate(args.items()):
        shown = json.dumps(value, ensure_ascii=False)
        parts.append(shown if i == 0 else f"{key}={shown}")
    return ", ".join(parts)
def _summarize_tool_output(output: str) -> tuple[str, int]:
    """工具结果在屏幕上只露第一行，再告诉人后面还有几行——完整内容在 /trace 和模型那里。"""
    text = (output or "").replace(os.getcwd() + os.sep, "")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return "（无输出）", 0
    return lines[0].replace("\t", " ").strip(), len(lines) - 1
def _clean_line(text: Any) -> str:
    """把模型/用户写的一段话压成一行能安全上屏的文字：去转义码、去控制字符、合并空白。"""
    text = _ANSI_RE.sub("", str(text or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\t\n ")
    return " ".join(text.split())
def _retention_bar(retention: float, cells: int = 5) -> str:
    """保持率画成 5 格。至少亮 1 格：还在被想起的记忆，就不该显示成一格都没有。"""
    k = max(1, min(cells, round(float(retention) * cells)))
    return "▰" * k + "▱" * (cells - k)
def _paint_bar(bar: str) -> str:
    return paint(bar.rstrip("▱"), "accent") + paint(bar[len(bar.rstrip("▱")):], "dim")
def _ago(value: Any, now: datetime.datetime) -> str:
    then = _parse_dt(value, now)
    sec = max(0.0, (now - then).total_seconds())
    if sec < 60:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 48 * 3600:
        return f"{int(sec // 3600)} 小时前"
    return f"{int(sec // 86400)} 天前"
def _todo_stamp() -> str:
    # 带时区：夏令时切换那一晚，不带时区的本地时间会让「用时」多出或少掉一小时
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()
def _todo_duration(items: list[dict]) -> str:
    """整张清单用了多久：从列出清单到最后一项打勾。每一项都带时间戳才算，缺一项就不说——不编。
    这是挂钟时间，中间关掉程序、隔天再来做完的，也算在里面。"""
    try:
        starts = [datetime.datetime.fromisoformat(str(it["created_at"])) for it in items]
        ends = [datetime.datetime.fromisoformat(str(it["done_at"])) for it in items]
    except (KeyError, TypeError, ValueError):
        return ""
    if not starts or any(t.tzinfo is None for t in starts + ends):
        return ""  # 没时区的时间戳算不准，宁可不说
    seconds = (max(ends) - min(starts)).total_seconds()
    if seconds < 0:
        return ""
    minutes = seconds / 60
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    return f"{int(minutes // 60)} 小时 {int(minutes % 60)} 分钟"
def render_constellation(items: list[dict], width: int, *, indent: str = "  ", current_label: str = "正在做",
                         show_current: bool = True, goal: str = "", compact: bool = False,
                         just_done: int | None = None) -> list[str]:
    """把任务清单画成一串星座：做完的 ✦ 用实线 ━ 连起来，正在做的那颗最亮，还没做的 ✧ 用虚线 ┄。

    意思全靠字形表达（✦/✧、━/┄、↑），颜色只是锦上添花——关掉颜色照样看得懂。
    宽度一律按显示宽度算，中文标签不会把星和字错开。放不下就改成竖排。
    compact=True 只画一行星星，给中途打勾用：模型一口气勾好几项时，不至于刷出好几大块。"""
    texts = [_clean_line(it.get("text", "")) for it in items]
    done = [bool(it.get("done")) for it in items]
    n = len(items)
    if not n:
        return []
    all_done = all(done)
    # 一行版没有「↑ 正在做」那行字，当前那颗要是也画成亮的 ✦，关掉颜色后就和做完的分不清了
    cur = next((i for i, d in enumerate(done) if not d), None) if show_current and not compact else None
    def star(i: int) -> str:
        if all_done:
            return paint("✦", "shine", "bold")
        if done[i]:
            return paint("✦", "accent")
        if i == cur:
            return paint("✦", "shine", "bold")
        return paint("✧", "dim")
    def link(i: int, length: int) -> str:
        if all_done:
            return paint("━" * length, "glow")
        solid = done[i] and (done[i + 1] or i + 1 == cur)
        return paint("━" * length, "accent") if solid else paint("┄" * length, "dim")
    summary = ""
    if all_done:
        took = _todo_duration(items)
        text = f"{goal or '全部完成'} · {n} 步"
        room = width - _visible_width(indent) - 2
        if took and _display_width(text + f" · 用时 {took}") <= room:
            text += f" · 用时 {took}"  # 放不下就整段不要，不截成半句「用时 11 小」
        summary = paint("✓", "accent") + " " + paint(_fit_display(text, max(4, room)), "dim")
    if compact:
        row = indent + "".join(star(i) + (link(i, 3) if i < n - 1 else "") for i in range(n))
        tail = f" {sum(done)}/{n}"
        if just_done is not None and 1 <= just_done <= n:
            tail += f" · 完成：{texts[just_done - 1]}"
        room = width - _visible_width(row) - 1
        if room >= 4:
            row += " " + paint(_fit_display(tail.strip(), room), "dim")
        if _visible_width(row) <= width:
            return [row]
        # 一行星星都放不下的话（几十项），就退回完整排版，让下面的竖排接手
    labels = [f"{i + 1}.{t}" for i, t in enumerate(texts)]
    finish = "  " + paint("✧ 完成", "shine", "bold") if all_done else ""
    avail = width - _visible_width(indent) - _visible_width(finish)
    slot = min(avail // n, max(10, max(_display_width(l) for l in labels) + 2), 24)
    rows: list[str] = []
    if slot >= 8:
        rows.append(indent + "".join(star(i) + (link(i, slot - 1) if i < n - 1 else "") for i in range(n)) + finish)
        cells = []
        for i, label in enumerate(labels):
            fitted = _pad_display(_fit_display(label, slot - 1), slot) if i < n - 1 else _fit_display(label, max(4, avail - i * slot))
            style = ("accent", "bold") if i == cur else ("dim",) if not done[i] else ()
            cells.append(paint(fitted, *style))
        rows.append((indent + "".join(cells)).rstrip())
        if cur is not None:
            col = min(cur * slot, max(0, width - _visible_width(indent) - _display_width(current_label) - 2))
            rows.append(indent + " " * col + paint(f"↑ {current_label}", "glow"))
    else:
        joints = n <= 6  # 太多项时连接线会把清单拉得老长，只留星星
        for i, text in enumerate(texts):
            mark = f"  ← {current_label}" if i == cur else ""
            body = _fit_display(f"{i + 1}. {text}", max(4, width - _visible_width(indent) - 2 - _display_width(mark)))
            style = ("accent", "bold") if i == cur else ("dim",) if not done[i] else ()
            rows.append(indent + star(i) + " " + paint(body, *style) + paint(mark, "glow"))
            if joints and i < n - 1:
                solid = all_done or (done[i] and (done[i + 1] or i + 1 == cur))
                rows.append(indent + (paint("┃", "glow" if all_done else "accent") if solid else paint("┊", "dim")))
        if all_done:
            rows.append(indent + paint("✧ 完成", "shine", "bold"))
    if summary:
        rows.append(indent + summary)
    return rows
class StreamView:
    """一回合的屏幕版面：思考、回答、工具调用排成一列，块与块之间空一行。

    回答逐行排版。一行还没收到换行时，每来一段字就把这半行重新排一遍、擦掉旧的画上新的，
    所以字是一个个流出来的，而 ** 这类记号从头到尾不会在屏幕上露面；
    这一行收全了就定稿，不再动它。

    按环境自动选三种工作方式：
    - 终端 + 动画：半行预览 + 擦除重画，外加等待动画；
    - 终端但 POLARIS_ANIMATE=0：一行收全了再排好输出，不动光标；
    - 不是终端（或子代理）：原样直出，不排版、不加转义码。"""
    def __init__(self, tag: str = "", *, render: bool | None = None, animate: bool | None = None,
                 width: int | None = None, out: Any = None):
        self.tag = tag if not tag or tag.endswith(" ") else tag + " "
        self.render = (_TTY and not tag) if render is None else render
        self.animate = (_ANIMATE and not tag) if animate is None else animate
        self.live = self.animate and self.render
        self._width = width
        self._out = out
        self.spinner: Spinner | None = None
        self.verb = random.choice(Spinner.VERBS)
        self._neutral_verb = self.verb
        # 心情从哪儿读（主代理接的是 MOOD.snapshot）；None 就一直用普通动词
        self.mood_source: Callable[[], dict] | None = None
        self._mood_key: str | None = None
        self.turn_started = time()
        self.block: str | None = None
        self._reset_block()
    def _reset_block(self) -> None:
        self.buf = ""            # 当前行已经收到、还没等到换行的部分
        self.previewing = False  # 屏幕上是不是挂着这半行的预览
        self.rows = 0            # 预览往下折了几排（擦的时候往上退几排）
        self.lines_out = 0       # 这一块已经上屏几行（第一行才带 ✦）
        self.blank_pending = 0   # 攒着的空行：后面还有正文才补出来，块尾的直接丢
        self.md: MarkdownRenderer | None = None
    @property
    def out(self) -> Any:
        return self._out or sys.stdout
    @property
    def width(self) -> int:
        return self._width or _term_width()
    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()
    # ── 等待动画 ──
    def begin_turn(self) -> None:
        # 这里不读心情：调用时 MOOD.begin_turn 还没跑，读到的是上一回合的状态
        self._neutral_verb = self.verb = random.choice(Spinner.VERBS)
        self._mood_key = None
        self.turn_started = time()
    def wait(self, label: str = "") -> None:
        """开始等（模型还没吐字、工具还在跑）。已经在转就不重开。

        心情在每次等待时重新读：一回合里每跑完一个工具心情都会变（失败一次受挫就涨一截），
        但动词只在心情换档时才换，平常的一回合从头到尾是同一个词，不会一闪一变。"""
        if self.spinner is not None and self.spinner.running:
            return
        self.close()
        style = None
        source = getattr(self, "mood_source", None)
        if self.animate and source is not None:
            try:
                style = spinner_mood(source())
            except Exception:
                style = None
        key = style.key if style else None
        if key != getattr(self, "_mood_key", None):
            self._mood_key = key
            self.verb = style.verb if style else getattr(self, "_neutral_verb", self.verb)
        # 明确给了 label 的（比如「回头检查」）字不变，但节奏和明暗照样跟着心情走
        self.spinner = Spinner(label or self.verb, since=self.turn_started, enabled=self.animate, gap=True,
                               interval=style.interval if style else None,
                               sweep=style.sweep if style else 1,
                               faint=bool(style and style.faint)).start()
    def stop_wait(self) -> None:
        if self.spinner is not None:
            self.spinner.stop()
    # ── 思考与回答 ──
    def _open(self, kind: str) -> None:
        self.stop_wait()
        if self.block == kind:
            return
        self.close()
        self.block = kind
        self._reset_block()
        if kind == "thinking":
            self._write(f"\n{self.tag}{paint('✳ 思考', 'accent')}\n")
        else:
            self._write("\n")
            self.md = MarkdownRenderer(self.width) if self.render else None
    def _prefixes(self) -> tuple[str, str]:
        if self.block == "thinking":
            return self.tag + "  ", self.tag + "  "
        if not self.render:
            return self.tag, self.tag
        first = paint("✦", "accent") + " " if self.lines_out == 0 else "  "
        return self.tag + first, self.tag + "  "
    def thinking(self, piece: str) -> None:
        if piece:
            self._open("thinking")
            self._feed(piece)
    def answer(self, piece: str) -> None:
        if self.block != "answer":
            piece = piece.lstrip("\n")  # 回答开头的空行没有意义
        if piece:
            self._open("answer")
            self._feed(piece)
    def _feed(self, piece: str) -> None:
        for i, part in enumerate(piece.split("\n")):
            if i:
                self._commit()
            if part:
                self._preview(part)
    def _flush_blanks(self) -> None:
        if self.blank_pending and self.lines_out:
            self._write("\n")  # 连续多个空行只留一个
        self.blank_pending = 0
    def _render_line(self, line: str, *, preview: bool) -> list[str]:
        first, rest = self._prefixes()
        if self.block == "thinking":
            return _wrap_segments([(line.strip(), ("dim", "italic"))], self.width, first, rest) if line.strip() else []
        if self.md is None:
            return [first + line]
        md = self.md
        if preview:
            # 预览排的是半行，不能动真正的渲染器状态：半行 ``` 也会把代码块开关拨一下
            md = MarkdownRenderer(self.width, preview=True)
            md.in_code = self.md.in_code
        return md.render(line, first, rest)
    def _erase_seq(self) -> str:
        if not self.previewing:
            return ""
        seq = (f"\x1b[{self.rows}A" if self.rows else "") + "\r\x1b[J"
        self.previewing, self.rows = False, 0
        return seq
    def _preview(self, text: str) -> None:
        starting = not self.buf
        self.buf += text
        if not self.render:
            if starting:
                self._flush_blanks()
                self._write(self._prefixes()[0])
            self._write(paint(text, *(("dim", "italic") if self.block == "thinking" else ())))
            return
        if not self.live:
            return
        # 只有空格的半行先不画：它多半是个空行，画出来再擦会多留一个空行
        in_code = self.md is not None and self.md.in_code
        rows = self._render_line(self.buf, preview=True) if (self.buf.strip() or in_code) else []
        seq = self._erase_seq()
        if rows:
            if self.blank_pending and self.lines_out:
                seq += "\n"
            self.blank_pending = 0 if self.lines_out else self.blank_pending
            # 擦旧的、画新的放在同一次 write 里，终端一次画完，不会闪
            seq += "\n".join(rows)
            self.previewing, self.rows = True, len(rows) - 1
        self._write(seq)
    def _commit(self) -> None:
        line, self.buf = self.buf, ""
        if not self.render:
            if line:
                self._write("\n")
                self.lines_out += 1
            else:
                self.blank_pending += 1
            return
        seq = self._erase_seq()
        in_code = self.md is not None and self.md.in_code
        if not line.strip() and not in_code:
            self._write(seq)
            self.blank_pending += 1
            return
        rows = self._render_line(line, preview=False)
        if rows:
            if self.blank_pending and self.lines_out:
                seq += "\n"  # 连续多个空行只留一个
            self.blank_pending = 0
            seq += "".join(row + "\n" for row in rows)
            self.lines_out += 1
        self._write(seq)
    def _erase_preview(self) -> None:
        self._write(self._erase_seq())
    def close(self) -> None:
        """结束当前这一块：没收到换行的半行也排出来。"""
        if self.block is None:
            return
        if self.buf:
            self._commit()
        self._erase_preview()
        self.block = None
        self._reset_block()
    # ── 工具与提示 ──
    def tool_call(self, name: str, args_str: str) -> None:
        self.stop_wait()
        self.close()
        room = self.width - _display_width(self.tag) - _display_width(name) - 4
        args = _fit_display(_format_tool_args(args_str), max(8, room))
        self._write(f"\n{self.tag}" + paint("●", "accent") + " " + paint(name, "bold") + paint(f"({args})", "dim") + "\n")
    def tool_result(self, output: str, ok: bool) -> None:
        self.stop_wait()
        head, more = _summarize_tool_output(output)
        tail = f"  … +{more} 行" if more else ""
        room = self.width - _display_width(self.tag) - 4 - _display_width(tail)
        mark = paint("⎿", "gray" if ok else "red")
        self._write(f"{self.tag}  {mark} " + paint(_fit_display(head, max(8, room)), "dim" if ok else "red")
                    + paint(tail, "dim") + "\n")
    # ── 记忆 ──
    @staticmethod
    def _recall_when(row: dict, now: datetime.datetime) -> str:
        if row.get("cued") or row.get("dormant"):
            return "被唤醒"  # 本来已经沉睡，被一句足够具体的话叫了回来
        if row.get("pinned"):
            return "常驻"
        if Memory.tier_of(row.get("retention", 1.0)) == "fading":
            return "正在淡去"
        return _ago(row.get("last_review") or row.get("time"), now)
    def recall(self, rows: list[dict], now: datetime.datetime | None = None) -> None:
        """◌ 想起 N 件事：每条记忆在被这次想起之前还剩多清晰、上次想起是什么时候。"""
        if not rows:
            return
        self.stop_wait()
        self.close()
        now = now or _now_dt()
        shown = rows[:3]
        whens = [self._recall_when(r, now) for r in shown]
        contents = [_clean_line(r.get("content", "")) for r in shown]
        width = self.width - _display_width(self.tag)
        # 「  · 」+ 内容 +「  」+ 5 格 +「 」+ 时间；内容栏对齐，进度条才排得成一列。
        # 终端太窄时先丢时间、再丢进度条，内容至少留 8 格——宁可少说，也不折行
        show_when, show_bar = True, True
        room = width - 4 - 2 - 5 - 1 - max(_display_width(w) for w in whens)
        if room < 8:
            show_when, room = False, width - 4 - 2 - 5
        if room < 8:
            show_bar, room = False, width - 4
        col = max(4, min(max(_display_width(c) for c in contents), room))
        lines = [f"\n{self.tag}" + paint("◌", "accent") + " " + paint(_fit_display(f"想起 {len(rows)} 件事", max(4, width - 2)), "dim")]
        for row, text, when in zip(shown, contents, whens):
            faint = when in ("正在淡去", "被唤醒")
            line = (f"{self.tag}  " + paint("·", "dim") + " "
                    + paint(_pad_display(_fit_display(text, col), col), *(("dim",) if faint else ())))
            if show_bar:
                line += "  " + _paint_bar(_retention_bar(row.get("retention", 1.0)))
            if show_when:
                line += " " + paint(when, "dim")
            lines.append(line)
        if len(rows) > 3:
            lines.append(f"{self.tag}    " + paint(f"… 还有 {len(rows) - 3} 件", "dim"))
        self._write("\n".join(lines) + "\n")
    def remembered(self, content: str, *, label: str = "记住了", as_result: bool = False) -> bool:
        """✎ 记住了：新写下的记忆。刚写下的保持率就是 1，所以满格不是装饰。
        as_result=True 时顶替工具的 ⎿ 结果行，不然 remember(…)、已写入 #N、记住了 会把同一件事说三遍。"""
        self.stop_wait()
        self.close()
        lead = f"{self.tag}  " if as_result else f"\n{self.tag}"
        tail = "  " + paint("▰▰▰▰▰", "accent") + " " + paint("新", "dim")
        # 「✎ 」两格 + 「标签：」+ 内容 + 尾巴，加起来不能超过 width；窄到放不下时先丢进度条
        fixed = _visible_width(lead.lstrip("\n")) + 2 + _display_width(f"{label}：")
        room = self.width - fixed - _visible_width(tail)
        if room < 8:
            tail, room = "", self.width - fixed
        self._write(lead + paint("✎", "accent") + " " + paint(f"{label}：", "dim")
                    + _fit_display(_clean_line(content), max(4, room)) + tail + "\n")
        return True
    # ── 任务进度 ──
    FLASH_INTERVAL = 0.09
    def todo_progress(self, items: list[dict], *, flash: bool = False, compact: bool = False,
                      just_done: int | None = None) -> bool:
        """把任务清单画成星座，顶替工具的 ⎿ 结果行。只是换个画法——发给模型的结果原样不动。
        不是终端时返回 False，调用方照旧打印普通的一行结果。"""
        self.stop_wait()
        if not self.render:
            return False
        self.close()
        width = self.width - _display_width(self.tag) - 4
        rows = render_constellation(items, width, indent="", compact=compact, just_done=just_done)
        if not rows:
            return False
        first = f"{self.tag}  " + paint("⎿", "gray") + " "
        rest = [f"{self.tag}    " + row for row in rows[1:]]
        # 只有横排时第一行才是整串星座；竖排的第一行只是第一项，闪它会像是第一项刚做完
        if not (flash and self.live and "完成" in _ANSI_RE.sub("", rows[0])):
            self._write("\n".join([first + rows[0]] + rest) + "\n")
            return True
        # 最后一项打勾：整串星座闪三下再定住。只在这一行上原地重画（\r），不往上挪光标，
        # 所以闪完留在屏幕上的和不闪时一模一样。闪的时候按 Ctrl+C：先把剩下的画完，再把打断交出去
        bright = paint(_ANSI_RE.sub("", rows[0]), "shine", "bold")
        interrupted = False
        try:
            self._write("\x1b[?25l" + first + rows[0])
            try:
                for _ in range(3):
                    for frame in (bright, rows[0]):
                        threading.Event().wait(self.FLASH_INTERVAL)
                        self._write("\r" + first + frame + "\x1b[K")
            except KeyboardInterrupt:
                interrupted = True
                self._write("\r" + first + rows[0] + "\x1b[K")
            self._write("\n" + "".join(line + "\n" for line in rest))
        finally:
            self._write("\x1b[?25h")
        if interrupted:
            raise KeyboardInterrupt
        return True
    def notice(self, text: str, *styles: str) -> None:
        self.stop_wait()
        self.close()
        self._write(f"\n{self.tag}{paint(text, *styles)}\n")
    def end(self) -> None:
        """回合结束（包括被打断、出异常）：停动画，把没写完的收尾。"""
        self.stop_wait()
        self.close()
def _as_str_list(value: Any) -> list[str]:
    """把模型传来的「一组字符串」参数规范化。

    工具签名写的是 list[str]，但模型完全可能直接传一个字符串。
    Python 的 for 会把字符串按字符迭代，于是 set_todos("买牛奶") 变成
    「买」「牛」「奶」三条待办，而且原清单已经被覆盖掉了；
    delegate_tasks 遇到同样的输入会派出 3 个真实的子代理会话。
    单个字符串按「一项」处理——这几乎肯定是模型的本意，
    也比悄悄炸成一堆单字安全得多。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    elif not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
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
# 启动门禁：开机先问模型「现在有没有必要上线」，它可以拒绝启动。默认关——
# 用户已经敲了命令，就是要用它；每次启动多花一次调用、多等几秒，还可能被拒，不值。
STARTUP_GATE = _env_bool("POLARIS_STARTUP_GATE", False)
# 记忆闪回：回答前亮出这一回合想起了哪几条记忆、各自还剩多清晰。
# 默认开——遗忘曲线是这个项目的核心，平时看不见它等于没有。录屏、演示时可以关掉，
# 也可以在会话里用 /flashback off 临时关。
SHOW_RECALL = _env_bool("POLARIS_SHOW_RECALL", True)
# 开屏星空会把记忆片段和上次聊的话题直接印在屏幕上；共享屏幕时可以用 POLARIS_SKY=0 换回朴素开屏
SHOW_SKY = _env_bool("POLARIS_SKY", True)
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
# 一次 delegate_tasks 最多能派几个子代理。每个都是真实的 LLM 会话，要花钱。
MAX_PARALLEL_TASKS = max(1, int(os.environ.get("POLARIS_MAX_PARALLEL_TASKS", "8")))
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
# cot = 真正的思维链：模型在做决定之前自己写下推理，这段推理留在上下文里、
# 影响后续每一步，同时展示给用户。template = 离线模板独白（不发请求）。
# llm / hybrid 是 1.1.6 之前的老行为：为一段打印完就丢的话另开一次请求，保留仅为兼容。
MONOLOGUE_MODE = os.environ.get("POLARIS_MONOLOGUE_MODE", "cot").strip().lower() or "cot"
# 模型的思考要不要写进消息历史。关掉就只展示不入历史（个别服务商建议
# 多轮之间不回喂自己的 CoT），但那样思考就不再影响后续决策了。
COT_FEEDBACK = _env_bool("POLARIS_COT_FEEDBACK", True)
# 工具档位：full / code / min / read，见 TOOL_PROFILES。默认 full 保持向后兼容，
# 用 /lean 或这个环境变量切到精简档能砍掉六成以上的每步固定开销。
TOOL_PROFILE = os.environ.get("POLARIS_TOOL_PROFILE", "full").strip().lower() or "full"
# 易变的运行时上下文（心情/记忆/工作区）放哪：
# tail = 挂在消息末尾，让「工具 + 人设 + 历史」成为稳定前缀，可被服务端缓存；
# system = 1.1.6 之前的老排法，塞进系统提示词（每回合都变，等于每回合都在废掉缓存）。
# 对话历史的 token 预算。只按消息条数裁剪挡不住大文件：一条 read_file 结果
# 可达两万多字符，40 条就是约 24 万 tokens，早就撑爆窗口了。
MAX_CONTEXT_TOKENS = max(1000, int(os.environ.get("POLARIS_MAX_CONTEXT_TOKENS", "32000")))
CONTEXT_POSITION = os.environ.get("POLARIS_CONTEXT_POSITION", "tail").strip().lower() or "tail"
if CONTEXT_POSITION not in {"tail", "system"}:
    CONTEXT_POSITION = "tail"
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
        # 记忆文件可以手改、也可以从别处同步过来，里面可能是带时区的时间（…Z、…+08:00）。
        # 全程拿它和不带时区的「现在」相减，所以统一换成本地时间、去掉时区——不然一减就崩。
        # 3.10 的 fromisoformat 还不认结尾的 Z，先换成 +00:00
        dt = datetime.datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text)
        return dt.astimezone().replace(tzinfo=None) if dt.tzinfo is not None else dt
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
               quality: float = 1.0, trace: list | None = None) -> list[dict]:
        """trace 给一个列表时，把每条命中在「被这次召回强化之前」的样子记进去。

        界面上的记忆闪回要显示的是想起它那一刻有多清晰。强化会原地改写这些记忆，
        之后再读就全是满格、全是「刚刚」——那就成了骗人的进度条。"""
        query = keyword.strip()
        if not query:
            pool = [m for m in self.items if include_dormant or not m.get("dormant")]
            picked = list(reversed(pool[-limit:]))
            if trace is not None:
                now = _now_dt()
                trace.extend(self._recall_snapshot(m, self.retention(m, now)) for m in picked)
            return picked
        hits = self.rank(query, limit=limit, semantic=semantic, include_dormant=include_dormant)
        snaps = [self._recall_snapshot(row["item"], row["retention"], cued=bool(row.get("cued")))
                 for row in hits] if trace is not None else []
        if reinforce and hits:
            # 只强化真正被端上来的前几条，一次翻 50 条不该让整个库都变结实。
            # 注意这里不能用 any(生成器)：any 会短路，第一条复习成功后面几条就被跳过了。
            touched = [self.reinforce(row["item"]["id"], quality) for row in hits[:8]]
            for snap, changed in zip(snaps, touched):
                snap["reinforced"] = changed
            if any(touched):
                self._save()
        if trace is not None:
            trace.extend(snaps)
        return [row["item"] for row in hits]
    @staticmethod
    def _recall_snapshot(item: dict, retention: float, *, cued: bool = False) -> dict:
        # 拷值不拷引用：item 马上就会被 reinforce 原地改掉
        return {
            "id": item["id"], "content": item.get("content", ""), "category": item.get("category", ""),
            "retention": float(retention), "last_review": item.get("last_review"), "time": item.get("time"),
            "pinned": bool(item.get("pinned")), "dormant": bool(item.get("dormant")),
            "cued": cued, "reinforced": False,
        }
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
    # 「清晰」「正在淡忘」的分界。/memstat、开屏星空、记忆闪回都从这里取，
    # 三处各写一份数字的话，迟早有一处会和另外两处对不上。
    VIVID_AT = 0.7
    FADING_BELOW = 0.3
    @classmethod
    def tier_of(cls, retention: float, *, dormant: bool = False) -> str:
        """dormant / vivid（清晰）/ steady（微光，夹在中间那一档）/ fading（正在淡忘）。"""
        if dormant:
            return "dormant"
        if retention >= cls.VIVID_AT:
            return "vivid"
        return "fading" if retention < cls.FADING_BELOW else "steady"
    def tier(self, item: dict, now: datetime.datetime | None = None) -> str:
        return self.tier_of(self.retention(item, now), dormant=bool(item.get("dormant")))
    def curve_stats(self, now: datetime.datetime | None = None) -> dict:
        now = now or _now_dt()
        active = [m for m in self.items if not m.get("dormant")]
        tiers = [self.tier_of(self.retention(m, now)) for m in active]
        retentions = [self.retention(m, now) for m in active]
        return {
            "total": len(self.items),
            "active": len(active),
            "dormant": sum(1 for m in self.items if m.get("dormant")),
            "pinned": sum(1 for m in self.items if m.get("pinned")),
            "vivid": tiers.count("vivid"),
            "steady": tiers.count("steady"),
            "fading": tiers.count("fading"),
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
@tool(mutating=True)
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
@tool(dangerous=True, mutating=True)
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
@tool(mutating=True)
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
        # created_at / done_at 只给界面上的「用时」用；发给模型的 render() 一个字都不变。
        # 老文件里的条目没有时间戳，那就不显示用时，绝不估一个出来
        now = _todo_stamp()
        self.items = [{"text": str(t).strip(), "done": False, "created_at": now} for t in todos if str(t).strip()]
        self._save()
    def complete(self, index: int) -> bool:
        if 1 <= index <= len(self.items):
            item = self.items[index - 1]
            if not item.get("done"):
                item["done_at"] = _todo_stamp()  # 重复打勾不刷新完成时间
            item["done"] = True
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
@tool(mutating=True)
def set_todos(todos: list[str]) -> str:
    """用一组新任务整体覆盖当前待办清单，适合在开始多步任务前先把计划列出来。
    注意是整体替换而不是追加，调用时要把还没做完的旧任务一起带上。"""
    items = _as_str_list(todos)
    if not items:
        # 传了但解析不出内容时，不能默默把旧清单清空。
        return "错误：todos 需要是一组任务文本，收到的内容解析不出任何任务。当前清单保持不变。"
    TODOS.set(items)
    return "Task list updated:\n" + (TODOS.render() or "(empty)")
@tool(mutating=True)
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
    last_reasoning = ""  # 最近一次模型真实写下的思考（cot 模式），供上下文与 /thought 展示
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
class ThinkingSplitter:
    """把模型输出里的 <thinking> 段和正式回答分开，支持流式增量喂入。

    这是「真 CoT」和「内心戏」的分界线：
    旧的 ThoughtEngine 是另开一次请求生成一段话、打印完就丢，
    对答案没有任何因果影响——好看，但纯属表演，还白花一次调用。
    现在这段思考是模型在同一次生成里、做决定之前写下的，
    而且原样留在 messages 里，后续每一步都建立在它之上。
    它只是不进「给用户看的答案」而已。"""
    OPEN, CLOSE = "<thinking>", "</thinking>"
    def __init__(self):
        self.raw = ""        # 原样保留，进 messages
        self.thinking = ""
        self.answer = ""
        self._pending = ""   # 还看不出是标签开头还是正文的部分
        self._state = "unknown"
    def feed(self, delta: str) -> tuple[str, str]:
        """喂入一段增量，返回这次新增的 (思考片段, 答案片段)。"""
        if not delta:
            return "", ""
        self.raw += delta
        think_out, answer_out = "", ""
        buf = self._pending + delta
        self._pending = ""
        while buf:
            if self._state == "answer":
                answer_out += buf
                self.answer += buf
                break
            if self._state == "thinking":
                idx = buf.find(self.CLOSE)
                if idx == -1:
                    # 收尾可能正好切在 </thinking> 中间，留住尾巴等下一片
                    keep = self._partial_tail(buf, self.CLOSE)
                    body = buf[:len(buf) - keep] if keep else buf
                    self._pending = buf[len(buf) - keep:] if keep else ""
                    think_out += body
                    self.thinking += body
                    break
                think_out += buf[:idx]
                self.thinking += buf[:idx]
                buf = buf[idx + len(self.CLOSE):].lstrip("\n")
                self._state = "answer"
                continue
            # state == unknown：还在判断开头是不是 <thinking>
            stripped = buf.lstrip()
            if stripped.startswith(self.OPEN):
                buf = stripped[len(self.OPEN):]
                self._state = "thinking"
                continue
            if self.OPEN.startswith(stripped) and stripped:
                self._pending = buf  # 可能是标签的前缀，继续等
                break
            self._state = "answer"
        return think_out, answer_out
    @staticmethod
    def _partial_tail(buf: str, tag: str) -> int:
        """buf 末尾有多少字符可能是 tag 的前缀（跨分片的半个标签）。"""
        for size in range(min(len(tag) - 1, len(buf)), 0, -1):
            if tag.startswith(buf[-size:]):
                return size
        return 0
    def finish(self) -> tuple[str, str]:
        """流结束：还没判定的残留一律算正文。返回这次补出来的 (思考片段, 答案片段)，
        调用方要把它们也显示出来——不然回答最后几个字会只进历史、不上屏。"""
        think_out, answer_out = "", ""
        if self._pending:
            if self._state == "thinking":
                think_out = self._pending
                self.thinking += self._pending
            else:
                answer_out = self._pending
                self.answer += self._pending
            self._pending = ""
        if self._state == "unknown":
            self._state = "answer"
        return think_out, answer_out
    @classmethod
    def split(cls, text: str) -> tuple[str, str]:
        """一次性拆分（非流式路径用）。"""
        sp = cls()
        sp.feed(text)
        sp.finish()
        return sp.thinking.strip(), sp.answer.strip()
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
    @staticmethod
    def _risk(mood: dict) -> int:
        risk = 0
        if mood["fatigue"] >= 75:
            risk += 2
        if mood["frustration"] >= 70:
            risk += 2
        if mood["confidence"] <= 30:
            risk += 2
        if len(WORKSPACE.recent_files) >= 12:
            risk += 1
        if len(WORKSPACE.special_files) >= 5:
            risk += 1
        if len(TODOS.items) >= 6:
            risk += 1
        if len(WORKSPACE.file_index) == 0:
            risk = max(0, risk - 1)
        return risk
    def snapshot(self) -> dict:
        mood = MOOD.snapshot()
        recent_files = len(WORKSPACE.recent_files)
        special_files = len(WORKSPACE.special_files)
        todo_count = len(TODOS.items)
        file_count = len(WORKSPACE.file_index)
        risk = self._risk(mood)
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
        return self.budget_for(MOOD)
    def budget_for(self, mood: "MoodState") -> int:
        """给定一份心情，这一回合会有几步。开屏那行心情用它来预测——
        和真正跑循环时走同一段代码，说出来的步数才不会差一两步。"""
        budget = mood.turn_budget()
        risk = self._risk(mood.state)
        if risk >= 5:
            budget += 3
        elif risk >= 3:
            budget += 1
        if len(TODOS.items) >= 8:
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
        # 最近一次计划拆出来的步骤，给 /plan 画星座用。_decompose 不是确定性的
        # （会看心情、看有没有补充上下文），所以只能存下来，不能事后再拆一遍
        self.last_steps: list[str] = []
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
        self.last_steps = list(steps)
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
        # 目录不能只在 __init__ 里建一次：用户手动删掉 .polaris_snapshots/、
        # 或者进程中途换了工作目录，写入就会 FileNotFoundError 直接崩。
        self.root.mkdir(parents=True, exist_ok=True)
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
            "reasoning": THOUGHT_ENGINE.last_reasoning[-200:],
        }
        return hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8", "replace")).hexdigest()
    def render(self, user_input: str = "") -> str:
        # 有真实推理就回放真实推理。模板独白在这里没有信息量——
        # 「平静 | Summary | ✓ Reading request」对完成任务没有任何帮助。
        if THOUGHT_ENGINE.last_reasoning:
            return "上一步的思考：" + " ".join(THOUGHT_ENGINE.last_reasoning.split())[:240]
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
    # 最近一次检索到了什么（强化之前的样子），给界面上的「记忆闪回」用。
    # 挂在实例上而不是全局 MEMORY 上：并行的子代理各有各的 provider，不会互相覆盖。
    last_recall: dict = {"kind": "none", "query": "", "rows": [], "at": 0.0}
    def render(self, user_input: str = "") -> str:
        query = _context_query(user_input)
        snaps: list[dict] = []
        if query:
            # 自动注入是「被动想起」，复习强度给一半：真正张口问出来的召回才算数。
            hits = MEMORY.search(query, limit=4, quality=0.5, trace=snaps)
        else:
            hits = MEMORY.search("", limit=3, trace=snaps)
        # kind=recent 是「没线索时顺手带上最近几条」，不是想起来的，界面上不当成回忆显示
        self.last_recall = {"kind": "recall" if query else "recent", "query": query,
                            "rows": snaps[:4] if hits else [], "at": time()}
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
    # run_shell 现在无条件带上退出码，这是唯一精确的成败信号——
    # 比在输出里模糊匹配 "error" 之类的词可靠得多（grep error 成功时也会命中）。
    if text.startswith("退出码 ") and not text.startswith("退出码 0"):
        return True
    # traceback.format_exc() 直接当返回值的情况
    return text.startswith("Traceback (most recent call last)")
@tool
def read_file(path: str, offset: int = 1, limit: int = 400) -> str:
    """按行读取文本文件，返回带行号的内容。修改任何文件之前都应该先读一遍，不要凭猜测改。
    offset 是起始行号（从 1 开始），limit 是最多读多少行。
    文件比 limit 长时结尾会明确告诉你还剩多少行、下一次该从哪一行接着读——
    大文件请翻页读完关键部分，不要只看开头就动手改。
    行号可以直接和 search_files 的输出对上。"""
    if not os.path.exists(path):
        return f"文件不存在：{os.path.abspath(path)}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except Exception:
        return traceback.format_exc(limit=2)
    if not lines:
        return "(空)"
    total = len(lines)
    start = max(1, int(offset or 1))
    if start > total:
        return f"错误：offset {start} 超出文件长度（共 {total} 行）。"
    count = max(1, int(limit or 400))
    end = min(total, start + count - 1)
    width = len(str(end))
    body = "\n".join(f"{n:>{width}}\t{lines[n - 1]}" for n in range(start, end + 1))
    header = f"{os.path.abspath(path)} 第 {start}-{end} 行 / 共 {total} 行"
    # 老实说清楚还剩多少：旧实现只丢一句「已截断」，模型根本不知道自己漏了什么，
    # 也没有任何办法读到后面——于是对 175 行之后的代码全盲却毫无察觉。
    footer = "" if end >= total else f"\n\n（还剩 {total - end} 行未读，继续读请用 offset={end + 1}）"
    return _truncate_text(f"{header}\n{body}", 24000) + footer
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
    返回值第一行永远是退出码，这是判断成败最可靠的依据，别只看输出文字。
    重要：不要用 `| tail`、`| head` 之类的管道包住测试命令——管道会把退出码换成最后一个命令的，
    于是 pytest 明明挂了也显示「退出码 0」。要截断输出请直接跑，输出太长会自动截断。
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
    # 退出码必须无条件回给模型。旧实现只在「没有任何输出」时才附上退出码，
    # 于是最要紧的一类结果被丢了：pytest 挂了 5 个用例 → 有输出 → 模型只看到
    # 一段文字，拿不到成败信号，轨迹还标成绿色的成功。
    # stdout / stderr 也分开标注，不再拼成一坨分不清谁说的。
    parts = []
    if r.stdout.strip():
        parts.append(f"[stdout]\n{r.stdout.strip()}")
    if r.stderr.strip():
        parts.append(f"[stderr]\n{r.stderr.strip()}")
    status = "退出码 0（成功）" if r.returncode == 0 else f"退出码 {r.returncode}（失败）"
    return _truncate_text(status + "\n" + ("\n".join(parts) or "(无输出)"))
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
    items = _as_str_list(tasks)
    if not items:
        return "(空任务列表)"
    # 每一项都是一次真实的子代理 LLM 会话。参数畸形时不设上限的话，
    # 一个 30 字符的字符串就等于 30 次调用——真金白银。
    if len(items) > MAX_PARALLEL_TASKS:
        return (f"错误：一次最多并行 {MAX_PARALLEL_TASKS} 个子任务，收到 {len(items)} 个。"
                f"请合并任务，或分多次调用。")
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
# ───────────────────────────────────────────────────────────────────────────────
# 工具档位：省 token 最有效的一个旋钮
#
# 实测过一遍：单步固定开销里 82% 是工具 schema（35 个工具约 3.5k tokens），
# 而且每一步都要重发。相比之下人设 + CoT 指令 + 十个 context provider 加起来
# 才不到 800 tokens——所以砍人设基本是白费力气，砍工具才是大头。
#
# 少给工具还有个正向副作用：模型不用在 35 个里挑花眼，选择更准。
# 档位只筛内置工具；插件和 MCP 工具是用户自己接进来的，一律照常暴露。
# ───────────────────────────────────────────────────────────────────────────────
TOOL_PROFILES: dict[str, set[str] | None] = {
    "full": None,  # None = 不筛
    "code": {
        "read_file", "write_file", "edit_file", "search_files", "run_shell", "run_python",
        "set_todos", "complete_todo", "remember", "recall_memories", "get_current_time",
    },
    "min": {"read_file", "edit_file", "search_files", "run_shell"},
    "read": {"read_file", "search_files", "recall_memories", "search_workspace", "search_chat_history"},
}
def _estimate_tokens(text: str) -> int:
    """粗估 token 数：中文约 1.5 字符/token，其余按 4 字符/token。
    只用来给用户一个量级感（/lean 的省了多少），不参与任何计费或截断决策。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return int(cjk / 1.5 + (len(text) - cjk) / 4)
def tool_profile_cost(profile: str) -> tuple[int, int]:
    """返回某个档位下（内置工具的数量, 估算 token 数）。"""
    allowed = TOOL_PROFILES.get(profile)
    picked = [t for t in BUILTIN_TOOLS if allowed is None or t.schema["name"] in allowed]
    payload = json.dumps(
        [{"type": "function", "function": {"name": t.schema["name"],
          "description": t.schema["description"], "parameters": t.schema["input_schema"]}}
         for t in picked], ensure_ascii=False)
    return len(picked), _estimate_tokens(payload)
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
COT_INSTRUCTION = """### 思考方式

每次回答或调用工具之前，先在 <thinking></thinking> 里把事情想清楚，然后再动手。

思考里写这些：现在确切知道什么、还缺什么、下一步为什么选这个做法、哪里最可能出错。
不确定就说不确定——这段思考是你后续判断的依据，会一直留在上下文里，
所以要写真实的推理，不要写"让我仔细分析一下"这种没有信息量的场面话。

</thinking> 之后再给正式回复。用户看得到你的思考，但思考不是答案本身。

改代码之前，思考里至少要能回答：我读全了相关代码吗？改动会影响到谁？怎么验证它真的对？"""
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
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        auto_load_plugins: bool = True,
        mcp_servers: list[MCPServerStdio] | None = None,
        show_thought: bool | None = None,
        relationship_enabled: bool = True,
        archive_enabled: bool = True,
        trace_enabled: bool = True,
        tool_profile: str | None = None,
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
        self.show_recall = SHOW_RECALL
        self.relationship_enabled = relationship_enabled
        # 子代理必须关掉档案写入：它的 prompt 里塞满了 shared_context，
        # 一旦写进 agent_conversations.jsonl 就会污染 search_chat_history /
        # last_window / ExperienceModel，让「共同经历」里混进一堆机器自言自语。
        self.archive_enabled = archive_enabled
        self.trace_enabled = trace_enabled
        self.tag = tag
        self.max_context_messages = max_context_messages
        self.max_context_tokens = max_context_tokens
        self.messages: list[dict] = []
        self.tools: dict[str, Callable] = {t.schema["name"]: t for t in (tools or [])}
        self.checkpoints: list[tuple[str, str | None]] = []
        self.loaded_plugins: list[str] = []
        self.context_builder = ContextBuilder()
        self._system_prompt_cache_key = ""
        self._system_prompt_cache = ""
        self._current_user_input = ""
        self._thinking_open = False
        self.last_reasoning = ""
        self._native_reasoning_seen = False
        picked = (tool_profile or TOOL_PROFILE).strip().lower()
        self.tool_profile = picked if picked in TOOL_PROFILES else "full"
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
    def _tool_in_profile(self, name: str) -> bool:
        """档位只筛内置工具。插件和 MCP 工具是用户主动接进来的，不替他做主。"""
        allowed = TOOL_PROFILES.get(self.tool_profile)
        if allowed is None:
            return True
        if name in allowed:
            return True
        return not any(t.schema["name"] == name for t in BUILTIN_TOOLS)
    def set_tool_profile(self, profile: str) -> str:
        profile = (profile or "").strip().lower()
        if profile not in TOOL_PROFILES:
            return f"未知档位 {profile!r}。可选：{'、'.join(TOOL_PROFILES)}"
        before_n, before_t = tool_profile_cost(self.tool_profile)
        self.tool_profile = profile
        self._system_prompt_cache_key = ""  # 档位变了，提示词缓存作废
        after_n, after_t = tool_profile_cost(profile)
        saved = before_t - after_t
        line = f"工具档位：{profile}（内置工具 {after_n} 个，约 {after_t:,} tokens/步）"
        if saved > 0:
            line += f"\n每步省下约 {saved:,} tokens；10 步的任务约省 {saved * 10:,}。"
        elif saved < 0:
            line += f"\n每步多花约 {-saved:,} tokens。"
        return line
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
            if self._tool_in_profile(t.schema["name"])
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
    def _pinned_messages(self, budget: int) -> list[dict]:
        """用户说过的话优先保住，工具结果才是该让位的那个。

        按时间裁剪意味着最老的先死，但在编程会话里，用户消息又短又关键——
        任务、约束、中途的纠正——而占地方的全是工具结果。实测一个 34 条的
        编程会话：所有用户消息加起来 22 tokens，占总量 0.3%。

        只钉第一条是不够的。真实场景里你会中途改主意（「等一下，改用 RS256」），
        那句比最初的任务更贴近当下，丢了它 Agent 会自信地按旧方案继续做，
        比全忘了还糟。

        总量超过预算一半时优先留最新的几条，但第一条（任务描述）始终保留——
        它带着那些只说过一次、之后再没重复过的约束。"""
        users = [msg for msg in self.messages if msg.get("role") == "user"]
        if not users:
            return []
        def cost_of(msg: dict) -> int:
            return _estimate_tokens(json.dumps(msg, ensure_ascii=False))
        # 上限是硬的。先给第一条（任务描述）预留，再用剩下的额度从新往旧装。
        # 早先那版是「装完再无条件把第一条插回去」，结果用户粘两段大日志就
        # 直接冲破预算——钉住用户消息不能反过来变成撑爆上下文的新途径。
        cap = max(1, budget // 2)
        chosen: list[dict] = []
        used = 0
        if cost_of(users[0]) <= cap:
            chosen.append(users[0])
            used = cost_of(users[0])
        for msg in reversed(users[1:]):
            cost = cost_of(msg)
            if used + cost > cap:
                break
            chosen.append(msg)
            used += cost
        order = {id(msg): idx for idx, msg in enumerate(self.messages)}
        chosen.sort(key=lambda msg: order.get(id(msg), 0))
        return chosen
    def _keep_window(self) -> list[dict]:
        """按「条数」和「token 预算」两个上限取较紧的那个。

        只按条数裁是不够的：一条 read_file 的结果可以有两万多字符，
        40 条这样的消息约 24 万 tokens，早就把窗口撑爆了（直接 400）。
        这里从最新的一条往回收，收满预算为止；无论如何至少保留最后一条，
        否则一条超长消息会把整个历史清空。"""
        window = self.messages[-self.max_context_messages:]
        def collect(budget: int) -> list[dict]:
            kept: list[dict] = []
            used = 0
            for msg in reversed(window):
                cost = _estimate_tokens(json.dumps(msg, ensure_ascii=False))
                if kept and used + cost > budget:
                    break
                kept.append(msg)
                used += cost
            kept.reverse()
            return kept
        budget = max(1000, self.max_context_tokens)
        kept = collect(budget)
        # 给用户消息单独留位置。注意这不会多删任何东西——该删几条还是几条，
        # 只是把被删的名额优先让给工具结果。
        # 判断依据必须是「它有没有活下来」：条数没超但 token 预算超了的时候，
        # 它照样会在上面那一关被刷掉。
        pinned = self._pinned_messages(budget)
        missing = [msg for msg in pinned if not any(k is msg for k in kept)]
        if missing:
            reserve = sum(_estimate_tokens(json.dumps(msg, ensure_ascii=False)) for msg in missing)
            kept = collect(max(1, budget - reserve))
            missing = [msg for msg in pinned if not any(k is msg for k in kept)]
            # 插回去要保持原始先后顺序，不能按别的顺序拼——
            # 对话是有前后文的，顺序错了模型读出来就是另一个意思。
            order = {id(msg): idx for idx, msg in enumerate(self.messages)}
            kept = sorted(kept + missing, key=lambda msg: order.get(id(msg), len(order)))
        return kept
    def _trim_messages(self) -> None:
        trimmed = self._keep_window()
        if len(trimmed) == len(self.messages):
            return
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
    def _view(self) -> StreamView:
        """这个代理的屏幕版面。懒创建：自检里的探针是 Agent.__new__ 出来的，不走 __init__。"""
        view = self.__dict__.get("_stream_view")
        if view is None:
            view = StreamView(getattr(self, "tag", ""))
            if not getattr(self, "tag", ""):
                view.mood_source = MOOD.snapshot  # 只有主代理的等待动画跟着心情走
            self._stream_view = view
        return view
    def interrupt(self) -> None:
        """回合被 Ctrl+C 打断（或出异常）之后收拾现场。

        打断可能正好落在「模型要调工具、工具结果还没回来」的半截上。这种半截消息
        留在历史里，下一次请求会被 API 直接 400，整个会话就卡死了。
        所以给没等到结果的调用补一条「被打断」的结果，再补一句助手的话把这一轮收住。"""
        self._view().end()
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        for msg in reversed(self.messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    if tc.get("id") not in answered:
                        self.messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": tc.get("function", {}).get("name", ""),
                            "content": "（用户打断了这次调用，它没有执行完。）",
                        })
                break
        if self.messages and self.messages[-1].get("role") in {"user", "tool"}:
            self.messages.append({"role": "assistant", "content": "（这一轮被用户打断了，没有说完。）"})
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
        self._view().end()
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
        final = _truncate_text("\n\n".join(notes), self.context_builder.max_chars + 200)
        # 记下真正发出去的那段文字：记忆闪回只认出现在这里面的记忆（截断掉的不算想起）
        self._last_context_note = final
        return final
    def _injected_memories(self) -> tuple[str, list[dict]]:
        """这一回合真正塞进上下文的记忆（强化之前的样子）。

        provider 检索到的不一定都发出去了：记忆段只有 320 字，最后一条常被截掉一截。
        所以拿检索结果去对照最终发出去的文字，只留出现在里面的；只露了个头的标 partial。"""
        builder = getattr(self, "context_builder", None)
        provider = next((p for p in getattr(builder, "providers", []) if isinstance(p, MemoryContextProvider)), None)
        rec = getattr(provider, "last_recall", None) or {}
        note = getattr(self, "_last_context_note", "")
        view = self.__dict__.get("_stream_view")
        # 上下文有缓存：这一回合要是没重新检索，last_recall 还是上一回合的，不能拿来充数
        if not note or not rec.get("rows") or rec.get("at", 0.0) < getattr(view, "turn_started", 0.0):
            return rec.get("kind", "none"), []
        rows = []
        for row in rec["rows"]:
            if f"#{row['id']} {row['content'][:8]}" not in note:
                continue
            rows.append(dict(row, partial=f"#{row['id']} {row['content'][:90]}" not in note))
        return rec.get("kind", "none"), rows
    def _show_recall(self) -> None:
        """回答之前亮出这一回合想起了什么——每回合只在第一次调模型时亮一次。"""
        if getattr(self, "quiet", True) or getattr(self, "tag", ""):
            return
        kind, rows = self._injected_memories()
        if rows:
            # 没线索时顺手带上的最近几条不叫「想起」，轨迹里也得这么记
            title = f"想起 {len(rows)} 条记忆" if kind == "recall" else f"附带最近 {len(rows)} 条记忆（没有检索线索）"
            self._trace_event(title, TraceLevel.INFO,
                              detail=" · ".join(f"#{r['id']} R={r['retention']:.2f}" for r in rows),
                              ids=[r["id"] for r in rows], kind=kind)
        if kind == "recall" and rows and getattr(self, "show_recall", SHOW_RECALL):
            self._view().recall(rows)
    def _system_prompt(self) -> str:
        """只放**稳定**的部分：人设、思考方式、项目指令。

        心情、记忆、工作区这些每回合都在变的东西不放这里——它们一旦混进系统消息，
        系统消息就每回合都不一样，而系统消息是整个提示词的第一条，
        它一变，后面整段对话历史的缓存前缀全部作废。
        对话越长，这笔浪费越大。所以易变的部分挪到末尾（见 _runtime_context_message）。"""
        self.instructions = self.reload_instructions()
        # 运行时上下文只拼一次，缓存键和正文共用这一份。以前这里拼两次：第二次拼的时候
        # 第一次已经把记忆「复习」过了，记忆闪回拿到的就成了复习之后的满格；
        # 两次之间工作区要是刷新过，存进缓存的正文和缓存键也对不上
        note = self._runtime_context_note() if CONTEXT_POSITION == "system" else ""
        cache_key = "|".join([
            self.system_base, self.instructions, self.model,
            f"native_cot={self._native_reasoning_seen}",
            f"ctx_pos={CONTEXT_POSITION}",
            note,
        ])
        if cache_key == self._system_prompt_cache_key and self._system_prompt_cache:
            return self._system_prompt_cache
        parts = [self.system_base]
        # 模型自己会思考的话就不用再教它思考了：原生推理已经在做这件事，
        # 再要一个 <thinking> 只会让它把话说两遍，白烧 token。
        if MONOLOGUE_MODE == "cot" and not self._native_reasoning_seen:
            parts.append(COT_INSTRUCTION)
        if self.instructions:
            parts.append("[项目自定义指令]\n" + self.instructions)
        if CONTEXT_POSITION == "system":
            parts.append(note)  # 1.1.6 之前的老排法
        prompt = "\n\n".join(parts)
        self._system_prompt_cache_key = cache_key
        self._system_prompt_cache = prompt
        return prompt
    def _runtime_context_message(self) -> dict | None:
        """把易变的运行时上下文做成一条挂在末尾的消息。

        放末尾有两个好处：前面的稳定前缀能被服务端缓存；
        而且它离生成点最近，反而比塞在系统提示词里更容易被模型注意到。
        这条消息只在发请求时临时拼进去，绝不写回 self.messages——
        一旦写回去，它就变成历史的一部分，下回合又会破坏前缀。"""
        if CONTEXT_POSITION == "system":
            return None
        note = self._runtime_context_note().strip()
        if not note:
            return None
        return {"role": "system", "content": "[运行时状态｜供参考，不是用户的话]\n" + note}
    def _api_messages(self) -> list[dict]:
        self._trim_messages()
        msgs = [{"role": "system", "content": self._system_prompt()}] + self.messages
        tail = self._runtime_context_message()
        return msgs + [tail] if tail else msgs
    def chat(self, user_input: str) -> str:
        self._current_user_input = user_input
        if self.trace_enabled:
            # 每个新回合开一棵新的轨迹树；子代理不清空，避免抹掉主代理的轨迹。
            TRACE_ENGINE.clear()
        view = self._view()
        view.begin_turn()
        # 记忆闪回每回合只亮一次：由这一回合第一次调模型时消费掉
        self._recall_pending = True
        try:
            MOOD.begin_turn(user_input)
            if self.archive_enabled:
                ARCHIVE.append("user", user_input, kind="turn")
            self.messages.append({"role": "user", "content": user_input})
            self._trim_messages()
            with self._trace_step("Turn", user_input[:80], user_len=len(user_input)):
                # cot 模式下不再单独生成内心戏：真正的思考来自下面这次真实调用，
                # 而且会留在上下文里。旧的 llm/hybrid 模式要为一段打印完就丢的话
                # 额外发一次完整请求，纯属浪费。
                if MONOLOGUE_MODE != "cot":
                    self._emit_thought(user_input)
                final_text, used_tools = self._run_loop()
                if self.reflect and used_tools:
                    verdict = self._self_reflect()
                    if verdict and not verdict.get("passed", True):
                        issues = verdict.get("issues", "不完整")
                        view.notice(f"↻ 自检没通过：{issues}。再改一轮。", "yellow")
                        self._trace_event("自检不通过，进入自愈", TraceLevel.WARNING, detail=str(issues)[:120])
                        self.messages.append({"role": "user", "content": f"（系统反思自动提示：{issues}。请进行修正。）"})
                        self._trim_messages()
                        with self._trace_step("Self-heal", str(issues)[:60]):
                            final_text, _ = self._run_loop()
                    elif verdict and verdict.get("lesson"):
                        mid = MEMORY.add(str(verdict["lesson"]), "lesson")
                        if getattr(self, "show_recall", SHOW_RECALL):
                            view.remembered((MEMORY.get(mid) or {}).get("content", str(verdict["lesson"])),
                                            label="记下一条教训")
                        else:
                            view.notice(f"✎ 记下一条教训 #{mid}：{verdict['lesson']}", "dim")
                        self._trace_event(f"沉淀教训 #{mid}", TraceLevel.INFO, detail=str(verdict["lesson"])[:120])
            if self.archive_enabled:
                ARCHIVE.append("assistant", final_text, kind="turn")
            MOOD.record_response(final_text, used_tools)
            if self.relationship_enabled:
                RELATIONSHIP.observe_turn(user_input, final_text, used_tools=used_tools)
            return final_text
        finally:
            view.end()
            self._recall_pending = False
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
            self._view().notice(f"[!] 达到最大步数上限（{loop_cap} 步）。", "yellow")
            self._trace_event(f"达到步数上限 {loop_cap}", TraceLevel.WARNING)
        return final_text, used_tools
    def _print_thinking(self, piece: str) -> None:
        """把思考流打到「思考」通道，和正式回答在视觉上分开（淡色斜体，见 StreamView）。"""
        if not self.quiet:
            self._view().thinking(piece)
    def _end_thinking(self) -> None:
        self._view().close()
    def _fold_reasoning(self, content: str, native: str) -> str:
        """把模型的原生思考写进消息内容，让它和 <thinking> 走同一条因果链。

        原生推理（reasoning_content）如果只是打印出来就丢，对 DeepSeek-R1 /
        QwQ 这类模型来说，CoT 依旧是不因果的——展示了，但下一步看不见。
        这里把它折进 content，后续每一步都读得到。

        注意：折进的是 content（普通文本），绝不能把 reasoning_content 原样塞回
        请求里——DeepSeek 等服务端会直接返回 400。
        少数服务商建议多轮之间不要回喂自己的 CoT，需要的话
        POLARIS_COT_FEEDBACK=0 可以只展示不入历史。"""
        native = (native or "").strip()
        if not native:
            return content
        self._native_reasoning_seen = True
        if not COT_FEEDBACK:
            return content
        return f"<thinking>{native}</thinking>\n{content}".strip()
    def _record_reasoning(self, reasoning: str) -> None:
        """留档最近一次真实推理：给 /trace、给上下文 provider 用。"""
        text = (reasoning or "").strip()
        if not text:
            return
        self.last_reasoning = text
        THOUGHT_ENGINE.last_reasoning = text
        self._trace_event("思考", TraceLevel.TRACE, detail=text[:160])
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
        # 这时候上面的 _api_messages() 已经把这一步的上下文拼好了，想起了哪些记忆是定局；
        # 而等待动画还没开始转，正好插在用户那句话和动画之间
        if getattr(self, "_recall_pending", False):
            self._recall_pending = False
            self._show_recall()
        if self.stream and not self.quiet:
            view = self._view()
            view.wait()
            current_msg = {"role": "assistant", "content": ""}
            tool_calls_dict: dict[int, dict] = {}
            splitter = ThinkingSplitter()
            native_reasoning = ""
            try:
                stream = self.backend.create_chat_completion(**params, stream=True)
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    # DeepSeek-R1 / QwQ / vLLM 这类端点会把模型原生的推理放在
                    # reasoning_content 里。有就直接用——那是模型真实的思考过程，
                    # 一分钱不用多花，也不需要靠提示词去要。
                    piece = getattr(delta, "reasoning_content", None)
                    if piece:
                        native_reasoning += piece
                        view.thinking(piece)
                    if getattr(delta, "content", None):
                        # 思考和回答分进两个块：以前回答会直接接在思考那一行后面，
                        # 看着像思考的一部分。
                        think_piece, answer_piece = splitter.feed(delta.content)
                        view.thinking(think_piece)
                        view.answer(answer_piece)
                        current_msg["content"] += delta.content
                    if getattr(delta, "tool_calls", None):
                        # 模型开始写工具调用了。参数可能很长（write_file 要写整个文件），
                        # 这段时间屏幕上一个字都不出，得接着转，不然看着像卡死。
                        view.wait()
                        self._merge_tool_call_deltas(tool_calls_dict, delta.tool_calls)
                think_rest, answer_rest = splitter.finish()
                view.thinking(think_rest)
                view.answer(answer_rest)
            finally:
                view.end()
            if tool_calls_dict:
                current_msg["tool_calls"] = [v for _, v in sorted(tool_calls_dict.items())]
            # 存进 messages 的是原样输出（含 <thinking>），后续每一步都看得到它——
            # 这正是「真 CoT」的关键：思考留在上下文里，才谈得上影响决策。
            current_msg["content"] = self._fold_reasoning(current_msg["content"], native_reasoning)
            self.messages.append(current_msg)
            self._trim_messages()
            self._record_reasoning(native_reasoning or splitter.thinking)
            visible = dict(current_msg)
            visible["content"] = splitter.answer.strip() if (splitter.thinking or native_reasoning) else current_msg["content"]
            return visible
        view = self._view()
        if not self.quiet:
            view.wait()
        try:
            res = self.backend.create_chat_completion(**params)
        finally:
            view.stop_wait()
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
        thinking, answer = ThinkingSplitter.split(msg.content or "")
        native = getattr(msg, "reasoning_content", "") or ""
        if not self.quiet:
            view.thinking(native or thinking)
            view.answer(answer)
            view.end()
        built_msg["content"] = self._fold_reasoning(built_msg["content"], native)
        self.messages.append(built_msg)
        self._trim_messages()
        self._record_reasoning(native or thinking)
        visible = dict(built_msg)
        if thinking or native:
            visible["content"] = answer
        return visible
    @staticmethod
    def _merge_tool_call_deltas(slots: dict[int, dict], deltas: Any) -> None:
        """把流式 tool_call 分片拼回完整调用。"""
        for tc in deltas:
            idx = tc.index
            # 不同的 OpenAI-Compatible 服务端分片方式差别很大：有的第一片
            # 只带 id/type 不带 function，有的 id 要到后面才补上。旧实现
            # 假设第一片一定带 function（直接 tc.function.name → 崩），
            # 且 id 只从第一片取（晚到就永远是 None → tool_call_id 为空被 API 拒）。
            slot = slots.setdefault(
                idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
            )
            if getattr(tc, "id", None):
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["function"]["name"] += fn.name
                if getattr(fn, "arguments", None):
                    slot["function"]["arguments"] += fn.arguments
    def _run_tools_and_feed_back(self, assistant_msg: dict) -> None:
        for tc in assistant_msg["tool_calls"]:
            name = tc["function"]["name"]
            args_str = tc["function"]["arguments"]
            try:
                args = json.loads(args_str) if args_str.strip() else {}
            except Exception:
                args = {}
            # 用统一的符号前缀让对话流可以一眼扫读：● 是动作，⎿ 是它的结果。
            view = self._view()
            view.tool_call(name, args_str)
            # 按函数本身认，不按名字认：插件也可以注册一个叫 remember 的工具，那不该被画成记忆
            builtin = getattr(self, "tools", {}).get(name)
            todo_tool = builtin is set_todos or builtin is complete_todo
            was_all_done = todo_tool and bool(TODOS.items) and all(it.get("done") for it in TODOS.items)
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
            # 先把结果记进历史，再往屏幕上画。画的时候可能有动画（任务做完会闪一下），
            # 这时按 Ctrl+C，历史里也已经是工具真实的结果，而不是一句「没执行完」
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
            # 下面几种只是换个画法，发给模型的 output 原样不动
            drawn = False
            if ok and todo_tool and output.startswith(("Task list updated:", "Marked complete.")):
                finishing = builtin is complete_todo and not was_all_done and all(it.get("done") for it in TODOS.items)
                index = args.get("index") if isinstance(args.get("index"), int) else None
                drawn = view.todo_progress(TODOS.items, flash=finishing,
                                           compact=builtin is complete_todo and not finishing, just_done=index)
            elif ok and builtin is remember and getattr(self, "show_recall", SHOW_RECALL):
                mid = re.search(r"#(\d+)", output)
                item = MEMORY.get(int(mid.group(1))) if mid else None
                drawn = view.remembered((item or {}).get("content") or str(args.get("content", "")), as_result=True)
            if not drawn:
                view.tool_result(output, ok)
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
        # 权限确认问完了，接下来工具在跑、屏幕上没字，让动画转起来。
        # 只给内置工具转：插件是用户自己的代码，可能自己往屏幕上打字，会和动画抢同一行；
        # 派发子代理也不转，子代理会自己打进度。
        if not getattr(self, "quiet", True) and fn in BUILTIN_TOOLS and name not in {"delegate_task", "delegate_tasks"}:
            self._view().wait()
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
        view = self._view()
        try:
            # 复用 _api_messages 拼好的前缀：反思和主调用共享同一段缓存，
            # 自己另拼一套等于把整段历史再按未缓存价算一遍。
            r_msg = self._api_messages() + [{"role": "user", "content": REFLECT_INSTRUCTION}]
            # 回答已经说完了，这一步是它回头检查自己。不转的话用户只会觉得「怎么还没好」。
            if not self.quiet:
                view.wait("回头检查")
            try:
                res = self.backend.create_chat_completion(
                    model=self.model,
                    messages=r_msg,
                    max_tokens=600,
                    response_format={"type": "json_object"},
                )
            finally:
                view.stop_wait()
            verdict = _safe_json_load(res.choices[0].message.content or "")
            if verdict:
                MOOD.record_reflection(bool(verdict.get("passed", True)), str(verdict.get("lesson", "")))
            return verdict
        except Exception:
            return None
def evaluate_online_necessity(model: str = GATE_MODEL) -> bool:
    # MINIAGENT_SKIP_GATE 是门禁默认开着那会儿的开关，留着兼容老配置。
    if not STARTUP_GATE or _env_bool("MINIAGENT_SKIP_GATE", False):
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
        with Spinner("评估要不要上线"):
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
        # read_file 必须能读完整个文件。旧实现在 8000 字符处截断且没有翻页参数，
        # 模型对 175 行之后的代码全盲，还不知道自己是盲的。
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "big.py")
            _atomic_write_text(path, "\n".join(f"line_{i}" for i in range(1, 1001)))
            head = read_file(path)
            tail = read_file(path, offset=990, limit=20)
            ok = (
                "共 1000 行" in head
                and "\t line_1" not in head and "1\tline_1" in head  # 带行号
                and "offset=" in head                                # 明确告诉模型怎么读下一段
                and "line_1000" in tail and "line_990" in tail       # 真的能读到结尾
                and read_file(path, offset=5000).startswith("错误：")
            )
            checks.append(("read_file_paging", ok, f"首屏 {len(head.splitlines())} 行 / 可翻至末行"))
    except Exception as e:
        checks.append(("read_file_paging", False, str(e)))
    try:
        # run_shell 必须无条件交出退出码，否则 agent 分不清测试过没过。
        ok_cmd = run_shell("echo hi")
        bad_cmd = run_shell("python3 -c \"import sys; sys.exit(7)\"")
        ok = (
            ok_cmd.startswith("退出码 0") and "hi" in ok_cmd
            and bad_cmd.startswith("退出码 7")
            and not _looks_like_tool_failure(ok_cmd)
            and _looks_like_tool_failure(bad_cmd)  # 失败要能被轨迹/心情/反思看见
        )
        checks.append(("shell_exit_code", ok, "成功/失败都带退出码且能被识别"))
    except Exception as e:
        checks.append(("shell_exit_code", False, str(e)))
    try:
        # 流式 tool_call 分片：不同 OpenAI-Compatible 服务端切法不同，
        # 第一片可能不带 function，id 可能晚到——这两种以前都会出事。
        def _delta(index, tid=None, name=None, args=None, with_fn=True):
            fn = types.SimpleNamespace(name=name, arguments=args) if with_fn else None
            return types.SimpleNamespace(index=index, id=tid, function=fn)
        def _chunk(tool_calls):
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(delta=types.SimpleNamespace(content=None, tool_calls=tool_calls))])
        class _StreamBackend(ChatBackend):
            name = "selftest_stream"
            def create_chat_completion(self, **kwargs):
                return iter([
                    _chunk([_delta(0, tid="call_a", with_fn=False)]),   # 只有 id
                    _chunk([_delta(0, name="read_", args='{"pa')]),
                    _chunk([_delta(0, name="file", args='th":"x.py"}')]),
                    _chunk([_delta(1, name="calculator", args="{}")]),  # id 缺席
                    _chunk([_delta(1, tid="call_b")]),                  # id 后补
                ])
        probe = Agent.__new__(Agent)
        probe.backend, probe.model, probe.max_tokens = _StreamBackend(), "m", 16
        # 流式分支的条件是 stream and not quiet，所以这里必须 quiet=False
        probe.stream, probe.quiet, probe.tag = True, False, ""
        probe.tools, probe.mcp_servers, probe._mcp_tool_map = {}, {}, {}
        probe.messages, probe.max_context_messages, probe.max_context_tokens = [], 40, MAX_CONTEXT_TOKENS
        probe.trace_enabled, probe._thinking_open, probe.last_reasoning = False, False, ""
        probe._api_messages = lambda: []
        calls = probe._call_llm().get("tool_calls") or []
        ok = (
            len(calls) == 2
            and calls[0]["id"] == "call_a" and calls[0]["function"]["name"] == "read_file"
            and calls[0]["function"]["arguments"] == '{"path":"x.py"}'
            and calls[1]["id"] == "call_b"  # 晚到的 id 必须补得上
        )
        checks.append(("stream_tool_call_merge", ok, f"合并出 {len(calls)} 个调用，跨片名/参数/id 都对"))
    except Exception as e:
        checks.append(("stream_tool_call_merge", False, str(e)))
    try:
        # 真 CoT 的判定标准只有一条：思考留在上下文里、影响后续决策，
        # 同时不混进给用户的答案。做不到这两点就还是内心戏。
        think, answer = ThinkingSplitter.split("<thinking>先读再改</thinking>\n改好了。")
        stream_sp = ThinkingSplitter()
        for piece in ["<think", "ing>推理", "中</thin", "king>答案"]:
            stream_sp.feed(piece)
        stream_sp.finish()
        class _CoTBackend(ChatBackend):
            name = "selftest_cot"
            def create_chat_completion(self, **kwargs):
                msg = types.SimpleNamespace(
                    content="<thinking>需要先确认边界情况</thinking>已修复。", tool_calls=None)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
        probe = Agent.__new__(Agent)
        probe.backend, probe.model, probe.max_tokens = _CoTBackend(), "m", 16
        probe.stream, probe.quiet, probe.tag = False, True, ""
        probe.tools, probe.mcp_servers, probe._mcp_tool_map = {}, {}, {}
        probe.messages, probe.max_context_messages, probe.max_context_tokens = [], 40, MAX_CONTEXT_TOKENS
        probe.trace_enabled, probe._thinking_open, probe.last_reasoning = False, False, ""
        probe._api_messages = lambda: []
        visible = probe._call_llm()
        stored = probe.messages[-1]["content"]
        ok = (
            think == "先读再改" and answer == "改好了。"
            and stream_sp.thinking == "推理中" and stream_sp.answer == "答案"  # 标签被切两半也要对
            and "<thinking>" in stored                    # 留在上下文里 → 有因果
            and "<thinking>" not in visible["content"]    # 不混进答案
            and visible["content"] == "已修复。"
            and probe.last_reasoning == "需要先确认边界情况"
        )
        checks.append(("cot_is_causal", ok, "思考入历史、不入答案、跨分片可拆"))
    except Exception as e:
        checks.append(("cot_is_causal", False, str(e)))
    try:
        # 模型自带思考时（DeepSeek-R1 / QwQ 的 reasoning_content），
        # 要直接用它的思考，而且同样必须进历史——只展示不入历史就还是不因果。
        class _NativeBackend(ChatBackend):
            name = "selftest_native"
            def create_chat_completion(self, **kwargs):
                msg = types.SimpleNamespace(
                    reasoning_content="先读文件再改，别猜。", content="已修复。", tool_calls=None)
                return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])
        probe = Agent.__new__(Agent)
        probe.backend, probe.model, probe.max_tokens = _NativeBackend(), "m", 16
        probe.stream, probe.quiet, probe.tag = False, True, ""
        probe.tools, probe.mcp_servers, probe._mcp_tool_map = {}, {}, {}
        probe.messages, probe.max_context_messages, probe.max_context_tokens = [], 40, MAX_CONTEXT_TOKENS
        probe.trace_enabled, probe._thinking_open = False, False
        probe.last_reasoning, probe._native_reasoning_seen = "", False
        probe._api_messages = lambda: []
        visible = probe._call_llm()
        stored = probe.messages[-1]["content"]
        ok = (
            "先读文件再改" in stored                       # 原生思考写进了历史
            and visible["content"] == "已修复。"            # 但不进答案
            and probe._native_reasoning_seen               # 记住了模型自带思考
            and probe.last_reasoning == "先读文件再改，别猜。"
            # 绝不能把 reasoning_content 当消息字段送回去，DeepSeek 会直接 400
            and "reasoning_content" not in json.dumps(probe.messages, ensure_ascii=False)
        )
        checks.append(("native_reasoning_causal", ok, "原生思考入历史且不回传 reasoning_content"))
    except Exception as e:
        checks.append(("native_reasoning_causal", False, str(e)))
    try:
        # 工具档位必须真的改变发给模型的工具列表，而不只是改个显示。
        # 同时：用户自己注册的工具（插件 / MCP）不能被档位悄悄吃掉。
        probe = Agent.__new__(Agent)
        probe.tools = {t.schema["name"]: t for t in BUILTIN_TOOLS}
        probe.mcp_servers, probe._mcp_tool_map = {}, {}
        probe._system_prompt_cache_key = ""
        def _extra(x: str) -> str:
            """用户自己接进来的工具"""
            return x
        probe.register_tool(_extra)
        probe.tool_profile = "full"
        full_names = {t["function"]["name"] for t in probe._all_openai_tools()}
        probe.set_tool_profile("min")
        min_names = {t["function"]["name"] for t in probe._all_openai_tools()}
        _, full_cost = tool_profile_cost("full")
        _, min_cost = tool_profile_cost("min")
        ok = (
            len(min_names) < len(full_names)               # 档位真的生效了
            and "read_file" in min_names
            and "delegate_tasks" not in min_names          # 被挡掉的确实不发了
            and "_extra" in min_names                      # 用户自己的工具不受影响
            and min_cost < full_cost * 0.5                 # 省下的量级对得上
            and probe.set_tool_profile("不存在").startswith("未知档位")
        )
        checks.append(("tool_profile_filter", ok,
                       f"full {len(full_names)} 个 / min {len(min_names)} 个，"
                       f"schema 从 {full_cost:,} 降到 {min_cost:,} tokens"))
    except Exception as e:
        checks.append(("tool_profile_filter", False, str(e)))
    try:
        # 缓存前缀必须稳定：易变的运行时状态改变时，系统消息和既有历史
        # 都不能跟着变，否则整段对话每回合都要按未缓存价重算一遍。
        probe = Agent.__new__(Agent)
        probe.system_base, probe.instructions = "base", ""
        probe.model, probe.mode, probe.tool_profile = "m", "ask", "full"
        probe.messages, probe.max_context_messages = [{"role": "user", "content": "先前的问题"}], 40
        probe.max_context_tokens = MAX_CONTEXT_TOKENS
        probe._system_prompt_cache_key, probe._system_prompt_cache = "", ""
        probe._native_reasoning_seen, probe._current_user_input = False, "现在的问题"
        probe.context_builder = ContextBuilder()
        probe.reload_instructions = lambda: ""
        before = probe._api_messages()
        # 制造真实会话里一定会发生的变化：心情浮动 + 写入一条新记忆
        original_mood = dict(MOOD.state)
        MOOD.state["fatigue"] = (MOOD.state.get("fatigue", 18) + 40) % 95
        probe._system_prompt_cache_key = ""
        after = probe._api_messages()
        MOOD.state.clear()
        MOOD.state.update(original_mood)
        same_system = before[0] == after[0]
        same_history = before[1:-1] == after[1:-1]
        tail_is_context = after[-1]["role"] == "system" and "运行时状态" in after[-1]["content"]
        # 这条上下文消息只在发请求时临时拼进去，绝不能写回历史——
        # 一旦写回去它就成了历史的一部分，下回合又会破坏前缀。
        not_persisted = all("运行时状态" not in (msg.get("content") or "") for msg in probe.messages)
        ok = (CONTEXT_POSITION != "tail") or (same_system and same_history and tail_is_context and not_persisted)
        checks.append(("prompt_cache_prefix", ok,
                       f"位置={CONTEXT_POSITION}｜心情变化后系统消息不变 {'OK' if same_system else 'FAIL'}"
                       f"｜上下文不落历史 {'OK' if not_persisted else 'FAIL'}"))
    except Exception as e:
        checks.append(("prompt_cache_prefix", False, str(e)))
    try:
        # 裁剪必须按 token 预算，不能只数消息条数。
        # read_file 单条能返回两万多字符，40 条这样的消息约 24 万 tokens，
        # 只按条数裁的话直接把窗口撑爆（400）。
        probe = Agent.__new__(Agent)
        probe.max_context_messages, probe.max_context_tokens = 40, 20000
        probe.messages = []
        for i in range(20):
            probe.messages.append({"role": "user", "content": f"读第 {i} 个文件"})
            probe.messages.append({"role": "user", "content": "x" * 24000})
        probe._trim_messages()
        within = _estimate_tokens(json.dumps(probe.messages, ensure_ascii=False)) <= 20000
        # tool_calls 和它的结果不能被裁散，开头也不能是孤立的 tool 消息
        probe2 = Agent.__new__(Agent)
        probe2.max_context_messages, probe2.max_context_tokens = 40, 1200
        probe2.messages = []
        for i in range(12):
            probe2.messages.append({"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
            probe2.messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file",
                                    "content": "内容" * 200})
        probe2._trim_messages()
        orphans = 0
        for idx, msg in enumerate(probe2.messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                need, got, j = len(msg["tool_calls"]), 0, idx + 1
                while j < len(probe2.messages) and probe2.messages[j].get("role") == "tool":
                    got += 1
                    j += 1
                if got < need:
                    orphans += 1
        head_ok = not probe2.messages or probe2.messages[0].get("role") != "tool"
        # 单条就超预算时也不能把历史清空，否则模型连用户问了什么都看不到
        probe3 = Agent.__new__(Agent)
        probe3.max_context_messages, probe3.max_context_tokens = 40, 1000
        probe3.messages = [{"role": "user", "content": "x" * 100000}]
        probe3._trim_messages()
        ok = within and orphans == 0 and head_ok and len(probe3.messages) == 1
        checks.append(("context_token_budget", ok,
                       f"预算内 {'OK' if within else 'FAIL'}｜孤儿 tool_calls {orphans} 个｜超长单条不清空"))
    except Exception as e:
        checks.append(("context_token_budget", False, str(e)))
    try:
        # 任务描述（第一条用户消息）永远不能被裁掉。它最老，所以按时间裁剪时
        # 第一个死——可它恰恰是「要干什么」。裁到一半 Agent 还在干活，
        # 却已经不知道自己在干嘛了。
        def _probe_with(tokens: int, build) -> Agent:
            probe = Agent.__new__(Agent)
            probe.max_context_messages, probe.max_context_tokens = 40, tokens
            probe.messages = [{"role": "user", "content": "任务：把登录模块重构成 JWT"}]
            build(probe)
            probe._trim_messages()
            return probe
        def _chat(p):
            for i in range(30):
                p.messages.append({"role": "assistant", "content": f"好的，第 {i} 步"})
                p.messages.append({"role": "user", "content": "继续。" + "闲聊内容。" * 30})
        def _tools(p):
            for i in range(10):
                p.messages.append({"role": "assistant", "content": "", "tool_calls": [
                    {"id": f"c{i}", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]})
                p.messages.append({"role": "tool", "tool_call_id": f"c{i}", "name": "read_file",
                                   "content": "内容" * 150})
        def _has_task(p) -> bool:
            return any("JWT" in (msg.get("content") or "") for msg in p.messages)
        # 用户中途改主意那句，比最初的任务更贴近当下，丢了比全忘更糟——
        # Agent 会自信地按旧方案继续做。
        def _chat_with_correction(p):
            for i in range(8):
                p.messages.append({"role": "user", "content": "继续。" + "闲聊内容。" * 40})
            p.messages.append({"role": "user", "content": "等一下，改用 RS256"})
            for i in range(8):
                p.messages.append({"role": "user", "content": "继续。" + "闲聊内容。" * 40})
        correction_probe = _probe_with(3000, _chat_with_correction)
        keeps_correction = any("RS256" in (msg.get("content") or "") for msg in correction_probe.messages)
        # 钉住用户消息不能反过来变成撑爆上下文的途径：用户粘几段大日志时，
        # 上限必须是硬的。
        flood = Agent.__new__(Agent)
        flood.max_context_messages, flood.max_context_tokens = 40, 4000
        flood.messages = [{"role": "user", "content": "报错日志：\n" + "Traceback\n" * 800}
                          for _ in range(5)]
        flood._trim_messages()
        flood_size = _estimate_tokens(json.dumps(flood.messages, ensure_ascii=False))
        within_budget = flood_size <= 4000 * 1.1 or len(flood.messages) == 1
        chat_probe = _probe_with(3000, _chat)
        # 条数没超但 token 预算超了的情况：锚点会在预算那一关被刷掉，
        # 所以判断依据必须是「它有没有活下来」，不是「在不在窗口里」。
        tool_probe = _probe_with(800, _tools)
        orphans = 0
        for idx, msg in enumerate(tool_probe.messages):
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                need, got, j = len(msg["tool_calls"]), 0, idx + 1
                while j < len(tool_probe.messages) and tool_probe.messages[j].get("role") == "tool":
                    got += 1
                    j += 1
                if got < need:
                    orphans += 1
        for _ in range(5):  # 反复裁剪必须稳定，不能越裁越少
            chat_probe._trim_messages()
        ok = (
            _has_task(chat_probe) and _has_task(tool_probe)
            and keeps_correction and within_budget
            and orphans == 0
            and tool_probe.messages[0].get("role") != "tool"
            and len(chat_probe.messages) < 61      # 确实裁掉了东西，不是啥也没做
        )
        checks.append(("user_messages_survive_trim", ok,
                       f"任务 {'OK' if _has_task(chat_probe) and _has_task(tool_probe) else 'FAIL'}｜"
                       f"中途纠正 {'OK' if keeps_correction else 'FAIL'}｜"
                       f"大日志不爆预算 {'OK' if within_budget else 'FAIL'}｜孤儿 {orphans} 个"))
    except Exception as e:
        checks.append(("user_messages_survive_trim", False, str(e)))
    try:
        # 改文件是这个 agent 最容易造成真实损失的动作，却一直没被自检覆盖。
        # 最要紧的是 edit_file 的唯一性保证：old_str 不唯一时必须拒绝，
        # 否则模型以为自己改了 A 处，实际改的是先出现的 B 处，而且悄无声息。
        with tempfile.TemporaryDirectory() as tmp:
            # 写类工具都会先记回滚点，所以需要一个 Agent 上下文；
            # 顺带也验证了回滚点确实被记下来了（/undo 有东西可回）。
            probe = Agent.__new__(Agent)
            probe.checkpoints = []
            saved_context = CURRENT_AGENT_CONTEXT.copy()
            CURRENT_AGENT_CONTEXT.update({"agent": probe})
            path = os.path.join(tmp, "sample.py")
            body = "def a():\n    return 1\n\ndef b():\n    return 1\n"
            _atomic_write_text(path, body)
            # 出现两次 → 必须拒绝，且文件一个字都不能动
            ambiguous = edit_file(path, "    return 1", "    return 2")
            untouched = open(path, encoding="utf-8").read() == body
            # 完全不存在 → 同样拒绝
            missing = edit_file(path, "does not exist", "x")
            # 带上下文变唯一 → 正常替换
            done = edit_file(path, "def b():\n    return 1", "def b():\n    return 2")
            after = open(path, encoding="utf-8").read()
            ok = (
                ambiguous.startswith("错误：") and untouched
                and missing.startswith("错误：")
                and _looks_like_tool_failure(ambiguous) and _looks_like_tool_failure(missing)
                and done.startswith("替换成功")
                and after == "def a():\n    return 1\n\ndef b():\n    return 2\n"
            )
            # write_file 覆盖写也要真的落盘
            target = os.path.join(tmp, "new.txt")
            write_file(target, "内容")
            ok = ok and open(target, encoding="utf-8").read() == "内容"
            # 被拒绝的那两次不该留下回滚点，成功的两次要留下
            ok = ok and len(probe.checkpoints) == 2
            CURRENT_AGENT_CONTEXT.clear()
            CURRENT_AGENT_CONTEXT.update(saved_context)
            checks.append(("file_edit_safety", ok,
                           "old_str 不唯一/不存在都拒绝且不动文件，唯一时正确替换"))
    except Exception as e:
        checks.append(("file_edit_safety", False, str(e)))
    try:
        # 高危命令的拦截必须走 run_shell 本身。
        # 原来的 shell_guard 测的是 _shell_deny_reason() 这个辅助函数，
        # 从没经过工具——把 run_shell 里的拦截整段删掉，shell_guard 照样 PASS。
        # 测辅助函数而不测接线，给的是虚假的安全感。
        with tempfile.TemporaryDirectory() as tmp:
            canary = os.path.join(tmp, "canary.txt")
            _atomic_write_text(canary, "still here")
            blocked = run_shell(f"rm -rf {tmp} # 高危演练")
            refused = blocked.startswith("已拒绝执行") and _looks_like_tool_failure(blocked)
            not_executed = os.path.exists(canary)   # 关键：不能只看返回值，要看它真没执行
            normal = run_shell("echo ok")
            works = normal.startswith("退出码 0") and "ok" in normal
            ok = refused and not_executed and works
            checks.append(("shell_guard_via_tool", ok,
                           f"拦截 {'OK' if refused else 'FAIL'}｜"
                           f"确实没执行 {'OK' if not_executed else 'FAIL'}｜日常命令不误伤 {'OK' if works else 'FAIL'}"))
    except Exception as e:
        checks.append(("shell_guard_via_tool", False, str(e)))
    try:
        # run_python 的沙箱在整套自检里被调用过 0 次——而注释里白纸黑字记着
        # 这里已经静默出过一次事（_safe_import 定义了却没装进 SAFE_BUILTINS，
        # 白名单等于完全没生效）。出过事、修好了、依然没有网。
        escapes = {
            "import os": "import os\nprint(os.getcwd())",
            "import subprocess": "import subprocess\nprint(subprocess)",
            "dunder 链": "print((1).__class__.__mro__[-1].__subclasses__())",
            "open": "print(open('/etc/passwd'))",
            "eval": "print(eval('1+1'))",
        }
        leaked = []
        for label, code in escapes.items():
            out = run_python(code)
            # 逃逸成功的标志是「跑出了结果」；被挡住时沙箱会抛异常打 traceback
            if "Traceback" not in out and "Error" not in out and "错误" not in out:
                leaked.append(label)
        # 正向用例：白名单里的模块必须还能用，否则「全都挡住」也算坏了
        allowed = run_python("import math\nprint(math.sqrt(16))")
        allowed_ok = "4.0" in allowed
        # 沙箱是两层：AST 静态检查 + 运行时的 _safe_import。两层互为备份，
        # 所以只坏一层的话，上面那些行为用例全都照样通过——实测确认过。
        # 防御纵深是好事，但它让行为测试抓不到单层退化，所以这里额外确认
        # 两层都还在。第三条尤其重要：_safe_import 定义了却没装进 SAFE_BUILTINS
        # 正是这个项目历史上真实发生过的那次静默失效。
        wrapper = _python_sandbox_wrapper()
        layers = {
            "AST 层": "isinstance(node, ast.Import)" in wrapper,
            # 缩进要算进来：这句话在生成的沙箱代码里出现三次（运行时一次、
            # AST 层两次），只按子串匹配的话，坏掉运行时那层也会在别处匹配上。
            "运行时白名单": "\n    if root not in ALLOWED_IMPORTS:\n" in wrapper,
            "白名单已接线": "SAFE_BUILTINS['__import__'] = _safe_import" in wrapper,
        }
        missing = [name for name, present in layers.items() if not present]
        ok = not leaked and allowed_ok and not missing
        checks.append(("python_sandbox_escapes", ok,
                       f"逃逸 {len(leaked)} 例{('：' + '、'.join(leaked)) if leaked else ''}｜"
                       f"白名单仍可用 {'OK' if allowed_ok else 'FAIL'}｜"
                       f"防线完整 {'OK' if not missing else '缺 ' + '、'.join(missing)}"))
    except Exception as e:
        checks.append(("python_sandbox_escapes", False, str(e)))
    try:
        # 工具签名写 list[str]，但模型可能直接传字符串。
        # Python 的 for 会按字符迭代，于是「买牛奶」变成三条待办，旧清单还被清空了。
        original_todos = json.loads(json.dumps(TODOS.items, ensure_ascii=False))
        TODOS.set(["原有任务"])
        set_todos("买牛奶")
        single = [item["text"] for item in TODOS.items] == ["买牛奶"]
        TODOS.set(["原有任务"])
        rejected = set_todos("").startswith("错误：") and [i["text"] for i in TODOS.items] == ["原有任务"]
        TODOS.items = original_todos
        TODOS.persist()
        # delegate_tasks 同样的输入会派出真实的子代理会话，必须有上限
        capped = delegate_tasks(["任务"] * (MAX_PARALLEL_TASKS + 1)).startswith("错误：")
        ok = single and rejected and capped
        checks.append(("tool_arg_coercion", ok,
                       f"字符串当单项 {'OK' if single else 'FAIL'}｜"
                       f"空输入不清空旧清单 {'OK' if rejected else 'FAIL'}｜"
                       f"并行上限 {'OK' if capped else 'FAIL'}"))
    except Exception as e:
        checks.append(("tool_arg_coercion", False, str(e)))
    try:
        # 会写持久化状态的工具都必须标 mutating，否则 plan 模式的「只读」是假的。
        # forget_memory 永久删记忆且没有 /undo，之前却连确认都不弹。
        probe = Agent.__new__(Agent)
        probe.tools = {t.schema["name"]: t for t in BUILTIN_TOOLS}
        probe.mcp_servers, probe._mcp_tool_map, probe.tag = {}, {}, ""
        probe.mode = "plan"
        must_block = ["write_file", "edit_file", "run_shell", "run_python",
                      "forget_memory", "set_todos", "complete_todo", "remember", "reinforce_memory"]
        must_pass = ["read_file", "search_files", "recall_memories", "memory_status", "get_mood"]
        leaked = [n for n in must_block if not probe._permission_denied(n)]
        blocked_reads = [n for n in must_pass if probe._permission_denied(n)]
        ok = not leaked and not blocked_reads
        checks.append(("plan_mode_is_readonly", ok,
                       f"漏网 {len(leaked)} 个{('：' + '、'.join(leaked)) if leaked else ''}｜"
                       f"误伤只读 {len(blocked_reads)} 个"))
    except Exception as e:
        checks.append(("plan_mode_is_readonly", False, str(e)))
    def _tiny_term(raw: str, width: int = 200) -> tuple[list[str], int]:
        """极简终端：只认这套界面会用到的几种控制码，返回 (屏幕各行, 光标所在行)。
        换行按真实终端的 onlcr 处理（\\n 同时回到行首）。"""
        screen, row, col = [""], 0, 0
        i = 0
        while i < len(raw):
            ch = raw[i]
            if ch == "\x1b":
                m = re.match(r"\x1b\[([0-9;?]*)([A-Za-z])", raw[i:])
                if m:
                    arg, op = m.group(1), m.group(2)
                    if op == "A":
                        row = max(0, row - int(arg or 1))
                    elif op == "J":
                        screen[row] = screen[row][:col]
                        del screen[row + 1:]
                    elif op == "K":
                        screen[row] = screen[row][:col]
                    i += m.end()
                    continue
            if ch == "\n":
                row, col = row + 1, 0
                while len(screen) <= row:
                    screen.append("")
            elif ch == "\r":
                col = 0
            else:
                line = screen[row].ljust(col)
                screen[row] = line[:col] + ch + line[col + 1:]
                col += 1
            i += 1
        return screen, row
    try:
        # Markdown 排版：记号不能漏到屏幕上，折行不能超宽，代码缩进和 snake_case 不能被吃掉
        md = MarkdownRenderer(30)
        sample = [
            "# 标题", "这是 **加粗** 和 `code` 还有*斜体*的字", "- 列表项目" + "很长" * 12,
            "```py", "    x = 1", "```", "> 引用一句", "1. 第一步", "| a | b |",
            "2*3*4 与 __init__ 还有 snake_case_name 保持原样",
        ]
        rows = [_ANSI_RE.sub("", r) for line in sample for r in md.render(line, "  ", "  ")]
        text = "\n".join(rows)
        ok = (
            rows[0].strip() == "标题"
            and "**" not in text and "```" not in text and "`" not in text
            and "斜体" in text and "*斜体*" not in text
            and any(r.startswith("  • 列表项目") for r in rows)
            and any(r.startswith("    长") for r in rows)         # 列表折行悬挂缩进
            and "│     x = 1" in text                               # 代码缩进保住
            and "2*3*4" in text and "__init__" in text and "snake_case_name" in text
            and all(_display_width(r) <= 30 for r in rows)
        )
        checks.append(("markdown_render", ok, f"{len(sample)} 种写法｜记号不外露｜最宽 {max(_display_width(r) for r in rows)}/30"))
    except Exception as e:
        checks.append(("markdown_render", False, str(e)))
    try:
        # 流式排版：边流边擦边重画（动画模式）和一行收全再排（无动画模式），
        # 最后留在屏幕上的必须一模一样——擦除算错一排，这里就对不上。
        pieces = [("t", "先读"), ("t", "文件"), ("a", "这是 **加"), ("a", "粗** 文字"),
                  ("a", "\n\n- 项"), ("a", "目一\n- 项目二\n``"), ("a", "`bash\nls -la\n``"),
                  ("a", "`\n" + "很长的一段话" * 9), ("a", "\n\n  \n结尾\n\n")]
        screens = {}
        for live in (True, False):
            buf = io.StringIO()
            view = StreamView(render=True, animate=live, width=30, out=buf)
            for kind, piece in pieces:
                (view.thinking if kind == "t" else view.answer)(piece)
            view.end()
            lines = [_ANSI_RE.sub("", line).rstrip() for line in _tiny_term(_ANSI_RE.sub(
                lambda m: m.group(0) if m.group(0)[-1] in "AJK" else "", buf.getvalue()))[0]]
            while lines and not lines[-1]:
                lines.pop()
            screens[live] = lines
        shown = screens[True]
        joined = "\n".join(shown)
        think_row = next((i for i, line in enumerate(shown) if "先读文件" in line), -1)
        answer_row = next((i for i, line in enumerate(shown) if "这是 加粗 文字" in line), -1)
        ok = (
            screens[True] == screens[False]
            and 0 <= think_row < answer_row                      # 思考和回答各占各的行
            and shown[answer_row].startswith("✦ ")
            and "**" not in joined and "```" not in joined
            and "\n\n\n" not in joined                            # 多余的空行被并掉
            and all(_display_width(line) <= 30 for line in shown)
            and shown[-1].strip() == "结尾"                       # 块尾的空行不留
        )
        checks.append(("stream_view_layout", ok, f"动画/无动画终屏一致 {'OK' if screens[True] == screens[False] else 'FAIL'}｜{len(shown)} 行"))
    except Exception as e:
        checks.append(("stream_view_layout", False, str(e)))
    try:
        # 等待动画：转完不能在屏幕上留下任何东西，光标要回到原来那一行
        real_stdout = sys.stdout
        buf = io.StringIO()
        sys.stdout = buf
        try:
            spin = Spinner("测试", enabled=True, gap=True).start()
            sleep_until = time() + Spinner.DELAY + Spinner.INTERVAL * 3
            while time() < sleep_until:
                threading.Event().wait(0.02)
            spin.stop()
            spin.stop()  # 停两次也不能出事
            quick = Spinner("一闪而过", enabled=True).start()
            quick.stop()  # 短于 DELAY：一帧都不该画
        finally:
            sys.stdout = real_stdout
        raw = buf.getvalue()
        screen, cursor_row = _tiny_term(raw)
        frames_differ = Spinner("x").frame(0, 0) != Spinner("x").frame(1, 0)
        plain = _ANSI_RE.sub("", raw)  # 开着颜色时每个字单独上色，要先去掉转义码再找字
        ok = ("测试" in plain and "一闪而过" not in plain and cursor_row == 0
              and not "".join(screen).strip() and raw.endswith("\x1b[?25h") and frames_differ
              and not Spinner(enabled=False).start().running)
        checks.append(("spinner_leaves_no_trace", ok, f"画了 {raw.count(chr(13)) - 1} 帧｜擦净 OK｜短等待不闪 OK"))
    except Exception as e:
        checks.append(("spinner_leaves_no_trace", False, str(e)))
    try:
        # 这条是用户截图里的 bug：同一步里先想后答，回答被接在思考那一行后面
        class _OneChunkBackend(ChatBackend):
            name = "selftest_one_chunk"
            def create_chat_completion(self, **kwargs):
                delta = types.SimpleNamespace(content="<thinking>先想想</thinking>这是回答", tool_calls=None)
                return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])])
        probe = Agent.__new__(Agent)
        probe.backend, probe.model, probe.max_tokens = _OneChunkBackend(), "m", 16
        probe.stream, probe.quiet, probe.tag = True, False, ""
        probe.tools, probe.mcp_servers, probe._mcp_tool_map = {}, {}, {}
        probe.messages, probe.max_context_messages, probe.max_context_tokens = [], 40, MAX_CONTEXT_TOKENS
        probe.trace_enabled, probe.last_reasoning, probe._native_reasoning_seen = False, "", False
        probe._api_messages = lambda: []
        buf = io.StringIO()
        probe._stream_view = StreamView(render=False, animate=False, out=buf)
        visible = probe._call_llm()
        out = _ANSI_RE.sub("", buf.getvalue())
        ok = "先想想这是回答" not in out and "先想想\n" in out and "这是回答" in out and visible["content"] == "这是回答"
        checks.append(("thinking_answer_separate", ok, "思考和回答不再挤在同一行"))
    except Exception as e:
        checks.append(("thinking_answer_separate", False, str(e)))
    try:
        # exit 不带斜杠也要能退；Ctrl+C 打断之后历史里不能留半截工具调用；启动门禁默认关
        words_ok = all(_is_exit_word(w) for w in ["exit", "  EXIT ", "quit", "退出"]) and not _is_exit_word("exit now")
        listed = any(cmd == "/exit" for _, items in COMMAND_GROUPS for cmd, _ in items)
        probe = Agent.__new__(Agent)
        probe.tag = ""
        probe._stream_view = StreamView(render=False, animate=False, out=io.StringIO())
        probe.messages = [
            {"role": "user", "content": "读两个文件"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "b", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "a", "name": "read_file", "content": "ok"},
        ]
        probe.interrupt()
        answered = {m.get("tool_call_id") for m in probe.messages if m.get("role") == "tool"}
        repaired = answered == {"a", "b"} and probe.messages[-1]["role"] == "assistant"
        original_build = globals()["build_backend"]
        def _no_backend():
            raise AssertionError("门禁默认关着，不该去连模型")
        globals()["build_backend"] = _no_backend
        try:
            gate_ok = STARTUP_GATE or evaluate_online_necessity() is True
        finally:
            globals()["build_backend"] = original_build
        ok = words_ok and listed and repaired and gate_ok
        checks.append(("exit_interrupt_gate", ok,
                       f"exit {'OK' if words_ok and listed else 'FAIL'}｜打断补全 {'OK' if repaired else 'FAIL'}｜"
                       f"门禁默认关 {'OK' if gate_ok else 'FAIL'}"))
    except Exception as e:
        checks.append(("exit_interrupt_gate", False, str(e)))
    def _aged_memory(tmp: str, specs: list[tuple]) -> "Memory":
        """临时记忆库：specs 是 (内容, 类别, 强度天数, 距上次想起的小时数, 是否休眠)。"""
        mem = Memory(os.path.join(tmp, "m.json"), os.path.join(tmp, "v.json"), service=EmbeddingService(mode="local"))
        now = _now_dt()
        for content, category, strength, hours, dormant in specs:
            item = mem.get(mem.add(content, category))
            item.update(strength=strength, dormant=dormant,
                        last_review=_iso(now - datetime.timedelta(hours=hours)),
                        created=_iso(now - datetime.timedelta(days=40 if dormant else 12)))
        mem.persist()
        return mem
    startup_agent = types.SimpleNamespace(tool_profile="full", instructions="", loaded_plugins=[], max_steps=20)
    try:
        # 开屏星空：只读不写（不能顺手复习记忆）、数字和 /memstat 同源、同样的记忆每次画在同一个地方
        with tempfile.TemporaryDirectory() as tmp:
            mem = _aged_memory(tmp, [
                ("回复一律用中文", "preference", 14, 2, False),          # 清晰
                ("README 的数字刚对齐过", "fact", 1.5, 20, False),       # 微光
                ("喜欢深色主题", "preference", 14, 480, False),          # 正在淡忘
                ("旧部署脚本在 /opt/old", "fact", 1.5, 720, True),       # 休眠
            ])
            arch = ConversationArchive(os.path.join(tmp, "c.jsonl"))
            fixed = _now_dt()
            before_items = json.dumps(mem.items, sort_keys=True, ensure_ascii=False)
            before_file = open(mem.path, "rb").read()
            cells, lines = render_startup(startup_agent, 72, memory=mem, archive=arch, now=fixed, tty=True, sky=True)
            sky = [_ANSI_RE.sub("", r) for r in _sky_rows(cells)]
            text = "\n".join(sky + [_ANSI_RE.sub("", l) for l in lines])
            untouched = (json.dumps(mem.items, sort_keys=True, ensure_ascii=False) == before_items
                         and open(mem.path, "rb").read() == before_file)
            stats = mem.curve_stats(fixed)
            parity = (f"{stats['vivid']} 颗亮着" in text and f"{stats['fading']} 颗在变暗" in text
                      and f"{stats['dormant']} 颗已沉睡" in text and stats["vivid"] == 1 and stats["fading"] == 1)
            shuffled = list(reversed(mem.items))
            stable = sky == [_ANSI_RE.sub("", r) for r in _sky_rows(_sky_cells(shuffled, 70, fixed, memory=mem))]
            honest = ("回复一律用中文" in text and "喜欢深色主题" not in text and "旧部署脚本" not in text)
            narrow_ok = all(_display_width(r) <= 72 for r in sky)
            newcomer = "初次见面" in text and "/selftest" in text and "/memstat 看哪些" not in text
            arch.session_id = "old_1"
            arch.append("user", "部署脚本太慢了怎么办")
            _, back = render_startup(startup_agent, 72, memory=mem, archive=arch, now=fixed, tty=True, sky=True)
            back_text = _ANSI_RE.sub("", "\n".join(back))
            returning = "上次我们在聊「部署脚本太慢了怎么办」" in back_text and "/memstat" in back_text and "/selftest" not in back_text
            empty = Memory(os.path.join(tmp, "e.json"), os.path.join(tmp, "ev.json"), service=EmbeddingService(mode="local"))
            _, empty_lines = render_startup(startup_agent, 72, memory=empty, archive=arch, now=fixed, tty=True, sky=True)
            no_memory = "还没有记忆" in _ANSI_RE.sub("", "\n".join(empty_lines))
            buf = io.StringIO()
            _twinkle_in(cells, out=buf, interval=0)
            screen = [line.rstrip() for line in _tiny_term(_ANSI_RE.sub(
                lambda m: m.group(0) if m.group(0)[-1] in "AJK" else "", buf.getvalue()))[0]]
            twinkle_same = [l for l in screen if l] == [l.rstrip() for l in sky if l.rstrip()] and buf.getvalue().endswith("\x1b[?25h")
            plain_cells, plain = render_startup(startup_agent, 72, memory=mem, archive=arch, now=fixed, tty=False)
            plain_ok = plain_cells is None and plain[:len(render_banner().splitlines())] == render_banner().splitlines()
            periods = [_greeting_period(h) for h in (6, 14, 20, 23, 2)] == ["早上好", "下午好", "晚上好", "夜深了", "夜深了"]
        ok = untouched and parity and stable and honest and narrow_ok and newcomer and returning and no_memory and twinkle_same and plain_ok and periods
        checks.append(("star_sky_startup", ok,
                       f"只读 {'OK' if untouched else 'FAIL'}｜与 /memstat 同数 {'OK' if parity else 'FAIL'}｜"
                       f"星位稳定 {'OK' if stable else 'FAIL'}｜沉睡/变暗不贴标签 {'OK' if honest else 'FAIL'}｜"
                       f"新老用户 {'OK' if newcomer and returning else 'FAIL'}｜亮起动画终屏一致 {'OK' if twinkle_same else 'FAIL'}"))
    except Exception as e:
        checks.append(("star_sky_startup", False, str(e)))
    try:
        # 有心情的等待动画：心情怎么映射、动画本身有没有守住「一个颜色」和「不超宽」、开屏那句话是不是真话
        cases = {
            "default": spinner_mood(dict(_MOOD_BASELINE)) is None and spinner_mood(None) is None,
            "tired": (spinner_mood({"fatigue": 65}) or SpinnerMood("", "")).key == "tired",
            "frustrated": (spinner_mood({"fatigue": 70, "frustration": 40}) or SpinnerMood("", "")).key == "frustrated",
            "on_a_roll": (spinner_mood({"confidence": 75}) or SpinnerMood("", "")).key == "on_a_roll",
            "bad_values": spinner_mood({"fatigue": "x", "frustration": None}) is None,
        }
        saved_color, saved_width = _COLOR, globals()["_term_width"]
        try:
            globals()["_COLOR"] = True
            frames = [Spinner("测试", faint=True).frame(0, 3), Spinner("测试", sweep=2).frame(1, 3), Spinner("测试").frame(2, 3)]
            codes = {int(c) for f in frames for c in re.findall(r"38;5;(\d+)", f)}
            one_hue = codes <= {111, 153, 189} and not any(re.search(r"\x1b\[3[0-7]m", f) for f in frames)
            faint_dim = "\x1b[2m" in frames[0].split("测")[0]
            globals()["_term_width"] = lambda: 24
            narrow = _visible_width(Spinner("有点累，慢慢来，再想想").frame(0, 12)) <= 24
        finally:
            globals()["_COLOR"], globals()["_term_width"] = saved_color, saved_width
        state = dict(_MOOD_BASELINE)
        real_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            view = StreamView(render=False, animate=True, out=io.StringIO())
            view.mood_source = lambda: state
            view.begin_turn()
            labels = []
            for change in ({}, {}, {"fatigue": 70}, {"frustration": 40}):
                state.update(change)
                view.wait()
                labels.append((view.spinner.label, view.spinner.interval))
                view.stop_wait()
            view.wait("回头检查")
            explicit = view.spinner.label == "回头检查" and view.spinner.interval == labels[-1][1]
            view.stop_wait()
            drew = sys.stdout.getvalue()
        finally:
            sys.stdout = real_stdout
        verbs_ok = (labels[0][0] in Spinner.VERBS and labels[0][0] == labels[1][0]
                    and labels[2] == ("有点累，慢慢来", 0.14) and labels[3][0] == "有点受挫，接着找"
                    and explicit and not drew.strip())
        late = datetime.datetime(2026, 1, 1, 23, 30)
        def line(**kw):
            return _ANSI_RE.sub("", _mood_line(startup_agent, 80, now=kw.pop("now", late), state=dict(_MOOD_BASELINE, **kw)))
        calm, morning = line(), line(now=datetime.datetime(2026, 1, 1, 10, 0))
        lines_ok = (calm.strip().startswith("☾") and "状态平稳" in calm and morning.strip().startswith("☀")
                    and "16" in line(fatigue=70) and "14" in line(fatigue=84)
                    and "有点累" in line(fatigue=61) and "步" not in line(fatigue=61)  # 阈值边上：第一回合其实没少步数
                    and "多留" in line(frustration=64) and line(confidence=75).strip().startswith("✧"))
        no_lies = not any(word in line(**kw) for kw in ({"fatigue": 90}, {"frustration": 90}, {"confidence": 20})
                          for word in ("拆", "子代理", "委托"))
        ok = all(cases.values()) and one_hue and faint_dim and narrow and verbs_ok and lines_ok and no_lies
        checks.append(("mood_spinner", ok,
                       f"映射 {sum(cases.values())}/{len(cases)}｜只用一个蓝 {'OK' if one_hue else 'FAIL'}｜"
                       f"窄终端不折行 {'OK' if narrow else 'FAIL'}｜换档才换词 {'OK' if verbs_ok else 'FAIL'}｜"
                       f"开屏心情说真话 {'OK' if lines_ok and no_lies else 'FAIL'}"))
    except Exception as e:
        checks.append(("mood_spinner", False, str(e)))
    try:
        # 记忆闪回：显示的是「被这次想起之前」有多清晰；只认真正发出去的；每回合只亮一次
        saved_memory = globals()["MEMORY"]
        saved_mood = json.loads(json.dumps(MOOD.state))
        with tempfile.TemporaryDirectory() as tmp:
            try:
                mem = _aged_memory(tmp, [
                    ("回复要用中文", "preference", 14, 48, False),
                    ("README 的数字刚对齐过", "fact", 1.5, 6, False),
                    ("先给示例图再动手", "preference", 14, 216, False),
                ])
                globals()["MEMORY"] = mem
                pre = {m["id"]: mem.retention(m) for m in mem.items}
                provider = MemoryContextProvider()
                provider.render("README 中文 示例图")
                rec = provider.last_recall
                pre_ok = (rec["kind"] == "recall" and rec["rows"]
                          and all(abs(r["retention"] - pre[r["id"]]) < 1e-6 and r["retention"] < 0.99 for r in rec["rows"])
                          and all(mem.retention(mem.get(r["id"])) > 0.999 for r in rec["rows"]))  # 强化照样发生了
                before_reviews = [m["reviews"] for m in mem.items]
                provider.render("好")
                recent_ok = provider.last_recall["kind"] == "recent" and [m["reviews"] for m in mem.items] == before_reviews
                # 走一遍真实的上下文拼装，再连调两次模型：闪回只出现一次，而且排在回答前面
                class _Answer(ChatBackend):
                    name = "selftest_answer"
                    def create_chat_completion(self, **kwargs):
                        delta = types.SimpleNamespace(content="这是回答", tool_calls=None)
                        return iter([types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])])
                probe = Agent.__new__(Agent)
                probe.system_base, probe.instructions, probe.model, probe.mode = "base", "", "m", "auto"
                probe.messages, probe.max_context_messages, probe.max_context_tokens = [], 40, MAX_CONTEXT_TOKENS
                probe._system_prompt_cache_key, probe._system_prompt_cache = "", ""
                probe._native_reasoning_seen, probe._current_user_input = False, "README 中文 示例图"
                probe.context_builder, probe.reload_instructions = ContextBuilder(), lambda: ""
                probe.backend, probe.max_tokens, probe.stream, probe.quiet, probe.tag = _Answer(), 16, True, False, ""
                probe.tools, probe.mcp_servers, probe._mcp_tool_map = {}, {}, {}
                probe.trace_enabled, probe.last_reasoning, probe.show_recall = False, "", True
                buf = io.StringIO()
                probe._stream_view = StreamView(render=False, animate=False, out=buf)
                probe._stream_view.turn_started = 0.0
                probe._recall_pending = True
                probe._call_llm()
                probe._call_llm()
                out = _ANSI_RE.sub("", buf.getvalue())
                once = out.count("想起") == 1 and out.index("想起") < out.index("这是回答") and "▰" in out
                # 截断：只露个头的、根本没发出去的，不能算想起
                provider.last_recall = {"kind": "recall", "rows": [dict(r, id=i, content=f"第{i}条记忆内容")
                                                                 for i, r in enumerate(rec["rows"][:2], 1)], "at": time()}
                probe.context_builder.providers = [provider]
                probe._last_context_note = "[memory] #1 第1条记忆内容"
                _, kept = probe._injected_memories()
                probe._stream_view.turn_started = time() + 60
                _, stale = probe._injected_memories()
                injected_ok = [r["id"] for r in kept] == [1] and stale == []
                quiet_buf = io.StringIO()
                for attrs in ({"quiet": True}, {"tag": "    [子代理]"}, {"show_recall": False}):
                    q = Agent.__new__(Agent)
                    q.quiet, q.tag, q.show_recall, q.trace_enabled = False, "", True, False
                    q.__dict__.update(attrs)
                    q.context_builder, q._last_context_note = probe.context_builder, probe._last_context_note
                    q._stream_view = StreamView(render=False, animate=False, out=quiet_buf)
                    q._stream_view.turn_started = 0.0  # 让检索结果算「这一回合的」，确保是开关在挡而不是过期在挡
                    q._show_recall()
                gated = "想起" not in quiet_buf.getvalue()
                # 写记忆：⎿ 行换成 ✎ 记住了，发给模型的结果原样不动
                writer = Agent.__new__(Agent)
                writer.tools, writer.mcp_servers, writer._mcp_tool_map = {"remember": remember}, {}, {}
                writer.mode, writer.quiet, writer.tag, writer.trace_enabled, writer.show_recall = "auto", True, "", False, True
                writer.model = "m"
                writer.messages, writer.max_context_messages, writer.max_context_tokens = [], 40, MAX_CONTEXT_TOKENS
                wbuf = io.StringIO()
                writer._stream_view = StreamView(render=False, animate=False, out=wbuf)
                writer._run_tools_and_feed_back({"tool_calls": [{"id": "w", "type": "function", "function": {
                    "name": "remember", "arguments": json.dumps({"content": " 测试记忆 ", "category": "fact"}, ensure_ascii=False)}}]})
                wout = _ANSI_RE.sub("", wbuf.getvalue())
                remember_ok = ("✎ 记住了：测试记忆  ▰▰▰▰▰ 新" in wout and "⎿" not in wout
                               and writer.messages[-1]["content"].startswith("已写入长期记忆 #"))
                render_buf = io.StringIO()
                stamp = _now_dt()
                StreamView(render=False, animate=False, out=render_buf, width=60).recall([
                    {"id": 1, "content": "回复要用中文", "retention": 0.87, "last_review": _iso(stamp - datetime.timedelta(days=2))},
                    {"id": 2, "content": "README 的数字刚对齐过", "retention": 0.55, "last_review": _iso(stamp - datetime.timedelta(hours=6))},
                    {"id": 3, "content": "先给示例图再动手", "retention": 0.2, "last_review": _iso(stamp - datetime.timedelta(days=9))},
                    {"id": 4, "content": "第四条", "retention": 0.9, "last_review": _iso(stamp)},
                ], now=stamp)
                rlines = _ANSI_RE.sub("", render_buf.getvalue()).strip("\n").splitlines()
                drawing_ok = (rlines[0] == "◌ 想起 4 件事" and "▰▰▰▰▱ 2 天前" in rlines[1] and "▰▰▰▱▱ 6 小时前" in rlines[2]
                              and "▰▱▱▱▱ 正在淡去" in rlines[3] and "还有 1 件" in rlines[4]
                              and all(_display_width(l) <= 60 for l in rlines)
                              and [_ago(_iso(stamp - d), stamp) for d in (datetime.timedelta(seconds=30), datetime.timedelta(minutes=5))] == ["刚刚", "5 分钟前"])
            finally:
                globals()["MEMORY"] = saved_memory
                MOOD.state.clear()
                MOOD.state.update(saved_mood)
                MOOD._save()
        ok = pre_ok and recent_ok and once and injected_ok and gated and remember_ok and drawing_ok
        checks.append(("memory_flashback", ok,
                       f"显示强化前的清晰度 {'OK' if pre_ok else 'FAIL'}｜没线索不算想起 {'OK' if recent_ok else 'FAIL'}｜"
                       f"每回合一次 {'OK' if once else 'FAIL'}｜截掉的不算 {'OK' if injected_ok else 'FAIL'}｜"
                       f"子代理静默 {'OK' if gated else 'FAIL'}｜记住了 {'OK' if remember_ok else 'FAIL'}"))
    except Exception as e:
        checks.append(("memory_flashback", False, str(e)))
    try:
        # 星座进度条：画法对、放不下会竖排、用时只在有时间戳时才说，而且发给模型的结果一个字不变
        def plain(rows: list[str]) -> list[str]:
            return [_ANSI_RE.sub("", r) for r in rows]
        three = [{"text": "定位文件", "done": True}, {"text": "修复", "done": False}, {"text": "验证", "done": False}]
        rows = plain(render_constellation(three, 40))
        stars = rows[0]
        second = [i for i, ch in enumerate(stars) if ch in "✦✧"][1]
        horizontal = (stars.count("✦") == 2 and stars.count("✧") == 1 and "━" in stars and "┄" in stars
                      and "━" not in stars[second:] and all(f"{i}." in rows[1] for i in (1, 2, 3))
                      and rows[2].index("↑") == second and all(_display_width(r) <= 40 for r in rows))
        many = [{"text": "一个很长很长的任务名称" * 2, "done": i < 3} for i in range(12)]
        vrows = plain(render_constellation(many, 30))
        vertical = len(vrows) == 12 and "━" not in "".join(vrows) and all(_display_width(r) <= 30 for r in vrows)
        finished = [dict(t, done=True) for t in three]
        no_time = "\n".join(plain(render_constellation(finished, 60)))
        start = datetime.datetime.now().astimezone().replace(microsecond=0)
        def stamped(begin, end):
            return [dict(t, created_at=begin.isoformat(), done_at=end.isoformat()) for t in finished]
        with_time = "\n".join(plain(render_constellation(stamped(start, start + datetime.timedelta(minutes=10)), 60)))
        naive = "\n".join(plain(render_constellation(stamped(start.replace(tzinfo=None), start.replace(tzinfo=None)), 60)))
        backwards = "\n".join(plain(render_constellation(stamped(start, start - datetime.timedelta(minutes=20)), 60)))
        long_took = plain(render_constellation(stamped(start, start + datetime.timedelta(hours=11, minutes=59)), 30))
        done_ok = ("✧ 完成" in no_time and "✓ 全部完成 · 3 步" in no_time and "用时" not in no_time
                   and "用时 10 分钟" in with_time
                   and "用时" not in naive and "用时" not in backwards           # 没时区、倒着走的时间都不报
                   and all(_display_width(r) <= 30 for r in long_took))      # 窄的时候整段「用时」宁可不要
        compact_row = plain(render_constellation(three, 75, compact=True, just_done=1))[0]
        compact_ok = compact_row.strip().startswith("✦┄┄┄✧┄┄┄✧")  # 一行版不标「当前」，做完的才是 ✦
        plan_rows = "\n".join(plain(render_constellation([{"text": s, "done": False} for s in ("a", "b")], 40, show_current=False)))
        plan_ok = "正在做" not in plan_rows and "✦" not in plan_rows
        with tempfile.TemporaryDirectory() as tmp:
            todo = TodoList(os.path.join(tmp, "t.json"))
            todo.set(["读", "改"])
            todo.complete(1)
            first_done = todo.items[0].get("done_at")
            todo.complete(1)
            stamps_ok = (all("created_at" in it for it in todo.items) and first_done and todo.items[0]["done_at"] == first_done
                         and todo.render() == "1. [x] 读\n2. [ ] 改")
        saved_todos = [dict(it) for it in TODOS.items]
        saved_mood = json.loads(json.dumps(MOOD.state))
        try:
            def run(tool, args, *, render, animate=False):
                probe = Agent.__new__(Agent)
                probe.tools = {"set_todos": set_todos, "complete_todo": complete_todo}
                probe.mcp_servers, probe._mcp_tool_map, probe.mode, probe.quiet, probe.tag = {}, {}, "auto", True, ""
                probe.trace_enabled, probe.messages, probe.model = False, [], "m"
                probe.max_context_messages, probe.max_context_tokens = 40, MAX_CONTEXT_TOKENS
                out = io.StringIO()
                probe._stream_view = StreamView(render=render, animate=animate, width=60, out=out)
                probe._run_tools_and_feed_back({"tool_calls": [{"id": "t", "type": "function", "function": {
                    "name": tool, "arguments": json.dumps(args, ensure_ascii=False)}}]})
                return out.getvalue(), probe.messages[-1]["content"]
            shown, sent = run("set_todos", {"todos": ["读", "改"]}, render=True)
            inline_ok = sent == "Task list updated:\n1. [ ] 读\n2. [ ] 改" and "✦" in shown and "Task list" not in shown
            piped, _ = run("set_todos", {"todos": ["读", "改"]}, render=False)
            piped_ok = "⎿ Task list updated:" in _ANSI_RE.sub("", piped) and "✦" not in piped
            missing, _ = run("complete_todo", {"index": 99}, render=True)
            missing_ok = "There is no item #99" in missing and "✦" not in missing
            StreamView.FLASH_INTERVAL, saved_flash = 0, StreamView.FLASH_INTERVAL
            screens = []
            try:
                for animate in (True, False):
                    TODOS.set(["读", "改"])
                    TODOS.complete(1)
                    raw, _ = run("complete_todo", {"index": 2}, render=True, animate=animate)
                    screens.append([l.rstrip() for l in _tiny_term(_ANSI_RE.sub(
                        lambda m: m.group(0) if m.group(0)[-1] in "AJK" else "", raw))[0]])
            finally:
                StreamView.FLASH_INTERVAL = saved_flash
            flash_ok = screens[0] == screens[1] and any("完成" in l for l in screens[0])
        finally:
            TODOS.items = saved_todos
            TODOS.persist()
            MOOD.state.clear()
            MOOD.state.update(saved_mood)
            MOOD._save()
        ok = horizontal and vertical and done_ok and compact_ok and plan_ok and stamps_ok and inline_ok and piped_ok and missing_ok and flash_ok
        checks.append(("constellation_progress", ok,
                       f"横排 {'OK' if horizontal else 'FAIL'}｜竖排兜底 {'OK' if vertical else 'FAIL'}｜"
                       f"用时不编 {'OK' if done_ok else 'FAIL'}｜一行版 {'OK' if compact_ok else 'FAIL'}｜发给模型的不变 {'OK' if inline_ok else 'FAIL'}｜"
                       f"管道原样 {'OK' if piped_ok else 'FAIL'}｜闪完终屏一致 {'OK' if flash_ok else 'FAIL'}"))
    except Exception as e:
        checks.append(("constellation_progress", False, str(e)))
    try:
        # 审查找出来的问题，一条一个回归检查：以后谁改回去，这里会先叫
        results = {}
        with tempfile.TemporaryDirectory() as tmp:
            arch = ConversationArchive(os.path.join(tmp, "c.jsonl"))
            arch.session_id = "old"
            arch.append("user", "客户张三的合同金额怎么报税")
            mem = _aged_memory(tmp, [("带时区的记忆", "fact", 1.5, 1, False)])
            mem.items[0]["last_review"] = "2026-01-01T08:00:00Z"   # 手改/同步来的带时区时间
            fixed = _now_dt()
            _, shared = render_startup(startup_agent, 80, memory=mem, archive=arch, now=fixed, tty=True, sky=False)
            shared = _ANSI_RE.sub("", "\n".join(shared))
            results["关掉星空不报话题"] = "张三" not in shared and "又见面了" in shared
            init = ConversationArchive(os.path.join(tmp, "i.jsonl"))
            init.append("user", INIT_PROMPT)
            results["没有 AGENT.md 不说初始化了"] = "初始化了" not in _last_session_greeting(init._read_all(), fixed)
            try:
                render_startup(startup_agent, 80, memory=mem, archive=arch, now=fixed, tty=True, sky=True)
                results["带时区的时间戳不崩"] = True
            except Exception:
                results["带时区的时间戳不崩"] = False
        # 心情行的步数：和真正跑循环时走同一个 REASON.budget_for，环境里文件多少、待办多少都算进去
        steps_ok = True
        for fatigue in (70, 84):
            probe_mood = MoodState.__new__(MoodState)
            probe_mood.state = dict(_MOOD_BASELINE, fatigue=fatigue)
            probe_mood.rebalance(0.05)
            base = MoodState.__new__(MoodState)
            base.state = dict(_MOOD_BASELINE)
            cap, calm = min(20, REASON.budget_for(probe_mood)), min(20, REASON.budget_for(base))
            said = _ANSI_RE.sub("", _mood_line(startup_agent, 100, state=dict(_MOOD_BASELINE, fatigue=fatigue)))
            steps_ok &= (f"收到 {cap} 步（平时 {calm}）" in said) if cap < calm else ("步" not in said.split("，")[1] if "，" in said else True)
        results["步数和循环一致"] = steps_ok
        # system 模式：一次拼提示词只拼一次运行时上下文（拼两次会让闪回拿到复习之后的满格）
        saved_pos = globals()["CONTEXT_POSITION"]
        try:
            globals()["CONTEXT_POSITION"] = "system"
            p = Agent.__new__(Agent)
            p.system_base, p.instructions, p.model, p._native_reasoning_seen = "b", "", "m", False
            p._system_prompt_cache_key, p._system_prompt_cache, p.reload_instructions = "", "", lambda: ""
            calls = []
            p._runtime_context_note = lambda: calls.append(1) or "note"
            prompt = p._system_prompt()
            results["system 模式只拼一次"] = len(calls) == 1 and prompt.endswith("note")
        finally:
            globals()["CONTEXT_POSITION"] = saved_pos
        # 宽度：✎ 那行、闪回、开屏统计、窄终端里的动画，都不许比给的宽度宽
        wbuf = io.StringIO()
        wide = StreamView(render=False, animate=False, out=wbuf, width=79)
        wide.remembered("这是一条很长很长的记忆" * 6, as_result=True)
        wide.remembered("这是一条很长很长的教训" * 6, label="记下一条教训")
        rbuf = io.StringIO()
        StreamView(render=False, animate=False, out=rbuf, width=24).recall(
            [{"id": 1, "content": "很长的一条记忆内容", "retention": 0.5, "last_review": _iso(_now_dt() - datetime.timedelta(hours=23))}])
        saved_size = shutil.get_terminal_size
        try:
            shutil.get_terminal_size = lambda fallback=(80, 24): os.terminal_size((16, 24))
            tiny = _visible_width(Spinner("有点累，慢慢来").frame(0, 3)) <= 15
        finally:
            shutil.get_terminal_size = saved_size
        rows = [l for buf in (wbuf, rbuf) for l in _ANSI_RE.sub("", buf.getvalue()).splitlines() if l]
        results["不超宽"] = (all(_display_width(l) <= 79 for l in rows[:2]) and all(_display_width(l) <= 24 for l in rows[2:])
                           and _visible_width(_sky_summary({"total": 1400, "vivid": 30, "steady": 30, "fading": 130, "dormant": 1200}, 60)) <= 60
                           and tiny)
        # 任务做完闪的那半秒按 Ctrl+C：历史里得是工具真实的结果，剩下的行也要画完、光标要还回来
        class _Interrupting(StreamView):
            def _write(self, text: str) -> None:
                if text.startswith("\r") and not getattr(self, "fired", False):
                    self.fired = True
                    raise KeyboardInterrupt
                super()._write(text)
        saved_todos = [dict(it) for it in TODOS.items]
        saved_mood = json.loads(json.dumps(MOOD.state))
        try:
            TODOS.set(["只有一项"])
            p = Agent.__new__(Agent)
            p.tools, p.mcp_servers, p._mcp_tool_map, p.mode, p.quiet, p.tag = {"complete_todo": complete_todo}, {}, {}, "auto", True, ""
            p.trace_enabled, p.messages, p.model, p.max_context_messages, p.max_context_tokens = False, [], "m", 40, MAX_CONTEXT_TOKENS
            ibuf = io.StringIO()
            p._stream_view = _Interrupting(render=True, animate=True, width=60, out=ibuf)
            saved_flash, StreamView.FLASH_INTERVAL = StreamView.FLASH_INTERVAL, 0
            try:
                p._run_tools_and_feed_back({"tool_calls": [{"id": "k", "type": "function",
                                                            "function": {"name": "complete_todo", "arguments": '{"index": 1}'}}]})
                interrupted = False
            except KeyboardInterrupt:
                interrupted = True
            finally:
                StreamView.FLASH_INTERVAL = saved_flash
            results["闪的时候打断不丢结果"] = (interrupted and p.messages and p.messages[-1]["content"].startswith("Marked complete.")
                                         and ibuf.getvalue().endswith("\x1b[?25h") and "全部完成" in ibuf.getvalue())
        finally:
            TODOS.items = saved_todos
            TODOS.persist()
            MOOD.state.clear()
            MOOD.state.update(saved_mood)
            MOOD._save()
        vbuf = io.StringIO()
        StreamView(render=True, animate=True, width=40, out=vbuf).todo_progress(
            [{"text": f"第{i}步", "done": True} for i in range(12)], flash=True)
        results["竖排不闪第一项"] = "\r" not in vbuf.getvalue()
        ok = all(results.values())
        failed = [k for k, v in results.items() if not v]
        checks.append(("ui_review_regressions", ok, f"{sum(results.values())}/{len(results)}" + (f"｜没过：{'、'.join(failed)}" if failed else "")))
    except Exception as e:
        checks.append(("ui_review_regressions", False, str(e)))
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
        if _TTY and TODOS.items:
            # 提示符前什么都没在跑，清单也可能是上次留下的，所以叫「下一步」而不是「正在做」
            print("\n".join(render_constellation(TODOS.items, _term_width(), current_label="下一步")))
        else:
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
        print(_mood_line(agent, _term_width()))
        # 「状态：超清醒」那行去掉：label() 不看疲劳，会和上面「有点累了」自相矛盾
        print("\n".join(line for line in MOOD.render().splitlines() if not line.startswith("状态：")))
        return True, None
    if command == "flashback":
        if args and args[0] in {"on", "off"}:
            agent.show_recall = args[0] == "on"
        elif not args:
            agent.show_recall = not getattr(agent, "show_recall", SHOW_RECALL)
        else:
            print("用法：/flashback [on|off]")
            return True, None
        print(f"记忆闪回已{'开启' if agent.show_recall else '关闭'}。")
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
            if _TTY and PRODUCTIVITY.last_steps:
                # 计划只是拆好了，还没开始做，也没写进待办清单：全画成暗星，不标「正在做」
                steps = [{"text": s, "done": False} for s in PRODUCTIVITY.last_steps]
                print()
                print("\n".join(render_constellation(steps, _term_width(), show_current=False)))
                print(paint("  （计划还没写进待办清单）", "dim"))
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
    if command == "lean":
        if args:
            print(agent.set_tool_profile(args[0]))
            return True, None
        if agent.tool_profile == "full":
            print(agent.set_tool_profile("code"))
            print("（想更省用 /lean min，只读任务用 /lean read，切回全量用 /lean full）")
        else:
            print(agent.set_tool_profile("full"))
        return True, None
    if command == "cost":
        full_n, full_t = tool_profile_cost("full")
        cur_n, cur_t = tool_profile_cost(agent.tool_profile)
        sys_t = _estimate_tokens(agent._system_prompt())
        print("【每步固定开销估算】每一步都会重发，是长任务里最大的一笔重复支出。")
        print(f"  工具 schema（{agent.tool_profile} 档，{cur_n} 个内置工具）：约 {cur_t:,} tokens")
        print(f"  系统提示词（人设 + 指令 + 运行时上下文）：      约 {sys_t:,} tokens")
        print(f"  合计：约 {cur_t + sys_t:,} tokens/步")
        print("\n各档位对比：")
        for name in TOOL_PROFILES:
            n, t = tool_profile_cost(name)
            mark = " ←当前" if name == agent.tool_profile else ""
            delta = "" if name == "full" else f"（相对全量 -{(1 - t / full_t) * 100:.0f}%）"
            print(f"  {name:<6} {n:>2} 个工具  约 {t:>6,} tokens {delta}{mark}")
        print(f"\n自检开关：反思{'开着' if agent.reflect else '关着'}"
              f"——开着的话，每个用过工具的回合会额外发一次完整请求（含全部历史）。/reflect 可切换。")
        if CONTEXT_POSITION == "tail":
            stable = _estimate_tokens(agent._system_prompt())
            tail_msg = agent._runtime_context_message()
            volatile = _estimate_tokens(tail_msg["content"]) if tail_msg else 0
            print(f"提示词排布：tail（易变状态挂末尾）。稳定前缀约 {cur_t + stable:,} tokens 可被服务端缓存，"
                  f"每回合真正变动的只有末尾约 {volatile:,} tokens。")
        else:
            print("提示词排布：system（老排法）。易变状态塞在系统提示词里，"
                  "任何一点变化都会让整段历史的缓存前缀作废。POLARIS_CONTEXT_POSITION=tail 可切回。")
        return True, None
    if command == "tools":
        def _flag_suffix(dangerous: bool, mutating: bool) -> str:
            flags = (["需确认"] if dangerous else []) + (["写操作"] if mutating else [])
            return f"（{'/'.join(flags)}）" if flags else ""
        hidden = 0
        for t in agent.tools.values():
            first_line = t.schema["description"].splitlines()[0]
            suffix = _flag_suffix(getattr(t, "dangerous", False), getattr(t, "mutating", False))
            if not agent._tool_in_profile(t.schema["name"]):
                hidden += 1
                continue
            print(f"  - {t.schema['name']}{suffix}: {first_line}")
        # MCP 工具以前在这里完全不显示，但模型是看得见也调得动的——
        # 用户无从知道自己接进来的服务器给了模型哪些能力。
        for name in agent._mcp_tool_map:
            found = agent._mcp_lookup(name)
            desc = (found[2].get("description") or "").splitlines()
            suffix = _flag_suffix(*agent._tool_flags(name))
            print(f"  - {name}{suffix} [MCP]: {desc[0] if desc else '(无描述)'}")
        if hidden:
            print(f"\n  （工具档位 {agent.tool_profile} 另外隐藏了 {hidden} 个内置工具，/lean full 可全部启用）")
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
            f"工具档位：{agent.tool_profile}（约 {tool_profile_cost(agent.tool_profile)[1]:,} tokens/步，/cost 看明细）",
        ]))
        return True, None
    if command in {"help", "h", "?"}:
        print(render_help())
        return True, None
    if command == "init":
        return True, INIT_PROMPT
    print(paint(f"未知命令 /{command}。输入 /help 查看全部命令。", "yellow"))
    return True, None
# ───────────────────────────────────────────────────────────────────────────────
# 命令目录：横幅只露出最常用的几条，其余交给 /help
#
# 旧版把 43 条命令全平铺在开屏，27 行——新用户根本看不出哪几条是现在要用的。
# 信息不是越多越好，第一屏要能让人知道下一步做什么。
# ───────────────────────────────────────────────────────────────────────────────
COMMAND_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    ("会话", [
        ("/help", "查看全部命令"),
        ("/mode [plan|ask|auto]", "权限模式：只读 / 每步确认 / 放手去做"),
        ("/reset", "清空当前会话与待办"),
        ("/reflect", "开关每回合的自我检查"),
        ("/thought [on|off|preview]", "思考轨迹的显示方式"),
        ("/exit", "退出（直接打 exit 也行）"),
    ]),
    ("记忆", [
        ("/memory [n]", "查看长期记忆与它们的遗忘曲线"),
        ("/recall <查询>", "语义召回，带相似度与保持率明细"),
        ("/memstat", "记忆整体状态：清晰 / 正在淡忘 / 已休眠"),
        ("/remember [-c 类别] <内容>", "手动写入一条长期记忆"),
        ("/forget <编号>", "删除一条记忆"),
        ("/pin <编号>", "设为常驻，永不衰减"),
        ("/unpin <编号>", "取消常驻"),
        ("/revive <编号>", "唤醒一条已休眠的记忆"),
        ("/searchmem <查询>", "按关键词搜索记忆"),
        ("/flashback [on|off]", "回答前显示想起了哪些记忆"),
    ]),
    ("工作与文件", [
        ("/plan <目标>", "把目标拆成可执行步骤"),
        ("/todo", "当前任务清单"),
        ("/undo", "回退上一次文件修改"),
        ("/workspace", "项目世界模型"),
        ("/insight", "依赖健康度与耦合热点"),
        ("/refreshws", "重新扫描工作区"),
        ("/searchws <查询>", "搜索工作区文件"),
        ("/init", "生成项目的 AGENT.md"),
    ]),
    ("观察", [
        ("/trace [n|json]", "上一回合到底做了什么"),
        ("/cost", "每步 token 开销与各档位对比"),
        ("/lean [档位]", "精简工具集省 token"),
        ("/stats", "统计概览"),
        ("/doctor", "系统诊断"),
        ("/selftest", "运行内置自检"),
        ("/tools", "当前暴露给模型的工具"),
        ("/plugins", "已加载的插件"),
    ]),
    ("内在状态", [
        ("/state", "心情 / 决策 / 世界模型 / 共同经历"),
        ("/mood", "心情状态"),
        ("/diary", "心情日记"),
        ("/persona", "人格设定"),
        ("/relationship", "关系状态"),
        ("/reason", "决策引擎"),
        ("/journal", "成长日志"),
    ]),
    ("历史", [
        ("/history", "上一个对话窗口"),
        ("/experience", "共同经历"),
        ("/timeline", "历史时间线"),
        ("/searchchat <查询>", "搜索全部对话"),
        ("/searchlast <查询>", "搜索上一个窗口"),
        ("/snapshot", "保存运行时状态快照"),
        ("/snapshots", "列出快照"),
        ("/restore <编号>", "恢复快照"),
    ]),
]
# 开屏只露这几条：一个新用户真正需要的最小集合
BANNER_COMMANDS = [
    ("/help", "全部命令"), ("/mode", "权限模式"),
    ("/memory", "长期记忆"), ("/trace", "执行轨迹"),
    ("/cost", "开销估算"), ("/lean", "精简省 token"),
    ("/undo", "回退文件"), ("/selftest", "运行自检"),
]
def _box(lines: list[str], width: int = 58) -> list[str]:
    """圆角边框。先按显示宽度补齐再上色——反过来做的话，
    转义码会被算进宽度里，边框就参差不齐了。"""
    top = paint("╭" + "─" * (width - 2) + "╮", "accent")
    bottom = paint("╰" + "─" * (width - 2) + "╯", "accent")
    body = []
    for line, styles in lines:
        padded = _pad_display(_fit_display(line, width - 4), width - 4)
        bar = paint("│", "accent")
        body.append(f"{bar} {paint(padded, *styles)} {bar}")
    return [top, *body, bottom]
def render_banner() -> str:
    out = _box([
        (f"✦ Polaris v{VERSION} · {CODENAME}", ("bold",)),
        ("记忆会遗忘，思考留痕迹，人格在生长", ("dim",)),
    ])
    out.append("")
    half = (len(BANNER_COMMANDS) + 1) // 2
    left, right = BANNER_COMMANDS[:half], BANNER_COMMANDS[half:]
    for i in range(half):
        row = "  "
        for cmd, desc in ((left[i],) + ((right[i],) if i < len(right) else ())):
            row += paint(_pad_display(cmd, 11), "accent") + _pad_display(desc, 17)
        out.append(row.rstrip())
    total = sum(len(items) for _, items in COMMAND_GROUPS)
    out.append("")
    out.append(paint(f"  输入 /help 查看全部 {total} 条命令", "dim"))
    return "\n".join(out)
def render_help() -> str:
    out = []
    for group, items in COMMAND_GROUPS:
        out.append(paint(f"\n{group}", "bold", "accent"))
        width = max(_display_width(cmd) for cmd, _ in items) + 2
        for cmd, desc in items:
            out.append("  " + paint(_pad_display(cmd, width), "accent") + paint(desc, "dim"))
    return "\n".join(out)
def _enable_line_editing() -> bool:
    """让输入行支持方向键、行内编辑和 ↑ 翻历史。

    Python 的 input() 只有在 readline 被导入过之后才有这些；没导入的话按方向键
    只会打出 ^[[D 这样的乱码。Windows 没有这个模块，跳过即可，它的控制台自带行编辑。"""
    try:
        import readline  # noqa: F401
        return True
    except Exception:
        return False
def _input_prompt(line_editing: bool) -> str:
    text = paint("你 ", "accent") + paint("› ", "dim")
    # stdin 不是终端时 readline 不接管输入，\001 \002 会被原样写出去
    if line_editing and _COLOR and sys.stdin.isatty():
        # 转义码要用 \001 \002 包起来，readline 才知道它们不占宽度；
        # 不包的话一按方向键，光标位置就全乱了。
        text = _ANSI_RE.sub(lambda m: "\001" + m.group(0) + "\002", text)
    return text
_EXIT_WORDS = {"exit", "quit", "退出"}
def _is_exit_word(text: str) -> bool:
    """不带斜杠的 exit 也要能退出——/help 里写着呢，以前却会被当成一句话发给模型。"""
    return text.strip().lower() in _EXIT_WORDS
def _startup_status(agent: Agent, width: int, *, with_memory: bool = True) -> list[str]:
    """开屏最下面那两行摘要。按终端宽度截断，所以只放真正有用的信息：
    完整的端点 URL、工作区摘要这类长东西，截一半等于没写。
    心情不再放这儿：MOOD.label() 在疲劳 66 时还是「超清醒」，会和上面那行心情打架。"""
    bits = [f"v{VERSION}", f"模型 {MODEL}"]
    if BACKEND_NAME not in {"openai_compatible", "openai-compatible", "openai"}:
        bits.append(f"后端 {BACKEND_NAME}")
    if BASE_URL:
        bits.append(f"端点 {urlparse(BASE_URL).netloc or BASE_URL}")
    bits.append(f"工具 {agent.tool_profile}({tool_profile_cost(agent.tool_profile)[0]})")
    state = [f"记忆 {len(MEMORY.items)} 条"] if with_memory else []
    state += [f"信任 {RELATIONSHIP.state.get('trust', 0)}", f"文件 {len(WORKSPACE.file_index)} 个"]
    if WORKSPACE.recent_files:
        state.append(f"最近改动 {Path(WORKSPACE.recent_files[0]['path']).name}")
    lines = [" · ".join(bits), " · ".join(state)]
    extras = []
    if agent.instructions:
        extras.append("已注入项目自定义指令")
    if agent.loaded_plugins:
        extras.append(f"插件 {len(agent.loaded_plugins)} 个")
    if _looks_local_endpoint(BASE_URL) or BACKEND_NAME in {"ollama", "lmstudio", "local"}:
        extras.append("本地兼容服务模式")
    if extras:
        lines.append(" · ".join(extras))
    return [paint("  " + _fit_display(line, width - 2), "dim") for line in lines]
# ───────────────────────────────────────────────────────────────────────────────
# 开屏：记忆星空 + 问候 + 心情
#
# 每颗星是一段长期记忆，亮度就是它此刻在遗忘曲线上的保持率。开屏只读不写：
# search / brief_context 之类的检索会顺手「复习」记忆、改写曲线并存盘——
# 开屏要是调了它们，就会改掉它正在展示的东西。这里只用 retention / tier / curve_stats。
# ───────────────────────────────────────────────────────────────────────────────
_SKY_ROWS = 7
_SKY_CENTER = "✦ Polaris"
_SKY_GLYPH = {"vivid": ("✦", ("shine", "bold")), "steady": ("✧", ("accent",)), "fading": ("⋆", ("accent", "dim"))}
def _sky_hash(*parts: Any) -> bytes:
    # 不能用内置 hash()：字符串的哈希每个进程都加了随机盐，星星每次开屏都会换位置
    return hashlib.sha1(":".join(str(p) for p in parts).encode("utf-8")).digest()
def _sky_cells(items: list[dict], width: int, now: datetime.datetime, *, memory: "Memory | None" = None) -> list[list[tuple]]:
    """算出星空的格子：每格 (字, 样式, 第几帧出现)。位置全部由记忆编号的哈希决定，
    同样的宽度下每次开屏星星都在同一个地方——这是一片「你的」星空，不是随机噪点。"""
    memory = memory or MEMORY
    grid: list[list[tuple | None]] = [[None] * width for _ in range(_SKY_ROWS)]
    taken: list[list[bool]] = [[False] * width for _ in range(_SKY_ROWS)]
    def claim(row: int, col: int, text: str, styles: tuple, frame: int = 0) -> None:
        for ch in text:
            grid[row][col] = (ch, styles, frame)
            taken[row][col] = True
            col += 1
    def free(row: int, start: int, length: int, pad: int = 1) -> bool:
        if start - pad < 0 or start + length + pad > width:
            return False
        return not any(taken[row][c] for c in range(start - pad, start + length + pad))
    mid = _SKY_ROWS // 2
    center_col = (width - len(_SKY_CENTER)) // 2
    claim(mid, center_col, _SKY_CENTER, ("bold",))
    for c in range(max(0, center_col - 3), min(width, center_col + len(_SKY_CENTER) + 3)):
        taken[mid][c] = True
    # 休眠的记忆不画：它们是「已沉睡」那个数，画成暗点又和背景的星尘分不清
    stars = []
    for item in items:
        if item.get("dormant"):
            continue
        retention = memory.retention(item, now)
        stars.append((item, retention, Memory.tier_of(retention)))
    # 太多就只画最亮的 40 颗（统计数字照样全算）；按编号从小到大落位，新记忆不会把老星星挤走
    stars = sorted(sorted(stars, key=lambda s: (-s[1], s[0]["id"]))[:40], key=lambda s: s[0]["id"])
    placed = []
    for item, retention, tier in stars:
        h = _sky_hash("polaris-star", item["id"])
        row = h[0] % _SKY_ROWS
        col = 1 + int(int.from_bytes(h[1:3], "big") / 65536 * (width - 2))
        for attempt in range(_SKY_ROWS * 4):
            if free(row, col, 1):
                glyph, styles = _SKY_GLYPH[tier]
                claim(row, col, glyph, styles, frame=1 + h[3] % 5)
                placed.append((item, retention, tier, row, col))
                break
            col = (col + 5) % max(2, width - 2) + 1
            if attempt % 4 == 3:
                row = (row + 1) % _SKY_ROWS
    # 只给最亮的几颗配上文字，而且只给「清晰」的——快忘了的记忆不该被写得像记得很清楚
    for item, retention, tier, row, col in sorted(placed, key=lambda p: -p[1])[:4]:
        if tier != "vivid":
            continue
        label = _fit_display(_clean_line(item.get("content", "")), 14)
        cells = []
        for ch in label:
            cells.append(ch)
            if _display_width(ch) == 2:
                cells.append("")  # 中文占两格，第二格占位
        frame = grid[row][col][2]
        for start in (col + 2, col - 1 - len(cells)):
            if free(row, start, len(cells)):
                for i, ch in enumerate(cells):
                    grid[row][start + i] = (ch, ("glow",), frame)
                    taken[row][start + i] = True
                break
    # 背景星尘：只由坐标决定，和记忆无关
    for row in range(_SKY_ROWS):
        for col in range(1, width - 1):
            if grid[row][col] is None and free(row, col, 1) and _sky_hash("polaris-dust", row, col)[0] < 10:
                grid[row][col] = ("⋅", ("dim",), 0)
    return [[cell if cell is not None else (" ", (), 0) for cell in row] for row in grid]
def _sky_rows(cells: list[list[tuple]], upto_frame: int = 99, gutter: str = "  ") -> list[str]:
    rows = []
    for row in cells:
        segs = []
        for ch, styles, frame in row:
            if ch == "":
                continue
            segs.append((ch, styles) if frame <= upto_frame else (" " * _display_width(ch), ()))
        rows.append((gutter + _paint_segments(segs)).rstrip())
    return rows
def render_star_sky(items: list[dict], width: int, now: datetime.datetime, *, memory: "Memory | None" = None) -> list[str]:
    return _sky_rows(_sky_cells(items, width - 2, now, memory=memory))
def _twinkle_in(cells: list[list[tuple]], out: Any = None, interval: float = 0.08) -> None:
    """星星一颗颗亮起来，不到半秒。最后一帧和静态画出来的一字不差。按 Ctrl+C 直接跳到最后一帧。"""
    out = out or sys.stdout
    frames = max((cell[2] for row in cells for cell in row), default=0)
    try:
        out.write("\x1b[?25l" + "".join(line + "\n" for line in _sky_rows(cells, 0)))
        out.flush()
        try:
            for f in range(1, frames + 1):
                threading.Event().wait(interval)
                out.write(f"\x1b[{len(cells)}A" + "".join("\r" + line + "\x1b[K\n" for line in _sky_rows(cells, f)))
                out.flush()
        except KeyboardInterrupt:
            out.write(f"\x1b[{len(cells)}A" + "".join("\r" + line + "\x1b[K\n" for line in _sky_rows(cells)))
    finally:
        out.write("\x1b[?25h")
        out.flush()
def _greeting_period(hour: int) -> str:
    if 5 <= hour < 12:
        return "早上好"
    if 12 <= hour < 18:
        return "下午好"
    if 18 <= hour < 23:
        return "晚上好"
    return "夜深了"
def _last_session_greeting(records: list[dict], now: datetime.datetime, *, topic: bool = True) -> str:
    """问候：几点了 + 上次聊到哪儿。话题取上一次会话里用户说的第一句话——那句最能说明来意。
    （ExperienceModel 的关键词是按中文二元组切的，会切出「们在」「在聊」这种碎片，不能拿来当话题。）
    topic=False 时不提上次聊了什么：POLARIS_SKY=0 是给共享屏幕用的，话题和记忆片段一样不该上屏。"""
    period = _greeting_period(now.hour)
    if not records:
        return f"{period}，初次见面——我是 Polaris。"
    if not topic:
        return f"{period}，又见面了。"
    try:
        sid = str(records[-1].get("session_id") or "")
        session = [r for r in records if str(r.get("session_id") or "") == sid]
        asks = [_clean_line(r.get("content", "")) for r in session if r.get("role") == "user"]
        asks = [a for a in asks if a]
        if not asks:
            return f"{period}，又见面了。"
        last_time = _parse_dt(session[-1].get("time"), now)
        days = (now.date() - last_time.date()).days
        when = "今天早些时候" if days <= 0 else "昨天" if days == 1 else f"{days} 天前"
        # /init 的那句话是程序替用户说的。只有 AGENT.md 真的在，才说「一起初始化了」——
        # 那一轮可能因为连不上模型、被 Ctrl+C、或者处在 plan 模式而什么都没写成
        if asks[0] == _clean_line(INIT_PROMPT) and os.path.exists("AGENT.md"):
            return f"{period}。{when}我们一起初始化了 AGENT.md。"
        return f"{period}。上次我们在聊「{_fit_display(asks[0], 20)}」，{when}。"
    except Exception:
        return f"{period}，又见面了。"
def _mood_line(agent: Any, width: int, *, now: datetime.datetime | None = None, state: dict | None = None) -> str:
    """开屏的心情那一行。只说真的：心情在代码里实际改变的只有单回合步数预算（MoodState.turn_budget），
    外加塞给模型的几句提醒（MoodState.advice）。没有任何代码会因为心情去拆任务、派子代理，所以绝不这么说。"""
    now = now or datetime.datetime.now()
    s = {key: _mood_int(state if state is not None else MOOD.state, key) for key in _MOOD_BASELINE}
    # 预测第一回合真正用的状态：begin_turn 会先 rebalance(0.05)，阈值附近差一两点，步数就不一样了
    probe = MoodState.__new__(MoodState)
    probe.state = dict(s)
    probe.rebalance(0.05)
    p = probe.state
    baseline = MoodState.__new__(MoodState)
    baseline.state = dict(_MOOD_BASELINE)
    # 步数和真正跑循环时一样算：心情给的预算，再加上工作区、待办带来的风险修正
    max_steps = getattr(agent, "max_steps", 20)
    calm = min(max_steps, REASON.budget_for(baseline))
    cap = min(max_steps, REASON.budget_for(probe))
    bright = False
    if s["frustration"] >= 35:
        phrase = "上回不太顺，还有点惦记"
        if cap > calm:
            phrase += f"，这回合多留 {cap - calm} 步排查"
        if p["frustration"] >= 70:
            phrase += "，动手前会提醒自己先验证"
    elif s["fatigue"] >= 60:
        phrase = "有点累了"
        phrase += f"，单回合步数预算收到 {cap} 步（平时 {calm}）" if cap < calm else "，不过还撑得住"
        if p["fatigue"] >= 75:
            phrase += "，会提醒自己先压缩上下文"
    elif s["confidence"] <= 35:
        phrase = "心里没太有底"
        if cap > calm:
            phrase += f"，多留 {cap - calm} 步核对"
    elif s["confidence"] >= 72:
        phrase, bright = "最近挺顺手", True
    elif s["focus"] >= 62:
        phrase, bright = "歇够了，脑子很清楚", True
    else:
        phrase = "状态平稳"
    icon = "✧" if bright else "☀" if 6 <= now.hour < 18 else "☾"
    return paint("  " + icon, "accent") + " " + paint(_fit_display(phrase, max(8, width - 4)), "dim")
def _sky_summary(stats: dict, width: int = 100) -> str:
    if not stats.get("total"):
        return paint("  " + _fit_display("还没有记忆，聊着聊着星星就多了", width - 2), "dim")
    # 数字和 /memstat 同出一源（curve_stats），开屏和 /memstat 永远对得上
    parts = [(stats["vivid"], "颗亮着", "shine"), (stats.get("steady", 0), "颗微光", "accent"),
             (stats["fading"], "颗在变暗", "accent"), (stats["dormant"], "颗已沉睡", "dim")]
    shown = [paint(str(n), style) + paint(f" {word}", "dim") for i, (n, word, style) in enumerate(parts) if n or i == 0]
    line = paint("  记忆星空 · ", "dim") + paint(" · ", "dim").join(shown)
    # 放不下就从最次要的那段开始丢，宁可少说一项，也不让这一行折到下一行
    while _visible_width(line) > width and len(shown) > 1:
        shown.pop()
        line = paint("  记忆星空 · ", "dim") + paint(" · ", "dim").join(shown)
    return line
def _banner_command_grid() -> list[str]:
    out = []
    half = (len(BANNER_COMMANDS) + 1) // 2
    left, right = BANNER_COMMANDS[:half], BANNER_COMMANDS[half:]
    for i in range(half):
        row = "  "
        for cmd, desc in ((left[i],) + ((right[i],) if i < len(right) else ())):
            row += paint(_pad_display(cmd, 11), "accent") + _pad_display(desc, 17)
        out.append(row.rstrip())
    total = sum(len(items) for _, items in COMMAND_GROUPS)
    out += ["", paint(f"  输入 /help 查看全部 {total} 条命令", "dim")]
    return out
def render_startup(agent: Any, width: int, *, memory: "Memory | None" = None, archive: "ConversationArchive | None" = None,
                   now: datetime.datetime | None = None, tty: bool | None = None, sky: bool | None = None) -> tuple[list[list[tuple]] | None, list[str]]:
    """返回 (星空格子, 星空下面那些行)。星空格子为 None 表示走朴素开屏。
    分开返回是为了让 main() 能先让星星一颗颗亮起来，再把下面的字一次打出来。"""
    memory = memory or MEMORY
    archive = archive or ARCHIVE
    now = now or _now_dt()
    tty = _TTY if tty is None else tty
    sky = SHOW_SKY if sky is None else sky
    try:
        records = archive._read_all()
    except Exception:
        records = []
    greeting = "  " + _fit_display(_last_session_greeting(records, now, topic=sky), width - 2)
    mood = _mood_line(agent, width, now=now)
    if not tty or not sky or width < 60:
        # 不是终端（管道、CI）或者太窄：还是原来那个开屏，外加问候和心情。
        # 窄终端放不下 58 格宽的框和命令表，只留一行标题
        head = render_banner().splitlines() if width >= 60 or not tty else [paint("  ✦ Polaris", "accent", "bold") + paint(f" v{VERSION}", "dim")]
        return None, head + ["", greeting, mood, ""] + _startup_status(agent, width)
    sky_width = min(width, 72)
    cells = _sky_cells(list(memory.items), sky_width - 2, now, memory=memory)
    lines = ["", _sky_summary(memory.curve_stats(now), width), "", greeting, mood, ""]
    if records:
        hint = (paint("/help", "accent") + paint(" 全部命令 · ", "dim") + paint("/memstat", "accent")
                + paint(" 看哪些记忆在变暗 · ", "dim") + paint("exit", "accent") + paint(" 退出", "dim"))
        if _visible_width(hint) + 2 > width:
            hint = paint("/help", "accent") + paint(" 全部命令 · ", "dim") + paint("exit", "accent") + paint(" 退出", "dim")
        lines.append("  " + hint)
    else:
        lines += _banner_command_grid()  # 第一次来：把最常用的几条命令摆出来
    lines += _startup_status(agent, width, with_memory=False)
    return cells, lines
def main():
    if BACKEND_NAME in {"ollama", "lmstudio", "local"}:
        pass
    elif not API_KEY and not _looks_local_endpoint(BASE_URL):
        sys.exit("错误：未检测到环境变量 OPENAI_API_KEY；如果你在用本地 OpenAI-Compatible 服务，请先设置 OPENAI_BASE_URL 或 MINIAGENT_BACKEND=ollama/lmstudio。")
    if not evaluate_online_necessity():
        return
    WORKSPACE.refresh()
    agent = Agent(tools=BUILTIN_TOOLS)
    # 旧版在这里一口气打印 11 行内部状态，人格那行 150 格宽，窄终端上直接折成一团。
    # 现在压成两行摘要，完整信息本来就有 /state、/doctor、/persona 可以看。
    try:
        cells, lines = render_startup(agent, _term_width())
    except Exception:
        # 开屏只是门面：状态文件里有什么怪东西，也不能让程序在提示符出来之前就崩掉
        cells, lines = None, render_banner().splitlines() + [""] + _startup_status(agent, _term_width())
    if cells is not None:
        if _ANIMATE:
            _twinkle_in(cells)
        else:
            print("\n".join(_sky_rows(cells)))
    print("\n".join(lines))
    line_editing = _enable_line_editing()
    prompt = _input_prompt(line_editing)
    while True:
        try:
            # 提示符前的空行单独打：提示符里带换行的话，readline 重画这一行时会错位。
            print()
            user_input = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            print(paint("\n再见！", "dim"))
            break
        if not user_input:
            continue
        if _is_exit_word(user_input):
            print(paint("再见！", "dim"))
            break
        handled, next_input = handle_cli_command(agent, user_input)
        if handled:
            if next_input == "__exit__":
                break
            if next_input is None:
                continue
            user_input = next_input
        try:
            agent.chat(user_input)
        except KeyboardInterrupt:
            # 以前 Ctrl+C 会把整个程序带走，连同这次会话的上下文；
            # 现在只打断这一回合，会话还在。
            agent.interrupt()
            print(paint("\n  已打断这一轮。接着说就行。", "dim"))
        except Exception as e:
            agent.interrupt()
            print(paint(f"\n[运行异常] {e}", "red"))
def run_selftest_cli() -> int:
    """命令行方式跑自检，返回退出码：0 全过，1 有检查没过。

    给 CI 用，也给「我刚装好，这东西到底能不能跑」用——不需要 API key。
    自检会往当前目录写状态文件（心情 / 待办 / 快照），所以跑在临时目录里，
    免得弄脏用户的项目或者 CI 的检出目录。"""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="polaris_selftest_") as tmp:
        try:
            os.chdir(tmp)
            report = run_self_tests()
        finally:
            os.chdir(cwd)
    print(report)
    # run_self_tests 出大问题时会直接返回 traceback，那种情况也要算失败，
    # 所以判定条件是「必须明确出现通过标记」，而不是「没看到 FAIL」。
    return 0 if "ALL CHECKS PASSED" in report else 1
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in {"--selftest", "-t"}:
        sys.exit(run_selftest_cli())
    main()