# 本地多智能体看板：实现计划

> 面向实现者：这是一个**薄的、本机只读状态看板 + 一键跳转器**，不是自动化多智能体平台，也**不是网页内嵌终端**。不要引入自动派活、自动关闭、自动归档、邮件、云端隧道、远程暴露，也**不要在网页里挂实时终端**（那正是 ReevesAgents 卡顿的根源）。

**目标：** 在浏览器里按项目统一**查看**所有 Hermes、Claude Code、Codex 等原生会话的状态；让用户一眼知道哪个席位在输出、哪个在等输入、哪个已空闲；想动手时**点一下，把用户自己的那个原生终端窗口瞬间切到该席位**，在又快又原生的终端里继续同一 topic。

**为什么不做网页内嵌终端：** Claude Code / Codex 等是全屏不停重绘的 TUI，同屏挂 N 个实时终端 = N 倍重绘 × N 路 WebSocket，必然卡。而用户本机已经有又快又原生的终端。所以网页只做**发现与导航**（便宜、永不卡），**操作留在原生终端**（本来就最快）。

**核心原则：**

1. 按项目组织，不按模型组织。
2. **网页不内嵌终端、不做实时终端流。** 网页只显示只读状态快照 + 跳转按钮；真正的交互发生在用户自己的原生终端里。
3. 只做终端状态，不试图自动判断任务的语义完成。LLM 的最终回复通常已足够清楚，用户自己一眼判断。
4. 会话“当前空闲”不等于会话结束。完成一轮工作后，席位仍留在看板、显示最终输出、可继续同一 topic。
5. 只有用户手动移除席位，才停止对应会话并从活跃视图移走。
6. **tmux 对用户隐形。** 用户不看 tmux 的 tab、不敲 tmux 命令；分辨项目与席位靠网页看板，桌面上只留一个终端窗口作“取景器”。
7. 所有工作台状态放在 `/Users/quzhijie/tools/agent_hub/`，不写入项目仓库，不污染项目 Git。
8. 服务只监听 `127.0.0.1`；禁止公网隧道、局域网监听、邮件访问、日历访问、Tailscale 操作、自动回复权限提示、自动续跑或自动发送消息。

---

## 一、成品形态（用户视角）

```text
屏幕上并排两样东西：

┌─ 浏览器：Agent Hub 看板 ─────────┐   ┌─ 一个终端窗口 ────────┐
│ 项目 QSOspec                     │   │ (现在显示 Codex)      │
│  ● Codex   工作中   [跳到终端]───┼─┐ │ codex> 正在改 fit.py… │
│  ⏸ Claude  等输入   [跳到终端]   │ │ │                       │
│ 项目 DIXE 白皮书                 │ └▶│ ← 点上面的卡片，       │
│  ○ Hermes  空闲     [跳到终端]   │   │   这个窗口就切过去     │
└──────────────────────────────────┘   └───────────────────────┘
   ↑ 在这里分辨/定位所有 agent          ↑ 只有一个，没有一排 tab
```

日常流程：

```text
1. 从看板“新建席位”起 agent → hub 用 tmux 在后台起 claude / codex / hermes。
2. 用户保持一个终端窗口 attach 在工作台的 tmux 上（一次即可）。
3. 平时扫看板：谁在跑、谁停下等输入、谁空闲——一眼看清。
4. 想动手：点该卡片的 [跳到终端] → 那一个终端窗口瞬间切到该 agent → 在原生终端里回它。
5. 回完继续看看板。窗口数从“一大堆”塌缩成 1，网页当目录。
```

---

## 二、需求边界

### 需要实现

```text
首页：项目卡片列表。
项目页：该项目的多个智能体席位卡。
席位卡：名称、提供方、工作目录、状态、最后输出摘要、最后活动时间、它在哪(tmux 会话名)。
状态：工作中／等待输入／空闲／已退出／状态未知。
跳转：一键把用户的原生终端窗口切到该席位（tmux switch-client）。
会话管理：新建、跳转、继续（在原生终端里）、手动移除。
```

### 明确不做

```text
网页内嵌终端、实时终端流、同屏多终端（ReevesAgents 式，一定卡）。
自动判断“任务完成”。
任务归档、看板工单、排期、统计报表。
自动派发任务或智能体之间自动通信。
自动 Git worktree、自动提交、自动重试、自动处理速率限制。
自动读取或修改项目文件；自动向终端发送任何按键。
远程访问、手机端、云端、邮件、通知推送。
将已有散落 Terminal 会话迁入系统（只管理 hub 自己起的会话）。
```

