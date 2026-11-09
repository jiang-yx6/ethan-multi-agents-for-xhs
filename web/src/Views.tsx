import { useEffect, useState, type FormEvent } from "react";
import { api, isBusy, post, put, requestId } from "./api";
import type {
  Account,
  Job,
  ModelSettings,
  Profile,
  ProfileRecord,
  TaskDetail,
} from "./types";
import { Heading, Icon, Modal, lines, message } from "./ui";

export function NewTask({
  profiles,
  pending,
  onClose,
  onCreate,
}: {
  profiles: ProfileRecord[];
  pending: boolean;
  onClose: () => void;
  onCreate: (body: Record<string, unknown>) => Promise<void>;
}) {
  const [error, setError] = useState("");
  const [id] = useState(requestId);
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    setError("");
    try {
      const goal = String(fields.get("goal") || "").trim();
      if (!goal) {
        setError("请填写本次创作目标。");
        return;
      }
      const raw = String(fields.get("trends") || "").trim();
      const trends = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(trends)) throw new Error("热点须为 JSON 数组。");
      void onCreate({
        request_id: id,
        profile_id: fields.get("profile_id"),
        trends,
        request: {
          goal,
          topic: goal,
          content_type: fields.get("content_type"),
          use_trends: Boolean(fields.get("use_trends")),
          needs_research: Boolean(fields.get("needs_research")),
          supplied_materials: lines(String(fields.get("materials") || "")),
          constraints: lines(String(fields.get("constraints") || "")),
        },
      });
    } catch (err) {
      setError(
        err instanceof SyntaxError
          ? "热点 JSON 格式无效，请检查标题与来源。"
          : message(err),
      );
    }
  }
  return (
    <Modal title="这次，想写些什么？" onClose={onClose}>
      <form id="new-task-form" onSubmit={submit}>
        <div className="form-field">
          <label htmlFor="new-goal">创作目标</label>
          <textarea
            id="new-goal"
            name="goal"
            required
            autoFocus
            placeholder="例如：整理一条周末散步路线，让读者可以照着走。"
          />
        </div>
        <div className="form-grid">
          <div>
            <label htmlFor="new-type">内容形式</label>
            <select id="new-type" name="content_type">
              {["分享", "攻略", "观点", "生活记录", "测评"].map((label) => (
                <option key={label}>{label}</option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="new-profile">创作画像</label>
            <select id="new-profile" name="profile_id">
              {profiles.map((item) => (
                <option key={item.profile_id} value={item.profile_id}>
                  {item.profile.name}
                </option>
              ))}
            </select>
          </div>
          <div className="full">
            <label htmlFor="new-materials">已有素材（每行一条，选填）</label>
            <textarea
              id="new-materials"
              name="materials"
              placeholder="真实经历、观察或准备好的资料"
            />
          </div>
        </div>
        <details className="advanced">
          <summary>热点、资料与其他约束</summary>
          <label className="checkbox-line">
            <input type="checkbox" name="use_trends" />
            结合我提供的热点
          </label>
          <label htmlFor="new-trends">热点 JSON（选填）</label>
          <textarea
            id="new-trends"
            name="trends"
            placeholder={
              '[{"title":"话题标题","url":"https://来源链接","summary":"相关背景"}]'
            }
          />
          <label className="checkbox-line">
            <input type="checkbox" name="needs_research" />
            整理外部资料（当前为 Mock 检索）
          </label>
          <label htmlFor="new-constraints">本次约束（每行一条）</label>
          <textarea
            id="new-constraints"
            name="constraints"
            placeholder="例如：不写未经核实的价格"
          />
        </details>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-foot">
          <button type="button" className="button" onClick={onClose}>
            稍后再写
          </button>
          <button
            className="button primary"
            disabled={pending || profiles.length === 0}
          >
            开始选题 <Icon name="arrow" />
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ProfileView({
  records,
  reload,
  notify,
}: {
  records: ProfileRecord[];
  reload: () => Promise<void>;
  notify: (value: string) => void;
}) {
  const [selected, setSelected] = useState(records[0]?.profile_id || "default"),
    [profile, setProfile] = useState<Profile | null>(null),
    [pending, setPending] = useState(false);
  useEffect(() => {
    setProfile(
      records.find((item) => item.profile_id === selected)?.profile ||
        records[0]?.profile ||
        null,
    );
  }, [records, selected]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!profile) return;
    setPending(true);
    try {
      await put(`/profiles/${selected}`, { profile });
      await reload();
      notify("画像已保存，新任务将使用新快照。");
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }
  async function add() {
    if (!profile) return;
    setPending(true);
    try {
      const saved = await post<ProfileRecord>("/profiles", {
        profile: { ...profile, name: "新的创作画像" },
      });
      await reload();
      setSelected(saved.profile_id);
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }
  if (!profile) return <p className="empty">正在载入画像…</p>;
  return (
    <>
      <Heading
        eyebrow="IDENTITY / 创作画像"
        title="先认识自己，再开始表达。"
        note="真实经历与表达偏好分开保存。已有任务保留创建时的画像快照。"
      />
      <div className="page-grid">
        <section className="surface">
          <div className="section-label">
            <select
              aria-label="选择创作画像"
              value={selected}
              onChange={(e) => setSelected(e.target.value)}
            >
              {records.map((item) => (
                <option key={item.profile_id} value={item.profile_id}>
                  {item.profile.name}
                </option>
              ))}
            </select>
            <button
              className="button small"
              disabled={pending}
              onClick={() => void add()}
            >
              新建画像
            </button>
          </div>
          <form id="profile-form" onSubmit={(e) => void save(e)}>
            <div className="form-grid">
              {(
                [
                  ["name", "画像名称"],
                  ["positioning", "账号定位"],
                  ["audience", "目标读者"],
                  ["style", "表达风格"],
                ] as const
              ).map(([key, label]) => (
                <div key={key}>
                  <label htmlFor={`profile-${key}`}>{label}</label>
                  <input
                    id={`profile-${key}`}
                    value={profile[key]}
                    required
                    onChange={(e) =>
                      setProfile({ ...profile, [key]: e.target.value })
                    }
                  />
                </div>
              ))}
              {(
                [
                  ["interests", "关注领域"],
                  ["experiences", "真实经历"],
                  ["preferences", "表达偏好"],
                  ["boundaries", "表达边界"],
                ] as const
              ).map(([key, label]) => (
                <div key={key} className="full">
                  <label htmlFor={`profile-${key}`}>{label}（每行一条）</label>
                  <textarea
                    id={`profile-${key}`}
                    value={profile[key].join("\n")}
                    onChange={(e) =>
                      setProfile({
                        ...profile,
                        [key]: e.target.value.split("\n"),
                      })
                    }
                  />
                </div>
              ))}
            </div>
            <div className="form-bottom">
              <button className="button primary" disabled={pending}>
                保存创作画像
              </button>
            </div>
          </form>
        </section>
        <aside className="surface profile-sample">
          <span className="label">YOUR OWN VOICE</span>
          <h2>
            有自己的视角，
            <br />
            内容才会被记住。
          </h2>
          <blockquote>“{profile.positioning}”</blockquote>
          <p>
            把真实的生活留给内容，
            <br />
            把表达的选择留给自己。
          </p>
        </aside>
      </div>
    </>
  );
}

export function SettingsView({
  onSaved,
  notify,
}: {
  onSaved: () => void;
  notify: (value: string) => void;
}) {
  const [settings, setSettings] = useState<ModelSettings | null>(null),
    [apiKey, setApiKey] = useState(""),
    [clearKey, setClearKey] = useState(false),
    [role, setRole] = useState("ideation"),
    [skills, setSkills] = useState<
      { id: string; name: string; description: string }[]
    >([]),
    [pending, setPending] = useState(false),
    [skillsLoading, setSkillsLoading] = useState(true);
  function changeProvider(value: string) {
    const keys: Record<string, string> = {
      deepseek: "DEEPSEEK_API_KEY",
      openai: "OPENAI_API_KEY",
      anthropic: "ANTHROPIC_API_KEY",
      qwen: "DASHSCOPE_API_KEY",
      zhipu: "ZHIPU_API_KEY",
      minimax: "MINIMAX_API_KEY",
      ollama: "OLLAMA_API_KEY",
    };
    setApiKey("");
    setClearKey(false);
    setSettings((current) =>
      current
        ? {
            ...current,
            provider: value,
            ...(value !== current.provider
              ? {
                  api_key_env: keys[value] || "XHS_LLM_API_KEY",
                  protocol: null,
                  base_url: null,
                  model: "",
                  credential_configured: false,
                  api_key_configured: false,
                }
              : {}),
          }
        : current,
    );
  }
  useEffect(() => {
    void api<ModelSettings>("/settings/ideation")
      .then(setSettings)
      .catch((error) => notify(message(error)));
  }, []);
  useEffect(() => {
    let active = true;
    setSkills([]);
    setSkillsLoading(true);
    void api<typeof skills>(`/skills?role=${role}`)
      .then((items) => {
        if (active) setSkills(items);
      })
      .catch((error) => notify(message(error)))
      .finally(() => {
        if (active) setSkillsLoading(false);
      });
    return () => {
      active = false;
    };
  }, [role]);
  async function save(event: FormEvent) {
    event.preventDefault();
    if (!settings) return;
    setPending(true);
    const {
      credential_configured,
      model_configured,
      api_key_configured,
      ...body
    } = settings;
    try {
      setSettings(
        await put<ModelSettings>("/settings/ideation", {
          ...body,
          base_url: body.base_url || null,
          protocol: body.protocol || null,
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
          clear_api_key: clearKey,
        }),
      );
      setApiKey("");
      setClearKey(false);
      onSaved();
      notify("选题配置已保存，将用于下一次执行或重试。");
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <Heading
        eyebrow="CAPABILITIES / 技能与模型"
        title="让每个助手，各司其职。"
        note="技能按角色授权；模型配置在服务端保存，选题时按需加载。"
      />
      <div className="page-grid">
        <section className="surface">
          <h2>角色与技能</h2>
          <p>当前展示实际注册表中的授权目录。</p>
          <div className="settings-tabs">
            {Object.entries({
              ideation: "选题",
              research: "资料",
              creation: "创作",
              review: "审核",
              publishing: "发布",
            }).map(([value, label]) => (
              <button
                key={value}
                className={role === value ? "active" : ""}
                onClick={() => setRole(value)}
              >
                {label}
              </button>
            ))}
          </div>
          {skills.map((skill) => (
            <div className="skill-row" key={skill.id}>
              <span className="skill-icon">✳</span>
              <div>
                <h3>{skill.id}</h3>
                <p>{skill.description}</p>
              </div>
              <span className="status">已授权</span>
            </div>
          ))}
          {skillsLoading ? (
            <p className="empty">正在读取授权目录…</p>
          ) : (
            skills.length === 0 && (
              <p className="empty">当前角色没有技能授权。</p>
            )
          )}
          <p className="form-help">
            首版提供只读目录；技能授权可继续通过现有 CLI 管理。
          </p>
        </section>
        <section className="surface">
          <h2>选题模型</h2>
          <p>资料整理与创作仍为 Mock，审核采用基本规则。</p>
          {settings && (
            <form id="model-form" onSubmit={(e) => void save(e)}>
              <div className="form-grid">
                <div className="full">
                  <label htmlFor="backend">选题方式</label>
                  <select
                    id="backend"
                    value={settings.backend}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        backend: e.target.value as "llm" | "mock",
                      })
                    }
                  >
                    <option value="llm">真实模型</option>
                    <option value="mock">离线示例（Mock）</option>
                  </select>
                </div>
                <div className="full">
                  <label htmlFor="provider">模型服务商</label>
                  <input
                    list="providers"
                    id="provider"
                    disabled={pending}
                    required
                    value={settings.provider}
                    onChange={(e) => changeProvider(e.target.value)}
                  />
                  <datalist id="providers">
                    {[
                      "deepseek",
                      "openai",
                      "anthropic",
                      "qwen",
                      "zhipu",
                      "minimax",
                      "ollama",
                    ].map((value) => (
                      <option key={value}>{value}</option>
                    ))}
                  </datalist>
                </div>
                <div className="full">
                  <label htmlFor="model-id">模型 ID</label>
                  <input
                    id="model-id"
                    disabled={pending}
                    required={settings.backend === "llm"}
                    placeholder="填写账号可用的实际模型 ID"
                    value={settings.model}
                    onChange={(e) =>
                      setSettings({ ...settings, model: e.target.value })
                    }
                  />
                </div>
                <div className="full">
                  <label htmlFor="credential-source">密钥配置方式</label>
                  <select
                    id="credential-source"
                    value={settings.credential_source}
                    disabled={pending}
                    onChange={(e) => {
                      setApiKey("");
                      setClearKey(false);
                      setSettings({
                        ...settings,
                        credential_source: e.target
                          .value as ModelSettings["credential_source"],
                        credential_configured: false,
                      });
                    }}
                  >
                    <option value="api_key">直接填写 API Key</option>
                    <option value="environment">使用服务端环境变量</option>
                  </select>
                </div>
                {settings.credential_source === "api_key" ? (
                  <div className="full">
                    <label htmlFor="model-api-key">模型 API Key</label>
                    <input
                      id="model-api-key"
                      type="password"
                      autoComplete="new-password"
                      spellCheck={false}
                      disabled={pending || clearKey}
                      value={apiKey}
                      placeholder={
                        settings.api_key_configured
                          ? "已保存，留空保持不变；输入新密钥可替换"
                          : "粘贴当前服务商的 API Key"
                      }
                      onChange={(e) => setApiKey(e.target.value)}
                    />
                    <p
                      className="form-help"
                      id="credential-status"
                      role="status"
                    >
                      {settings.api_key_configured
                        ? "已保存密钥。"
                        : "尚未保存当前接口的密钥。"}
                      密钥在本机加密保存，不会回显；保存后用于下一次执行，无需重启。
                    </p>
                    {settings.api_key_configured && (
                      <label className="checkbox-line">
                        <input
                          type="checkbox"
                          checked={clearKey}
                          disabled={pending}
                          onChange={(e) => {
                            setClearKey(e.target.checked);
                            setApiKey("");
                          }}
                        />
                        清除当前接口已保存的密钥
                      </label>
                    )}
                  </div>
                ) : (
                  <div className="full">
                    <label htmlFor="key-env">密钥环境变量名称</label>
                    <input
                      id="key-env"
                      required
                      value={settings.api_key_env}
                      disabled={pending}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          api_key_env: e.target.value,
                          credential_configured: false,
                        })
                      }
                    />
                    <p className="form-help">
                      {settings.credential_configured
                        ? "服务进程已找到对应凭据。"
                        : "请在启动服务的终端设置环境变量，修改后重启服务。"}
                    </p>
                  </div>
                )}
              </div>
              <details className="advanced">
                <summary>兼容网关与代理</summary>
                <label htmlFor="protocol">接口协议</label>
                <select
                  id="protocol"
                  disabled={pending}
                  value={settings.protocol || ""}
                  onChange={(e) => {
                    setApiKey("");
                    setClearKey(false);
                    setSettings({
                      ...settings,
                      api_key_configured: false,
                      credential_configured: false,
                      protocol: (e.target.value ||
                        null) as ModelSettings["protocol"],
                    });
                  }}
                >
                  <option value="">使用厂商预设</option>
                  <option value="openai">OpenAI Chat Completions</option>
                  <option value="anthropic">Anthropic Messages</option>
                </select>
                <label htmlFor="base-url">接口 Base URL（选填）</label>
                <input
                  id="base-url"
                  disabled={pending}
                  placeholder="https://gateway.example/v1"
                  value={settings.base_url || ""}
                  onChange={(e) => {
                    setApiKey("");
                    setClearKey(false);
                    setSettings({
                      ...settings,
                      api_key_configured: false,
                      credential_configured: false,
                      base_url: e.target.value || null,
                    });
                  }}
                />
                <label className="checkbox-line">
                  <input
                    type="checkbox"
                    checked={settings.trust_env}
                    onChange={(e) =>
                      setSettings({ ...settings, trust_env: e.target.checked })
                    }
                  />
                  使用系统代理环境变量
                </label>
              </details>
              <div className="form-bottom">
                <button className="button primary" disabled={pending}>
                  保存模型配置
                </button>
              </div>
            </form>
          )}
        </section>
      </div>
    </>
  );
}

