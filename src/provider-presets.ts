import type { Provider } from './types';

export type ProviderPreset = {
  id: string;
  group: '国内平台' | '国际平台' | '自定义与本地';
  name: string;
  kind: Provider['kind'];
  base_url: string;
  models: readonly string[];
  hint: string;
};

// These are editable connection shortcuts, never an allowlist of model IDs.
// HTTP presets leave the model blank for users to choose their available model.
export const PROVIDER_PRESETS: readonly ProviderPreset[] = [
  { id: 'kimi', group: '国内平台', name: 'Kimi · 月之暗面', kind: 'openai-compatible', base_url: 'https://api.moonshot.cn/v1', models: ['kimi-k3', 'kimi-k2.6', 'kimi-k2.7-code'], hint: '使用 Kimi 开放平台的 API Key；模型 ID 以控制台当前可用模型为准。' },
  { id: 'deepseek', group: '国内平台', name: 'DeepSeek', kind: 'openai-compatible', base_url: 'https://api.deepseek.com', models: ['deepseek-flash', 'deepseek-v4-pro'], hint: '使用 DeepSeek 开放平台的 API Key。' },
  { id: 'qwen', group: '国内平台', name: '通义千问 · 阿里云百炼', kind: 'openai-compatible', base_url: 'https://dashscope.aliyuncs.com/compatible-mode/v1', models: ['qwen-plus', 'qwen-flash'], hint: '预填中国内地地址；API Key、模型和服务地域须一致。' },
  { id: 'zhipu', group: '国内平台', name: '智谱 · GLM', kind: 'openai-compatible', base_url: 'https://open.bigmodel.cn/api/paas/v4', models: ['glm-5.3', 'glm-5.2'], hint: '使用智谱开放平台的 API Key；Coding Plan 请填写其专用兼容地址。' },
  { id: 'doubao', group: '国内平台', name: '豆包 · 火山方舟', kind: 'openai-compatible', base_url: 'https://ark.cn-beijing.volces.com/api/v3', models: [], hint: '填写方舟控制台已开通的模型 ID 或推理接入点 ID（ep-…），以及对应 API Key。' },
  { id: 'siliconflow', group: '国内平台', name: '硅基流动 · SiliconFlow', kind: 'openai-compatible', base_url: 'https://api.siliconflow.cn/v1', models: [], hint: '模型 ID 需包含平台列出的完整组织/模型名称，大小写与控制台一致。' },
  { id: 'hunyuan', group: '国内平台', name: '腾讯混元 · TokenHub', kind: 'openai-compatible', base_url: 'https://tokenhub.tencentmaas.com/v1', models: ['hy4-preview'], hint: '使用 TokenHub API Key；旧混元账号可改为原服务地址。SecretId/SecretKey 不能直接填入此处。' },
  { id: 'qianfan', group: '国内平台', name: '百度千帆 · 文心', kind: 'openai-compatible', base_url: 'https://qianfan.baidubce.com/v2', models: [], hint: '使用千帆新版 API Key 和控制台模型 ID，不使用旧版 AK/SK 或 access_token。' },
  { id: 'minimax', group: '国内平台', name: 'MiniMax', kind: 'openai-compatible', base_url: 'https://api.minimax.cn/v1', models: ['MiniMax-M2.7', 'MiniMax-M2.5'], hint: '使用 MiniMax 国内开放平台的 API Key 和文本模型 ID。' },
  { id: 'spark', group: '国内平台', name: '讯飞星火', kind: 'openai-compatible', base_url: 'https://spark-api-open.xf-yun.com/v1', models: ['4.0Ultra', 'lite'], hint: 'v1 HTTP 服务需填写 APIPassword；X 系列请改用对应地址。不要使用 WebSocket 的 APPID/APIKey/APISecret。' },
  { id: 'stepfun', group: '国内平台', name: '阶跃星辰 · StepFun', kind: 'openai-compatible', base_url: 'https://api.stepfun.com/v1', models: [], hint: '使用阶跃开放平台的 API Key 和已开通的文本模型 ID。' },
  { id: 'baichuan', group: '国内平台', name: '百川 · Baichuan', kind: 'openai-compatible', base_url: 'https://api.baichuan-ai.com/v1', models: ['Baichuan4-Turbo', 'Baichuan4-Air'], hint: '使用百川开放平台的 API Key 和已开通的文本模型 ID。' },
  { id: 'openai', group: '国际平台', name: 'OpenAI', kind: 'openai-compatible', base_url: 'https://api.openai.com/v1', models: [], hint: '填写 API 平台已开通的模型 ID 和 API Key。' },
  { id: 'anthropic', group: '国际平台', name: 'Anthropic', kind: 'anthropic', base_url: 'https://api.anthropic.com', models: [], hint: '使用 Anthropic Messages 接口的模型 ID 和 API Key。' },
  { id: 'gemini', group: '国际平台', name: 'Google Gemini', kind: 'gemini', base_url: 'https://generativelanguage.googleapis.com/v1beta', models: [], hint: '使用 Gemini API 的模型 ID 和 API Key。' },
  { id: 'codex-local', group: '自定义与本地', name: '本机 Codex · 账号登录', kind: 'codex-local', base_url: 'codex://local', models: ['default'], hint: '使用本机 Codex CLI 已登录的账号与对应额度，无需 API 密钥。代码片段会通过 Codex 发送给 OpenAI。' },
  { id: 'custom', group: '自定义与本地', name: '自定义兼容服务', kind: 'openai-compatible', base_url: '', models: [], hint: '可连接其他国内平台、公司网关或私有部署；填写兼容接口的基础地址和模型 ID。' },
  { id: 'ollama', group: '自定义与本地', name: 'Ollama · 本地模型', kind: 'openai-compatible', base_url: 'http://127.0.0.1:11434/v1', models: [], hint: '填写本机已下载的模型名；默认本地 Ollama 无需 API 密钥。' },
];

export const PROVIDER_GROUPS = ['国内平台', '国际平台', '自定义与本地'] as const;
