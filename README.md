# Aliyun Telegram Traffic Monitor

纯 Telegram Bot 的阿里云月度流量监控与自动关机工具。没有网页面板，不需要 Nginx、PHP、数据库或 Docker。

Monitor Aliyun CDT / SWAS monthly traffic from a Telegram bot, with automatic shutdown before the free quota runs out. No web panel, no database — one Python service plus a colored terminal management panel.

针对「多个独立阿里云账号，每个账号一台 ECS，各自使用 CDT 免费流量额度」的场景设计，机器数量不设上限。

```text
阿里云账号 A → ECS A（香港）  → CDT 200 GB
阿里云账号 B → ECS B（新加坡）→ CDT 200 GB
```

## 两个面板

**Telegram 面板**是一块会自我刷新的单条消息。每次操作都在原消息上重绘，不会刷屏。

每台机器是一张独立的卡片（Telegram 引用块），信息按「是什么 → 用了多少 → 会不会超 → 保护开着吗」四行排布：

```text
📊 阿里云流量监控
账期 2026-07 · 11 天后重置
🛰 每 5 分钟检查 · 🟢 08:20
合计 340.9 / 400 GB · 85.2%

▏🟡 Aliyun HK-1 · 运行中
▏▰▰▰▰▰▰▰▰▱▱  79.2%
▏158.32 / 200.00 GB · 余 41.68 GB
▏📈 日均 8.3 GB · 月底约 191 GB
▏⚠️ 预计 07-24 触及熔断线
▏🛡 熔断 95% 开 · 🗓 次月开机 关

▏🔴 Aliyun SG-1 · 已关机
▏▰▰▰▰▰▰▰▰▰▱  91.3%
▏182.54 / 200.00 GB · 余 17.46 GB
▏📈 日均 9.6 GB · 月底约 219 GB
▏🛡 熔断 90% 开
▏🛑 本账期已触发熔断 · 🗓 次月开机 开

[🔄 刷新全部]   [📋 事件记录]
[🟡 Aliyun HK-1 79%] [🔴 Aliyun SG-1 91%]
[🛠 全局设置]   [ℹ️ 帮助]
```

进度条和卡片样式在 `🛠 全局设置` 里有两个切换按钮，点一下换一种，回主面板即可看到效果——同一串字符在 iOS、Android、桌面版 Telegram 的渲染差别很大，挑一个在你自己手机上顺眼的：

| 进度条 | 样子 | 说明 |
|---|---|---|
| `▰▱ 细条` | `▰▰▰▰▰▱▱▱▱▱ 46.0%` | 默认，克制 |
| `█░ 实心` | `█████░░░░░ 46.0%` | 最粗最清楚，3.2 及以前的样式 |
| `━─ 细线` | `━━━━━───── 46.0%` | 最轻，接近一条进度线 |
| `🟩 彩块` | `🟩🟩🟩⬜⬜⬜⬜ 46.0%` | 手机上最醒目，方块本身按风险变 🟩→🟨→🟥 |

| 布局 | 说明 |
|---|---|
| `卡片式` | 默认，每台机器一个引用块，有底色和左侧竖线 |
| `平铺式` | 不用引用块，纯文本 + 空行分隔，部分客户端里更干净 |

**一个符号只表达一件事**：🟢/🟡/🔴 恒定表示**流量风险**；开关机状态一律用「运行中 / 已关机」文字表达（旧版主面板同一行有两个圆点，一个是电源、一个是风险，很容易读反）。标题行的圆点则是**数据新鲜度**——绿色代表刚更新，红色代表监控本身可能出了问题。

主面板是只读总览：月底用量与触线日期按 24 小时 / 7 日滚动均值预测，**没有风险时不显示风险行**，警告因此一眼可见。开关机、重启在点入机器详情页后操作，且都有二次确认；自动熔断采用双阈值——普通线（默认 95%）需连续两次检查确认，紧急线（默认 98%）一次即关，防止单次异常数据误关机，两条线现在都能在详情页用按钮直接调。`📋 事件记录` 保留最近 50 条熔断、开关机、配置修改与查询异常。所有层级都有 `🏠 返回主面板`。

**终端面板**（`sudo aliyun-monitor`）顶部是一条实时状态栏，读的是服务写入的 `state.json`，秒开不联网：

```text
── Aliyun Traffic Monitor 3.0.0 ────────────────────────────────
  服务 ● 运行中    账期 2026-07 · 11 天后重置    机器 2 台

  ● Aliyun HK-1   ███████████░░░  79.2%  158.32 GB / 200.00 GB  熔断 95% · 7 分钟前
  ○ Aliyun SG-1   █░░░░░░░░░░░░░   9.8%   19.56 GB / 200.00 GB  熔断 90% · 7 分钟前
────────────────────────────────────────────────────────────────
  1) 管理配置与机器
  2) 一键自检 / 诊断
  3) 查看实时日志
  4) 重启服务
  ...
```

## 查询超时怎么办

阿里云 API 偶发超时是常态，不是配置问题。程序按「抖动会自愈」这个前提设计：

