import { useEffect, useState, type FormEvent } from "react";
import { api, post, put } from "./api";
import type { AgentModelSettings, ModelRole, ModelSettings } from "./types";
import { Heading, Icon, message } from "./ui";
import "./agent-settings.css";

const agents: Record<
  ModelRole,
  { title: string; english: string; purpose: string }
> = {
  supervisor: {
    title: "父 Agent",
    english: "SUPERVISOR",
    purpose: "理解目标，委派任务与决定下一步。",
  },
  ideation: {
    title: "选题 Agent",
    english: "IDEATION",
    purpose: "结合需求、画像与热点提出内容方向。",
  },
  research: {
    title: "调研 Agent",
    english: "RESEARCH",
    purpose: "整理传入素材，提炼依据与待核实问题。",
  },
  creation: {
    title: "创作 Agent",
    english: "CREATION",
    purpose: "撰写内容，并落实每一次修改意见。",
  },
  review: {
    title: "审核 Agent",
    english: "REVIEW",
    purpose: "检查需求、依据与表达，保留规则校验。",
  },
};
const providers = [
  "deepseek",
  "openai",
  "anthropic",
  "qwen",
  "zhipu",
  "minimax",
  "ollama",
];
const vendorKeys: Record<string, string> = {
  deepseek: "DEEPSEEK_API_KEY",
  openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  qwen: "DASHSCOPE_API_KEY",
  zhipu: "ZHIPU_API_KEY",
  minimax: "MINIMAX_API_KEY",
  ollama: "OLLAMA_API_KEY",
};

function editable(settings: ModelSettings) {
  return {
    backend: settings.backend,
    provider: settings.provider,
    model: settings.model,
    credential_source: settings.credential_source,
    api_key_env: settings.api_key_env,
    protocol: settings.protocol,
    base_url: settings.base_url || null,
    trust_env: settings.trust_env,
    max_tokens: settings.max_tokens,
    timeout: settings.timeout,
  };
}
function ready(settings: ModelSettings) {
  return (
    settings.backend === "llm" &&
    settings.model_configured &&
    settings.credential_configured
  );
}

