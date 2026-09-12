# Polaris v1.1.6 — "Memory Curve"

An AI that grows with you.

Polaris is a single-file autonomous agent with a persistent personality, long-term
memory, and a visible reasoning trail. It runs against any OpenAI-compatible
endpoint — hosted or local — and keeps its entire state in plain JSON files next
to your project.

**Remember. Reflect. Learn. Grow.**

---

## What v1.1.6 adds

One thing, and it replaces the oldest part of Polaris: long-term memory.

### Memory: vectors and the forgetting curve

Long-term memory used to be an append-only log ranked by keyword overlap plus a
recency bonus. It had no way to tell a hard-won lesson from a throwaway note, and
nothing ever left the list — so the memory section of the prompt slowly filled up
with whatever happened to be written most recently.

Memory is now a **vector store on an Ebbinghaus forgetting curve**. Every entry
decays on its own schedule, and every successful recall makes it decay slower.

#### The curve

```
R = exp(-t / S)
```

`R` is the probability the memory is still available right now, `t` is days since
it was last recalled, and `S` is its strength in days. Half-life is `S · ln 2`.

Each recall counts as a review, and strength grows by the **spacing effect**:

```
S ← S · (1 + 1.8 · quality · (1 − R))
```

Recall something you just recalled (`R ≈ 1`) and it barely grows — cramming does
not work here either. Recall something you were about to lose (`R` low) and it
grows the most. Passive recall (a memory auto-injected into the prompt) counts at
half quality; an explicit `recall_memories` call counts in full.

New memories start at a strength set by their category, because not all memories
deserve the same lifespan:

| Category | Initial strength | Half-life |
|---|---|---|
| `identity` — who the user is | 30 d | ~21 d |
| `preference` — long-standing preferences | 14 d | ~10 d |
| `lesson` — a trap already stepped in | 5 d | ~3.5 d |
| `fact` — everything else | 1.5 d | ~1 d |

Those numbers are deliberately short. A memory that genuinely matters gets
recalled, and recall is what makes it permanent — a fact recalled four or five
times is already good for months.

#### Retrieval

Search is hybrid, and retention is a weight rather than a filter:

```
base  = 0.62 · cosine(query, memory) + 0.38 · keyword_overlap
score = base · (0.35 + 0.65 · R)
```

Being hard to recall is not the same as being unreachable, so a fading memory
that matches well still beats a vivid one that matches poorly.

#### Forgetting, and how to undo it

A memory at least three days old that has fallen below 5% retention goes
**dormant** (both thresholds are configurable).
Dormant memories drop out of the auto-injected context and out of `/memory` —
they are **never deleted**. Three ways back:

- A **specific cue** pulls one back on its own (cued recall): a query that matches
  it precisely still retrieves it, and doing so revives it. A vague query does not
  — otherwise nothing would ever be forgotten.
- `/recall <query>` and `recall_memories` can search dormant entries explicitly.
- `/revive <id>` wakes one; `/pin <id>` makes it permanent and exempt from decay.

`/memstat` shows the whole curve: what is vivid, what is fading, what is asleep.

#### Embeddings

Two backends, chosen automatically:

| | `remote` | `local` (fallback) |
|---|---|---|
| Source | any OpenAI-compatible `/embeddings` endpoint | pure-Python hashed bag-of-features |
| Semantics | real — paraphrases match | **lexical only** — shares tokens and character bigrams |
| Cost | one batched call per new memory set | none |
| Persisted | yes, to `agent_memory_vectors.json` | no — recomputed on demand |

The local embedder is honest about what it is: a 1024-dimensional signed hash of
tokens and character bigrams with sublinear term weighting. It will not connect
"canine" to "dog". It exists so that Polaris keeps working with no API key, no
network, and no dependencies, and so that a remote outage degrades quietly
instead of taking memory down with it. If you want real semantic recall, point
`POLARIS_EMBED_MODEL` at an embedding model.

