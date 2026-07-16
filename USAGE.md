# Agent Hub 使用说明

桌面用 **web 看板**(只读、控制、跳转),手机用 **handmux**(交互、给 agent 打字)。
两者共用同一个 tmux 后端(默认 socket),所以手机上能直接看到并操作看板里建的席位。

---

## 分工一览

| | 桌面 web 看板 | 桌面取景器终端 | 手机 handmux |
|---|---|---|---|
| 建 / 删席位、控制、看全局状态 | ✅ | | |
| 跳转、原生打字干活 | 触发跳转 | ✅ 主力 | |
| 出门在外给 agent 回话 | | | ✅ |

> 纪律:**建席位只在桌面看板做**。别在 handmux 里手动 `new-session` —— 那种会话不进看板、没状态。

> 进阶:桌面看板还能把多步任务编成**流水线**(如 plan→implement→review,每步等你批准、隔离在独立 git worktree)——见「三、流水线」。

---

## 零、装好依赖

- **Python ≥ 3.11** 和 **tmux ≥ 3.0**(macOS:`brew install python tmux`)。
- 克隆仓库后,`./run.sh` 首次运行会自建虚拟环境并装好依赖,不用手动 `pip`。

```sh
git clone https://github.com/quzhijie/agent_hub.git
cd agent_hub
./run.sh
```

启动后终端会打印带 token 的地址,点开即用。token 首次运行本地生成、存在 `data/token`
(已 gitignore),服务只监听 `127.0.0.1`。

---

## 一、桌面端

### 1. 让它开机自启(可选,推荐)

想让服务**登录即启动、崩了自动拉起**,注册一个 per-user 的 launchd 服务即可。plist
由你的克隆位置现算生成,不含任何写死的路径:

```sh
./run.sh install       # 现在就起 + 每次登录自启
./run.sh uninstall     # 注销服务(仓库/venv/data 都不动)
```

管理命令(`$(id -u)` 会自动取你的用户 id):

```sh
launchctl bootout   gui/$(id -u)/com.agent-hub                       # 停
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.agent-hub.plist   # 起
tail -f data/hub.log                                                 # 看日志
```

> 不装服务也行:直接 `./run.sh` 前台跑即可。改了后端代码想让服务生效:`./run.sh install`
> 重跑一次(它会先 bootout 再 bootstrap),或手动 bootout 再 bootstrap。
> 跑测试:`./run.sh test`(端口不冲突,但别在服务运行时再手动 `./run.sh run`)。

### 2. 打开看板(地址固定,收藏一次即可)

- **好记版**:`http://agent-hub.localhost:8787`(Chrome 直接可用)
- 标准版:`http://127.0.0.1:8787`

**不需要带 token**——页面自动注入。右上角显示"已连接"即正常。

**状态一目了然**(5 个状态):
- **工作中**(绿):在生成/思考/跑命令。判定带**黏性**——工具结果和下一个 spinner
  之间偶尔一帧没标记,不会再乱跳到「等待输入」或「已完成」。
- **等待输入**(琥珀):agent 抛了问题给你(claude 的许可弹窗、明显的问句)。
- **已完成**(青):干完一轮、你还没看的产出。
- **空闲**(灰):歇着,或你已经看过、无需再处理。
- **状态未知**(蓝):看不懂当前屏幕的保底态。

> **「等待输入」和「已完成」是长期态,不会自己消失**:只有当你**跳过去看了那个
> tmux 席位、再把取景器切走**,它才落回「空闲」(看过即确认)。看过后同一块静止
> 的屏幕不会再重复打扰,除非该 agent 又开始真正工作。没去看,它就一直挂着。

- 顶栏中间是**全局状态栏**(彩点+数字);每个项目标题行也有同款迷你彩点(悬停看含义);
- 浏览器标签标题:`(2⚠ 1✓) Agent Hub` = 2 个等待输入、1 个已完成;
- **macOS 系统通知**:席位从**工作中→等待输入**、或**工作中→已完成**时各弹一次
  (完成判定有防抖:至少真正连续工作过、且连续两帧确认结束才算,刚启动停在输入框的不算)。
  不想要:服务环境变量加 `AGENT_HUB_NOTIFY=0`。
