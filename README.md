# 麦麦绘卷 (Claude MAInet) - 智能多模型图片生成插件

基于 MaiBot SDK 2.x 的智能多模型图片生成插件，支持文生图和图生图自动识别。兼容 OpenAI、OpenAI-Chat、豆包（火山方舟）、Gemini、魔搭、硅基流动、砂糖云、梦羽、智谱 GLM、阿里百炼 DashScope（通义千问/万相/Z-Image/可灵）、ComfyUI 等多种 API 格式。提供命令式风格转换、自拍模式、自动自拍发说说、日程感知、模型配置管理、运行时聊天流隔离状态、对外 @API 生图接口等功能。

## ✨ 主要特性

### 🎯 智能图片生成
- **文生图**: 根据对话内容自动生成图片
- **图生图**: 检测到消息中有图片时自动使用图生图模式
- **自拍模式**: 支持 standard（前置自拍）/ mirror（对镜自拍）/ photo（第三人称照片）三种风格
  - 优化的提示词权重，确保手机不在画面中（standard/photo）或仅在镜子中可见（mirror）
  - 优先使用 LLM 生成风格感知的手部动作，失败时回退到风格专属动作池
  - 支持日程活动增强场景（需 autonomous_planning 插件）
  - 可配置参考图进行图生图
- **提示词优化**: 自动将中文描述优化为专业英文 SD 提示词（自拍模式仅优化场景，不干扰角色外观）
- **结果缓存**: 相同参数复用之前的结果
- **自动撤回**: 可按模型配置延时撤回（使用 NapCat 官方 API）

### 🎨/dr 命令系统

#### 图片生成

| 命令 | 说明 |
|------|------|
| `/dr <风格名>` | 对最近的图片应用预设风格（图生图） |
| `/dr <描述>` | 自然语言生成图片（自动判断文/图生图） |
| `/dr 用model2画一只猫` | 指定模型生成 |

#### 风格管理

| 命令 | 说明 |
|------|------|
| `/dr styles` | 列出所有可用风格 |
| `/dr style <名>` | 查看风格详情 |
| `/dr help` | 帮助信息 |

#### 配置管理（需管理员权限）

| 命令 | 说明 |
|------|------|
| `/dr list` | 列出所有模型 |
| `/dr config` | 显示当前聊天流配置 |
| `/dr set <模型ID>` | 设置 /dr 命令使用的模型 |
| `/dr default <模型ID>` | 设置智能生图 Tool 默认模型（当前聊天流，覆盖上述自动选择） |
| `/dr model on\|off <模型ID>` | 开关指定模型 |
| `/dr recall on\|off <模型ID>` | 开关指定模型的撤回 |
| `/dr on` / `/dr off` | 开关插件（当前聊天流） |
| `/dr selfie on\|off` | 开关自拍日程增强（当前聊天流） |
| `/dr selfie standard\|mirror\|photo` | 切换自拍风格（当前聊天流） |
| `/dr reset` | 重置当前聊天流的所有运行时配置 |

> 运行时配置（模型切换、开关等）仅保存在内存中，重启后恢复为 config.toml 的全局设置。

### 文生图 / 图生图 / 自拍的模型自动选择

当 LLM 调用智能生图 Tool 且**未显式指定 `model_id`** 时，插件按场景自动选模型，回退顺序如下：

| 优先级 | 场景 | 使用的模型 |
|--------|------|-----------|
| 1 | 自动自拍（后台定时任务） | `selfie.selfie_model` |
| 2 | 普通自拍（聊天里要自拍） | `selfie.llm_selfie_model`（留空→下一行） |
| 3 | 图生图（消息附图 / 引用图片 / 自拍参考图） | `basic.default_img2img_model`（留空→下一行） |
| 4 | 兜底（纯文生图） | `basic.default_txt2img_model` |

**典型用法**：文生图模型和图生图模型分开填——`default_txt2img_model` 填文生图专用模型，`default_img2img_model` 填图生图专用模型。这样自然语言画图、改图、自拍三条路自动走对模型，LLM 完全无感。普通自拍必然携带 `reference_image_path` 指定的参考图，所以即使 `llm_selfie_model` 留空也会落到第 3 行的图生图模型，不会错误地用文生图模型画自拍。