1024 dimensions is measured, not guessed: at 256, hash collisions crushed the
cosine of a related Chinese sentence pair from 0.124 to 0.051 and manufactured a
−0.065 signal between unrelated ones. At 1024 both converge on their
collision-free values. Local vectors are not persisted because recomputing one
costs well under a millisecond — far less than the megabytes a dense sidecar
would cost. Remote vectors are persisted, because those cost money.

#### Upgrading

Old `agent_memory.json` files load unchanged. Entries with no curve data are
treated as reviewed *at upgrade time* rather than at their original write date —
otherwise every memory older than a few days would go dormant the moment you
upgraded, and it would look like Polaris had wiped your history.

---

## What v1.1.5 added

v1.1.0 gave Polaris an inner life. v1.1.5 made that inner life **observable and
auditable** — and fixed several places where the internal state was working
against the agent instead of for it. It ships here as part of v1.1.6.

### Trace Engine

Every turn now writes a structured execution trace: a tree of `TraceStep` nodes
carrying status, duration, metadata, and nested events. Two rendering levels:

```
/trace          summary timeline    ✓ 调用模型 gpt-4o — 2 tool calls
/trace 30       deeper summary
/trace json     full serialized tree
```

The call stack is **thread-local by design**. `delegate_tasks` runs sub-agents in
parallel, and a shared stack would let concurrent workers pop each other's steps
and corrupt the tree.

### Context Providers

The system prompt used to be one hardcoded blob. It is now assembled from ten
independent providers — persona, relationship, mood, inner voice, decision,
productivity, workspace, memory, experience, todo — each with its own
`priority`, `max_chars`, and `fingerprint()`. The prompt is rebuilt only when a
fingerprint actually changes.

Budget allocation runs in two passes. The old "first come, first served" approach
let the personality layers consume `remaining // 2` each, and the providers at the
back of the queue — memory, experience, todo — routinely got **zero characters**.
Those are precisely the factual providers most useful for finishing a task. The
current allocator gives every provider a floor first, then redistributes the
remainder by priority. A self-test (`context_no_starvation`) guards the invariant.

### Snapshot Manager

`/snapshot`, `/snapshots`, `/restore` capture and roll back the whole runtime
state — mood, relationship, todos, plan, archive position — into
`.polaris_snapshots/`. This is separate from `/undo`, which rolls back a single
file edit.

### Self-tests

`/selftest` runs a twenty-five-check suite covering JSON parsing, context building,
snapshot round-trip, calculator sandbox escapes, shell guard rules, context
starvation, step-budget direction, trace wiring, sub-agent isolation, tool
description coverage, and three memory checks — curve direction and the spacing
effect, vector recall with cross-process determinism, and forgetting behaviour
(dormancy without deletion, cued recall, legacy migration), the MCP permission
gate, the embedding timeout, file paging, shell exit-code reporting, streaming
tool-call assembly, whether the chain of thought is actually causal, and the
file-editing safety rules. No API key required.

It runs in CI too. `python polaris_1_1_6_memorycurve.py --selftest` exits non-zero
if any check fails, and GitHub Actions runs it on every push across Python
3.10–3.13 — plus one job with **no dependencies installed at all**, which exists to
keep the "one dependency" claim honest rather than aspirational.

The `file_edit_safety` check was added when the CI was being set up, for a reason
worth recording: deleting `edit_file`'s uniqueness guard — the single most
dangerous regression in the tool set, since it silently rewrites the *first*
match instead of the intended one — left the suite entirely green. A CI that
cannot fail is worse than no CI, so the check that catches it went in before the
workflow did.

### Step budget: direction reversed

Earlier versions cut the step budget when frustration rose and confidence fell,
bottoming out at 4 steps. That pushed debugging into a death spiral — *more
failures → fewer steps → less able to fix it → more failures* — tying the agent's
hands exactly when it needed room to investigate.