- **「最近推送」条**(顶栏和第一个项目之间):按时间倒序记录最近产生推送的状态变动
  ——谁开始等待输入(琥珀点)、谁干完活了(青点)。**错过弹窗也能回溯是哪个 agent
  要处理**;点其中一条即可跳到那个席位(席位已移除的不可点)。没有任何推送时这条自动隐藏。
  即使关了系统通知(`AGENT_HUB_NOTIFY=0`)也照常记录,只是不弹 banner。
  - **处理完就归档**:鼠标悬停某条,右侧出现 **✕** 点一下即从列表移除;标题右边「**清空**」
    一次归档当前所有条目。归档是**软删除**(只是不再显示,DB 里留痕、可回溯),**不影响
    对应 agent**;归档后若那个 agent 再次触发推送,会作为新条目重新出现。

### 3. 建项目

右上「**+ 新建项目**」→ 填 `项目名` + `根目录(绝对路径)`,如 `~/code/my-project`
的绝对形式 `/Users/you/code/my-project`。

### 4. 建席位(登记一个 agent)

项目卡右上「**+ 新建席位**」:

- `席位名`:executor / reviewer 之类
- `提供方`:hermes / claude / codex / custom
- `工作目录`:默认填项目根目录,可改
- `启动命令`:留空则用该提供方默认命令;**custom 必填**
- 点「**登记**」。此时只是登记,还没跑。

### 5. 启动

席位卡点「**启动**」,agent_hub 就在 tmux 里把这个 agent 拉起来。

> **重新启动会自动续上对话**:席位退出/关机后点「重新启动」,claude 席位自动带
> `--continue`、codex 席位自动带 `resume --last`,接着上次聊。想开全新对话:进去后
> `/clear`(或给席位填自定义启动命令——自定义命令永远原样执行,不会被加续接参数)。

> **在 agent 里 `/clear`(claude)/ `/new`(codex):不崩窗口、不丢历史、只留痕。**
> 这是 agent 进程**内部**清空当前上下文、另起一段新对话——tmux 会话/窗口/进程都不变,
> 看板还是同一张卡(状态多半从「工作中」刷回「空闲」),**窗口不会崩**;后端只读,感知不到这个动作。
> 旧上下文**不删**,各自归档在磁盘留痕(一段对话一个文件,互不覆盖):
> - claude:`~/.claude/projects/<项目路径编码>/<会话UUID>.jsonl`
> - codex:`~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl`
>
> ⚠️ 唯一要记的坑:**「重新启动」的自动续接(`--continue` / `resume --last`)接的是"最近一次"**。
> 你一 `/clear`、`/new`,那段新对话就成了"最近",之后重启会续上**它**,而不是之前那段有料的。
> 想翻回更早那段得**手动挑**:claude 用 `claude --resume`,codex 用 `codex resume`(列表里选)。

### 6. 开一个取景器终端(只需一次)

任意终端里跑:

```sh
tmux attach
```

> ⚠️ 先**启动过至少一个席位**再 attach,否则可能报 `no sessions`(空服务器没法连)。
> 席位和你日常 tmux 同处一个 server,`tmux attach` 可能先落到你自己的某个会话上,切一下即可。

### 7. 跳到某个 agent

看板点席位卡的「**跳到终端**」→ 取景器终端**瞬间切**到这个 agent,同时**那个
Terminal 窗口自动弹到最前、选中正确的 tab**——不用在一堆窗口里找。网页上只闪一个
小提示,不弹窗打断。

> - jump 会挑**最宽**的那个 client(即桌面),所以哪怕手机也连着,桌面这颗按钮也只驱动桌面。
> - 自动置前支持 Terminal.app 和 iTerm2。首次使用如果提示"python 想要控制 Terminal",
>   点允许;拒绝了想恢复:系统设置 → 隐私与安全 → 自动化。置前失败不影响切换本身。

### 8. 看状态

看板每 2.5 秒自动刷新:`工作中 / 等待输入 / 已完成 / 空闲 / 已退出 / 状态未知`,每张卡带最近输出预览。有 agent 在"等待输入"时,项目标题会提示 `N 个等输入`。

> 「工作中」永远不会自己悄悄变「空闲」;「等待输入 / 已完成」也不会——它们只在你
> **跳过去看过再切走**后才落回「空闲」。所以看板长期挂着的琥珀/青点,就是还没处理的活。

