# Agent Hub 使用说明

> ## 🚀 出门在外速查:natapp 太卡怎么办(exit node + handmux)
>
> 场景:在外面用手机连 handmux(走 natapp),但工作必须开着 Tailscale exit node ——
> 此时 natapp 的流量会绕经 exit node,变得很卡。用 `natapp-bypass` 把 **只有 natapp 的流量**
> 拉直(走物理网关),工作流量继续走 exit node。
>
> ```sh
> handmux start                 # 1) 起隧道
> sudo natapp-bypass            # 2) 另开一个终端,常驻(Ctrl-C 停)
> # 看到 “✓ natapp <IP> → 直连 via <网关>” 后,重启一次 handmux 让 natapp 走新路
> ```
>
> - 换 WiFi / 热点、natapp 换服务器 IP → 脚本 5 秒内自动重设,不用管。
> - 验证生效:`lsof -nP -iTCP -a -c natapp -sTCP:ESTABLISHED` 的源地址应从 `100.x`(Tailscale)
>   变成物理网卡地址(如 `192.168.x`)。
> - 脚本在 `~/bin/natapp-bypass`。**在家同 WiFi 时不用它** —— 直接用 Direct(见下文「移动端」)。
> - ⚠️ 若 `127.0.0.1:19999` 突然连不上:多半是旧实例残留在别的终端里。
>   `sudo pkill -f natapp-bypass` → `sudo route -n delete -host 127.0.0.1` → 重新 `sudo natapp-bypass`。
>   (新版脚本启动时会自动拒绝重复实例、自动修复被劫持的环回路由。)
> - 想彻底免脚本:试 **Tailscale Funnel**（`https://<机器名>.<tailnet>.ts.net/`,零安装、天然绕开
>   exit node),但国内入口能否连上是未知数;通了最省心,不通就守着 `natapp-bypass`。

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

---

## 一、桌面端

### 1. 服务是开机自启的,不用手动跑

agent_hub 已注册为 launchd 服务(`~/Library/LaunchAgents/com.agent-hub.plist`):
**开机自动启动、崩了自动拉起**,地址永远固定。管理命令:

```sh
launchctl bootout gui/501/com.agent-hub                                   # 停
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.agent-hub.plist    # 起
tail -f ~/tools/agent_hub/data/hub.log                                    # 看日志
```

> 手动 `agent_hub` 命令仍可用于 `agent_hub test` 跑测试;但别在服务运行时手动
> `agent_hub`(端口会冲突)。改了后端代码想生效:`bootout` 再 `bootstrap` 一次。

### 2. 打开看板(地址永远不变,收藏一次即可)

- **好记版**:`http://agent-hub.localhost:8787`(Chrome 直接可用)
- 标准版:`http://127.0.0.1:8787`

**不需要带 token**——页面自动注入。右上角显示"已连接"即正常。

**状态一目了然**:
- 顶栏中间是**全局状态栏**(彩点+数字:工作中/等输入/空闲/已退出);
- 每个项目标题行也有同款迷你彩点(鼠标悬停看含义);
- 浏览器标签标题:`(2⚠ 1✓) Agent Hub` = 2 个等输入、1 个空闲(完成);
- **macOS 系统通知**:席位**开始等输入**、或**干完活回到空闲**时各弹一次
  (完成通知有防抖,至少连续工作 6 秒才算)。不想要:服务环境变量加 `AGENT_HUB_NOTIFY=0`。
- **「最近推送」条**(顶栏和第一个项目之间):按时间倒序记录最近产生推送的状态变动
  ——谁开始等输入(琥珀点)、谁干完活回到空闲(灰点)。**错过弹窗也能回溯是哪个 agent
  要处理**;点其中一条即可跳到那个席位(席位已移除的不可点)。没有任何推送时这条自动隐藏。
  即使关了系统通知(`AGENT_HUB_NOTIFY=0`)也照常记录,只是不弹 banner。
  - **处理完就归档**:鼠标悬停某条,右侧出现 **✕** 点一下即从列表移除;标题右边「**清空**」
    一次归档当前所有条目。归档是**软删除**(只是不再显示,DB 里留痕、可回溯),**不影响
    对应 agent**;归档后若那个 agent 再次触发推送,会作为新条目重新出现。

