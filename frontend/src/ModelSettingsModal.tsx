import { KeyRound, LoaderCircle, Server, X } from "lucide-react";
import { FormEvent, useState } from "react";
import { saveModelSettings, testModelConnection } from "./api";
import type { ModelSettings, ModelSettingsInput } from "./types";

interface Props {
  settings: ModelSettings;
  onClose: () => void;
  onSaved: (settings: ModelSettings) => void;
}

export function ModelSettingsModal({ settings, onClose, onSaved }: Props) {
  const [enabled, setEnabled] = useState(settings.enabled);
  const [provider, setProvider] = useState<"openai" | "deepseek">(
    settings.provider === "deepseek" ? "deepseek" : "openai",
  );
  const [apiStyle, setApiStyle] = useState(settings.api_style);
  const [model, setModel] = useState(settings.model);
  const [baseUrl, setBaseUrl] = useState(settings.base_url);
  const [apiKey, setApiKey] = useState("");
  const [timeout, setTimeoutValue] = useState(settings.timeout_seconds);
  const [maxOutputTokens, setMaxOutputTokens] = useState(settings.max_output_tokens);
  const [selectorModel, setSelectorModel] = useState(settings.selector_model);
  const [selectorMaxTools, setSelectorMaxTools] = useState(settings.selector_max_tools);
  const [selectorMaxOutputTokens, setSelectorMaxOutputTokens] = useState(settings.selector_max_output_tokens);
  const [clearApiKey, setClearApiKey] = useState(false);
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const currentProviderHasKey = provider === settings.provider && settings.has_api_key;

  function payload(): ModelSettingsInput {
    return {
      enabled,
      provider,
      api_style: apiStyle,
      model: model.trim(),
      base_url: baseUrl.trim(),
      api_key: apiKey.trim() || undefined,
      clear_api_key: clearApiKey,
      timeout_seconds: timeout,
      max_output_tokens: maxOutputTokens,
      selector_model: selectorModel.trim(),
      selector_max_tools: selectorMaxTools,
      selector_max_output_tokens: selectorMaxOutputTokens,
    };
  }

  function changeProvider(nextProvider: "openai" | "deepseek") {
    setProvider(nextProvider);
    if (nextProvider === "deepseek") {
      setApiStyle("responses");
      setModel("deepseek-flash");
      setSelectorModel("deepseek-chat");
      setBaseUrl("https://api.deepseek.com");
    } else {
      setApiStyle("responses");
      setModel("gpt-5.5");
      setSelectorModel("gpt-4.1-mini");
      setBaseUrl("https://api.openai.com/v1");
    }
    setApiKey("");
    setClearApiKey(false);
    setFeedback(null);
  }

  async function save(event?: FormEvent) {
    event?.preventDefault();
    setBusy("save");
    setError(null);
    setFeedback(null);
    try {
      const updated = await saveModelSettings(payload());
      onSaved(updated);
      setApiKey("");
      setClearApiKey(false);
      setFeedback("设置已保存，后续任务会立即使用新配置。完整 API Key 不会返回前端。");
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : "保存失败");
    } finally {
      setBusy(null);
    }
  }

  async function saveAndTest() {
    setBusy("test");
    setError(null);
    setFeedback(null);
    try {
      const updated = await saveModelSettings(payload());
      onSaved(updated);
      setApiKey("");
      setClearApiKey(false);
      const result = await testModelConnection();
      setFeedback(`${result.message}：${result.model}`);
    } catch (testError) {
      setError(testError instanceof Error ? testError.message : "连接测试失败");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="settings-modal" role="dialog" aria-modal="true" aria-labelledby="model-settings-title">
        <header className="modal-header">
          <div>
            <span className="eyebrow"><Server size={14} /> 模型运行时</span>
            <h2 id="model-settings-title">模型配置</h2>
          </div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭模型配置"><X size={19} /></button>
        </header>

        <form onSubmit={save} className="settings-form">
          <label className="toggle-row">
            <span>
              <strong>启用真实模型</strong>
              <small>关闭时继续使用确定性模拟运行时</small>
            </span>
            <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} />
          </label>

          <div className="form-grid">
            <label>
              <span>提供商</span>
              <select value={provider} onChange={(event) => changeProvider(event.target.value as "openai" | "deepseek")}>
                <option value="openai">OpenAI</option>
                <option value="deepseek">DeepSeek</option>
              </select>
            </label>
            <label>
              <span>API 类型</span>
              <select value={apiStyle} onChange={(event) => setApiStyle(event.target.value as ModelSettings["api_style"])}>
                <option value="responses">Responses API</option>
                <option value="chat_completions">Chat Completions</option>
              </select>
            </label>
          </div>

          <label>
            <span>模型名称</span>
            <input required value={model} onChange={(event) => setModel(event.target.value)} placeholder={provider === "deepseek" ? "deepseek-flash" : "gpt-5.5"} />
          </label>
          <label>
            <span>轻量 Tool Selector 模型</span>
            <input required value={selectorModel} onChange={(event) => setSelectorModel(event.target.value)} placeholder={provider === "deepseek" ? "deepseek-chat" : "gpt-4.1-mini"} />
            <small>只接收已授权 Tool 的简短元数据，主 Agent 只看到筛选后的完整 Schema。</small>
          </label>
          <label>
            <span>Base URL</span>
            <input required value={baseUrl} onChange={(event) => setBaseUrl(event.target.value)} placeholder="https://api.openai.com/v1" />
          </label>
          <label>
            <span>API Key</span>
            <div className="key-input-wrap">
              <KeyRound size={16} />
              <input
                type="password"
                value={apiKey}
                onChange={(event) => { setApiKey(event.target.value); setClearApiKey(false); }}
                placeholder={currentProviderHasKey ? settings.api_key_masked ?? "sk-..." : "sk-..."}
                autoComplete="new-password"
              />
            </div>
            <small>
              {currentProviderHasKey
                ? `已配置 Key，来源：${settings.api_key_source === "environment" ? "环境变量" : "Windows DPAPI 加密配置"}。留空不会覆盖。`
                : "尚未配置 Key。Key 仅发送到本机后端，不写入浏览器存储。"}
            </small>
          </label>

          {currentProviderHasKey && settings.api_key_source !== "environment" && (
            <label className="clear-key-row">
              <input type="checkbox" checked={clearApiKey} onChange={(event) => { setClearApiKey(event.target.checked); if (event.target.checked) setApiKey(""); }} />
              保存时清除本地 API Key
            </label>
          )}

          <div className="form-grid">
            <label>
              <span>超时（秒）</span>
              <input type="number" min={10} max={600} value={timeout} onChange={(event) => setTimeoutValue(Number(event.target.value))} />
            </label>
            <label>
              <span>最大输出 Token</span>
              <input type="number" min={256} max={100000} value={maxOutputTokens} onChange={(event) => setMaxOutputTokens(Number(event.target.value))} />
            </label>
          </div>
          <div className="form-grid">
            <label>
              <span>Selector 最大 Tool 数</span>
              <input type="number" min={2} max={20} value={selectorMaxTools} onChange={(event) => setSelectorMaxTools(Number(event.target.value))} />
            </label>
            <label>
              <span>Selector 输出 Token</span>
              <input type="number" min={128} max={8000} value={selectorMaxOutputTokens} onChange={(event) => setSelectorMaxOutputTokens(Number(event.target.value))} />
            </label>
          </div>

          <div className="security-note">
            网页保存的 Key 使用 Windows DPAPI 绑定当前登录用户加密，并写入被 Git 忽略的本机配置。正式生产环境建议接入企业密钥管理服务。
          </div>
          {error && <div className="settings-message error-message">{error}</div>}
          {feedback && <div className="settings-message success-message">{feedback}</div>}

          <footer className="modal-actions">
            <button className="secondary-button" type="button" onClick={onClose}>取消</button>
            <button className="secondary-button" type="button" onClick={saveAndTest} disabled={busy !== null}>
              {busy === "test" && <LoaderCircle className="spin" size={15} />} 保存并测试
            </button>
            <button className="primary-button" type="submit" disabled={busy !== null}>
              {busy === "save" && <LoaderCircle className="spin" size={15} />} 保存设置
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}