| 机制 | 行为 |
|---|---|
| 单次调用重试 | 4 次尝试，退避 3/6/12 秒（带抖动），总时长上限 120 秒 |
| 端点兜底 | ECS 状态查询第 2、4 次改用中心端点 `ecs.aliyuncs.com` |
| 错误分类 | 超时/限流/5xx 才重试；AccessKey 错、权限不足、实例不存在直接返回 |
| 快速重试 | 一轮失败后 45 秒只重查失败的机器（`retry_delay_seconds`） |
| 部分成功 | 流量读到了就保留，只有状态读失败时降级为「状态未知」 |
| 通知去噪 | 连续 3 次失败才发通知（`error_notify_after_failures`），恢复自动撤回 |
| 事件记录 | 单次抖动不记录，只有构成真实故障的连续失败才留档 |

排查某次失败的具体原因：

```bash
sudo journalctl -u aliyun-traffic-bot | grep -E "读超时|连接超时|被限流|权限"
```

错误信息带 API 名和中文原因，例如 `ListCdtInternetTraffic 读超时: ...`——能直接看出是流量接口还是状态接口在抖。

如果失败频率明显偏高（每天十几次以上），通常是管理机到阿里云的网络质量问题，而不是程序问题。可以把 `interval_seconds` 调大（查询次数少，撞上抖动的概率也低），或把 Bot 换一台网络更稳的机器。

## 3.4 的主要变化

- 进度条 4 种、卡片布局 2 种，在 Telegram 里点按钮直接切换（`panel_style` / `bar_style`）；
- 彩块进度条按风险变色 🟩→🟨→🟥，颜色直接长在进度条上；
- 顶部合计行带自己的进度条，取所有机器里最坏的风险等级着色；
- 百分比不再右对齐补空格——没有机器超过 100% 时那个空隙看着像 bug。

## 3.3 的主要变化

- 面板视觉重做：机器卡片化、符号语义唯一化、行长收敛到不折行；
- 账期按**阿里云计费时区**（Asia/Shanghai）翻月，不再跟随显示时区；
- 终端面板改完配置后，Bot 会**热加载 config.json**，不再有覆盖窗口；
- 已停用的机器重新出现在主面板按钮里，可以直接在 Telegram 里启用；
- 消息超长、渲染被拒、回调异常都有兜底，不会再把面板卡死。

## 3.0 的主要变化

- Telegram 面板改为单消息原地刷新，层级导航，全部带返回；
- 主面板直接展开每台机器的进度条与全部关键状态，两台机器无需再点进去；
- 阈值支持 ±1% / ±5% 快调；月度额度、提醒线、汇总时间、管理员都能在 Telegram 里直接改；
- **录入 AccessKey 后立即调用阿里云 API 验活**，填错当场知道，不用等 Telegram 报错；
- 新增 `aliyun-monitor doctor` 一键自检：配置结构、文件权限、systemd、时区账期、Telegram Token、每个账号的阿里云 API 连通性；
- 终端面板顶部彩色实时状态栏；
- 重启后从 `state.json` 恢复上次读数，面板不再显示「尚未查询」；
- 修复 `python-telegram-bot 22.8` 实际要求 Python 3.10+ 导致 Debian 11 安装失败；
- 修复刷新时 Telegram「message is not modified」报错、以及一次回调重复 answer 导致提示不显示。

## 计量口径：先读这一段

CDT 统计的是**整个阿里云账号**的公网流量池，**不是**单台 ECS 的网卡计量。同账号下的其他 ECS、EIP、NAT 网关都会计入同一个数。一个账号只跑一台主力机时最准确 —— 这正是你的结构。

CDT 免费额度分两个池：**非中国内地 200 GB/月**，**中国内地 20 GB/月**（合计 220 GB）。香港、新加坡、日本、美国等地域都计入「非中国内地」池，所以海外机器用 `traffic_scope: overseas` + `quota_gb: 200`。

### GB 还是 GiB

程序按 **1 GB = 1024³ 字节**换算额度，所以 `quota_gb: 200` 对应 214.7 个十进制 GB。如果阿里云的免费额度实际是十进制口径，95% 阈值会落在约 204 GB —— 已经超出免费线。

**因此默认建议阈值 90%**（约 193 十进制 GB），两种口径下都安全。向导在你填完阈值后会直接算出对应的十进制 GB 并在越线时告警。

## 部署位置

把 Bot 装在**不会被自动关机的第三台管理服务器**上。如果和受监控 ECS 同机，关机后面板和下月自动开机都会一起离线。

## 安装

一键安装（升级也用同一条命令，配置自动保留并备份）：

```bash
curl -fsSL https://raw.githubusercontent.com/bear4f/aliyun-tg-traffic-monitor/main/install.sh | sudo bash
```

没有 curl 的系统用 wget：

```bash
wget -qO- https://raw.githubusercontent.com/bear4f/aliyun-tg-traffic-monitor/main/install.sh | sudo bash
```

或者传统方式：

```bash
git clone https://github.com/bear4f/aliyun-tg-traffic-monitor.git
cd aliyun-tg-traffic-monitor
sudo ./install.sh
```

