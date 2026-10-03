# autoLCDE —— LCDE 剧本生成器（独立版）

**给它一个主题，它调用外部大模型 API 写出一部 LCDE 剧本**，编译成引擎能读的规范文档，
生成占位素材与游戏文件，再用内置校验器自检；**校验出硬错误就把错误原文回喂给模型重写**。

这个包是**独立**的：不需要 LCDE 仓库、不需要安装任何第三方库，包里自带格式内核。

```
autoLCDE-<版本>\
├─ autoLCDE.py            命令行入口（python autoLCDE.py …）
├─ autoLCDE.cmd           Windows 双击/命令行启动器（自动找 python）
├─ autoLCDE\              生成器本体
├─ lcde\ + lcde.py      内置的 LCDE 格式内核（构建/校验游戏文件，可单独用）
├─ schema\              规范格式的 JSON Schema
├─ LICENSE              MIT 许可
├─ examples\            离线生成样例（不需要密钥就能看它产出什么）
└─ build-info.json      打包信息（版本、构建时间、来源）
```

也可以只用 **单文件版** `autoLCDE.pyz`：一个文件拷走就能跑（`python autoLCDE.pyz …`）。
或者用 **免装 Python 的 exe**：`autoLCDE.exe --help`（27 MB，机器上完全不需要 Python）。

---

## 一、要求

* **便携目录 / `.pyz`**：需要 **Python 3.9 或更高**（`python --version` 能跑就行）。
  没有任何第三方依赖。
* **`autoLCDE.exe`**：**不需要 Python**，双击或命令行直接跑（自带的运行时在里面）。
* 一个**大模型 API 密钥**（除 `--provider mock` 之外都需要）。
* Windows / macOS / Linux 都行；路径与编码都按各自平台处理。

### 三种形态怎么选

| 形态 | 适合 | 说明 |
| --- | --- | --- |
| `autoLCDE.exe` | **发给不装 Python 的人** | 单文件、直接跑、启动稍慢（每次要解包），约 27 MB |
| `autoLCDE.pyz` | 自己用、机器上有 Python | 一个 0.13 MB 的文件，`python autoLCDE.pyz …` |
| 便携目录 | 要改提示词 / 看源码 | 解压即用，`python autoLCDE.py`，附带示例与内置内核 |


---

## 二、30 秒上手

```powershell
# 1) 填密钥（推荐环境变量；下面三个任选其一）
$env:DEEPSEEK_API_KEY = "sk-..."

# 2) 先确认密钥能用（会真的发一次最小请求）
python autoLCDE.py doctor --ping

# 3) 生成一部剧本
python autoLCDE.py gen --premise "四等小站的末班车停了十年，可每个雪夜都有人来等"

# 4) 或者开网页界面，表单填完点按钮
python autoLCDE.py web
```

**先不花钱看看它会产出什么**（离线、不要密钥）：

```powershell
python autoLCDE.py gen --provider mock --premise "随便写点什么" --scenes 3
# 用 exe 的话把 `python autoLCDE.py` 换成 `autoLCDE.exe` 即可
autoLCDE.exe gen --provider mock --premise "随便写点什么" --scenes 3
```

内置的是一段原创短剧《末班雪》，走的是完全相同的流水线（含校验与回喂闭环），
`examples\` 里已经有一份现成的产物可以直接翻。

装进游戏：把产出目录里 `save\` 下的 `Character/`、`BG/`、`Dialog/` 合并到
`%USERPROFILE%\AppData\LocalLow\Soalin\LCDE` 即可（覆盖前会自动留备份）。

---

## 三、密钥怎么给

优先级从高到低：

```powershell
# 1) 环境变量（推荐）
$env:DEEPSEEK_API_KEY   = "sk-..."     # DeepSeek（默认提供方）
$env:OPENAI_API_KEY     = "sk-..."     # OpenAI
$env:MOONSHOT_API_KEY   = "sk-..."     # Kimi
$env:SILICONFLOW_API_KEY= "sk-..."     # 硅基流动
$env:DASHSCOPE_API_KEY  = "sk-..."     # 通义
$env:AUTOLCDE_API_KEY     = "sk-..."     # 通用兜底 / 自建服务

# 2) 命令行（会留在命令历史里，谨慎）
python autoLCDE.py gen --premise "..." --api-key sk-...