> 旧配置里的 `default_model` 无需手工改动：解析时会把它作为「默认文生图模型」的回退源继续生效（见 `resolve_txt2img_model`），字段值本身不会被搬走。
> 若某模型实际不支持图生图（模型配置里 `support_img2img = false`），即使被选为图生图默认也会自动降级为文生图，不会报错。

### 自动自拍

定时生成自拍图片并发布到 QQ 空间说说。

### 推荐周边插件（依赖）

| 插件 | 作用 | 必需？ |
|---|---|---|
| [`MaiTrace`](https://github.com/Rabbit-Jia-Er/MaiTrace)（麦时迹） | QQ 空间自动化插件，提供发说说、刷空间、写日记等功能。自动自拍功能需要它来发布到 QQ 空间 | 否（不装时自动自拍不会发布到空间） |
| [`xuqian13_autonomous-planning-plugin-v4`](https://github.com/xuqian13/autonomous_planning_plugin) | 日程规划插件，提供当前活动数据。自动自拍会根据日程生成场景 | 否（不装时自动自拍场景为"无活动"） |

---

**特性**:
- 可配置间隔（默认 2 小时）
- 安静时段控制（默认 00:00-07:00 不发）
- LLM 根据日程活动描述生成风格感知的英文 SD 场景标签（动作/环境/表情/光线），自动适配 standard/mirror/photo 的物理约束，LLM 失败则跳过本次自拍
- 支持参考图片进行图生图自拍（可配置 `selfie.reference_image_path`，模型不支持时自动回退文生图）
- 配文基于日程活动 + MaiBot 人设 + 表达风格自然生成，生成失败则跳过不发
- 连续失败指数退避，避免频繁请求，无日程数据时自动跳过，不会发空内容

---

## 支持的 API 格式

| format | 平台 | 说明 |
|--------|------|------|
| `openai` | OpenAI / 硅基流动 / Grok / NewAPI 等 | 通用 `/images/generations` 接口 |
| `openai-chat` | 支持生图的 Chat 模型 | 通过 `/chat/completions` 生图，多策略提取图片 |
| `doubao` | 豆包（火山引擎） | 使用 Ark SDK，支持 seed/guidance_scale/watermark |
| `gemini` | Google Gemini | 原生 `generateContent` 接口，支持 Gemini 2.5/3 系列 |
| `modelscope` | 魔搭社区 | 异步任务模式，自动轮询结果 |
| `dashscope` | 阿里百炼 DashScope | 支持通义千问/万相/Z-Image/可灵等模型 |
| `shatangyun` | 砂糖云 (NovelAI) | GET 请求，URL 参数传递 |
| `mengyuai` | 梦羽 AI | 支持图生图（默认走 Qwen Image Edit，可通过 `img2img_model_index` 切换） |
| `comfyui` | 本地 ComfyUI | 加载工作流 JSON，替换占位符，轮询结果（支持代理配置） |

---

## 🚀 快速开始

## 安装插件
  - 使用命令行工具或是 git base 进入你的麦麦目录

   ```shell
   cd MaiBot/plugins
   ```

  - 克隆本仓库

   ```shell
   git clone https://github.com/1021143806/mais-art-journal.git
   ```

  - 重启 maibot 后你会看到在当前插件文件夹 `MaiBot/plugins/mais_art_journal`中生成了一个配置文件 `config.toml`
  - 按照配置文件中的说明填写必要参数后重启 MaiBot 即可让你的麦麦学会不同画风的画画（如何申请 key 请自行前往对应平台官网查看 api 文档）

## 配置说明

配置文件: `config.toml`，首次启动自动生成。版本更新时自动备份到 `old/` 目录。

### 基础设置

```toml
[plugin]
enabled = true                    # 启用插件
config_version = "4.2.0"          # 主程序版本识别，请勿改

[basic]
command_prefix = "/dr"            # 命令前缀，可改 /dv、/draw 等，必须以 / 开头
default_txt2img_model = "model1"  # 默认文生图模型 ID（纯文生图时使用）
default_img2img_model = "model2"  # 默认图生图模型 ID（消息附图/引用图片时使用，留空=与文生图相同）
enable_unified_generation = true  # 启用智能生图 Tool（draw_picture）
enable_pic_command = true         # 启用 /dr 图片生成命令
enable_pic_config = true          # 启用 /dr 配置管理命令
enable_pic_style = true           # 启用 /dr 风格管理命令
pic_command_model = "model1"      # /dr 命令默认模型（可通过 /dr set 动态切换）
admin_users = ["12345"]           # 管理员 QQ 号列表（字符串格式）
max_retries = 2                   # API 失败重试次数
enable_debug_info = false         # 在聊天中显示生成进度等调试信息
enable_verbose_debug = false      # 打印完整 API 请求/响应到日志
cache_enabled = true              # 启用结果缓存
cache_max_size = 10               # 最大缓存数量
auto_recall_enabled = false       # 自动撤回总开关（还需在模型配置里设 auto_recall_delay > 0）
prompt_optimizer_enabled = true   # 使用 MaiBot LLM 优化提示词
llm_task_name = "utils"           # 辅助 LLM 任务名：utils / replyer / planner / memory / learner
```

### 自拍配置（合并了"自拍模式 + 自动自拍"）

```toml
[selfie]
enabled = true                    # 启用自拍模式
reference_image_path = ""         # 参考图路径（留空=纯文生图，配置后自动图生图）
prompt_prefix = "blue hair, red eyes, 1girl"  # Bot 外观描述
negative_prompt = ""              # 额外负面提示词（自动附加手部质量负面提示词）
schedule_enabled = true           # 日程增强（需 autonomous_planning），可通过 /dr selfie on|off 按聊天流覆盖
default_style = "standard"        # 默认风格: standard / mirror / photo，可通过 /dr selfie standard|mirror|photo 按聊天流覆盖
llm_selfie_model = "model2"       # 普通自拍（聊天里要自拍）模型 ID；自拍必然带参考图走图生图，留空=用默认图生图模型

# 以下 auto_ 前缀字段属于自动自拍
auto_enabled = false              # 启用自动自拍（需 MaiTrace + autonomous_planning）
interval_minutes = 120            # 自拍间隔（分钟）
selfie_model = "model1"           # 自动自拍使用的模型 ID
quiet_hours_start = "00:00"       # 安静时段开始（HH:MM），此时段不发自拍
quiet_hours_end = "07:00"         # 安静时段结束
caption_enabled = true            # 是否生成配文
```

### 代理设置

```toml
[proxy]
enabled = false                   # 启用代理（所有 API 请求经代理）
url = "http://127.0.0.1:7890"     # 支持 HTTP / HTTPS / SOCKS5
timeout = 60                      # 代理连接超时（秒）
```

### 风格配置（数组结构，可在 WebUI 增删）

```toml
[[styles.items]]
name = "cartoon"                  # 英文名（命令引用，不可重复）
aliases = "卡通,动漫"              # 中文别名，逗号分隔，可留空
prompt = "cartoon style, anime style, colorful, vibrant colors, clean lines"

[[styles.items]]
name = "watercolor"
aliases = "水彩"
prompt = "watercolor painting style, soft colors, artistic"
```

### 模型配置（数组结构，可在 WebUI 增删）

```toml
[[models.items]]
id = "model1"                                    # 唯一标识（命令引用，不可重复）
name = "我的模型"                                 # 显示名称
base_url = "https://api.siliconflow.cn/v1"       # API 地址
api_key = "Bearer sk-xxx"                        # API 密钥（统一 'Bearer xxx' 格式）
format = "openai"                                # API 格式，见上方"支持的 API 格式"表
model = "Kwai-Kolors/Kolors"                     # 模型标识
fixed_size_enabled = false                       # 固定尺寸（关闭=LLM 自动选）
default_size = "1024x1024"                       # 默认尺寸
seed = -1                                        # 随机种子（-1=随机）
guidance_scale = 2.5                             # 引导强度（CFG）；填 -1 = 不发送该参数（模型不支持时用）
num_inference_steps = 20                         # 推理步数
watermark = false                                # 是否加水印（豆包等支持）
custom_prompt_add = ", best quality"             # 追加正面提示词
negative_prompt_add = "lowres, bad anatomy"      # 追加负面提示词
support_img2img = true                           # 是否支持图生图（按平台文档自行判断）
auto_recall_delay = 0                            # 自动撤回延时（秒），0=不撤回

# 砂糖云（shatangyun）专用：
# artist = ""
# cfg = 0.0                       # CFG Rescale (0.0-1.0)
# sampler = "k_euler_ancestral"
# nocache = 0
# noise_schedule = "karras"

# DashScope（阿里百炼）专用：
# endpoint_path = ""              # 留空走多模态默认；万相 2.5 i2i / 可灵需指定
# dashscope_async = false         # 异步模式
# prompt_extend = true            # 智能扩写

# 梦羽 AI 专用：
# img2img_model_index = 19        # 图生图模型 index，默认 19 (Qwen Image Edit)
```

新增模型：复制整个 `[[models.items]]` 块，把 `id` 改成 `model2` / `model3` 等。

### ComfyUI 支持

通过本地或远程 ComfyUI 实例的 HTTP API 生成图片。需先在 ComfyUI Web UI 中点击 **"保存(API格式)"** 导出工作流 JSON，放到插件 `workflow/` 目录下。

**基本配置：**
```toml
[[models.items]]
id = "model6"
name = "ComfyUI-本地"
base_url = "http://127.0.0.1:8188"  # ComfyUI 服务地址
api_key = ""                         # 不需要，留空
format = "comfyui"                   # 必须填 comfyui
model = "my_workflow.json"           # 工作流文件名（相对 workflow/ 目录，也可填绝对路径）
fixed_size_enabled = true            # 建议开启，尺寸通过占位符传入工作流
default_size = "1024x1024"           # 通过 ${width}/${height} 传入工作流
seed = -1                            # -1=每次随机，通过 ${seed} 传入
guidance_scale = 8                   # 通过 ${cfg} 传入
num_inference_steps = 30             # 通过 ${steps} 传入
custom_prompt_add = ", masterpiece"  # 拼接到用户提示词末尾，通过 ${prompt} 传入
negative_prompt_add = "lowres"       # 通过 ${negative_prompt} 传入
support_img2img = false              # 需工作流中包含 ${image} 占位符
```

**工作流占位符：**

在导出的 API 格式 JSON 中，将需要动态控制的字段值替换为占位符（带引号）：

| 占位符 | 来源 | 工作流中的典型位置 |
|--------|------|---------------------|
| `"${prompt}"` | 用户提示词 + `custom_prompt_add` | CLIPTextEncode 节点的 `text` |
| `"${seed}"` | `seed` 配置值 | KSampler 节点的 `seed` |
| `"${negative_prompt}"` | `negative_prompt_add` | 负面 CLIPTextEncode 节点的 `text` |
| `"${steps}"` | `num_inference_steps` | KSampler 节点的 `steps` |
| `"${cfg}"` | `guidance_scale` | KSampler 节点的 `cfg` |
| `"${width}"` | 从 `default_size` 解析 | EmptyLatentImage 节点的 `width` |
| `"${height}"` | 从 `default_size` 解析 | EmptyLatentImage 节点的 `height` |
| `"${denoise}"` | 图生图降噪强度 | KSampler 节点的 `denoise` |
| `"${image}"` | 用户发送的图片（自动上传） | LoadImage 节点的 `image` |

**示例** — 工作流 JSON 片段：
```json
{
  "3": {
    "inputs": {
      "seed": "${seed}",
      "steps": "${steps}",
      "cfg": "${cfg}",
      "sampler_name": "dpmpp_2m",
      "denoise": "${denoise}",
      "model": ["4", 0],
      "positive": ["6", 0],
      "negative": ["7", 0],
      "latent_image": ["5", 0]
    },
    "class_type": "KSampler"
  },
  "5": {
    "inputs": {
      "width": "${width}",
      "height": "${height}",
      "batch_size": 1
    },
    "class_type": "EmptyLatentImage"
  },
  "6": {
    "inputs": {
      "text": "${prompt}",
      "clip": ["4", 1]
    },
    "class_type": "CLIPTextEncode"
  },
  "7": {
    "inputs": {
      "text": "${negative_prompt}",
      "clip": ["4", 1]
    },
    "class_type": "CLIPTextEncode"
  }
}
```

> 不使用的占位符可以不写，对应字段会保持工作流中的原始值，完全向后兼容。

## 💡 使用示例

### 自然语言生图（可以指定model1，model2 等，支持中文）
```
用户：麦麦，画一张美少女
麦麦：[生成图片]

用户：[发送图片]
用户：[回复 麦麦： [图片] ]，说：麦麦，把这张图的背景换成海滩
麦麦：[图生图：基于输入图片生成]
```

### 自拍模式
```
用户：麦麦，来张自拍！
麦麦：[生成Bot角色的自拍照片，包含随机手部动作]
```

### 命令式风格转换（仅图生图）
```
用户：[发送图片]
用户：[回复 麦麦： [图片] ]，说：/dr cartoon
麦麦：[应用卡通风格]
```

### 命令式自然语言生成（文/图生图）
```
用户：/dr 画一只可爱的猫
麦麦：[文生图：生成新图片]

用户：[发送图片]
用户：[回复 麦麦： [图片] ]，说：麦麦，把这张图的背景换成海滩
麦麦：[图生图：基于输入图片生成]
```

## 🔧 依赖说明

- 需 Python 3.12+
- 依赖 MaiBot 项目插件系统，目前改为一直支持最新版
- MaiBot 项目地址：https://github.com/Mai-with-u/MaiBot
- 如果使用豆包（火山方舟）API，需要安装豆包 SDK

   ```shell
   cd MaiBot
   uv pip install "volcengine-python-sdk[ark]"
   ```

## ⚠️ 注意事项

- 模型是否支持图生图请参考各平台官方文档（注：在`support_img2img = true` - 是否支持图生图中填写true/false，请自行判断）
- 请妥善保管 API 密钥，不要在公开场合泄露，各平台 API 可能有调用频率限制，请注意控制使用频率，生成的图片内容受模型和提示词影响，请遵守相关平台的使用规范（注：梦羽AI和砂糖云支持NSFW，请自行判断）

## 常见问题

- **API 密钥未配置/错误**：请检查 `config.toml` 中对应模型的 api_key 配置。
- **图片尺寸无效**：支持如 `1024x1024`，宽高范围 100~10000。
- **依赖缺失**：请确保 MaiBot 插件系统相关依赖已安装。
- **api 调用报错**
400：参数不正确，请参考报错信息（message）修正不合法的请求参数，可能为插件发送的报文不兼容对应 api 供应商；
401：API Key 没有正确设置；
403：权限不够，最常见的原因是该模型需要实名认证，其他情况参考报错信息（message）；
429：触发了 rate limits；参考报错信息（message）判断触发的是 RPM /RPD / TPM / TPD / IPM / IPD 中的具体哪一种，可以参考 Rate Limits 了解具体的限流策略
504 / 503：一般是服务系统负载比较高，可以稍后尝试；

## 贡献和反馈

- **制作者水平有限，任何漏洞、疑问或建议,欢迎提交 Issue 和 Pull Request！**
- **或联系QQ：1021143806,3082618311**
- **其余问题请联系作者修复或解决（部分好友请求可能被过滤导致回复不及时，请见谅）**

---

## 🤝 插件开发历程

- 该插件基于 MaiBot 最早期官方豆包生图示例插件修改而来，最早我是为了兼容 GPT 生图进行修改，添加对 GPT 生图模型直接返回 base64 格式图片的兼容判断，因为 GPT 生图太贵了，所以后续想兼容魔搭社区的免费生图，新增一层报文兼容。（我不是计算机专业，大部分代码来自 DeepSeek R1 研究了很久，不得不说确实很好玩。）
- 目前支持三种报文返回，即三个平台的图片返回报文 url，image，base64，如果其他平台返回的报文符合以上三种格式也可以正常使用，可以自行尝试。
- MaiBot 0.8 版本更新，根据新插件系统进行重构。
- Rabbit-Jia-Er 加入，添加可以调用多个模型和命令功能。
- saberlights Kiuon 加入，添加自拍功能和自然语言命令功能。
- v4.0.0 迁移到 MaiBot SDK 2.x，并重构为 Pipeline + 装饰器子命令架构：
  - 4 条入口（智能 Action / `/dr` 风格 / `/dr` 自然语言 / 自动自拍 / 独立接口）复用同一组可组合 Step（解析模型 → 校验配置 → 协商尺寸 → 优化提示词 → 构造自拍提示词 → 合并负面 → 图生图回退 → 缓存查询 → 调用 API → 解析响应 → 解析图片 → 发送 → 写缓存 → 调度撤回）。
  - 子命令通过 `@subcommand` 装饰器注册到查找表，新增子命令只需在 `core/commands/handlers/` 下加一个函数。
  - 自拍提示词、动作池、风格约束、LLM 手部动作生成全部独立到 `core/prompts/selfie_prompt_builder.py`。
  - API 客户端依赖 `ClientContext` 而非 Action 实例，外部 `generate_image_standalone` 不再需要桩对象。

## 🔗 版权信息

- 作者：Ptrel, Rabbit-Jia-Er, saberlights Kiuon
- 许可证：GPL-v3.0-or-later
- 项目主页：https://github.com/1021143806/mais-art-journal