export function AccountsView({ notify }: { notify: (value: string) => void }) {
  const [accounts, setAccounts] = useState<Account[]>([]),
    [showAdd, setShowAdd] = useState(false),
    [pending, setPending] = useState(false);
  async function reload() {
    try {
      setAccounts(await api<Account[]>("/accounts"));
    } catch (error) {
      notify(message(error));
    }
  }
  useEffect(() => {
    void reload();
    const timer = setInterval(() => void reload(), 2000);
    return () => clearInterval(timer);
  }, []);
  async function operate(account: Account, kind: string) {
    setPending(true);
    try {
      await post<Job>(
        `/accounts/${account.platform}/${account.account_id}/${kind}`,
        { request_id: requestId(), timeout: 180 },
      );
      await reload();
      notify(
        kind === "login"
          ? "登录作业已启动，请在本机浏览器窗口完成扫码。"
          : "身份校验已启动。",
      );
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }
  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    setPending(true);
    try {
      await post("/accounts", {
        account: {
          platform: "xiaohongshu",
          account_id: fields.get("account_id"),
        },
        label: fields.get("label"),
      });
      setShowAdd(false);
      await reload();
      notify("账号已登记，可以开始登录。");
    } catch (error) {
      notify(message(error));
    } finally {
      setPending(false);
    }
  }
  return (
    <>
      <Heading
        eyebrow="CHANNELS / 发布账号"
        title="每篇内容，都有明确的去处。"
        note="本地浏览器维护登录态；页面展示实际绑定结果与校验记录。"
        action={
          <button className="button primary" onClick={() => setShowAdd(true)}>
            <Icon name="plus" />
            添加账号
          </button>
        }
      />
      <div className="page-grid">
        <section className="surface">
          <h2>我的发布账号</h2>
          {accounts.map((account) => (
            <div
              key={account.platform + account.account_id}
              className="account-row"
            >
              <div className="account-card">
                <div className="platform-logo">小红书</div>
                <div className="account-description">
                  <h3>
                    {account.nickname || account.label || account.account_id}
                  </h3>
                  <p>
                    {account.platform} / {account.account_id}
                  </p>
                  <span
                    className={`status ${account.user_id ? "" : "neutral"}`}
                  >
                    {account.user_id ? "已绑定身份" : "尚未登录绑定"}
                  </span>
                </div>
                <div className="account-buttons">
                  <button
                    className="button small"
                    disabled={pending || isBusy(account.job)}
                    onClick={() => void operate(account, "login")}
                  >
                    登录
                  </button>
                  <button
                    className="button small"
                    disabled={pending || isBusy(account.job)}
                    onClick={() => void operate(account, "whoami")}
                  >
                    校验身份
                  </button>
                </div>
              </div>
              {isBusy(account.job) && (
                <p className="notice">
                  {account.job?.kind === "login"
                    ? "请在本机弹出的浏览器中完成扫码或验证。"
                    : "正在校验登录身份…"}
                </p>
              )}
              {account.job?.error && (
                <p className="notice error">{account.job.error}</p>
              )}
              {account.job?.status === "succeeded" && (
                <p className="form-help">
                  最近身份校验完成：
                  {new Date(account.job.updated_at).toLocaleString("zh-CN")}
                </p>
              )}
            </div>
          ))}
          {!accounts.length && (
            <div className="empty">
              还没有登记发布账号。
              <p>添加本地账号 ID 后，在本机浏览器完成登录。</p>
            </div>
          )}
          <div className="capabilities">
            <span className="capability">小红书图文</span>
            <span className="capability">人工核实回执</span>
            <span className="capability unsupported">视频 · 尚未接入</span>
            <span className="capability unsupported">定时发布 · 尚未接入</span>
          </div>
        </section>
        <aside className="surface">
          <h2>从提交到发布，每一步都清楚。</h2>
          <div className="timeline">
            <p>
              <strong>明确目标账号</strong>发布前核对真实平台用户与内容版本。
            </p>
            <p>
              <strong>提交后等待核实</strong>已提交和结果不明都会保留当前版本。
            </p>
            <p>
              <strong>登记实际作品链接</strong>
              人工核实后再标记发布成功，避免重复发帖。
            </p>
          </div>
        </aside>
      </div>
      {showAdd && (
        <Modal title="添加发布账号" onClose={() => setShowAdd(false)}>
          <form id="account-form" onSubmit={(e) => void add(e)}>
            <label htmlFor="account-id">本地账号 ID</label>
            <input
              id="account-id"
              name="account_id"
              required
              pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
              placeholder="例如 main"
            />
            <p className="form-help">用于隔离浏览器会话，会规范为小写。</p>
            <label htmlFor="account-label">账号备注</label>
            <input id="account-label" name="label" placeholder="例如 主账号" />
            <div className="modal-foot">
              <button className="button primary" disabled={pending}>
                登记账号
              </button>
            </div>
          </form>
        </Modal>
      )}
    </>
  );
}