### 用户交互语义

```text
一个项目：长期存在的网页卡片，例如 QSOspec、某篇文章、DIXE whitepaper。
一个席位：项目内一个原生智能体会话，例如 Codex 修复者、Claude Code 审阅者、Hermes 协调者。
一轮工作：用户在一个席位中下达的一段任务；系统不单独管理它。
空闲：终端看起来已结束一轮输出并回到交互提示符；不自动移除席位。
跳转：把用户唯一的取景器终端切到该席位；只读动作，不发任何按键。
手动移除：用户确认该 topic 彻底结束后，点按钮停止 tmux 会话并从活跃视图移走。
```

---

## 三、建议架构

```text
浏览器（只读看板，无终端）
  ├─ 项目列表与席位状态卡
  ├─ 项目详情
  └─ 状态徽章 + 最后输出预览 + [跳到终端] 按钮
          ⇅ HTTP/轮询或 SSE（本机认证，校验 Origin/Host）
Python 本机服务
  ├─ 项目与席位注册表（SQLite）
  ├─ tmux 会话管理器（new-session / kill-session）
  ├─ tmux 输出采样与状态检测器（capture-pane，只读）
  └─ 跳转器（list-clients + switch-client）
          ⇅
tmux（对用户隐形的持久层，单一 server）
  ├─ agent-hub-<项目>-<席位>  → hermes / claude / codex 等原生命令行
  ├─ …每个席位一个独立会话…
  └─ 用户的一个终端窗口 attach 于此，作取景器
```

### 建议技术栈

```text
后端：Python 3.11+、FastAPI、Uvicorn、SQLite、asyncio、subprocess(调用 tmux)。
前端：极简单页；原生 TS 或轻量框架均可；不需要 xterm.js、不需要 WebSocket 终端桥。
测试：pytest；前端最小浏览器自动化即可。
部署：工作台专属虚拟环境 /Users/quzhijie/tools/agent_hub/.venv/。
状态目录：/Users/quzhijie/tools/agent_hub/data/。
```

实现者可以调整框架，但不可改变“本机、只读状态、显式跳转、不内嵌终端、无自动代理动作”的边界。

---

## 四、数据模型

### projects

```text
id                UUID
name              用户可读项目名
root_dir          项目根目录的绝对路径
created_at
updated_at
is_removed        默认 false
```

### sessions

```text
id                UUID
project_id        外键
name              例如 executor、reviewer、drafting
provider          hermes / claude / codex / custom
launch_command    实际启动命令；不保存密钥
working_dir       绝对路径
tmux_session      唯一 tmux 会话名（字符集受限，见阶段 2）
status            active / waiting / idle / exited / unknown
last_output       经长度限制、清理 ANSI 后的尾部预览
last_activity_at
created_at
removed_at        仅用户手动移除时写入
```

### session_events（只记录操作与状态变化，非任务系统）

```text
id
session_id
kind              created / started / status_changed / manually_removed
old_status
new_status
created_at
```

保存最近终端输出用于刷新看板和诊断，但不要把无限滚屏全部复制进 SQLite；tmux 本身是原始会话记录。跳转是只读动作，不入 events。

---

## 五、状态检测策略（本产品的真正核心）

状态检测的目标是帮用户找到“值得看一眼的窗口”，不是断言任务是否完成。**这是整套系统唯一的核心价值，也是最难的部分**——因为 Claude Code / Codex 是全屏重绘的 TUI，底部常驻输入框和 spinner，不能当成滚动日志简单 tail。必须当一等公民做，别当只读小功能。

### 通用状态

```text
active：两次采样之间内容有变化，或检测到模型正在生成（spinner / “esc to interrupt” 等）。
waiting：检测到模型向用户提问、权限确认、选择菜单、错误交互等需要输入的提示。
idle：检测到提供方的交互提示符，且最近一小段时间内容无变化。
exited：tmux 会话或目标进程已不存在。
unknown：无法识别，必须展示最后输出，绝不猜测为完成。
```

### 实现原则