Now only `fatigue` (a proxy for context bloat) shrinks the budget, with a floor of
12. Failure signals **raise** it. `ReasonEngine` follows the same rule: high risk
means a more careful strategy, not a smaller budget.

### Hardening

- **Calculator** no longer uses `eval`. `eval(expr, {"__builtins__": {}}, allowed)`
  looks safe but does not stop attribute-chain traversal — from any literal you can
  walk `__class__` up the type tree and recover what was stripped. `_calc_eval`
  walks the AST directly and has no `ast.Attribute` branch at all, so the chain
  cannot form. Exponent bombs like `9**9**9` are rejected too.
- **Python sandbox** import allowlist actually works now. `SAFE_BUILTINS` was
  missing `__import__`, so every whitelisted module failed to import — the
  allowlist was decorative.
- **Shell guard** blocks eight destructive patterns (`rm -rf`, `mkfs`, disk
  overwrite, fork bombs, `curl | sh`, …). This stops *accidents in auto mode*, not
  a determined attacker — real isolation means running Polaris in a container or
  under a dedicated low-privilege account.
- **Sub-agents** no longer write into the main conversation archive or trace tree.
- **The task statement is never trimmed away.** Trimming drops the oldest
  messages first, and the oldest message is the one that says what the job *is*.
  Measured on a 61-message session: "refactor login to use JWT, leave the database
  alone" was gone, while twenty-odd "ok, step 16" replies survived. The agent kept
  working without knowing what it was working on. The first user message is now
  kept regardless of age — it costs nothing extra, since the same number of
  messages is dropped either way.
- **History is trimmed by token budget, not just message count.** Counting
  messages was survivable while every tool result was capped at 8,000 characters.
  Once `read_file` could return 24,000, forty such messages came to roughly
  240,000 tokens — past the window of most models, and a hard API error rather
  than a graceful degradation. Both caps now apply, whichever binds first, and the
  tool-call/result pairing repair still runs afterwards.
- **`read_file` can read a whole file.** It used to cut off at 8,000 characters
  with no way to continue — the agent went blind after ~175 lines and had no way
  to know it. It now takes `offset` / `limit`, returns numbered lines that line up
  with `search_files`, and says how many lines remain.
- **`run_shell` always reports the exit code**, with stdout and stderr labelled
  separately. It used to include the exit code only when there was *no* output,
  which threw away the one signal a coding agent needs most: did the tests pass?
  A non-zero exit now also registers as a failure in the trace, the mood system,
  and self-reflection.
- **Streaming tool-call assembly** survives providers that send the id in a later
  chunk or omit `function` from the first one. Both used to break — a crash in one
  case, a null `tool_call_id` in the other — and both are more common on
  OpenAI-compatible servers than on OpenAI itself.
- **MCP tools go through the same permission gate as local tools.** They used to
  be dispatched before the `plan` / `ask` checks ran, so an external server could
  write files in supposedly read-only mode and never prompt. Tools from an MCP
  server are now treated as dangerous *and* mutating by default; a server may
  declare `readOnlyHint` / `destructiveHint` to relax that, but those are hints
  for noise reduction, not a security boundary. `/tools` lists MCP tools and
  their flags too — previously the model could see and call them while the user
  could not.

---

## Thinking, for real

Polaris used to print an "inner monologue" before each turn. It was theatre: a
separately generated block of text that was printed and then **thrown away**. It
never entered the message history, so it could not influence a single downstream
decision — and in `hybrid` mode it cost a whole extra API call to produce.

It is now an actual chain of thought.

Before answering or calling a tool, the model writes its reasoning in a
`<thinking>` block: what it knows, what it is missing, why this step, what could
go wrong. That block **stays in the conversation**, so every later step is built
on top of it. It is shown to you on its own channel, and stripped from the final
answer.