export function PublishModal({
  detail,
  onClose,
  onSubmit,
}: {
  detail: TaskDetail;
  onClose: () => void;
  onSubmit: (params: Record<string, unknown>) => Promise<boolean>;
}) {
  const [accounts, setAccounts] = useState<Account[]>([]),
    [selected, setSelected] = useState(""),
    [valid, setValid] = useState(false),
    [error, setError] = useState(""),
    [pending, setPending] = useState(false);
  const [target] = useState({
    version: detail.task.current_version,
    interrupt: detail.workflow.interrupts[0]?.id,
  });
  const stale =
    target.version !== detail.task.current_version ||
    target.interrupt !== detail.workflow.interrupts[0]?.id;
  useEffect(() => {
    void api<Account[]>("/accounts")
      .then((items) => setAccounts(items.filter((item) => item.user_id)))
      .catch((err) => setError(message(err)));
  }, []);
  const account = accounts.find(
    (item) => `${item.platform}/${item.account_id}` === selected,
  );
  async function preflight() {
    if (!account || stale) return;
    setPending(true);
    setError("");
    try {
      await post(`/tasks/${detail.task.task_id}/preflight`, {
        expected_version: target.version,
        account: { platform: account.platform, account_id: account.account_id },
      });
      setValid(true);
    } catch (err) {
      setValid(false);
      setError(message(err));
    } finally {
      setPending(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!valid || !account || stale) return;
    setPending(true);
    const ok = await onSubmit({
      account: { platform: account.platform, account_id: account.account_id },
      confirmed: true,
      expected_version: target.version,
      interrupt_id: target.interrupt,
    });
    if (ok) onClose();
    else setPending(false);
  }
  return (
    <Modal title="确认本次发布" onClose={onClose}>
      <form id="publish-form" onSubmit={(event) => void submit(event)}>
        {stale && (
          <p className="notice error" role="alert">
            内容或发布确认位置已变化，请关闭弹窗后重新确认。
          </p>
        )}
        <dl className="publish-summary">
          <dt>内容版本</dt>
          <dd>v{detail.task.current_version} · 已人工批准</dd>
          <dt>笔记标题</dt>
          <dd>{detail.task.draft?.title}</dd>
          <dt>图片数量</dt>
          <dd>{detail.task.draft?.images.length || 0} 张</dd>
        </dl>
        <label htmlFor="publish-account">选择发布账号</label>
        <select
          id="publish-account"
          value={selected}
          onChange={(event) => {
            setSelected(event.target.value);
            setValid(false);
            setError("");
          }}
          required
        >
          <option value="">请选择已绑定身份的账号</option>
          {accounts.map((item) => (
            <option
              key={item.account_id}
              value={`${item.platform}/${item.account_id}`}
            >
              小红书 · {item.nickname || item.label || item.account_id}（
              {item.account_id}）
            </option>
          ))}
        </select>
        {!accounts.length && (
          <p className="notice">请先到“发布账号”登记账号并完成登录绑定。</p>
        )}
        <div className="preflight-row">
          <button
            type="button"
            className="button"
            disabled={!account || pending || stale}
            onClick={() => void preflight()}
          >
            检查发布条件
          </button>
          {valid && <span className="status">本地预检通过</span>}
        </div>
        {error && (
          <p className="notice error" role="alert">
            {error}
          </p>
        )}
        <label className="checkbox-line">
          <input type="checkbox" required />
          我确认当前版本和目标账号，允许真实提交
        </label>
        <p className="form-help">
          点击后将向所选账号实际提交图文。提交结果会保留，等待核实。
        </p>
        <div className="modal-foot">
          <button
            className="button primary"
            disabled={!valid || pending || stale}
          >
            确认真实发布
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function ConfirmModal({
  onClose,
  onSubmit,
}: {
  onClose: () => void;
  onSubmit: (params: Record<string, unknown>) => Promise<boolean>;
}) {
  const [pending, setPending] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const url = new FormData(event.currentTarget).get("note_url");
    setPending(true);
    const ok = await onSubmit({ note_url: url, confirmed: true });
    if (ok) onClose();
    else setPending(false);
  }
  return (
    <Modal title="核实发布结果" onClose={onClose}>
      <p className="modal-note">
        请先到创作者后台确认账号、内容和笔记链接。登记回执不会再次提交。
      </p>
      <form id="confirm-form" onSubmit={(event) => void submit(event)}>
        <label htmlFor="note-url">实际小红书笔记链接</label>
        <input
          id="note-url"
          type="url"
          name="note_url"
          placeholder="https://www.xiaohongshu.com/explore/…"
          required
        />
        <label className="checkbox-line">
          <input type="checkbox" required />
          我已在平台核实该笔记实际发布
        </label>
        <div className="modal-foot">
          <button className="button primary" disabled={pending}>
            登记已发布
          </button>
        </div>
      </form>
    </Modal>
  );
}