系统要求：Debian 11+ / Ubuntu / Alpine / Rocky / AlmaLinux，Python 3.9+，root 权限。

首次安装进入向导：Telegram Token → 管理员 User ID → 全局设置 → 逐台添加机器（每台录完自动验活）。管道安装时向导会自动接回终端（/dev/tty），交互不受影响。

## 日常使用

```bash
sudo aliyun-monitor          # 交互面板
sudo aliyun-monitor doctor   # 一键自检（联网）
sudo aliyun-monitor check    # 结构校验（不联网）
sudo aliyun-monitor status   # 状态栏 + systemd 状态
sudo aliyun-monitor logs     # 跟随日志
sudo aliyun-monitor restart
```

## 哪些能在 Telegram 改，哪些不能

| 操作 | Telegram | 终端 |
|---|---|---|
| 查看流量 / 状态 | ✅ | ✅ |
| 开机 / 关机 / 重启（二次确认） | ✅ | — |
| 自动熔断开关、关机阈值、紧急阈值 | ✅ | ✅ |
| 月度额度、下月自动开机、启用/停用 | ✅ | ✅ |
| 提醒线、汇总时间、检查间隔、管理员 | ✅ | ✅ |
| **新增 / 删除机器** | ❌ | ✅ |
| **AccessKey / Region / Instance ID** | ❌ | ✅ |

密钥相关操作只在管理服务器本地完成，**不经过 Telegram 聊天传输**。

## Telegram 命令

```text
/menu    打开控制面板
/status  刷新并打开面板
/id      查看自己的 Telegram User ID 和 Chat ID
```

## EIP 与实例安全（3.0.1 起）

针对「公网 IP 珍贵，绝不能因脚本丢失」的场景做了三层防护：

1. **代码层**：全部代码只调用 7 个 API 动作 —— 读流量、读实例状态、开机、关机、重启。不存在任何删除实例、释放/解绑 EIP、变更计费方式的调用路径。
2. **关机模式层**：`StopCharging`（节省停机）已从代码中**彻底移除** —— 该模式会回收实例的固定公网 IP。关机硬编码为 `KeepCharging`，公网 IP 与全部资源保留，不可配置。旧配置中的 `StopCharging` 会在加载时被强制改写。注意：KeepCharging 意味着关机期间实例照常计费，这是保 IP 的代价。
3. **RAM 策略层**：`ram-policy-ecs-cdt.json` 在最小 Allow 之外增加了**显式 Deny**（`ecs:DeleteInstance`、`vpc:ReleaseEipAddress`、`vpc:UnassociateEipAddress` 等）。RAM 中 Deny 永远压过 Allow —— 即使这个 RAM 用户日后被误加了更宽的权限，删机和释放 EIP 依然被挡住。

`aliyun-monitor doctor` 会输出「实例 / EIP 安全」一节，逐条确认以上防线，并在发现旧配置残留 `StopCharging` 时提醒。

## RAM 权限

每个阿里云账号单独创建 RAM 用户，**不要用主账号 AccessKey**。策略见 `ram-policy-ecs-cdt.json`：

```text
Allow:
  cdt:ListCdtInternetTraffic
  ecs:DescribeInstances
  ecs:StartInstance / StopInstance / RebootInstance
Deny（防护层，压过一切 Allow）:
  ecs:DeleteInstance / DeleteInstances
  ecs:ModifyInstanceChargeType
  vpc:ReleaseEipAddress / UnassociateEipAddress / DeleteEipAddress
```

## 熔断安全规则

- 流量查询失败时**绝不**执行自动关机；
- 状态读取失败（流量正常）时也不会盲目关机，但若流量已超熔断线会发「需要人工确认」通知，并保持熔断待触发，状态恢复后立即执行；
- 实例状态不是 `Running` 时不会重复发送关机指令；
- 同账期熔断一次后不再重复触发；
- 手动开机时若流量仍高于阈值且熔断开启，会被拦截并说明原因；
- 新账期自动开机必须先确认流量已低于确认线（默认 ≤10%），而不是仅凭日历翻月；
- 账期翻月按阿里云计费时区 `Asia/Shanghai` 判定（3.3 起），显示时区只影响时间戳的展示；
- 阿里云控制台仍应同时配置费用预算告警，作为第二道防线。

## 文件

```text
/opt/aliyun-traffic-bot/config.json   配置与密钥（600）
/opt/aliyun-traffic-bot/state.json    告警与账期状态（600）
/opt/aliyun-traffic-bot/monitor.log   程序日志
/opt/aliyun-traffic-bot/app.py        Telegram Bot 与监控循环
/opt/aliyun-traffic-bot/panel.py      终端面板
/opt/aliyun-traffic-bot/common.py     共享的阿里云 API 与格式化层
```

服务以独立系统用户 `aliyunmon` 运行。

## 升级与卸载

```bash
sudo ./install.sh    # 保留配置并自动备份
sudo ./uninstall.sh  # 询问是否删除含密钥的配置目录
```