# 3) 配置文件（跟随本包：autoLCDE.config.json）
python autoLCDE.py init          # 生成模板，然后把 apiKey 填进去
```

密钥在**任何日志与报告里都会打码**（`sk-abc…f123`），网页界面回显的也是打码值。

支持的提供方（`python autoLCDE.py providers` 看全部）：

| provider | 默认模型 | 密钥环境变量 |
| --- | --- | --- |
| `deepseek`（默认） | `deepseek-chat` | `DEEPSEEK_API_KEY` |
| `openai` | `gpt-4o-mini` | `OPENAI_API_KEY` |
| `moonshot` | `moonshot-v1-32k` | `MOONSHOT_API_KEY` |
| `siliconflow` | `deepseek-ai/DeepSeek-V3` | `SILICONFLOW_API_KEY` |
| `dashscope` | `qwen-plus` | `DASHSCOPE_API_KEY` |
| `custom` | 自己填 | `AUTOLCDE_API_KEY` |
| `mock` | —（离线，不需要密钥） | — |

任何 **OpenAI 兼容**的中转/自建服务都能接：

```powershell
python autoLCDE.py --provider custom --base-url https://your-host/v1 --model 模型名 gen --premise "..."
```

---

## 四、命令

| 命令 | 作用 |
| --- | --- |
| `providers` | 列出提供方、接口地址、默认模型、密钥环境变量 |
| `init` | 写出配置文件模板 `autoLCDE.config.json` |
| `doctor [--ping]` | 检查配置与密钥；`--ping` 会真的调一次 |
| `plan --premise "..."` | **只做概念设计**（选角 / 背景 / 音频 / 场次表），便宜，用来先定骨架 |
| `gen --premise "..."` | 一条命令跑完：生成 → 编译 → 占位素材 → 游戏文件 → 校验 → 回喂重写 |
| `gen --concept 概念.json` | 复用 `plan` 审过的骨架，跳过概念阶段的调用 |
| `compile 剧本.json` | 从中间表示重新编译，**不再调 API**（改台词用这个） |
| `web` | 本地网页界面（默认 http://127.0.0.1:8756） |

`gen` / `plan` 常用选项：

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--premise` / `--premise-file` / 直接跟在命令后 | — | 主题梗概（必填；长篇设定用文件） |
| `--title` / `--style` / `--tone` | — | 标题 / 风格致敬 / 情绪基调 |
| `--scenes` / `--cast` / `--duration` | 8 / 4 / 12 | 场次数 / 角色数 / 目标时长（分钟） |
| `--scenes-per-call` | 2 | 每次 API 调用连写几场（1 最稳，3~4 更连贯更省调用） |
| `--constraints` | — | 硬性约束，如「全年龄；不要出现死亡」 |
| `--out` / `--save-dir` | `projects\<标题>` / `<产出目录>\save` | 产出目录 / 游戏文件目录 |
| `--no-build` | 关 | 只出 JSON，不写游戏文件、不校验 |
| `--repair-rounds` | 2 | 校验失败时回喂重写的最大轮数（0 = 关闭） |
| `--x-left/--x-center/--x-right` | 220/400/580 | 立绘横向槽位（舞台像素 0..800） |
| `--dry-run` | 关 | 照常调 API，但不写任何文件 |

---

## 五、产出物

```
projects\<标题>\
├─ <标题>.json          LCDE 规范文档（交给引擎/工具链）
├─ 剧本.json            中间表示（IR）—— 手工改它，再 compile，不必重调 API
├─ 概念.json            概念阶段的原始输出（选角/背景/音频/场次表）
├─ raw\剧本raw.txt      人读剧本（△动作 / 台词 / 【字幕】 / ［分镜重点］ / ⟨演出指令⟩）
├─ 立绘清单.md          交给美术：每个表情的文件名、尺寸、出现次数与出处
├─ 分镜备注.md          引擎表达不了的运镜与音频需求
├─ 生成报告.md          本次用了哪个模型、多少 token、哪些地方被自动修正、校验结果
└─ save\                可直接合并进游戏存档目录
```

想改台词：改 `raw\剧本raw.txt` 是为了看，改 `剧本.json` 才是真的改；改完

```powershell
python autoLCDE.py compile projects\某剧本\剧本.json
```

重新编译并**自动再校验一遍**（不消耗 token）。要重跑结构就重新 `gen`。

---

## 六、包里那个 `lcde.py` 是什么

它是 LCDE 格式内核的命令行入口，autoLCDE 全程都在用它（命令表、字节布局、校验规则、占位图）。
单独拿出来也能用，例如检查一份游戏存档目录：

```powershell
python lcde.py env                                   # 看存档目录在哪、里面有什么
python lcde.py validate                              # 检查一致性与潜在崩溃
python lcde.py --save-dir "D:\某存档目录" validate
python lcde.py story list
python lcde.py project export -o 全量.json
python lcde.py project build 全量.json --dry-run     # 看看会写哪些文件
```

`validate` 会模拟运行时状态（屏幕上现在有哪些立绘、下标是否越界），
能抓出**必然崩溃**的剧本。退出码 `0` 通过、`2` 有错误。

---

## 七、网页界面

```powershell
python autoLCDE.py web                    # http://127.0.0.1:8756
python autoLCDE.py web --port 9000 --open
python autoLCDE.py web --token 我的口令    # 访问 URL 需要带 ?token=我的口令
```