Where the endpoint exposes native reasoning tokens (`reasoning_content` — DeepSeek-R1,
QwQ, vLLM and friends), **that** is the chain of thought: the model's own reasoning
is used directly, folded into the message history exactly like a `<thinking>` block,
at no extra cost. Polaris also stops asking for `<thinking>` once it notices the
model reasons natively — no point teaching a model to do what it already does, and
paying twice in tokens to hear it said twice.

The reasoning is folded in as ordinary message *content*. `reasoning_content` is
never sent back as a request field, because providers such as DeepSeek reject that
outright. If your provider would rather not see its own prior reasoning, set
`POLARIS_COT_FEEDBACK=0` — the thinking is still displayed, but it stops being
causal, which is the whole point, so only do this if a provider forces you to.

The difference is causal, not cosmetic, and `/selftest` checks exactly that: the
reasoning must appear in the message history and must not appear in the answer.

The old behaviour is still available — `POLARIS_MONOLOGUE_MODE=template` for the
offline flavour text, `llm` or `hybrid` for the pre-1.1.6 extra-call version.

---

## Token cost

The per-step overhead is dominated by one thing, and it is not the personality:

| Component | Estimated tokens | Resent every step |
|---|---:|---|
| 35 tool schemas | ~3,560 | yes |
| System prompt + CoT instruction | ~610 | yes |
| Runtime context (10 providers) | ~180 | yes |

**82% of it is the tool definitions.** Trimming the persona or the memory
injection saves almost nothing; trimming the toolset saves most of the bill. It
also improves tool selection — the model is not picking from 35 options when six
are relevant.

`/lean` switches profiles, `/cost` shows the current bill:

| Profile | Tools | Tokens | vs `full` |
|---|---:|---:|---:|
| `full` (default) | 35 | ~3,560 | — |
| `code` | 11 | ~1,420 | −60% |
| `read` | 5 | ~630 | −82% |
| `min` | 4 | ~620 | −83% |

On a ten-step task, `/lean code` alone saves roughly 21,000 tokens. Profiles
filter **built-in** tools only — plugin and MCP tools were added deliberately, so
they are always exposed.

The other lever is `/reflect`: with self-check on, every turn that used a tool
spends one extra request carrying the entire history.

### Keeping the cache prefix stable

Providers discount tokens they have already seen, as long as the *prefix* of the
prompt is unchanged. Polaris used to put mood, memory, workspace state and the
todo list inside the system message — the very first thing in the prompt. Any one
of those changing invalidated everything after it, which is the entire
conversation.

Volatile state now goes in a message at the **end** of the request instead, so
tools, persona and the whole transcript form a prefix that only ever grows. It is
assembled per request and never written back into the history — persisting it
would make it part of the transcript and break the prefix all over again.

Measured over a ten-turn session, comparing the shareable prefix between
consecutive requests:

| | old layout | `tail` (default) |
|---|---:|---:|
| Nothing in the context changes | 92% | 72% |
| A real coding session — todos updated, lessons written, mood drifting | **0%** | **70%** |

It is a genuine trade-off, not a free win: with a completely static context the
old layout cached more, because the volatile block sat inside the already-cached
system message. But the moment anything changed it fell to zero, and during real
work something changes every turn. The `tail` layout gives up the best case to
never hit the worst one. `POLARIS_CONTEXT_POSITION=system` restores the old
behaviour.

---

## Quick start

```bash
pip install openai

export OPENAI_API_KEY=sk-...
python polaris_1_1_6_memorycurve.py
```

Local models — no API key needed:

```bash
export MINIAGENT_BACKEND=ollama        # or: lmstudio
export MINIAGENT_MODEL=qwen2.5:14b
python polaris_1_1_6_memorycurve.py
```

Verify the install without spending a token:

```
你 > /selftest
```

**Requirements:** Python 3.10+, an OpenAI-compatible endpoint, Windows / Linux / macOS.
The only third-party dependency is `openai`.

---

## Permission modes

