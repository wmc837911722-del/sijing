# 模型平台配置参考

思径 0.2.9 提供 12 个国内平台预设，以及 OpenAI、Anthropic、Gemini、Ollama 和自定义服务。预设是可编辑的连接模板，不是模型白名单。添加模型时默认选中 Kimi；模型 ID 需要选择参考名称或填写控制台已开通的型号。平台地址、接口类型和模型名称均可修改，同一平台可以保存多个配置。

以下官方资料核对于 2026-10-08。模型列表、地域和套餐可能变化，应以自己的控制台为准；参考型号不代表账号已获授权。当前支持非流式文本对话接口，不包含图片生成、音视频接口或仅支持流式输出的模型。没有使用真实账号密钥逐个平台调用，协议测试与实际账号连通性分开记录。

| 平台 | 预填 API 基础地址 | 凭据与模型填写要点 | 官方参考 |
| --- | --- | --- | --- |
| Kimi · 月之暗面 | `https://api.moonshot.cn/v1` | Kimi API Key；填写当前控制台模型 ID | [模型说明](https://platform.kimi.com/docs/models) |
| DeepSeek | `https://api.deepseek.com` | DeepSeek API Key | [接入说明](https://api-docs.deepseek.com/zh-cn/) |
| 通义千问 · 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | API Key、模型和地域应一致；可以替换成工作空间专用地址 | [基础地址](https://help.aliyun.com/zh/model-studio/base-url) |
| 智谱 · GLM | `https://open.bigmodel.cn/api/paas/v4` | 平台 API Key；Coding Plan 使用对应专用地址 | [兼容接口](https://docs.bigmodel.cn/cn/guide/develop/openai/introduction) |
| 豆包 · 火山方舟 | `https://ark.cn-beijing.volces.com/api/v3` | 方舟 API Key；官方模型 ID 或自己的 `ep-…` 接入点 ID | [地址与认证](https://docs.volcengine.com/docs/ark/base-url-and-authentication?lang=zh) |
| 硅基流动 | `https://api.siliconflow.cn/v1` | API Key；完整模型命名空间，包括可能存在的 `Pro/` 前缀 | [快速开始](https://docs.siliconflow.cn/docs/userguide/quickstart) |
| 腾讯混元 · TokenHub | `https://tokenhub.tencentmaas.com/v1` | TokenHub API Key；不是腾讯云 SecretId/SecretKey | [接入说明](https://cloud.tencent.com/document/product/1823/132252) |
| 百度千帆 · 文心 | `https://qianfan.baidubce.com/v2` | 新版千帆 API Key；不是旧版 AK/SK 或 access_token | [V2 接口](https://ai.baidu.com/ai-doc/WENXINWORKSHOP/0m2vwrjws) |
| MiniMax | `https://api.minimax.cn/v1` | 国内开放平台 API Key 和文本模型 ID | [兼容接口](https://platform.minimax.cn/docs/api-reference/text-openai-api) |
| 讯飞星火 | `https://spark-api-open.xf-yun.com/v1` | HTTP APIPassword；不是 WebSocket 的 APPID/APIKey/APISecret。X 系列按控制台更换地址 | [HTTP 接口](https://www.xfyun.cn/doc/spark/HTTP调用文档.html) |
| 阶跃星辰 | `https://api.stepfun.com/v1` | 开放平台 API Key 和文本模型 ID | [开放平台](https://platform.stepfun.com/) |
| 百川 | `https://api.baichuan-ai.com/v1` | 开放平台 API Key 和文本模型 ID | [API 文档](https://platform.baichuan-ai.com/docs-v2/api) |

API 地址填写基础路径，不要加 `/chat/completions`。企业代理、地区地址或专用套餐需同时匹配其密钥与模型；应用不会自动迁移已有配置。切换服务预设会清空表单中的密钥，编辑已有配置时留空则保留原密钥。

其他平台可选择「自定义兼容服务」，填写兼容 OpenAI Chat Completions、Anthropic Messages 或 Gemini 的地址与模型 ID；专有鉴权或其他协议需要额外适配。Ollama 预填 `http://127.0.0.1:11434/v1`，模型填写本机已下载的完整名称，默认本地服务无需密钥。聊天会员和 Coding Plan 不自动等同于通用 API 权限。

保存后通过卡片上的「测试」发起一次短请求，再按需启用或在问答中选择该配置。测试会产生服务商实际用量。思考型模型可能耗尽短测试的输出额度：只有接口返回有效的思考内容和明确的额度截断状态，才提示已连通但完整回答尚未验证；业务问答、SQL 生成和仓库分析仍要求最终正文，思考文本不能当作可执行 SQL。

MiniMax 官方地址使用独立思考字段，避免思考片段混入最终 JSON；星火官方兼容路径显式关闭默认联网搜索。普通业务调用不自动扩大输出额度或重复重试。部分深度思考模型可能超出当前调用额度，这种情况会明确报错；可换用支持短回答的非思考模型。

缓存统计只采用服务商实际返回的数据。Kimi、百炼等的 `prompt_tokens_details.cached_tokens` 与 DeepSeek 的 `prompt_cache_hit_tokens` 都能识别，未提供字段时为未知，不编造命中数。不承诺固定命中率。配置平台不会自动发送代码；仓库仍需单独绑定允许使用的模型。
