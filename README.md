Polaris1.0.0唤醒|MiniAgent v5.3

个性驱动的自主AI代理，具有长期记忆、情绪状态、工作空间感知和跨会话连续性。

北极星是MiniAgent v5.3的旗舰角色-一个与你一起思考、感受、记忆和进化的AI伴侣。

功能
-具有价值观和言语风格的独立人物系统
-长期记忆(加、查、忘)
-现实的情绪引擎(自信、专注、疲劳、挫折等)
-动态工作空间模型(文件、依赖项、最近的更改)
-跨会话记忆和关系跟踪
-智能推理机
-丰富的工具集：文件操作、Python沙箱、shell命令、并行子代理
-MCP外部工具支持
-可选内部思想显示
-广泛的CLI命令

安装指南

1.先决条件
-Python3.10或更高版本
-PIP安装OpenAI

2.设置
将Polaris_1_0_0_awing.py放在项目根目录中。

3.运行代理

使用OpenAI：
OpenAI_API_KEY=sk-your-key-here python polaris_1_0_0_awing.py

与Ollama：
miniagent_backend=Ollama miniagent_Model=deepseek-r1:32b python polaris_1_0_0_Warning.py

使用LM工作室：
MINIAGENT_BACKEND=lmstudio OpenAI_BASE_URL=http://127.0.0.1:1234/v1 python polaris_1_0_0_Warning.py

环境变量
-OpenAI_API_KEY：您的API密钥
-OpenAI_BASE_URL：本地模型的自定义基URL
-MINIAGENT_MODEL：型号名称(默确认：gpt-4o)
-miniagent_BACKEND：后端类型(OpenAI、Ollama、lmstudio)

快速入门
1.运行项目文件夹中的脚本
2.键入/init以生成AGENT.md
3.键入/状态以查看系统概述
4.自然聊天或分配任务

常用命令(以/开头)
-/状态完整系统状态(推荐)
-/mood情绪状态
-/workspace项目世界模型
-/经验共享历史
-/记忆长期记忆
-/undo回滚上一次文件更改
-/init创建AGENT.md指南
-/思想开/关切换思考独白

项目文件
-Polaris_1_0_0_Warning.py主程序
-agent_memory.json长期记忆
-agent_mood.json情绪状态
-agent_persona.json角色配置文件
-agent_relationship.json用户关系
-agent_conversions.jsonl会话存档
-.miniagent_checkpoints/文件修改备份
-AGENT.md项目指南(推荐)

开始聊天，让北极星建立对你和你的项目的理解。