### 9. 收拾席位

- 「**移除**」:软删除 → kill 掉 tmux 会话,但**不删你的目录/文件**;席位落到卡片下方「**已手动移除席位**」折叠区(该折叠区展开后不会被自动刷新收起)。
- 折叠区里每张卡可「**恢复**」(变回未启动状态)或「**彻底删除**」(永久抹掉记录,不可恢复)。

---

## 二、移动端(handmux)

移动端只做一件事:**用 handmux 接进这台 Mac 的同一个 tmux,直接和 agent 交互**。手机端零安装(浏览器 PWA)。

### 前置要求

- 电脑:**Node ≥ 18** 和 **tmux ≥ 3.0**(macOS 上 `brew install node tmux`)。
- 手机:一个浏览器即可。

### 1. 安装并启动 handmux

```sh
# 二选一
brew install handmux/tap/handmux      # macOS 首选,顺带装好 node + tmux
npm i -g handmux                       # 已有 node 时

handmux start                          # 仅本机 / 同一 WiFi
```

`start` 会打印一个**二维码** + 地址 + token。手机扫码即登录,能看到你真实的 tmux 会话 ——
**因为共用默认 socket,agent_hub 建的席位(`hub-项目-席位-xxxx`,如 `hub-review-plan-e28d`)就在里面**,点开就能操作。

在浏览器里选「**添加到主屏幕**」,即成全屏 PWA,和原生 App 基本无异。

### 一键登录:让 `?token=` 链接真的一键

⚠️ **stock handmux(0.12.x)的 web 端根本不读 URL 里的 `?token=`**。它只认 `localStorage['tw_token']`,而这个值**只有那个手动输入框会写**——所以扫码 / 点带 `?token=` 的链接,第一次仍会让你手输一次 token(README 说的"扫码即登录"对这个 build 不成立)。更烦的是:隧道一换域名(natappfree 重连给新子域名、cloudflare 临时域名每次都变)就是**新 origin**,localStorage 清空,又得重输。

修法:往 handmux 自带的 `index.html`(`$(npm root -g)/handmux/public/index.html`)里,在那行 `<script type="module" …>` **之前**塞一小段引导脚本,开页时先把 token 写进 localStorage 再交给 App:

```html
<script>/*handmux-token-autologin*/(function(){try{var u=new URL(location.href);var t=u.searchParams.get('token');if(!t&&u.hash){var m=u.hash.match(/(?:[#&?])token=([^&]+)/);if(m)t=decodeURIComponent(m[1]);}if(t){localStorage.setItem('tw_token',t);u.searchParams.delete('token');var h=u.hash.replace(/([#&?])token=[^&]*/,'$1').replace(/[?#&]$/,'');history.replaceState(null,'',u.pathname+(u.searchParams.toString()?'?'+u.searchParams.toString():'')+h);}}catch(e){}})();</script>
```

之后任何 `URL/?token=xxx` 链接都能**真·一键进会话**,换域名也自动登录。handmux 的 Service Worker 对页面导航是 network-first、服务端每次从磁盘读 `index.html`,所以**改完下次打开即生效,不用重启、不用清缓存**。

> `npm i -g handmux` / `handmux update` 会覆盖 `index.html`,补丁被冲掉。放进你的启动脚本每次自愈重注即可:用 marker `handmux-token-autologin` 判是否已存在,没有就 `awk` 在 `<script type="module"` 前重插。(本机已在 `~/bin/handmux-up` 的 `ensure_token_patch()` 里做了这件事。)

### 2. 出门在外:开公网隧道

```sh
handmux start --tunnel cloudflare      # 即时公网 HTTPS(自动装 cloudflared)
```

> ⚠️ **安全须知**:公网隧道 = 把一个"能跑任意命令的真实终端"挂到互联网,**唯一的门就是 URL 里那个 token**,边缘没有额外鉴权。请务必按下面硬化。

### 3. 隧道硬化(强烈建议)

- 优先用**具名隧道 + Cloudflare Access**:
  ```sh
  handmux start --tunnel cloudflare-named --cf-hostname hub.<your-domain>.com
  ```
  然后在 Cloudflare Zero Trust 里给这个 hostname 加一条 **Access 策略**(邮箱/SSO 验证)——
  这才是真正的鉴权墙,而不只靠 token。