| Mode | Behavior |
|---|---|
| `plan` | All write operations are blocked. Read and analyze only. |
| `ask` | Every mutating or dangerous tool call requires typed confirmation. **Default.** |
| `auto` | Runs freely. Use only in a sandbox or on a throwaway checkout. |

Switch at any time with `/mode plan|ask|auto`.

Every file write is checkpointed first — `/undo` restores the previous version.

---

## Command reference

**State and introspection**

| Command | Description |
|---|---|
| `/state` | Everything at once: mood, decision engine, world model, shared history |
| `/mood` · `/diary` | Emotional state; the mood journal |
| `/persona` · `/relationship` | Personality profile; trust / familiarity / warmth / humor |
| `/reason` | Decision engine: risk score and recommendation |
| `/trace [n\|json]` | Execution trace for the current turn |
| `/thought [on\|off\|preview]` | Inner monologue display |
| `/reflection [on\|off\|preview]` | Mood-synced reflection line |
| `/doctor` · `/stats` | System diagnostics; productivity summary |
| `/selftest` | Run the built-in check suite |

**Workspace**

| Command | Description |
|---|---|
| `/workspace` · `/refreshws` | Project world model; force a rescan |
| `/insight` | Dependency health and coupling hotspots |
| `/searchws <query>` | Search indexed files |

**Memory and history**

| Command | Description |
|---|---|
| `/memory [n]` · `/remember [-c category] <text>` · `/forget <id>` | Long-term memory, with each entry's retention and half-life |
| `/searchmem <query>` | Search memory |
| `/recall <query> [n]` | Vector recall with per-hit semantic / keyword / retention scores |
| `/memstat` | Forgetting-curve report: vivid, fading, dormant |
| `/pin <id>` · `/unpin <id>` · `/revive <id>` | Exempt from decay; resume decay; wake a dormant memory |
| `/history` · `/searchchat` · `/searchlast` | Conversation archive across windows |
| `/experience` · `/timeline` | Shared history and session timeline |

**Work**

| Command | Description |
|---|---|
| `/plan <goal>` | Decompose a goal into steps |
| `/todo` · `/journal` | Task list; growth log |
| `/snapshot` · `/snapshots` · `/restore <id>` | Runtime state snapshots |
| `/undo` | Roll back the last file edit |
| `/mode` · `/reset` · `/reflect` | Permission mode; clear session; toggle self-check |
| `/tools` · `/plugins` · `/init` | Tool list; loaded plugins; generate `AGENT.md` |
| `/help` | Every command, grouped by what it is for |
| `/lean [profile]` · `/cost` | Shrink the exposed toolset; show the per-step token bill |

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | API key. Optional for local endpoints. |
| `OPENAI_BASE_URL` | — | Custom endpoint. |
| `MINIAGENT_BACKEND` | `openai_compatible` | `openai` · `ollama` · `lmstudio` · `local` |
| `MINIAGENT_MODEL` | `gpt-4o` | Model for gate, main agent, and sub-agents alike. |
| `MINIAGENT_SKIP_GATE` | `false` | Skip the startup readiness check. |
| `MINIAGENT_SHOW_THOUGHT` | `true` | Print the inner monologue. |
| `MINIAGENT_THOUGHT_STYLE` | `balanced` | Thinking style. |
| `POLARIS_MONOLOGUE_MODE` | `cot` | `cot` (real chain of thought) · `template` (offline flavour, no API call) · `llm` / `hybrid` (legacy: a separate call whose output never reaches the answer) |
| `POLARIS_MONOLOGUE_MODEL` | (main model) | Separate model for the monologue. |
| `POLARIS_MONOLOGUE_MAX_TOKENS` | `180` | Monologue length cap (legacy modes only). |
| `POLARIS_COT_FEEDBACK` | `true` | Keep the model's reasoning in the message history so it informs later steps. Turning it off makes the chain of thought decorative again. |
| `POLARIS_TOOL_PROFILE` | `full` | `full` · `code` · `min` · `read`. Which built-in tools are exposed to the model — the single biggest lever on token cost. |
| `POLARIS_CONTEXT_POSITION` | `tail` | Where volatile runtime state goes. `tail` keeps the cache prefix stable; `system` is the pre-1.1.6 layout. |
| `POLARIS_MAX_CONTEXT_TOKENS` | `32000` | Token budget for the conversation history. Lower it for small local models. |
| `POLARIS_COLOR` | auto | `1`/`0` to force colour on or off. Off automatically when not a TTY. `NO_COLOR` is honoured. |
| `POLARIS_SHELL_ALLOW_DANGEROUS` | `false` | Lift the destructive-command block. |
| `MINIAGENT_PLUGIN_DIRS` | `plugins` | Comma-separated plugin directories. |
| `POLARIS_EMBED_BACKEND` | `auto` | `auto` · `remote` · `local`. `auto` uses the remote model when an endpoint is configured, else the local hash embedder. |
| `POLARIS_EMBED_MODEL` | `text-embedding-3-small` | Embedding model for the remote backend. |
| `POLARIS_EMBED_DIM` | `1024` | Local hash embedder dimensions. Lower is cheaper and less accurate. |
| `POLARIS_EMBED_BATCH_CAP` | `256` | Max memories embedded per batch — caps the cost of the first search after an import. |
| `POLARIS_EMBED_TIMEOUT` | `30` | Seconds before an embedding request is abandoned and the local embedder takes over. `0` uses the SDK default (600s). |
| `POLARIS_MEMORY_FORGET_THRESHOLD` | `0.05` | Retention below which a memory goes dormant. `0` disables forgetting. |
| `POLARIS_MEMORY_DORMANT_MIN_DAYS` | `3` | Minimum age before a memory may go dormant. |