* 默认**只监听本机**；要给别人用加 `--host 0.0.0.0`，并**务必**配 `--token`。
* 网页里填的密钥只留在内存，勾「记住到配置文件」才会落盘；回显一律打码。
* 生成在后台跑，关掉页面不影响；重新打开看日志即可。
* 「测试连接」按钮会真的发一次最小请求（约 10 个 token）。

---

## 八、故障排查

| 现象 | 处理 |
| --- | --- |
| `没有找到 API 密钥` | 设环境变量 / `--api-key` / 写进 `autoLCDE.config.json`；或 `--provider mock` 先试 |
| `HTTP 401 / 403` | 密钥不对或没权限；`doctor --ping` 复验 |
| `HTTP 404` | `base_url` 或 `model` 写错（`base_url` 要写到 `/v1` 这一层） |
| `HTTP 429` | 限流或余额不足；工具会自动退避重试，仍失败就减少 `--scenes-per-call` |
| `服务端不支持 JSON 模式` | 工具会自动去掉 `response_format` 重试；也可 `--no-json-mode` |
| `模型没有返回可解析的 JSON` | 换更强的模型，或把 `--scenes-per-call` 降到 1 |
| 控制台中文乱码 | 工具已按终端编码自适应；若仍乱码，`chcp 65001` 后再跑 |
| 立绘位置偏 | 改 `--x-left/--x-center/--x-right`（舞台横向 0..800） |
| 不想污染当前目录 | `--out D:\某处\项目名`，产出就只落在那儿 |

---

## 九、目录

| 文件 | 作用 |
| --- | --- |
| `autoLCDE.py` | 命令行入口 |
| `autoLCDE/cli.py` | 子命令与参数 |
| `autoLCDE/config.py` | 提供方预设、密钥解析（命令行 > 环境变量 > 配置文件 > 预设）、打码 |
| `autoLCDE/paths.py` | 运行模式探测（仓库 / 独立包 / 单文件）与默认落点 |
| `autoLCDE/llm.py` | OpenAI 兼容客户端：重试退避、JSON 抽取、token 记账 |
| `autoLCDE/prompts.py` | 三阶段提示词 + 引擎能力清单（想调风格就改这里） |
| `autoLCDE/ir.py` | 剧本中间表示与容错归一化 |
| `autoLCDE/compile.py` | IR → LCDE 规范文档（立绘下标状态机） |
| `autoLCDE/art.py` | 人读剧本 / 立绘清单 / 分镜备注 / 生成报告 |
| `autoLCDE/pipeline.py` | 编排与「校验失败 → 回喂重写」闭环 |
| `autoLCDE/mock.py` | 离线提供方（内置原创短剧，也是自测夹具） |
| `autoLCDE/web.py` | 本地网页界面（无前端构建） |
| `autoLCDE/package.py` | 打包器（本包就是它产出的） |
| `autoLCDE/bridge.py` | 与内置格式内核的桥接 |
| `lcde/` | 内置的 LCDE 格式内核（原样，未改动） |

---

## 十、附：重新打包

本包由 `autoLCDE/package.py` 生成，含打包自检（在新目录里真跑一次离线生成并校验）。
若你改了提示词或参数想重出一份：

```powershell
python autoLCDE.py pack --out ..\dist              # 本地构建
python autoLCDE.py pack --out ..\dist --public     # 公开分发：抹掉 build-info.json 里的本机路径
python autoLCDE.py pack --exe                      # 额外冻结免装 Python 的 autoLCDE.exe（需 PyInstaller）
```

产物：便携目录 + `.zip` + 单文件 `.pyz`（`--exe` 再加一个 `autoLCDE.exe`）。
`examples\` 里的示例是用**相对路径**生成的，所以整包搬到任何地方都能直接用。

**exe 用 PyInstaller 冻结**，需要先装上（不想污染全局 site-packages 就用后者）：

```powershell
python -m pip install pyinstaller
python -m pip install --target .pylibs pyinstaller pillow
$env:PYTHONPATH = "<本目录>\.pylibs"     # 只在这条命令的环境里生效
python autoLCDE.py pack --exe --exe-python "<另一个已装好 PyInstaller 的解释器>"
```

`pack --exe` 会在打包后**真的运行一次 exe**（跑完整的离线生成并检查产物），
所以少收模块这类问题当场就能发现。若运行环境不允许 onefile 解包（例如受限的
临时目录），这条检查会标成「○ 跳过」并提示在普通终端里补验，而不是谎报通过。

---

## 十一、许可

MIT License —— 见 [`LICENSE`](LICENSE)。© 2026 NekoHome-Studio。

也就是说：可以自由使用、修改、再分发（含商用），只需保留版权与许可声明。
软件按「原样」提供，不含任何担保。

许可覆盖的是**本仓库的代码**（`autoLCDE/`、`lcde/`、`schema/`、文档与示例）。
它不覆盖 LCDE 游戏本体、游戏内素材或任何第三方插件——那些不包含在本仓库里。

