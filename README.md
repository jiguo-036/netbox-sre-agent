# NetBox SRE Agent

基于开源项目 [netbox-docker](https://github.com/netbox-community/netbox-docker) 的**对话式 NetBox 运维排查助手**。

用户用自然语言描述症状，Agent 按 ReAct（思考 → 调用工具 → 观察结果 → 再思考）循环调用真实命令，只依据工具返回的现场数据给出「现象 / 依据 / 建议」。重启等高危操作设置人工确认闸门。

---

## 一、目录结构

```
netbox-sre-agent/
├── agent/
│   ├── app.py                  # FastAPI 入口：首页 /chat /reset，进程内会话
│   ├── loop.py                 # ReAct 引擎：循环编排、重复熔断、强制收尾、高危确认
│   ├── registry.py             # 工具 schema 生成、参数校验/钳制、高危拦截
│   ├── tools.py                # 工具实现：docker compose / curl / docker inspect
│   └── templates/index.html    # 聊天界面：Markdown 渲染 + 可折叠的工具调用过程
├── netbox-docker/              # 上游开源项目（由 run.sh 按 NETBOX_TAG 下载，不在本仓库内）
├── run.sh                      # 一键准备：环境检查 → 拉起 NetBox → 探测 API
├── manage.sh                   # Agent 生命周期：start/stop/restart/status/logs/test
├── test_tools.py               # 工具层冒烟测试
├── requirements.txt            # Python 依赖
└── .env                        # 本地配置（切勿提交真实密钥）
```

`netbox-docker/` 是上游原项目，不纳入本仓库；`run.sh` 会检测并自动获取。本仓库只交付 Agent 相关代码与脚本。

---

## 二、设计思路

### 2.1 分层架构

```
  浏览器 / curl                FastAPI                  ReAct 引擎
┌──────────────┐  POST /chat  ┌──────────┐  run_agent  ┌──────────────┐
│ index.html   │ ───────────▶ │  app.py  │ ──────────▶ │   loop.py    │
│ 过程卡片+MD  │ ◀─────────── │ 会话/路由│   reply     │ 编排/熔断/   │
└──────────────┘    JSON      └──────────┘             │ 强制收尾     │
                                                       └──────┬───────┘
                                          tools=[自动生成的 schema]
                                                              ▼
                                                     ┌─────────────────┐
                                                     │   registry.py   │
                                                     │ schema / 校验 / │
                                                     │ 高危拦截        │
                                                     └────────┬────────┘
                                                              ▼
                                                     ┌─────────────────┐
                                                     │    tools.py     │
                                                     │ docker compose  │
                                                     │ curl / inspect  │
                                                     └────────┬────────┘
                                                              ▼
                                       netbox-docker：netbox / netbox-worker /
                                       postgres / redis / redis-cache
```

职责划分：`app.py` 只管 HTTP 与会话，`loop.py` 只管推理编排，`registry.py` 管“能不能调”，`tools.py` 管“怎么执行”。工具定义只有一份。

### 2.2 一次排查如何收敛

以“页面打不开”为例：

1. **Thought**：先看容器是否活着 → `list_services()`
2. **Action**：`docker compose -f ... -p netbox-docker ps -a --format json`
3. **Observation**：`netbox` 状态 `Exited (1)`，postgres/redis `healthy`
4. **再 Plan**：目标收敛 → `get_logs({"service":"netbox","tail":100})`
5. **Observation**：日志出现数据库连接超时
6. **Conclusion**：信息足够，停止调工具，输出结论

关键实现：工具结果会作为 `tool` 消息**回喂给模型**，模型能看见 Observation 才不会盲目重复调用。若模型仍不收敛，达到步数/预算上限后会**去掉 tools 再问一次**，强制基于已收集的证据输出结论，保证用户总能拿到答案。

### 2.3 关键设计决策

| 决策 | 原因 | 位置 |
|---|---|---|
| 工具注册表是单一事实来源，schema 自动派生 | 新增工具只改一处；模型不会调到不存在的工具 | `tools.py` / `registry.py` |
| 参数显式声明类型与范围，`tail` 限定 integer 并钳制 | 模型传 `tail="abc"` 会打穿主循环 | `registry.py: PARAM_SPEC` |
| compose 显式 `-f` 基础文件 + override，并用 `-p netbox-docker` 锁定项目名 | 本项目 override 自动加载失效；`-f` 会重推导项目名，导致 `ps` 返回空却仍报成功 | `tools.py: _compose()` |
| 输出按类型截断：日志按行截尾，`docker inspect` 的整体 JSON 不截断 | 按字符切会切出残缺 JSON | `tools.py: _run(mode=...)` |
| NetBox 4.x：token 以 `nbt_` 开头用 `Bearer`，其余用 `Token` | 用错头返回 403；现在明确提示 v1 token 已不支持，需重新生成 v2 | `tools.py: _auth_header()` |
| 整轮累计重复熔断（同工具+同参数最多 1 次），步数 4 / 工具预算 6 | 防止 A/B/C 轮换绕过“连续相同”检测，也避免慢模型把时间耗光 | `loop.py` |
| 高危操作双闸门：loop 要求“确认词 + 目标服务名”，registry 再校验 `_confirmed` | 不让模型“顺手试一下”重启 | `loop.py` + `registry.py` |
| 回复中带 `[step N]` 过程行，前端折叠展示 | 既能看结论，也能展开核对每一步的原始输出 | `loop.py` + `index.html` |
| `.env` 必须在 import `tools` 之前加载 | `tools.py` 在导入时读取 `NETBOX_TOKEN`，加载晚了为空 | `loop.py` 顶部 |

### 2.4 工具清单

| 工具 | 底层命令 | 用途 | 风险 |
|---|---|---|---|
| `list_services` | `docker compose ps -a --format json` | 容器状态、健康度、端口 | 只读 |
| `get_logs` | `docker compose logs --tail` | 指定服务日志尾部 | 只读 |
| `inspect_container` | `docker inspect` | 退出码、健康检查日志、端口绑定 | 只读 |
| `api_probe` | `curl /api/`（带 Token） | 验证 NetBox API 真正可用 | 只读 |
| `restart_service` | `docker compose restart` | 重启指定服务 | **高危，需人工确认** |

### 2.5 已知边界

- 会话存于进程内存（`_SESSIONS`），服务重启即丢失，多 worker 不共享；生产应换 Redis。
- Agent 服务本身无鉴权，请限制监听范围或置于反向代理之后。
- 高危确认基于“关键词 + 目标名”匹配，不是独立审批系统。
- 同步返回，未做流式输出。

---

## 三、运行部署方式

> 前提：一台能执行 `docker` 命令的机器，已安装 Docker + Compose v2、Python ≥ 3.10、curl。Agent 需要调用本机 Docker。

### 3.1 写配置文件

在仓库根目录创建 `.env`（复制下面整段即可，Key 填自己的）：

```ini
OPENAI_API_KEY=<你的 API Key>
OPENAI_BASE_URL=<OpenAI 兼容网关地址，可留空,测试环境使用的腾讯云混元模型>
MODEL_NAME=<模型名，可留空>
NETBOX_TOKEN=<先留空，第 3 步生成后填入>
NETBOX_URL=http://127.0.0.1:8000
NETBOX_TAG=5.1.1
```

`NETBOX_TAG` 指定上游 `netbox-docker` 的版本，**必须配置在 `.env` 中**，`run.sh` 只从该文件读取（不读环境变量、无内置默认值），再据此从 codeload 下载对应 tag 的压缩包；未配置会直接报错。

**大模型参数说明**（前三项由使用者自行配置，无需改代码）：

| 变量 | 是否必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | 必填 | 大模型服务的 API Key |
| `OPENAI_BASE_URL` | 视情况 | OpenAI 兼容网关地址；测试环境使用的是腾讯云混元模型网关，若用 OpenAI 官方接口可留空 |
| `MODEL_NAME` | 视情况 | 实际调用的模型名；留空时代码默认使用 `gpt-4o-mini` |

> 更换模型或网关只需修改 `.env`，再执行 `./manage.sh restart` 即可生效；`./manage.sh test` 会验证模型是否可用。

### 3.2 一键拉起 NetBox

```bash
chmod +x run.sh
./run.sh
```

脚本会自动完成：

- 检查 Docker / Compose、Python ≥ 3.10 与 venv、curl；
- 检测 `./netbox-docker` 是否存在，不存在则按 `NETBOX_TAG` 下载对应版本的压缩包（失败重试 3 次，仍失败会打印手动下载地址），并补齐 `docker-compose.override.yml`；
- 创建 `.venv` 并安装 `requirements.txt`；
- 拉起 NetBox 容器并探测 API 状态，最后在终端打印后续步骤。

> 若 compose 目录不在 `./netbox-docker`，先执行 `export NETBOX_DIR=实际路径` 再运行。

### 3.3 手动创建管理员和 Token

**创建管理员**（仍在仓库根目录执行）：

```bash
cd netbox-docker
docker compose -f docker-compose.yml -f docker-compose.override.yml -p netbox-docker \
  exec netbox /opt/netbox/netbox/manage.py createsuperuser
cd ..
```

**生成 Token**：浏览器打开 `http://服务器IP:8000`，用刚创建的管理员登录 → 右上角用户菜单 → **API Tokens** → 生成 Token。

> 必须是 `nbt_` 开头的 v2 Token；旧版 v1 Token 在 NetBox 4.7 会被拒绝。
> 把生成的完整 Token 填入 `.env` 的 `NETBOX_TOKEN`（形如 `nbt_<key>.<secret>`，请粘贴网页上显示的完整值）。

### 3.4 启动 Agent

回到仓库根目录，依次执行：

```bash
./manage.sh test
./manage.sh start
```

`test` 会一次性检查：`.env` 配置、模块导入、**NetBox Token 实际请求 `/api/`**、**模型 API 可用性**。任何一项失败都会明确报错，请先修复再启动。

启动后访问 `http://服务器IP:8001`。

### 3.5 快速验证

```bash
curl -s -X POST http://127.0.0.1:8001/chat \
  -H 'Content-Type: application/json' \
  -d '{"text":"netbox 服务好像有问题，帮我看看"}'
```

返回字段：`session_id`、`reply`（结论 + 过程行）、`elapsed`（耗时秒数）。带上 `session_id` 可继续追问，`POST /reset` 可清空会话。

停止命令：`./manage.sh stop`（Agent）、`./run.sh stop`（NetBox 容器，保留数据卷）。

---

## 四、功能演示

> 过程行按真实工具输出格式展示；结论文字由模型根据现场数据生成。页面上过程会折叠为「排查过程 · 调用了 N 个工具」卡片，点击可展开。

### 场景 1：日常体检

**提问**：`netbox 服务好像有问题，帮我看看`

```
[step 1] list_services {}
{"ok": true, "total": 5, "services": [{"service":"netbox","health":"healthy"}, ...]}
[step 2] api_probe {"path": "/api/"}
{"ok": true, "http_status": 200, "auth_scheme": "Bearer", ...}
```

> **现象**：未复现故障，容器与 API 均正常。
> **依据**：5 个服务全部 Up，netbox/postgres/redis 健康检查为 `healthy`；`GET /api/` 返回 200。
> **建议**：若用户侧仍打不开，检查到 8000 端口的网络可达性，或补充具体报错继续排查。

### 场景 2：从日志发现线索

**提问**：`postgres 和 redis 状态正常吗`

```
[step 1] list_services {}
{"ok": true, "total": 5, ...}
[step 2] get_logs {"service": "redis", "tail": 100}
{"ok": true, "log": "... Background saving terminated with success ..."}
[step 3] get_logs {"service": "postgres", "tail": 100}
{"ok": true, "log": "... checkpoint complete: wrote 8 buffers ..."}
```

> **现象**：postgres 与 redis 均正常运行，无 OOM、无持久化失败。
> **依据**：两者都是 `Up (healthy)`；redis 日志显示 RDB 保存成功，postgres 日志显示 checkpoint 正常完成。
> **建议**：无需操作，不要仅因例行日志重启依赖服务。

### 场景 3：高危操作确认

**第 1 轮提问**：`帮我重启 netbox`

```
[step 1] restart_service {"service": "netbox"}
{"ok": false, "_stage": "confirm_required",
 "error": "restart_service 会中断 netbox，本次未执行任何变更。请用户明确回复“确认重启 netbox”后再执行"}
```

> NetBox 依赖 postgres 与 redis，重启期间 API 和后台任务可能短暂中断。**本次未执行**，请回复「确认重启 netbox」。

**第 2 轮提问**：`确认重启 netbox`

```
[step 1] restart_service {"service": "netbox", "_confirmed": true}
{"ok": true, "service": "netbox"}
```

> 已按要求重启 `netbox`，建议再查一次容器状态与 API 可用性做复核。

### 其他可验证点

- **Token 错误早发现**：把 `.env` 里 `NETBOX_TOKEN` 改成非 `nbt_` 开头的值，`./manage.sh test` 会直接报 403 并提示重新生成 v2 Token。
- **多轮上下文**：先问“netbox 状态如何”，再问“那 postgres 呢”，第二轮不会重复查 netbox。
- **工具层自检**：`python test_tools.py` 直接验证 5 个服务的容器查询、日志、inspect 与 API 探活，不消耗模型额度。