State file locations are configurable via `MINIAGENT_MEMORY_FILE`,
`MINIAGENT_MEMORY_VECTOR_FILE`,
`MINIAGENT_MOOD_FILE`, `MINIAGENT_PERSONA_FILE`, `MINIAGENT_RELATIONSHIP_FILE`,
`MINIAGENT_CONVERSATION_FILE`, `MINIAGENT_TODO_FILE`,
`MINIAGENT_CHECKPOINT_DIR`, and `POLARIS_SNAPSHOT_DIR`.

> The `MINIAGENT_*` prefix is a leftover from the project's original name. It is
> kept as-is so existing configurations and checkpoint directories keep working.

### Runtime files

Polaris writes its state into the **current working directory**:

```
agent_memory.json         long-term memory + forgetting curve
agent_memory_vectors.json memory embeddings (remote embedder only)
agent_mood.json           emotional state + mood journal
agent_persona.json        personality profile
agent_relationship.json   relationship with you
agent_conversations.jsonl cross-window conversation archive
agent_todos.json          task list
.miniagent_checkpoints/   pre-edit file backups (/undo)
.polaris_snapshots/       runtime state snapshots (/restore)
```

These are personal to one working session and are excluded by `.gitignore`.
Do not commit them — they contain your conversation history.

### Project instructions

Polaris reads `AGENT.md` from the current directory and `~/.miniagent/AGENT.md`,
injecting both into the system prompt. `/init` generates a starter file.

---

## Architecture

```
main()  ── CLI loop, 43 slash commands
  │
  └── Agent.chat()
        ├── MoodState.begin_turn()        emotional state entering the turn
        ├── ThoughtEngine                 inner monologue (template / LLM / hybrid)
        ├── ContextBuilder.build()        10 providers → budgeted system prompt
        ├── run loop                      steps = min(max_steps, ReasonEngine.turn_budget())
        │     └── tool calls → TraceEngine records each step
        ├── self-reflection               JSON verdict; self-heal on failure,
        │                                 distill lessons into long-term memory
        └── RelationshipState.observe()   update trust / familiarity / warmth / humor
```

