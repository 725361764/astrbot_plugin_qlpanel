# astrbot_plugin_qlpanel —— 青龙面板对接·签到插件（3D壁纸 + 子比网站）

**单面板共享架构**：管理员绑定自己的青龙面板，所有用户共用面板上的签到脚本。
用户上传自己的 Cookie → 自动合并写入青龙环境变量 → 手动或每天定时运行签到脚本 → 运行结果按账号推送给每位用户。

内置两个签到功能：
1. 📱 **3D壁纸(3gbizhi)**：所有用户签同一个站，Cookie 合并 `||` 写入 `BIZHI_COOKIE`
2. 🌐 **子比主题(Zibll)网站**：每位用户绑定**自己的网站**，配置合并写入 `ZIBI_CONFIG`

> **每用户可绑定多个账号**：3D壁纸每个 Cookie 是一个账号，子比每个网站是一个账号。
> `/3d set`、`/zibi set` 均为**追加**，不会覆盖旧账号；`/3d del <序号>`、`/zibi del <序号>` 删除指定账号。
> 日志推送按账号归属精确隔离：每个用户只收到自己的账号结果。

## 功能

- 📱 **3D壁纸签到**：内置适配 `3gbizhi.py`（多账号版，环境变量 `BIZHI_COOKIE`，`||` 分隔）
- 🌐 **子比网站签到**：内置适配 `zibi_check_in.py`（多账号版，环境变量 `ZIBI_CONFIG`，JSON 数组），自动提取 `wordpress_logged_in` 登录态
- 👥 **每用户多账号**：3D壁纸可绑多个 Cookie、子比可绑多个网站，上传是追加不是覆盖；`/3d me`、`/3d del <序号>` 管理自己的账号
- 🚀 **一键初始化**：管理员 `/ql init` 自动检测连接、创建定时任务、添加订阅拉取脚本
- 🔑 **双认证模式**：支持 OpenAPI（Client ID/Secret，推荐）和系统 API（用户名密码）
- 📎 **上传即注册**：用户上传 Cookie（命令或直接发 txt 文件）
- 🚀 **一键运行**：`/3d run`、`/zibi run` 触发青龙任务，轮询日志并按账号推送结果
- ⏰ **个人定时**：每位用户可自定义签到时间（`/3d time HH:MM`，与子比共用），也可跟随全局
- 🔔 **推送开关**：每位用户可独立开启/关闭结果推送（`/3d push on/off`）
- 👥 **多用户隔离**：每位用户只收到自己账号的签到结果，看不到其他用户数据
- 🔄 **Token 自动续期**：青龙 Token 失效自动重新登录

## 快速开始（3 步）

### 1. WebUI 配置面板

在 AstrBot WebUI → 插件管理 → 本插件 → 插件配置中填写：

| 配置项 | 说明 |
| --- | --- |
| `ql_url` | 青龙面板地址，如 `http://127.0.0.1:5700` |
| `ql_client_id` | **推荐**：青龙 OpenAPI Client ID（系统设置→应用设置→创建应用，权限勾选环境变量/定时任务/脚本） |
| `ql_client_secret` | 与 Client ID 配对的 Secret（密码框） |
| `ql_username` | 面板登录用户名（仅在未配置 Client ID 时使用） |
| `ql_password` | 面板登录密码（仅在未配置 Client ID 时使用，密码框） |
| `daily_time` | 全局默认定时签到时间（HH:MM，留空禁用），默认 `09:00` |
| `push_after_run` | 运行后是否推送结果，默认开 |
| `task_name` | 3D壁纸签到任务名，默认 `3gbizhi签到` |
| `env_name` | 3D壁纸脚本环境变量名，固定 `BIZHI_COOKIE` |

**认证优先级**：填了 `ql_client_id` 就走 OpenAPI（`/open/*` 接口）；否则走系统 API（`/api/*`）。

> 脚本通过青龙订阅自动拉取（仓库 `https://gitee.com/fiezhu/astrbot_plugin_qlpanel-sql.git`），无需手动上传。

### 2. 一键初始化

管理员在机器人执行：

```
/ql init
```

自动完成：
- ✅ 检测青龙连接是否正常（失败会引导检查配置）
- ✅ 创建定时任务 `3gbizhi签到`（命令 `task 3gbizhi.py`）与 `zibi签到`（命令 `task zibi_check_in.py`）
- ✅ 添加/更新青龙订阅 `astrbot_plugin_qlpanel-sql`（仓库 `https://gitee.com/fiezhu/astrbot_plugin_qlpanel-sql.git`，每天0点自动拉取）
- ✅ **订阅开启自动添加（autoAddCron + 白名单 `3gbizhi.py|zibi_check_in.py`）**：拉取后脚本自动同步进青龙「脚本管理」并自动创建任务，不再出现"只检测不添加"
- ✅ 触发订阅拉取，两个脚本自动同步到青龙脚本管理
- ✅ 检查环境变量 `BIZHI_COOKIE` / `ZIBI_CONFIG`（不存在则在用户上传时自动创建）

> 若订阅添加失败，插件会给出手动添加订阅的步骤（名称/类型/链接/定时规则）。
> 手动创建订阅时请勾选「自动添加任务」，白名单填 `3gbizhi.py|zibi_check_in.py`，否则脚本只被检测、不会同步进脚本管理。

### 3. 用户使用