1. 每个提供方实现独立、可测试的纯函数：`detect_status(frames) -> Status` 与 `extract_last_message(frame) -> str`。
2. 状态规则只分析 **ANSI 清理后的 capture-pane 快照**；**绝不向终端发送任何字符**。
3. **主信号是“连续两次采样的内容 diff/hash 是否变化”**（变=active）；provider 专属文本（spinner、权限菜单、提示符）为辅助信号。单帧快照不可靠。
4. **TUI 感知解析**：Claude Code / Codex 底部是带框输入区和状态栏，naive tail 会抓到边框而非模型回复。`extract_last_message` 必须跳过底部 chrome，向上找真正的消息块。先确认每个 provider 是否使用备用屏幕缓冲区（alt-screen）——若是，capture-pane 只能取当前帧、取不到滚动历史，需据此调整预览策略。
5. 初始实现只覆盖当前实际使用的 Hermes、Claude Code、Codex；DeepSeek 等作为以后可配置的 custom 命令提供方。
6. 规则未匹配时返回 `unknown`，不要返回 `idle`。
7. 状态旁始终显示最后 3–10 行有意义输出和最后活动时间，便于用户快速判断 LLM 是否报告了遗留问题。
8. 不应把 `/clear`、上下文压缩、关闭浏览器、终端暂时静默解释为任务完成或会话结束。

---

## 六、分阶段实施

### 阶段 0：项目脚手架与安全边界

**目标：** 建立独立工程，不影响已有研究／代码仓库。

**文件：**

```text
/Users/quzhijie/tools/agent_hub/
├─ README.md
├─ pyproject.toml
├─ backend/
├─ frontend/
├─ tests/
├─ data/                 # 加入 .gitignore
└─ .gitignore
```

**步骤：**

1. 初始化独立 Git 仓库；不得在任何用户项目仓库中初始化或写入状态文件。
2. 创建 Python 3.11 虚拟环境，仅安装后端与测试依赖。
3. 前端建立最小页面，后端提供健康检查接口。
4. 将监听地址固定为 `127.0.0.1`；拒绝 `0.0.0.0` 和任意远程地址。
5. 创建随机本机令牌，所有接口需同源会话认证；**并在 HTTP 与任何推送连接上校验 Origin/Host 头**，防止恶意网页对 127.0.0.1 做 DNS-rebinding。
6. 启动前检查：确认 tmux 存在；确认目标工作目录存在；绝不自动安装智能体命令行。

**验收：**

```text
服务只能由显式命令启动。
lsof 显示仅监听 127.0.0.1。
非同源 / 错误 Origin 的请求被拒绝。
没有新建邮件、Tailscale、云端隧道或外部网络连接。
```

### 阶段 1：项目与席位注册表

**目标：** 允许网页创建项目并登记空白智能体席位。

**后端：**

```text
backend/app/models.py
backend/app/db.py
backend/app/routes/projects.py
backend/app/routes/sessions.py
```

**前端：**

```text
frontend/src/pages/ProjectsPage
frontend/src/pages/ProjectDetailPage
frontend/src/components/ProjectCard
frontend/src/components/SessionCard
```

**步骤：**

1. 为 projects、sessions、session_events 建 SQLite 迁移。
2. 实现项目创建、编辑名称、设置项目根目录、手动隐藏／恢复项目。
3. 实现“新建席位”表单：名称、提供方、工作目录、启动命令。
4. 仅在用户点“启动席位”后才创建 tmux 会话；新建登记本身不启动任何进程。
5. 首页每张项目卡只显示项目名、活跃席位数、需要注意（等输入）的席位数和最近活动时间。

**测试：**

```text
项目与席位的增删改查。
非法相对路径、缺失目录、重复 tmux 名称的拒绝逻辑。
删除席位必须显式确认；不会删除工作目录或项目文件。
```

### 阶段 2：tmux 生命周期管理（隐形持久层）

**目标：** 为新创建的席位启动可恢复的真实原生会话，全程对用户隐形。

**文件：**

```text
backend/app/tmux.py
backend/app/providers/base.py
backend/app/providers/hermes.py
backend/app/providers/claude.py
backend/app/providers/codex.py
```

**步骤：**

1. 设计确定且可反查的 tmux 会话名，如 `agent-hub-<短项目编号>-<短席位编号>`。**限制字符集**为 `[a-zA-Z0-9_-]`——tmux 的 target 语法里 `.` 是 window.pane 分隔符、`:` 是 window 分隔符，含这些会让精确匹配失效。
2. 每个提供方只负责构造启动命令：
   - Hermes：本机可用的 `hermes` 原生交互命令；
   - Claude Code：`claude`；
   - Codex：`codex`；
   - custom：用户完整填写命令。