**Persistent state**

| Component | Storage | Role |
|---|---|---|
| `MoodState` | `agent_mood.json` | Six dimensions — confidence, focus, fatigue, curiosity, frustration, stability — with damped adjustment, daily decay, and baseline pull |
| `PersonaProfile` | `agent_persona.json` | Values, habits, speaking style |
| `RelationshipState` | `agent_relationship.json` | Trust, familiarity, warmth, humor |
| `Memory` | `agent_memory.json` | Long-term memory on an Ebbinghaus forgetting curve — strength, last recall, review count, pin and dormant flags |
| `VectorStore` | `agent_memory_vectors.json` | Memory embeddings, keyed by embedder signature; remote vectors only |
| `ConversationArchive` | `agent_conversations.jsonl` | Cross-window dialogue archive |
| `WorkspaceModel` | in-memory | File index, AST import graph, git status |
| `TraceEngine` | in-memory | Execution trace tree |

Mood is not decoration — it is wired into control flow. High fatigue shrinks the
step budget; high frustration triggers delegation to sub-agents.

**Retrieval** is hand-rolled and dependency-free. The keyword channel scores token
overlap ×3 + character bigram overlap ×2 + phrase hit +10 + alias-group bonus +6,
with CJK segmented by character and bigram. Memory search blends that with cosine
similarity over embeddings and weights the result by the entry's current retention
(see *Memory: vectors and the forgetting curve*). Still no vector database — the
index is a dict of unit vectors and a dot product, which is the right shape for a
store that holds hundreds of memories, not millions.

**Extensibility**: MCP servers over stdio, plus a plugin system that loads any
`plugins/*.py` exposing `register(agent)`, `TOOLS`, or `TOOL`.

---

## Safety

Polaris ships 35 built-in tools, including file writes and shell execution.

- Review generated code before running it.
- Prefer `plan` or `ask` mode. Reserve `auto` for sandboxes.
- Apply least privilege — run Polaris in a container or under a dedicated
  low-privilege account, not as your primary user.
- The shell blocklist stops accidents, not adversaries. It is trivially bypassed
  by anyone deliberately trying.
- Do not use Polaris in safety-critical, medical, legal, or financial contexts
  without independent verification.
- Keep backups before letting any AI system modify your files.

---

## The Polaris Constitution

**Truth before fluency.** Never pretend certainty. Communicate uncertainty clearly.

**Verify before acting.** Observe first. Verify assumptions. Then act.

**Preserve user intent.** Optimize for what the user actually wants.

**Learn, but never assume.** Adapt gradually without drawing unsupported conclusions.

**Grow through experience.** Every interaction should improve future performance.

---

## Roadmap

**Shipped in 1.x** — Persona Engine · Mood Engine · Long-Term Memory · Reflection ·
Workspace Awareness · MCP Support · Plugin System · Productivity Engine ·
Thought Engine · Experience Model · Relationship State · Reason Engine ·
Trace Engine · Context Providers · Snapshot Manager · Vector memory on an
Ebbinghaus forgetting curve

**Next** — Internationalization · Persona Engine v2 ·
Workflow graph · Web UI · Voice interaction

---

## Project status

Active development. Features, APIs, and internal architecture may change between
releases.

Polaris is an experimental open-source agent for research, learning, and software
development. Despite its safety mechanisms it can still produce inaccurate
information, wrong code, or unintended actions. You are responsible for reviewing
everything it generates before applying it anywhere that matters. The maintainers
are not liable for damages resulting from its use.

Bug reports, feature requests, and pull requests are all welcome.

---

## License

MIT.

---

*The future of AI is not simply about answering questions. It is about building
systems that remember, reflect, learn, and develop a consistent identity.*

*Polaris isn't trying to be the biggest agent. It's trying to be one of the most
trustworthy.*
