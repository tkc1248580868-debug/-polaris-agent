# Polaris

**An AI that grows with you.**

[![selftest](https://github.com/tkc1248580868-debug/-polaris-agent/actions/workflows/selftest.yml/badge.svg)](https://github.com/tkc1248580868-debug/-polaris-agent/actions/workflows/selftest.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Dependencies](https://img.shields.io/badge/dependencies-1-brightgreen)
![Single file](https://img.shields.io/badge/single--file-4.7k%20lines-orange)
![Version](https://img.shields.io/badge/version-1.1.6-lightgrey)

An open-source autonomous AI agent with a persistent personality, long-term
memory that actually forgets, and a visible reasoning trail — in **one Python
file with one dependency**.

Most AI agents are built to complete tasks. Polaris is built to **remember,
reflect, learn, and grow**. Rather than behaving like a stateless chatbot, it
maintains an evolving internal state that influences its planning, reasoning,
and communication.

Runs against any OpenAI-compatible endpoint — hosted or local — and keeps its
entire state in plain JSON files next to your project.

---

## Quick start

```bash
git clone https://github.com/tkc1248580868-debug/-polaris-agent.git
cd ./-polaris-agent
pip install -r requirements.txt

export OPENAI_API_KEY=sk-...
python polaris_1_1_6_memorycurve.py
```

Prefer a local model? No API key needed:

```bash
export MINIAGENT_BACKEND=ollama        # or: lmstudio
export MINIAGENT_MODEL=qwen2.5:14b
python polaris_1_1_6_memorycurve.py
```

Verify the install without spending a single token:

```bash
python polaris_1_1_6_memorycurve.py --selftest    # exits non-zero if anything fails
```

or `/selftest` from inside a session.

Twenty-four checks covering the sandbox, the shell guard, file-edit safety,
context assembly, the trace tree, and the memory curve. No API key required.

**Requirements:** Python 3.10+, and an OpenAI-compatible endpoint.
Works on Windows, Linux, and macOS. The only dependency is `openai` — every
other import is from the standard library.

Tested against OpenAI, Gemini, Claude, Ollama, and LM Studio.

---

## What makes it different

### Memory on a forgetting curve

Most agents' "long-term memory" is an append-only log that grows until it is
useless. Polaris puts every memory on an **Ebbinghaus forgetting curve**:

```
R = exp(-t / S)
```

Each memory decays on its own schedule, and every time it is recalled it decays
more slowly — with a real spacing effect, so cramming does not work here either.
Memories that stop mattering fade out of the prompt on their own; memories that
keep coming up become permanent. Nothing is ever deleted behind your back —
faded entries go dormant and a specific enough cue still brings them back.

Retrieval is hybrid: cosine similarity over embeddings, blended with keyword
overlap, weighted by how well the memory is currently retained. Bring your own
embedding model, or use the built-in pure-Python fallback so it keeps working
with no API key and no network.

### An inner life that is wired into control flow

Mood is not decoration. High fatigue shrinks the step budget; high frustration
triggers delegation to sub-agents. Persona, relationship, and mood shape the
prompt on every turn — and all three persist across sessions.

### A visible reasoning trail

Every turn writes a structured execution trace you can actually read:
`/trace` for the timeline, `/trace json` for the whole tree.

### One file, one dependency

4,700 lines of Python, no framework, no vector database, no build step. Copy it
anywhere and run it.

---

## Core features

| Engine | What it does |
|---|---|
| **Persona Engine** | Persistent identity, speaking style, core values, adaptive personality |
| **Mood Engine** | Six dimensions — confidence, focus, curiosity, fatigue, frustration, stability — that feed back into planning |
| **Memory Engine** | Vector recall on an Ebbinghaus forgetting curve; reinforcement, dormancy, pinning |
| **Relationship State** | Trust, familiarity, warmth, humor — evolving with every interaction |
| **Thought Engine** | Internal monologue before acting (template / LLM / hybrid) |
| **Workspace Awareness** | File index, AST import graph, dependency health, git status |
| **Trace Engine** | Structured, inspectable execution trace per turn |
| **Reason Engine** | Risk assessment, step budgeting, delegation strategy |
| **Productivity Engine** | Goal decomposition, progress journal, statistics |
| **Experience Model** | Cross-session topics, continuity, shared timeline |
| **Multi-Agent** | Task decomposition and parallel sub-agents |
| **MCP + Plugins** | Model Context Protocol over stdio; drop-in `plugins/*.py` |
| **Safety** | Python sandbox, shell guard, file checkpoints, permission modes |

35 built-in tools. 43 slash commands.

---

## Commands

A few of the ones worth knowing on day one:

| Command | Description |
|---|---|
| `/mode plan\|ask\|auto` | Permission mode — how much it may do without asking |
| `/memory` · `/memstat` | Long-term memory, and its forgetting curve |
| `/recall <query>` | Semantic recall with per-hit scores |
| `/pin <id>` · `/revive <id>` | Never forget this; wake a dormant memory |
| `/trace [n\|json]` | What it actually did last turn |
| `/mood` · `/persona` · `/relationship` | Its inner state |
| `/plan <goal>` · `/todo` | Decompose a goal; track progress |
| `/snapshot` · `/restore <id>` | Save and roll back runtime state |
| `/undo` | Roll back the last file edit |
| `/selftest` · `/doctor` | Verify the install; diagnose configuration |

Full reference, architecture notes, and every environment variable:
**[README_Polaris_1_1_6.md](README_Polaris_1_1_6.md)**

---

## The Polaris Constitution

Every decision made by Polaris is guided by five core principles.

**Truth before fluency.** Never pretend certainty. If Polaris is unsure, it says so.

**Verify before acting.** Observe first. Verify assumptions. Then act.

**Preserve user intent.** Always optimize for what the user actually wants.

**Learn, but never assume.** Adapt gradually without drawing unsupported conclusions.

**Grow through experience.** Every interaction should improve future performance.

---

## Safety

Polaris can write files and run shell commands. It ships with a Python sandbox,
a destructive-command guard, file checkpoints with `/undo`, and three permission
modes — but none of that makes it safe to point at anything you cannot afford to
lose.

- Review generated code before running it.
- Apply the principle of least privilege.
- Run it in a container or under a dedicated low-privilege account for real isolation.
- Do not use it in safety-critical, medical, legal, or financial settings without
  independent verification.
- Keep backups before letting any AI system modify your files.

The shell guard stops *accidents in auto mode*, not a determined attacker.

---

## Roadmap

**Shipped** — Persona Engine · Mood Engine · Long-Term Memory · Vector memory on
an Ebbinghaus forgetting curve · Reflection · Workspace Awareness · MCP Support ·
Plugin System · Productivity Engine · Thought Engine · Experience Model ·
Relationship State · Reason Engine · Trace Engine · Context Providers ·
Snapshot Manager

**Next** — Internationalization · Persona Engine v2 · Workflow graph · Web UI ·
Voice interaction

---

## Project status

Active development. Features, APIs, and internal architecture may change between
releases.

Contributions of all kinds are welcome — bug reports, documentation, ideas, or
code. Open an issue or a pull request.

---

## Disclaimer

Polaris is an experimental open-source AI agent intended for research, learning,
and software development. Despite its safety mechanisms it can still generate
inaccurate information, produce incorrect code, or perform unintended actions.
You are responsible for reviewing everything it generates before applying it
anywhere that matters. The maintainers are not liable for any direct or indirect
damages resulting from its use.

---

## License

[MIT](LICENSE)

---

## Vision

The future of AI is not simply about answering questions. It is about building
systems that can remember, reflect, learn, and develop a consistent identity.

Polaris isn't trying to become the biggest AI agent. It's trying to become one of
the most trustworthy ones.

*Technology should make AI more capable. Character should make AI more trustworthy.*