3. 通过 `tmux new-session -d -s <name> -c <working_dir>` 启动；**用 `-x`/`-y` 给一个较大的初始尺寸**（如 220×50），这样即使该会话当前没有客户端接着，capture-pane 也能取到足够宽的输出用于状态与预览。不添加绕过权限的参数。
4. 每个席位一个独立 tmux 会话（便于 switch-client 跳转与 kill-session 隔离）；全部位于同一 tmux server。
5. 不复制、不读取、不改写任何项目文件。
6. “手动移除席位”只执行精确的 `tmux kill-session -t =<name>`，然后标记 `removed_at`；必须确保目标名精确匹配且属于 agent_hub 登记的席位。
7. 服务重启后扫描已登记 tmux 会话并**对账**：仍在的恢复运行状态；停机期间已死的标记 exited；DB 里的孤儿行不误杀真实会话。

**测试：**

```text
用临时 Git 仓库与 harmless shell 命令测试创建、重启对账、手动停止。
验证停止一个席位不会停止同项目的另一个席位，也不会碰非 agent_hub 的 tmux 会话。
验证 tmux 名字非法字符被拒。
```

### 阶段 3：只读状态看板与最后输出（核心）

**目标：** 让用户不用翻窗口，就能一眼知道哪个席位值得点开。永不卡。

**文件：**

```text
backend/app/status.py
backend/app/tmux_capture.py
backend/app/providers/*_status.py
frontend/src/components/StatusBadge
frontend/src/components/OutputPreview
```

**步骤：**

1. **先建样本集**：为 Hermes、Claude Code、Codex 各保存真实但脱敏的 capture-pane 帧样本，覆盖：正在生成、模型提问、权限确认、报错、最终回复后提示符、未知输出。规则围着样本写。
2. 每 2–5 秒对每个已登记 tmux 窗格 `tmux capture-pane -p`（只读，不写入任何内容）取最后固定行数。
3. 清理 ANSI 控制字符，跳过 TUI 底部输入框/状态栏 chrome，提取最后的有意义输出行。
4. **状态判定以“相邻两帧内容 diff”为主信号**，provider 专属文本为辅；对 Hermes、Claude Code、Codex 分别产出 `active` / `waiting` / `idle` / `unknown`。
5. 首版规则保守：无法确定时显示 `unknown`，绝不猜 `idle`。
6. 用短轮询或 SSE 更新项目卡与席位卡；页面打开后立即拉当前状态。
7. 席位空闲时仍保留在项目页，卡片显示其最终输出；不自动隐藏、归档、关闭或创建新会话。

**测试：**

```text
用样本集验证“模型提问 / 权限确认 / 报错 / 最终回复后提示符 / 未知输出”不被混淆。
验证 active 主要由帧间 diff 判定，而非单帧碰运气。
验证没有任何状态检测测试会向 tmux 发送按键。
```

### 阶段 4：一键跳转到原生终端（取代旧的网页内嵌终端）

**目标：** 用户点卡片，自己那一个终端窗口瞬间切到该席位。这是整套导航体验的落点。

**文件：**

```text
backend/app/jump.py
backend/app/routes/jump.py
frontend/src/components/JumpButton
```

**步骤：**

1. 后端维护 `tmux_session` ↔ 席位 的映射，只允许跳到 agent_hub 登记的会话；拒绝任意会话名。
2. 点 `[跳到终端]`：
   - `tmux list-clients` 找到当前 attach 在工作台 server 上的客户端；
   - 若存在：`tmux switch-client -c <client> -t =<name>`，用户那个终端窗口瞬间切到目标 agent；
   - 若不存在（用户没开取景器终端）：网页显示一行可复制的精确命令 `tmux attach -t =<name>`；可选增强用 `open`/AppleScript 起一个 Terminal 执行 attach。
3. 跳转是**只读动作**：不向目标会话发送任何按键、提示注入或按键模拟。所有输入来自用户在原生终端的真实键盘。
4. 席位卡展示会话名（“它在哪”），跳转失败时给出清晰、可操作的提示。

**测试：**