```
# 3D壁纸签到（每个 Cookie 是一个账号，可多次 set 追加）
/3d set <Cookie>          # 追加一个签到账号（或直接发 txt 文件）
/3d run                   # 立即运行签到
/3d me                    # 查看我的所有账号
/3d del 2                 # 删除我的第 2 个账号

# 子比网站签到（每个网站是一个账号，可多次 set 追加）
/zibi set <网站地址> <Cookie>
/zibi run                 # 立即运行签到
/zibi del 1               # 删除我的第 1 个网站账号

# 公共设置（两个功能共用）
/3d time 08:30            # 设置个人签到时间（/zibi time 同样可用）
/3d push off              # 关闭结果推送（/zibi push 同样可用）
```

## 命令大全

### 管理员命令（/ql）

| 命令 | 说明 |
| --- | --- |
| `/ql init` | **一键初始化**：检测连接→创建任务→添加订阅拉取脚本 |
| `/ql bind <地址> <账号> <密码> [任务名]` | 聊天框绑定面板（备用，推荐 WebUI） |
| `/ql unbind` | 解绑命令绑定的面板（WebUI 配置需去 WebUI 清空） |
| `/ql status` | 面板状态、认证方式、注册人数、账号数、定时配置 |
| `/ql env list / add / del` | 面板环境变量管理 |
| `/ql cron list / run / log` | 面板任务管理 |

### 用户命令（/3d —— 3D壁纸）

| 命令 | 说明 |
| --- | --- |
| `/3d set <Cookie>` | **追加**一个签到账号（每个账号发一次，可绑定多个） |
| `/3d set` + 上传 txt 文件 | 文件内容作为 Cookie 追加 |
| `/3d run` | 立即运行签到（每人只收自己的账号结果） |
| `/3d me` | 查看我的所有账号（全局序号/Cookie/时间/推送开关） |
| `/3d del <序号>` | 删除我的指定账号（不带序号则列出账号） |
| `/3d time <HH:MM>` | 设置个人签到时间 |
| `/3d time reset` | 恢复跟随全局时间 |
| `/3d push on/off` | 开启/关闭结果推送 |
| `/3d list` | 查看全局账号列表 |
| `/3d help` | 帮助 |

### 用户命令（/zibi —— 子比网站）

| 命令 | 说明 |
| --- | --- |
| `/zibi set <网站地址> <Cookie>` | **追加**一个网站账号（Cookie 中需含 `wordpress_logged_in`，可绑定多个网站） |
| `/zibi set <网站地址>` + 上传 txt 文件 | Cookie 在文件内容里 |
| `/zibi run` | 立即运行子比签到（每人只收自己网站的结果） |
| `/zibi me` | 查看我的所有网站账号 |
| `/zibi del <序号>` | 删除我的指定网站账号（不带序号则列出账号） |
| `/zibi time <HH:MM>` | 设置个人签到时间（与 /3d 共用） |
| `/zibi time reset` | 恢复跟随全局时间 |
| `/zibi push on/off` | 开启/关闭结果推送（与 /3d 共用） |
| `/zibi list` | 查看全局账号列表 |
| `/zibi help` | 帮助 |

> 直接发送纯文件（不带命令）会追加为 3D壁纸账号。

## 运行与推送流程

1. 用户 `/3d set <cookie>`（可多次）→ 插件本地记录（`users.json`），全量合并所有用户的全部 Cookie（`||` 分隔）写入青龙 `BIZHI_COOKIE`
2. 用户 `/zibi set <网站> <cookie>`（可多次）→ 插件本地记录，合并所有用户的全部 `{"host","cookie"}` 写入青龙 `ZIBI_CONFIG`（JSON 数组，按全局账号顺序）
3. `/3d run`、`/zibi run` 或定时到点 → 插件分别触发对应青龙任务 → **触发后立即推送一条"等待运行结果"提示** → 轮询日志（遇 `任务结束` 提前结束）
4. 解析日志中 `【账号N】` 行 → 按全局账号序号聚合，**每个用户只收到属于自己账号的行**（一个用户多个账号合并为一条推送）
5. 触发者回复也只显示自己的账号结果

> 任务查找兼容两种来源：手动创建的任务（名称含 `3gbizhi签到`/`zibi签到`）与订阅自动添加的任务（命令含 `3gbizhi.py`/`zibi_check_in.py`），任一存在均可触发运行。

> 账号序号 = 全局平铺顺序（先用户注册顺序、再账号追加顺序）。用户删除账号后序号会前移，推送按当前顺序自动对应。

## 数据存储

- `data/plugin_data/astrbot_plugin_qlpanel/panel.json`：命令绑定的面板信息（全局，WebUI 配置优先）
- `data/plugin_data/astrbot_plugin_qlpanel/users.json`：注册用户列表（uid / cookies[] / zibi_sites[] / origin / 个人时间 / 推送开关），旧版单账号数据自动迁移
- Cookie 仅写入青龙环境变量与本地文件，机器人回复只显示脱敏摘要

## 青龙要求

- 版本 v2.10+ / v3.x，内置 API 开启（默认）
- OpenAPI 应用需勾选权限：环境变量、定时任务、脚本
- 面板地址形如 `http://ip:5700`（自动规范化，无需 `/api` 后缀）

## 目录结构

```
astrbot_plugin_qlpanel/
├── metadata.yaml
├── main.py
├── requirements.txt     # aiohttp
├── _conf_schema.json
├── scripts/3gbizhi.py      # 3D壁纸签到脚本（订阅拉取）
├── scripts/zibi_check_in.py # 子比网站签到脚本（订阅拉取）
└── README.md
```

## 免责声明

本插件仅用于在**自己拥有或已获授权**的青龙面板上运行签到脚本。Cookie 属敏感凭据，请妥善保管。
