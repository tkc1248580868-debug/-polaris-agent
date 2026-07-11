Polaris 1.0.0 Awakening | MiniAgent v5.3

A personality-driven autonomous AI agent with long-term memory, emotional states, workspace awareness, and cross-session continuity.

Polaris is the flagship persona of MiniAgent v5.3 - an AI companion that thinks, feels, remembers, and evolves alongside you.

Features
- Independent persona system with values and speech style
- Long-term memory (add, search, forget)
- Realistic emotional engine (confidence, focus, fatigue, frustration, etc.)
- Dynamic workspace model (files, dependencies, recent changes)
- Cross-session memory and relationship tracking
- Intelligent reasoning engine
- Rich toolset: file operations, Python sandbox, shell commands, parallel sub-agents
- MCP external tool support
- Optional internal thought display
- Extensive CLI commands

Installation Guide

1. Prerequisites
   - Python 3.10 or higher
   - pip install openai

2. Setup
   Place polaris_1_0_0_awakening.py in your project root directory.

3. Running the Agent

   With OpenAI:
   OPENAI_API_KEY=sk-your-key-here python polaris_1_0_0_awakening.py

   With Ollama:
   MINIAGENT_BACKEND=ollama MINIAGENT_MODEL=deepseek-r1:32b python polaris_1_0_0_awakening.py

   With LM Studio:
   MINIAGENT_BACKEND=lmstudio OPENAI_BASE_URL=http://127.0.0.1:1234/v1 python polaris_1_0_0_awakening.py

Environment Variables
- OPENAI_API_KEY: Your API key
- OPENAI_BASE_URL: Custom base URL for local models
- MINIAGENT_MODEL: Model name (default: gpt-4o)
- MINIAGENT_BACKEND: Backend type (openai, ollama, lmstudio)

Quick Start
1. Run the script in your project folder
2. Type /init to generate AGENT.md
3. Type /state to view system overview
4. Chat naturally or assign tasks

Common Commands (start with /)
- /state          Full system status (recommended)
- /mood           Emotional state
- /workspace      Project world model
- /experience     Shared history
- /memory         Long-term memories
- /undo           Rollback last file change
- /init           Create AGENT.md guidelines
- /thought on/off Toggle thinking monologue

Project Files
- polaris_1_0_0_awakening.py     Main program
- agent_memory.json              Long-term memory
- agent_mood.json                Mood state
- agent_persona.json             Persona profile
- agent_relationship.json        User relationship
- agent_conversations.jsonl      Conversation archive
- .miniagent_checkpoints/        File modification backups
- AGENT.md                       Project guidelines (recommended)

Start chatting and let Polaris build its understanding of you and your project.