export default function AgentSettings({
  onSaved,
  notify,
}: {
  onSaved: () => void;
  notify: (value: string) => void;
}) {
  const [configs, setConfigs] = useState<AgentModelSettings[]>([]);
  const [role, setRole] = useState<ModelRole>("supervisor");
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [clearKey, setClearKey] = useState(false);
  const [copyFrom, setCopyFrom] = useState("");
  const [pending, setPending] = useState(false);
  const [loading, setLoading] = useState(true);
  const [skills, setSkills] = useState<
    { id: string; name: string; description: string }[]
  >([]);
  const [skillsLoading, setSkillsLoading] = useState(true);
  const [sourceChoice, setSourceChoice] = useState<"startup" | "web">(
    "startup",
  );
  const saved = configs.find((item) => item.role === role);
  const dirty = Boolean(
    apiKey ||
    clearKey ||
    (saved && sourceChoice !== saved.config_source) ||
    (sourceChoice === "web" &&
      settings &&
      saved &&
      JSON.stringify(editable(settings)) !== JSON.stringify(editable(saved))),
  );
  const sources = configs.filter((item) => item.role !== role && ready(item));
  const displayed =
    settings && sourceChoice === "startup" && settings.startup
      ? { ...settings, ...settings.startup.values }
      : settings;
  const origins = {
    file: "启动配置文件",
    provided: "启动时传入的配置",
    environment: "服务端环境变量",
    defaults: "启动参数 / 默认值",
  };

  useEffect(() => {
    let active = true;
    void api<AgentModelSettings[]>("/settings")
      .then((items) => {
        if (!active) return;
        setConfigs(items);
        setSettings(items.find((item) => item.role === "supervisor") || null);
        setSourceChoice(
          items.find((item) => item.role === "supervisor")?.config_source ||
            "startup",
        );
      })
      .catch((error) => notify(message(error)))
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [notify]);
  useEffect(() => {
    let active = true;
    setSkills([]);
    setSkillsLoading(true);
    void api<typeof skills>(`/skills?role=${role}`)
      .then((items) => {
        if (active) setSkills(items);
      })
      .catch((error) => {
        if (active) notify(message(error));
      })
      .finally(() => {
        if (active) setSkillsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [role, notify]);

  function chooseRole(next: ModelRole) {
    if (next === role || pending) return;
    if (dirty && !window.confirm("当前 Agent 有未保存的修改，是否放弃并切换？"))
      return;
    setRole(next);
    setSettings(configs.find((item) => item.role === next) || null);
    setSourceChoice(
      configs.find((item) => item.role === next)?.config_source || "startup",
    );
    setApiKey("");
    setClearKey(false);
    setCopyFrom("");
  }
  function patch(values: Partial<ModelSettings>, endpointChanged = false) {
    if (!settings) return;
    setSettings({
      ...settings,
      ...values,
      ...(endpointChanged
        ? { api_key_configured: false, credential_configured: false }
        : {}),
    });
    if (endpointChanged) {
      setApiKey("");
      setClearKey(false);
    }
  }
  function accept(value: ModelSettings) {
    setSettings(value);
    setSourceChoice(value.config_source);
    setConfigs((items) =>
      items.map((item) => (item.role === role ? { ...value, role } : item)),
    );
    setApiKey("");
    setClearKey(false);
    setCopyFrom("");
    onSaved();
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings || pending) return;
    setPending(true);
    try {
      accept(
        sourceChoice === "startup"
          ? await api<ModelSettings>(`/settings/${role}`, { method: "DELETE" })
          : await put<ModelSettings>(`/settings/${role}`, {
              ...editable(settings),
              ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
              clear_api_key: clearKey,
            }),
      );
      notify(`${agents[role].title} 配置已保存，将用于下次执行或重试。`);
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }
  async function copy() {
    if (!copyFrom || pending) return;
    setPending(true);
    try {
      accept(
        await post<ModelSettings>(`/settings/${role}/copy`, {
          source_role: copyFrom,
        }),
      );
      notify(`已为${agents[role].title}应用模型与独立凭据副本。`);
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <>
      <Heading
        eyebrow="AGENT SETTINGS / 接口配置"
        title="各有所长，连接合适的模型。"
        note="每个 Agent 明确选择配置来源；网页独立保存，启动文件单独管理。"
      />
      <div className="agent-role-tabs" role="tablist" aria-label="选择 Agent">
        {(
          Object.entries(agents) as [ModelRole, (typeof agents)[ModelRole]][]
        ).map(([id, agent], index) => {
          const config = configs.find((item) => item.role === id);
          const status = !config
            ? "正在读取"
            : ready(config)
              ? "已配置"
              : config.backend === "rules"
                ? "规则审核"
                : config.backend === "mock"
                  ? "离线示例"
                  : "待配置";
          return (
            <button
              key={id}
              id={`agent-tab-${id}`}
              role="tab"
              aria-selected={role === id}
              aria-controls="agent-model-panel"
              disabled={pending || loading}
              className={`agent-role-tab ${role === id ? "active" : ""}`}
              onClick={() => chooseRole(id)}
            >
              <span className="agent-tab-top">
                <span>
                  {String(index + 1).padStart(2, "0")} / {agent.english}
                </span>
                <i className={config && ready(config) ? "configured" : ""} />
              </span>
              <strong>{agent.title}</strong>
              <span className="agent-tab-status">{status}</span>
            </button>
          );
        })}
      </div>
      <div className="agent-settings-layout">
        <section
          className="surface agent-model-panel"
          id="agent-model-panel"
          role="tabpanel"
          aria-labelledby={`agent-tab-${role}`}
        >
          <div className="agent-panel-heading">
            <div>
              <span className="eyebrow">{agents[role].english} / MODEL</span>
              <h2>{agents[role].title}的模型接口</h2>
              <p>{agents[role].purpose}</p>
            </div>
            {dirty && <span className="status warning">未保存</span>}
          </div>
          {settings && (
            <section className="model-source-box" aria-label="配置来源与优先级">
              <div className="model-source-heading">
                <label htmlFor="agent-config-source">配置来源</label>
                <span className="source-active" role="status">
                  当前生效：
                  {saved?.config_source === "web"
                    ? "网页独立配置"
                    : "跟随启动配置"}
                </span>
              </div>
              <select
                id="agent-config-source"
                value={sourceChoice}
                disabled={pending}
                onChange={(event) => {
                  setSourceChoice(event.target.value as "startup" | "web");
                  setApiKey("");
                  setClearKey(false);
                }}
              >
                <option value="startup">跟随启动配置（文件 / 环境变量）</option>
                <option value="web">网页独立配置</option>
              </select>
              <p>
                {sourceChoice === "web"
                  ? "保存后完整覆盖此 Agent 的启动配置，下次执行或重试生效；不会写回 JSON 文件。"
                  : "下方展示本次服务启动时的配置。切换来源后保存，即撤销此 Agent 的网页覆盖；修改启动文件需要重启服务。"}
              </p>
              {settings.startup && (
                <div className="startup-reference">
                  <div>
                    <strong>{origins[settings.startup.origin]}</strong>
                    <span>
                      {saved?.config_source === "web"
                        ? "已被网页配置覆盖"
                        : "当前使用"}
                    </span>
                  </div>
                  {settings.startup.path && (
                    <code>{settings.startup.path}</code>
                  )}
                  <p>
                    {settings.startup.values.provider} ·{" "}
                    {settings.startup.values.model || "未配置模型 ID"} ·
                    最大输出 {settings.startup.values.max_tokens} Token · 超时{" "}
                    {settings.startup.values.timeout} 秒
                  </p>
                </div>
              )}
            </section>
          )}
          <div className="model-copy-box">
            <div>
              <strong>复用已配置模型</strong>
              <p>复制接口参数与凭据，之后仍可分别修改。</p>
            </div>
            <div className="model-copy-controls">
              <select
                aria-label="选择已配置模型"
                value={copyFrom}
                disabled={pending || !sources.length}
                onChange={(event) => setCopyFrom(event.target.value)}
              >
                <option value="">
                  {sources.length
                    ? "选择其他 Agent 已配置的模型"
                    : "先为一个 Agent 保存可用模型"}
                </option>
                {sources.map((item) => (
                  <option key={item.role} value={item.role}>
                    {item.provider} · {item.model} — {agents[item.role].title}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="button"
                disabled={pending || !copyFrom}
                onClick={() => void copy()}
              >
                <Icon name="arrow" />
                一键应用
              </button>
            </div>
            {dirty && copyFrom && (
              <p className="form-help">应用后将替换当前表单并立即保存。</p>
            )}
          </div>
          {loading ? (
            <p className="empty">正在读取模型配置…</p>
          ) : (
            displayed && (
              <form onSubmit={(event) => void save(event)} id="model-form">
                <fieldset disabled={pending} className="agent-model-fields">
                  <fieldset
                    className="form-grid agent-connection-fields"
                    disabled={sourceChoice === "startup"}
                  >
                    <div className="full">
                      <label htmlFor="agent-backend">运行方式</label>
                      <select
                        id="agent-backend"
                        value={displayed.backend}
                        onChange={(event) =>
                          patch({
                            backend: event.target
                              .value as ModelSettings["backend"],
                          })
                        }
                      >
                        <option value="llm">真实模型</option>
                        {role === "review" ? (
                          <option value="rules">本地规则审核</option>
                        ) : (
                          <option value="mock">离线示例（Mock）</option>
                        )}
                      </select>
                    </div>
                    <div>
                      <label htmlFor="agent-provider">模型服务商</label>
                      <input
                        id="agent-provider"
                        list="agent-providers"
                        required
                        value={displayed.provider}
                        onChange={(event) => {
                          const provider = event.target.value;
                          patch(
                            {
                              provider,
                              protocol: null,
                              base_url: null,
                              model: "",
                              api_key_env:
                                role === "ideation"
                                  ? vendorKeys[provider] || "XHS_LLM_API_KEY"
                                  : `XHS_${role.toUpperCase()}_API_KEY`,
                            },
                            true,
                          );
                        }}
                      />
                      <datalist id="agent-providers">
                        {providers.map((provider) => (
                          <option key={provider} value={provider} />
                        ))}
                      </datalist>
                    </div>
                    <div>
                      <label htmlFor="agent-model">模型 ID</label>
                      <input
                        id="agent-model"
                        value={displayed.model}
                        required={displayed.backend === "llm"}
                        placeholder="填写实际可用的模型 ID"
                        onChange={(event) =>
                          patch({ model: event.target.value })
                        }
                      />
                    </div>
                    <div>
                      <label htmlFor="agent-protocol">接口协议</label>
                      <select
                        id="agent-protocol"
                        value={displayed.protocol || ""}
                        onChange={(event) =>
                          patch(
                            {
                              protocol: (event.target.value ||
                                null) as ModelSettings["protocol"],
                            },
                            true,
                          )
                        }
                      >
                        <option value="">使用厂商预设</option>
                        <option value="openai">OpenAI 兼容接口</option>
                        <option value="anthropic">Anthropic Messages</option>
                      </select>
                    </div>
                    <div>
                      <label htmlFor="agent-base-url">Base URL（选填）</label>
                      <input
                        id="agent-base-url"
                        type="url"
                        value={displayed.base_url || ""}
                        placeholder="留空使用厂商默认地址"
                        onChange={(event) =>
                          patch({ base_url: event.target.value || null }, true)
                        }
                      />
                    </div>
                    <div>
                      <label htmlFor="agent-max-tokens">最大输出 Token</label>
                      <input
                        id="agent-max-tokens"
                        type="number"
                        min="1"
                        step="1"
                        required
                        value={displayed.max_tokens ?? 4096}
                        onChange={(event) =>
                          patch({ max_tokens: Number(event.target.value) })
                        }
                      />
                      <p className="form-help">
                        单次响应的输出预算。遇到 length
                        截断时可调高，以服务商支持的上限为准。
                      </p>
                    </div>
                    <div>
                      <label htmlFor="agent-timeout">请求超时（秒）</label>
                      <input
                        id="agent-timeout"
                        type="number"
                        min="0.1"
                        step="any"
                        required
                        value={displayed.timeout ?? 120}
                        onChange={(event) =>
                          patch({ timeout: Number(event.target.value) })
                        }
                      />
                      <p className="form-help">
                        较长输出可能需要更多等待时间。
                      </p>
                    </div>
                    <div className="full">
                      <label htmlFor="agent-credential-source">
                        密钥配置方式
                      </label>
                      <select
                        id="agent-credential-source"
                        value={displayed.credential_source}
                        onChange={(event) => {
                          patch({
                            credential_source: event.target
                              .value as ModelSettings["credential_source"],
                          });
                          setApiKey("");
                          setClearKey(false);
                        }}
                      >
                        <option value="api_key">直接填写 API Key</option>
                        <option value="environment">使用服务端环境变量</option>
                      </select>
                    </div>
                    {displayed.credential_source === "api_key" ? (
                      <div className="full">
                        <label htmlFor="agent-api-key">API Key</label>
                        <input
                          id="agent-api-key"
                          type="password"
                          autoComplete="new-password"
                          spellCheck={false}
                          value={apiKey}
                          disabled={clearKey || pending}
                          placeholder={
                            displayed.api_key_configured
                              ? "已保存，留空保留；填写新密钥可替换"
                              : "填写此接口的 API Key"
                          }
                          onChange={(event) => setApiKey(event.target.value)}
                        />
                        <p className="form-help" role="status">
                          {displayed.api_key_configured
                            ? "此接口已保存密钥。"
                            : "此接口尚未保存密钥。"}{" "}
                          密钥在本机加密，保存后不回显。
                        </p>
                        {displayed.api_key_configured && (
                          <label className="checkbox-line">
                            <input
                              type="checkbox"
                              checked={clearKey}
                              onChange={(event) => {
                                setClearKey(event.target.checked);
                                setApiKey("");
                              }}
                            />
                            清除此 Agent 当前接口的密钥
                          </label>
                        )}
                      </div>
                    ) : (
                      <div className="full">
                        <label htmlFor="agent-key-env">密钥环境变量名称</label>
                        <input
                          id="agent-key-env"
                          required
                          value={displayed.api_key_env}
                          onChange={(event) =>
                            patch({ api_key_env: event.target.value })
                          }
                        />
                        <p className="form-help">
                          {displayed.credential_configured
                            ? "服务进程已找到对应凭据。"
                            : "在启动服务的终端设置后重启服务。"}
                        </p>
                      </div>
                    )}
                    <label className="checkbox-line full">
                      <input
                        type="checkbox"
                        checked={displayed.trust_env}
                        onChange={(event) =>
                          patch({ trust_env: event.target.checked })
                        }
                      />
                      使用系统代理环境变量
                    </label>
                  </fieldset>
                  <div className="agent-save-bar">
                    <p>
                      仅影响{agents[role].title}下次执行，其他 Agent
                      保持各自配置。
                    </p>
                    <button className="button primary" type="submit">
                      {pending
                        ? "正在保存…"
                        : sourceChoice === "startup"
                          ? "保存配置来源"
                          : "保存 Agent 配置"}
                    </button>
                  </div>
                </fieldset>
              </form>
            )
          )}
        </section>
        <aside className="agent-settings-aside">
          <section className="surface agent-skills-panel">
            <span className="eyebrow">ROLE / CAPABILITIES</span>
            <h2>此 Agent 的技能</h2>
            <p>来自当前注册表的实际授权目录。</p>
            {skillsLoading ? (
              <p className="empty">正在读取…</p>
            ) : skills.length ? (
              skills.map((skill) => (
                <div className="skill-row" key={skill.id}>
                  <span className="skill-icon">✳</span>
                  <div>
                    <h3>{skill.id}</h3>
                    <p>{skill.description}</p>
                  </div>
                </div>
              ))
            ) : (
              <p className="empty">
                {role === "supervisor"
                  ? "父 Agent 通过注册表委派子 Agent，默认不读取技能。"
                  : "当前角色没有技能授权。"}
              </p>
            )}
          </section>
          <section className="agent-settings-note">
            <span className="eyebrow">CONFIGURATION / 使用规则</span>
            <h3>网页与文件，各自保存。</h3>
            <p>
              网页配置保存到当前工作区，优先于同角色的启动配置。切换为“跟随启动配置”并保存后，重新使用启动时的文件、环境变量或默认值。
            </p>
            <p>
              llm-config.json 只在通过 --llm-config 或 XHS_LLM_CONFIG
              指定时加载；修改后需要重启。启动文件中的高级扩展参数仅在跟随启动配置时生效。
            </p>
            <p>
              一键应用会复制网页支持的接口参数、输出上限、超时与凭据，并为目标
              Agent 创建独立的网页配置。
            </p>
            <p>
              调研模型目前整理已有素材，尚未接入联网检索。审核始终保留本地规则检查。
            </p>
            <p>发布使用已登录账号与平台连接器，无需配置模型。</p>
          </section>
        </aside>
      </div>
    </>
  );
}
