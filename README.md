# Harness Procurement Console

一个用 FastAPI + SSE 实现的智能采购助手执行台。页面展示主智能体、领域 SubAgent、任务状态、可展开工具调用、真实 LangFuse Trace 接入，以及沙箱 Proxy 热替换过程。

## 启动

```powershell
pip install -r requirements.txt
uvicorn main:app --reload
```

访问 <http://127.0.0.1:8000>。首页是业务工作概览，Agent 任务执行详情独立位于 <http://127.0.0.1:8000/runs>。

## 登录与权限

所有业务页面和 API 都经过服务端会话鉴权。演示账号：

| 用户 | 密码 | 权限 |
| --- | --- | --- |
| `admin` | `Admin@2026` | 查看完整工具链、LangFuse、沙箱并审批订单 |
| `employee` | `Employee@2026` | 采购对话、提交申请、查看自己的申请进度 |
| `li.ming` | `User@2026` | 与普通员工相同，用于演示切换用户 |

生产环境应通过 `HARNESS_ADMIN_PASSWORD`、`HARNESS_EMPLOYEE_PASSWORD`、`HARNESS_LIMING_PASSWORD` 和 `HARNESS_AUTH_SECRET` 覆盖默认值。

## 采购对话

访问 <http://127.0.0.1:8000/chat>。对话顶部可以按厂商和模型切换，接口使用 SSE 流式输出，并支持将对话内容提交为订单申请。每个用户可创建多条独立对话，左侧历史列表可点击打开、续聊或删除；标题、消息、模型与更新时间持久化到 SQLite，切换用户或重启服务不会丢失。删除接口执行服务端所有权校验，并级联删除该对话的消息。管理员可展开 Memory/Skills 工具调用和 Trace；普通员工仅看到业务回复与自己的申请状态。

## Skills 与沙箱控制面

管理员可访问 <http://127.0.0.1:8000/skills> 查看 5 个预置领域 Skill，执行清单/说明/沙箱策略校验，并控制 Skill 是否允许被 Agent 渐进式加载。真实对话加载 Skill 时会累计调用记录。

管理员可访问 <http://127.0.0.1:8000/sandboxes> 查看稳定 Proxy Handle、底层实例、两小时剩余生命周期与恢复次数；可以执行健康检查和热替换，热替换后句柄保持不变，底层实例、代次和审计事件持久化更新。Agent 重放中的沙箱热替换事件也会调用同一控制面。

## 统一模型中心

管理员访问 <http://127.0.0.1:8000/models>，统一配置 DeepSeek、OpenAI 和阿里云百炼。每个厂商只保存一份 API Key，同厂商下的多个模型直接复用；不同厂商分别配置。API Key 使用 Fernet 加密后持久化到 SQLite，浏览器和 API 只会收到掩码。普通员工只能在对话页选择已配置、已启用的模型。

也可通过 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY` 注入厂商密钥，通过 `HARNESS_MODEL_ENCRYPTION_SECRET` 指定密钥加密主密钥。

## 申请与审批历史

访问 <http://127.0.0.1:8000/applications>。每一笔采购申请拥有独立详情和审计时间线，提交、批准、审批人、审批时间与生成的订单号会持久化到 `data/procurement.db`。管理员查看并审批全部员工申请，普通员工只能查看本人提交的记录。可通过 `HARNESS_DB_PATH` 指定其他 SQLite 数据库位置。

未配置模型密钥时使用本地采购引擎，功能仍可完整演示。配置 DeepSeek-V4：

```powershell
$env:DEEPSEEK_API_KEY = "你的 DeepSeek API Key"
$env:DEEPSEEK_MODEL = "deepseek-v4-pro"
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
```

## LangFuse 接入

管理员访问 <http://127.0.0.1:8000/langfuse>，填写 LangFuse Host、Public Key 和 Secret Key。服务端会通过 Public API 验证项目并写入一条测试 Trace。Secret Key 只保存在当前 FastAPI 进程的内存中，浏览器仅保存 HttpOnly 会话标识；普通员工无法进入此页面或读取 Trace API。

也可以通过环境变量设置默认地址：

```powershell
$env:LANGFUSE_BASE_URL = "https://cloud.langfuse.com"
```

如果使用关闭 API Key 鉴权的自托管入口，可设置 `LANGFUSE_AUTH_DISABLED=true`，访问 `/langfuse` 时会直接跳转到 `LANGFUSE_BASE_URL`。

## 端口

| 服务 | 地址/端口 |
| --- | --- |
| 工作概览、对话、Agent 运行、Skills、Sandbox、模型中心、FastAPI API、SSE、LangFuse 接入页 | `127.0.0.1:8000` |
| LangFuse Cloud | 外部 HTTPS `443`，本地不占用端口 |
| 自托管 LangFuse | 由填写的 Host 决定，官方 Web 容器通常为 `3000` |

## 演示交互

- 点击任务行展开或折叠该任务的工具调用。
- 点击工具调用查看输入、输出与 LangFuse Span。
- 点击“重放执行”通过 SSE 重新播放完整采购链路。
- 报告生成完成后，可在订单任务中执行人工审批并模拟写入 ERP。
- 在“申请记录”中逐笔查看历史详情和审批时间线，重复批准不会产生重复审批事件。
- 接入 LangFuse 后，SSE 任务事件、工具调用和订单审批会写入真实 Trace。