### 3. 建项目

右上「**+ 新建项目**」→ 填 `项目名` + `根目录(绝对路径)`,如 `/Users/quzhijie/tools/xxx`。

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
> 现在席位和你日常 tmux 同处一个 server,`tmux attach` 可能先落到你自己的某个会话上,切一下即可。

### 7. 跳到某个 agent

看板点席位卡的「**跳到终端**」→ 取景器终端**瞬间切**到这个 agent,同时**那个
Terminal 窗口自动弹到最前、选中正确的 tab**——不用在一堆窗口里找。网页上只闪一个
小提示,不弹窗打断。

> - jump 会挑**最宽**的那个 client(即桌面),所以哪怕手机也连着,桌面这颗按钮也只驱动桌面。
> - 自动置前支持 Terminal.app 和 iTerm2。首次使用如果提示"python 想要控制 Terminal",
>   点允许;拒绝了想恢复:系统设置 → 隐私与安全 → 自动化。置前失败不影响切换本身。

### 8. 看状态

看板每 2.5 秒自动刷新:`工作中 / 等待输入 / 空闲 / 已退出 / 状态未知`,每张卡带最近输出预览。有 agent 在"等待输入"时,项目标题会提示 `N 个等输入`。

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

### 2. 出门在外:开公网隧道

```sh
handmux start --tunnel cloudflare      # 即时公网 HTTPS(自动装 cloudflared)
```

> ⚠️ **安全须知**:公网隧道 = 把一个"能跑任意命令的真实终端"挂到互联网,**唯一的门就是 URL 里那个 token**,边缘没有额外鉴权。请务必按下面硬化。

### 3. 隧道硬化(强烈建议)

- 优先用**具名隧道 + Cloudflare Access**:
  ```sh
  handmux start --tunnel cloudflare-named --cf-hostname hub.你的域名.com
  ```
  然后在 Cloudflare Zero Trust 里给这个 hostname 加一条 **Access 策略**(邮箱/SSO 验证)——
  这才是真正的鉴权墙,而不只靠 token。
- token 当密码保管:别截图二维码乱发;泄漏就用 `--token` 换一个。
- 用完就关:不需要时把隧道进程停掉,别长期挂着。
- 国内 Cloudflare 公共边缘不稳,可改用 `--tunnel natapp` / `--tunnel cpolar`(需各自的 authtoken)。

### 4. 更省心的替代:Tailscale(不开公网)

若你有 [Tailscale](https://tailscale.com):手机和 Mac 组私网后,**不用隧道**——

```sh
handmux start        # 不加 --tunnel
```

手机浏览器直接开 `http://<Mac的-tailscale-IP>:19999/…`,出门在外照样用,但**什么都不暴露公网**,比公网隧道安全一档。

### handmux 常用参数

| 参数 | 说明 |
|---|---|
| `--tunnel` | `cloudflare` / `cloudflare-named` / `ssh` / `natapp` / `cpolar` |
| `--port` | 端口,默认 `19999` |
| `--host` | 绑定地址,默认 `0.0.0.0` |
| `--token` | 自定义/重置令牌(默认自动生成) |
| `--no-qr` | 不打印二维码 |
| `-f` / `--foreground` | 前台运行 |

---

## 三、共享 socket 的一个副作用

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

## 四、排错

- **`tmux attach` 报 `no sessions`** —— 还没启动任何席位。先在看板点「启动」再 attach。
- **粘贴带 `=` 的 attach 命令在 zsh 里报 `... not found`** —— 已修:命令里的目标带单引号(`-t '=hub-…'`),别把引号删掉。
- **手机 handmux 里看不到席位** —— 确认 agent_hub 没设 `AGENT_HUB_TMUX_SOCKET`(即用默认 socket),且席位已「启动」。
- **手机接入后桌面视图变窄** —— 已设 `window-size largest`,正常不会;若仍出现,确认 tmux ≥ 3.0。

---

## 五、测试

```sh
./run.sh test     # 跑在隔离的 agent-hub-test socket 上,不碰你真实 tmux
```