- token 当密码保管:别截图二维码乱发;泄漏就用 `--token` 换一个。
- 用完就关:不需要时把隧道进程停掉,别长期挂着。

### 4. 更省心的替代:Tailscale(不开公网)

若你有 [Tailscale](https://tailscale.com):手机和 Mac 组私网后,**不用隧道**——

```sh
handmux start        # 不加 --tunnel
```

手机浏览器直接开 `http://<Mac的-tailscale-IP>:19999/…`,出门在外照样用,但**什么都不暴露公网**,比公网隧道安全一档。

### handmux 常用参数

| 参数 | 说明 |
|---|---|
| `--tunnel` | `cloudflare` / `cloudflare-named` / `ssh` 等(按 handmux 版本为准) |
| `--port` | 端口,默认 `19999` |
| `--host` | 绑定地址,默认 `0.0.0.0` |
| `--token` | 自定义/重置令牌(默认自动生成) |
| `--no-qr` | 不打印二维码 |
| `-f` / `--foreground` | 前台运行 |

---

## 三、流水线(可选:把多步任务串起来,一步步过审)

桌面看板顶栏「**+ 新建流水线**」把一件多步的活儿编成一条**线性流水线**——例如 `plan → implement → review`。每一步在专属 worktree 里 **headless 运行**(`claude -p` / `codex exec`,prompt 走 stdin,**无交互界面、不弹权限/信任窗**),输出写进 `data/pipelines/<id>/step-N.log` 日志。默认每步干完**停下等你点「批准」**再进下一步;建时勾「**全自动**」就一路跑完(某步非零退出仍会停下)。整条流水线跑在一个**专属 git worktree + 分支**里,完全不碰你当前工作区。

> **谁在指挥?没有 LLM 在指挥。** 编排器(`backend/app/orchestrator.py`)是个确定性状态机,**从不往任何终端打字**——它把每步 headless 拉起来、读它的日志判完成。默认开着,但**不建流水线就什么都不会跑**。

### 1. 前置:项目根目录得是 git 仓库

隔离靠 `git worktree`,所以建流水线的项目其**根目录必须是 git 仓库**(否则报「v1 隔离需要 git 仓库」)。流水线会在仓库**同级**新开一个 worktree 目录 `<仓库名>-pipe-<id>`、建一条分支 `agent-hub/pipe-<id>`(基线 = 你当时所在的分支)。

### 2. 建流水线

顶栏「**+ 新建流水线**」→ 选项目 →(名称可留空,默认取第一步名)→ 选**步骤来源**:

- **模板预填**:内置三个模板,选一个即把它的步骤铺进下面可编辑的步骤列表:
  - **写代码**:plan(写 PLAN.md)→ implement(按 PLAN.md 改代码并 git 提交)→ review(diff 基线、结论写 REVIEW.md,首行 PASS/FAIL)
  - **写文章**:起草 DRAFT.md → 查证写 CHECK.md → 按意见修订 DRAFT.md
  - **讨论**:主张 → 反驳 → 综合(都写进 DISCUSS.md)
  - 「任务 / 目标」框里填的东西会**烤进第一步的 `{task}`**。
- **从大纲导入**:填一个本机 `OUTLINE.md` 路径 →「解析大纲」。按**同一重复层级的 `##` 标题** / 编号 / 勾选项**自动切成步骤**(纯规则,不用 LLM;文档大标题、`###` 子节不单独成步);整份大纲会被复制进 worktree 作 `OUTLINE.md`,每个 agent 都能看到全局、只做自己那步。
  - 想让大纲一开始就切得干净:写它的时候用 Claude Code 的 **`/pipeline-outline`** skill——把"步骤=同一级 `##`、非步骤归入 `## 0.`、输出路径/worktree 决策显式化、禁过度声明"这些规矩交到**作者**手里,大纲生下来就 pipeline-ready,不需要事后整理。

无论哪种来源,下面的**步骤列表都能改**:改名、换 provider(claude / codex …)、改 prompt、加 / 删步骤、`↑↓` 排序。步骤列表下面有个「**全自动**」勾选框:**勾了**就无人值守一路跑完、事后看日志复查;**不勾**(默认)每步过审。确认后点「**创建并开始**」。

### 3. 跑起来:headless + 日志,是否逐步过审可选

流水线卡片按步显示进度,每步状态:`待开始 → 进行中 → 待批准 → 已完成`。

- 每步在 worktree 里 **headless 运行**,输出实时 tee 进日志;点该步的「**日志**」按钮就能看(prompt + 完整输出 + 退出码),**全程不用进任何终端**。
- **完成判定**看日志里的 `<<<AGENT-HUB-STEP-DONE exit=N>>>` 哨兵(进程退出)——可靠、可复查。
- **逐步过审(默认)**:某步干完转 **待批准**,你看完日志点「**批准 → 下一步 ▶**」下一步才启动;整条走完转「**已完成**」。
- **全自动**(建时勾了):自动一路跑完;但**某步非零退出会停在「待批准」**,不会盲目往下。
- 真想看现场也行:点「**跳到终端**」进去——就是普通 `hub-` 席位,tmux / handmux 里也在(headless 进程在里面跑,看完即可,无需操作)。

### 4. 中止 / 删除 / 取回成果

- 「**中止**」:结束这条流水线的所有 agent,但 **worktree 和分支都保留**,方便你去查看或合并成果。
- 「**🗑 删除**」:先中止(若在跑)、清掉它的 agent,并**移除 worktree 目录 + 日志**(分支仍保留);流水线记录一并删掉。
- **成果在哪**:全在那条 `agent-hub/pipe-<id>` 分支上。回你仓库里 `git merge` / `git cherry-pick` 它即可;`git worktree list` 能看到目录路径。删流水线只删 worktree**目录**,不删分支——所以删了也不丢活儿。

---

## 四、共享 socket 的一个副作用

席位现在和你日常 tmux **同处一个 server**,所以:

- 你普通 `tmux ls` 里会看到 `hub-项目-席位-xxxx` 这些会话(老席位重启后自动改成这种可读名)。
- `tmux attach` 可能先落到你自己的会话上(切一下即可)。
- handmux 会列出**所有**默认 socket 的会话(你的 + agent_hub 的)。

这是"手机能直接看到席位"的必然代价。**破坏性操作仍安全**:agent_hub 只会 kill 名字登记在自己 DB 里的会话(全是 `hub-` 前缀),碰不到你别的东西。

> 想重新回到完全隔离?设环境变量启动即可,handmux 就看不到席位了:
> ```sh
> AGENT_HUB_TMUX_SOCKET=agent-hub ./run.sh
> ```

---

## 五、排错

- **`tmux attach` 报 `no sessions`** —— 还没启动任何席位。先在看板点「启动」再 attach。
- **粘贴带 `=` 的 attach 命令在 zsh 里报 `... not found`** —— 已修:命令里的目标带单引号(`-t '=hub-…'`),别把引号删掉。
- **手机 handmux 里看不到席位** —— 确认 agent_hub 没设 `AGENT_HUB_TMUX_SOCKET`(即用默认 socket),且席位已「启动」。
- **手机接入后桌面视图变窄** —— 已设 `window-size largest`,正常不会;若仍出现,确认 tmux ≥ 3.0。
- **看板显示"未检测到 tmux"** —— PATH 里找不到 `tmux`。前台跑没事;作为 launchd 服务跑时,
  `./run.sh install` 已把安装时的 `$PATH` 烤进 plist,换了 tmux/node 安装位置后重跑一次 `install` 即可。
- **建流水线报「v1 隔离需要 git 仓库」** —— 该项目根目录不是 git 仓库。`cd` 进去 `git init` 并至少提交一次再建。
- **流水线某步卡在「进行中」不动** —— 那步的 agent 多半在等你输入(claude 许可弹窗之类)。点该步「跳到终端」进去看:编排器只读、不会替你回答;等它答完、屏幕落静,该步才会转「待批准」。
- **手机点链接 / 扫码后还是要手输 token** —— stock handmux 的 web 端不读 URL 里的 `?token=`(只认 localStorage 里手动输入的 token),换隧道域名后 localStorage 又清空。解法见上面 **二 · 一键登录** 一节的 `index.html` 注入补丁(`~/bin/handmux-up` 启动时会自愈重注)。

---

## 六、测试

```sh
./run.sh test     # 跑在隔离的 agent-hub-test socket 上,不碰你真实 tmux
```