```text
有一个 attach 的客户端时，switch-client 精确切到目标会话，不影响其他会话。
无客户端时，回退到显示 attach 命令，不报错、不误起进程。
跳转只允许登记会话名；非法名被拒。
验证跳转不向任何会话写入按键。
```

### 阶段 5：手动移除与轻量历史

**目标：** 让用户在确认 topic 真结束后，安全移走席位，同时保留可查的最终信息。

**步骤：**

1. 用户点“手动移除”时显示确认框：说明会停止哪一个 tmux 会话，但不会删除工作目录或项目文件。
2. 停止成功后，把席位从默认活跃视图隐藏，保留名称、提供方、最后输出摘要、最后活动时间和移除时间。
3. 项目页提供一个默认折叠的“已手动移除席位”区域；不是任务归档流程，只是防止误操作丢上下文。
4. 支持恢复一个已移除席位的登记记录，但恢复不自动启动命令；必须由用户再次点“启动”。

**测试：**

```text
确认错误席位不可能被移除。
确认项目目录和 Git 状态不受移除影响。
确认手动移除后，最终输出仍可查看。
```

---

## 七、验收场景

必须在临时目录完成，不应拿真实项目作第一轮压力测试。

### 场景 A：三席位项目

```text
项目：临时 Git 仓库
席位 1：Hermes    席位 2：Claude Code    席位 3：Codex
```

验收：

```text
看板首页只显示一个项目卡。
进入项目后能看见三个席位卡，各自显示状态与最后输出预览。
用户 attach 一个取景器终端；点任一卡片的 [跳到终端]，该窗口切到对应席位。
在原生终端发送无副作用测试提示，看板状态相应变化（工作中→空闲/等输入）。
挂三个席位，看板不卡（无实时终端流）。
```

### 场景 B：继续同一 topic

```text
Codex 完成一轮修改并停在交互提示符。
用户点 [跳到终端]，在原生终端继续追问同一 topic。
```

验收：

```text
不会创建新会话。
不会丢失之前最终输出。
同一席位状态重新变为工作中，然后再次变为空闲或等待输入。
```

### 场景 C：手动移除

```text
用户确认 Claude Code 的 topic 已完全结束。
```

验收：

```text
仅该席位 tmux 会话被停止。
项目、其他席位、工作目录和 Git 工作树保持不变。
默认活跃席位中不再显示它；轻量历史仍可查看最后输出。
```

---

## 八、已知风险与取舍

1. **终端状态只能是启发式。** 不把 idle 当作任务完成；最后输出才是用户判断依据。
2. **TUI 界面会变、且难解析。** Claude Code / Codex 全屏重绘，状态与“最后回复”提取必须有样本测试、provider 独立规则、以帧间 diff 为主信号；规则失效时退回 unknown。这是最大执行风险，别低估。
3. **不做网页内嵌终端 = 主动规避卡顿。** ReevesAgents 卡就是因为同屏挂多个实时终端；本方案只做只读状态 + 跳转，操作留在原生终端。
4. **安全面已缩小，但认证仍是关键。** 无浏览器终端后攻击面变小；仍需 `127.0.0.1` 绑定 + 令牌认证 + Origin/Host 校验防 DNS-rebinding；跳转只能精确切到已登记会话。
5. **跳转依赖一个 attach 的取景器终端。** 无客户端时优雅回退到显示 attach 命令，不静默失败。
6. **tmux 是唯一持久层，且对用户隐形。** 网页服务意外退出不应终止智能体；重启后必须对账并重连已有会话。用户不敲 tmux 命令。
7. **不要复刻 amux。** 避免自动继续、自动按键、邮件／日历访问、网络隧道、全网监听和未询问的后台行为。
8. **不要复刻 ReevesAgents 的缺口。** 不只显示 queued/open 之类没有操作意义的字段；必须有最后输出预览、状态含义、和一键跳到正确终端。

---

## 九、首版完成定义

首版完成仅需满足：

```text
用户打开本机网页；
看见按项目分组的席位卡；
一眼从状态和最后输出知道哪个窗口值得查看；
点一下，自己那一个原生终端窗口就切到正确的席位；
在原生终端里完成一轮工作后，终端与最终输出保留；
用户可以继续同一 topic；
用户确认结束后，手动移除该席位；
全程网页不内嵌终端、不发任何按键、不替用户操作。
```
