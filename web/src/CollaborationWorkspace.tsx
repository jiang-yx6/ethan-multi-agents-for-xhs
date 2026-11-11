import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, isBusy, safeLink } from "./api";
import type {
  ArtifactReference,
  ConversationMessage,
  Draft,
  Idea,
  TaskActions,
  TaskDetail,
} from "./types";
import { Badge, Icon, Modal, message } from "./ui";
import DraftEditor from "./DraftEditor";

const agentNames: Record<string, string> = {
  supervisor: "父 Agent",
  ideation: "选题 Agent",
  research: "调研 Agent",
  creation: "创作 Agent",
  review: "审核 Agent",
};
const artifactNames: Record<string, string> = {
  ideas: "候选选题",
  idea: "选定方向",
  brief: "资料简报",
  draft: "内容草稿",
  review: "审核报告",
  trends: "热点资料",
  material: "素材",
};

export default function CollaborationWorkspace(
  props: TaskActions & { detail: TaskDetail },
) {
  const [editor, setEditor] = useState(false);
  const [editorDirty, setEditorDirty] = useState(false);
  const [chatDirty, setChatDirty] = useState(false);
  useEffect(() => {
    props.onDirty(editorDirty || chatDirty);
  }, [editorDirty, chatDirty, props.onDirty]);
  useEffect(() => () => props.onDirty(false), [props.onDirty]);
  function closeEditor() {
    if (editorDirty && !window.confirm("草稿有未保存的修改，是否放弃？"))
      return;
    setEditorDirty(false);
    setEditor(false);
  }
  return (
    <>
      <Conversation
        {...props}
        disabled={editorDirty}
        onDirty={setChatDirty}
        onOpenDraft={() => setEditor(true)}
      />
      <Results detail={props.detail} onOpenDraft={() => setEditor(true)} />
      {editor && (
        <Modal
          title="草稿与审核"
          className="draft-editor-dialog"
          onClose={closeEditor}
        >
          <DraftEditor
            {...props}
            onDirty={setEditorDirty}
            onPublish={() => {
              setEditor(false);
              props.onPublish();
            }}
            onConfirm={() => {
              setEditor(false);
              props.onConfirm();
            }}
          />
        </Modal>
      )}
    </>
  );
}

function Conversation(
  props: TaskActions & {
    detail: TaskDetail;
    disabled: boolean;
    onOpenDraft: () => void;
  },
) {
  const { detail } = props;
  const { task } = detail;
  const busy = isBusy(detail.job);
  const pause = detail.workflow.interrupts[0];
  const [text, setText] = useState("");
  const [boundInterrupt, setBoundInterrupt] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const canAnswer = detail.allowed_actions.includes("answer");
  const canMessage = detail.allowed_actions.includes("message");
  const stale = Boolean(
    text.trim() && boundInterrupt && boundInterrupt !== pause?.id,
  );
  const canSend =
    (canAnswer || canMessage) &&
    !busy &&
    !props.loading &&
    !props.disabled &&
    !sending;
  const messages = [...(detail.collaboration?.messages || [])];
  if (
    pause?.kind === "ask_user" &&
    pause.value.question &&
    !messages.some(
      (m) => m.kind === "ask_user" && m.content === pause.value.question,
    )
  ) {
    messages.push({
      id: pause.id,
      role: "assistant",
      kind: "ask_user",
      content: pause.value.question,
    });
  }
  const progress = [...detail.events]
    .reverse()
    .find((event) =>
      ["model_call", "delegation_started", "skill_tool", "agent_tool", "graph_step"].includes(
        event.kind,
      ),
    );
  useEffect(() => {
    props.onDirty(Boolean(text.trim()));
  }, [text, props.onDirty]);
  useEffect(() => () => props.onDirty(false), [props.onDirty]);
  useEffect(() => {
    if (stick.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages.length, busy, task.current_version]);

  async function send(event: FormEvent) {
    event.preventDefault();
    if (!canSend || stale || !text.trim()) return;
    const interrupt = boundInterrupt || pause?.id;
    setSending(true);
    try {
      await props.onAction(
        canAnswer ? "answer" : "message",
        {
          ...(canAnswer ? { answer: text.trim() } : { text: text.trim() }),
          interrupt_id: interrupt,
          expected_version: task.current_version,
        },
        () => {
          setText("");
          setBoundInterrupt(null);
          props.onDirty(false);
        },
      );
    } finally {
      setSending(false);
    }
  }
  const error =
    task.last_error ||
    (detail.job?.status === "failed" ? detail.job.error : null) ||
    detail.workflow.error;
  const hint = props.disabled
    ? "请先保存或放弃草稿中的编辑，再发送消息。"
    : busy || sending
      ? "团队正在处理本轮消息，结果会出现在右侧。"
      : canAnswer
        ? "父 Agent 正在等你回复，补充后继续分工。"
        : canMessage
          ? "继续讨论内容或提出修改要求；发送后需要重新确认结果。"
          : task.status === "completed"
            ? "本轮任务已完成，可以新建创作开始新的对话。"
            : "当前任务暂不能发送消息，请先处理右侧结果或下方提示。";
  return (
    <section className="conversation-panel" aria-label="与父 Agent 对话">
      <header className="conversation-header">
        <div className="agent-avatar">主</div>
        <div>
          <h2>与父 Agent 对话</h2>
          <p>目标、想法与每一次反馈，都在这里。</p>
        </div>
        <Badge status={task.status} busy={busy} />
      </header>
      <div className="conversation-goal">
        <span>本次创作</span>
        <strong>{task.topic}</strong>
      </div>
      <div
        className="conversation-stream"
        ref={scroll}
        role="log"
        aria-label="对话记录"
        aria-live="polite"
        onScroll={() => {
          const el = scroll.current!;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        }}
      >
        <ChatBubble
          message={{
            id: "goal",
            role: "user",
            kind: "message",
            content:
              task.request.goal +
              (task.request.supplied_materials.length
                ? "\n\n提供的素材：\n" +
                  task.request.supplied_materials.join("\n")
                : ""),
          }}
        />
        {messages.map((item) => (
          <ChatBubble
            key={item.id}
            message={item}
            missing={
              item.kind === "ask_user" && item.content === pause?.value.question
                ? pause.value.missing_information
                : undefined
            }
          />
        ))}
        {!messages.length && !busy && (
          <p className="chat-system-message">
            父 Agent 会结合你的需求与画像安排工作，具体执行结果显示在右侧。
          </p>
        )}
        {busy && (
          <div className="chat-working" role="status">
            <span className="spinner" />
            {progress?.message || "父 Agent 正在协调团队…"}
          </div>
        )}
        {error && (
          <div className="notice error" role="alert">
            {error}
          </div>
        )}
      </div>
      {task.draft && (
        <button className="chat-draft-shortcut" onClick={props.onOpenDraft}>
          <span>
            已有草稿{" "}
            <strong>
              v{task.current_version} · {task.draft.title}
            </strong>
          </span>
          <span>查看与审核 ↗</span>
        </button>
      )}
      <form
        className="conversation-composer"
        onSubmit={(event) => void send(event)}
      >
        {stale && !sending && (
          <div className="notice" role="alert">
            当前问题已更新，请确认最新对话。
            <button
              type="button"
              className="button small"
              onClick={() => setBoundInterrupt(pause?.id || null)}
            >
              保留文字并回应当前问题
            </button>
          </div>
        )}
        <label htmlFor="supervisor-message" className="sr-only">
          给父 Agent 发消息
        </label>
        <textarea
          id="supervisor-message"
          value={text}
          maxLength={10000}
          rows={3}
          disabled={!canSend}
          placeholder={
            canAnswer
              ? "回复父 Agent，补充角度或真实素材…"
              : "说说你的想法，或希望怎样调整这篇内容…"
          }
          aria-describedby="conversation-hint"
          onChange={(event) => {
            if (!text) setBoundInterrupt(pause?.id || null);
            setText(event.target.value);
          }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              event.currentTarget.form?.requestSubmit();
            }
          }}
        />
        <div className="composer-footer">
          <p id="conversation-hint">{hint}</p>
          <button
            className="button primary"
            type="submit"
            disabled={!canSend || !text.trim() || stale}
          >
            {sending ? "处理中…" : "发送消息"}
            <Icon name="arrow" />
          </button>
        </div>
        <div className="chat-task-actions">
          {detail.allowed_actions.includes("retry") && (
            <button
              className="button small"
              type="button"
              disabled={props.loading}
              onClick={() => void props.onAction("retry")}
            >
              重试任务
            </button>
          )}
          {detail.allowed_actions.includes("reconcile") && (
            <button
              className="button small"
              type="button"
              disabled={props.loading}
              onClick={() => void props.onAction("reconcile")}
            >
              恢复待办
            </button>
          )}
          {error && (
            <button
              className="button text"
              type="button"
              onClick={props.onSettings}
            >
              检查模型设置
            </button>
          )}
          {detail.allowed_actions.includes("cancel") && !text.trim() && (
            <button
              className="button text"
              type="button"
              disabled={props.loading}
              onClick={() =>
                window.confirm("取消这次创作？") &&
                void props.onAction("cancel")
              }
            >
              取消创作
            </button>
          )}
        </div>
      </form>
    </section>
  );
}

function ChatBubble({
  message: item,
  missing,
}: {
  message: ConversationMessage;
  missing?: string[];
}) {
  const agent = agentNames[item.agent_id || ""] || item.agent_id;
  return (
    <article className={`chat-message ${item.role}`}>
      <div className="chat-author">
        <span className="chat-avatar">
          {item.role === "user" ? "你" : "主"}
        </span>
        <strong>{item.role === "user" ? "你" : "父 Agent"}</strong>
        {item.kind === "delegate" && (
          <span className="chat-kind">委派 · {agent}</span>
        )}
        {item.kind === "ask_user" && (
          <span className="chat-kind">确认方向</span>
        )}
        {item.kind === "finish" && <span className="chat-kind">本轮交付</span>}
      </div>
      <div className="chat-bubble">
        <p>
          {item.kind === "delegate" && (
            <span className="chat-delegation-lead">交给{agent}处理：</span>
          )}
          {item.content}
        </p>
        {Boolean(missing?.length) && (
          <div className="chat-missing">
            <span>可以一起补充</span>
            <ul>
              {missing!.map((value, i) => (
                <li key={i}>{value}</li>
              ))}
            </ul>
          </div>
        )}
        {item.reason && item.reason !== item.content && (
          <details className="chat-rationale">
            <summary>查看安排说明</summary>
            <p>{item.reason}</p>
          </details>
        )}
      </div>
    </article>
  );
}

function Results({
  detail,
  onOpenDraft,
}: {
  detail: TaskDetail;
  onOpenDraft: () => void;
}) {
  const collaboration = detail.collaboration;
  const history = collaboration?.history || [];
  const decisions = (collaboration?.messages || []).filter(
    (item) => item.role === "assistant",
  );
  const known = new Set(decisions.map((item) => item.delegation_id));
  const legacy: ConversationMessage[] = history
    .filter((record) => !known.has(record.task.delegation_id))
    .map((record) => ({
      id: record.task.delegation_id,
      role: "assistant",
      kind: "delegate",
      content: record.task.instruction,
      agent_id: record.task.target_agent,
      delegation_id: record.task.delegation_id,
    }));
  const steps = [...legacy, ...decisions];
  const busy = isBusy(detail.job);
  const latest = [...detail.events]
    .reverse()
    .find((event) => !["queued", "finished"].includes(event.kind));
  return (
    <aside className="collaboration-results" aria-label="步骤结果与进度">
      <header className="results-header">
        <div>
          <span className="eyebrow">WORK IN PROGRESS</span>
          <h2>每一步，都有结果。</h2>
        </div>
        <span className={`team-state ${busy ? "working" : ""}`}>
          <i />
          {busy ? "执行中" : "进度已保存"}
        </span>
      </header>
      <div className="results-scroll">
        <div className="results-summary">
          <strong>
            {history
              .filter((item) => item.result.status === "completed")
              .length.toString()
              .padStart(2, "0")}
          </strong>
          <span>
            次委派已完成
            <br />
            <small>分工随对话推进</small>
          </span>
          <span className="results-profile">{detail.task.profile.name}</span>
        </div>
        {busy && (
          <div className="step-running">
            <span className="spinner" />
            <p>{latest?.message || "正在安排下一步"}</p>
          </div>
        )}
        {detail.task.draft && (
          <section className="current-delivery">
            <span className="eyebrow">LATEST / 最新交付</span>
            <h3>{detail.task.draft.title}</h3>
            <p>
              草稿 v{detail.task.current_version} ·{" "}
              {detail.task.review
                ? `${detail.task.review.issues.length} 项审核提醒`
                : "等待自动审核"}
            </p>
            <button className="button" onClick={onOpenDraft}>
              打开草稿与审核
              <Icon name="arrow" />
            </button>
          </section>
        )}
        <div className="collaboration-steps">
          {!steps.length && (
            <div className="steps-empty">
              <span>01</span>
              <h3>从理解需求开始</h3>
              <p>
                父 Agent
                正在了解你的目标。作出安排后，委派与结果会按顺序显示在这里。
              </p>
            </div>
          )}
          {steps.map((step, index) => {
            const record = history.find(
              (item) => item.task.delegation_id === step.delegation_id,
            );
            const replied =
              step.kind === "ask_user" &&
              (collaboration?.messages || []).some(
                (item, i, all) =>
                  item.role === "user" &&
                  i > all.findIndex((value) => value.id === step.id),
              );
            const status =
              step.kind === "delegate"
                ? record?.result.status === "completed"
                  ? "已完成"
                  : record?.result.status === "needs_input"
                    ? "待补充"
                    : record?.result.status === "failed" ||
                        detail.task.status === "failed"
                      ? "执行失败"
                      : busy
                        ? "执行中"
                        : "待执行"
                : step.kind === "ask_user"
                  ? replied
                    ? "已回复"
                    : "待回复"
                  : "已交付";
            return (
              <details
                className="collaboration-step"
                key={step.id}
                open={index === steps.length - 1 || undefined}
              >
                <summary>
                  <span className="step-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                  <div>
                    <strong>
                      {step.kind === "delegate"
                        ? agentNames[step.agent_id || ""] || step.agent_id
                        : step.kind === "ask_user"
                          ? "确认内容方向"
                          : "本轮结果交付"}
                    </strong>
                    <small>
                      {step.kind === "delegate" ? "专业分工" : "父 Agent"}
                    </small>
                  </div>
                  <span
                    className={`step-status ${status === "执行失败" ? "failed" : ""}`}
                  >
                    {status}
                  </span>
                </summary>
                <div className="step-content">
                  <p className="step-instruction">{step.content}</p>
                  {record && (
                    <>
                      <div className="step-result">
                        <span>执行结果</span>
                        <p>{record.result.summary}</p>
                      </div>
                      {record.result.failure_reason && (
                        <p className="notice error">
                          {record.result.failure_reason}
                        </p>
                      )}
                      {record.result.missing_information.length > 0 && (
                        <p className="notice">
                          待补充：{record.result.missing_information.join("；")}
                        </p>
                      )}
                      {record.result.artifacts.map((ref) => (
                        <Artifact
                          key={`${ref.artifact_id}:${ref.version}`}
                          reference={ref}
                        />
                      ))}
                    </>
                  )}
                  {!record && step.kind === "delegate" && (
                    <p className="side-hint">
                      {status === "执行失败"
                        ? "本次执行尚未产生结果，可检查错误后重试。"
                        : "结果将在本次委派完成后显示。"}
                    </p>
                  )}
                </div>
              </details>
            );
          })}
        </div>
        {Boolean(collaboration?.artifacts.length) && (
          <details className="all-artifacts">
            <summary>全部产物 · {collaboration!.artifacts.length}</summary>
            {collaboration!.artifacts.map((ref) => (
              <Artifact
                key={`${ref.artifact_id}:${ref.version}`}
                reference={ref}
              />
            ))}
          </details>
        )}
      </div>
    </aside>
  );
}

function Artifact({ reference: ref }: { reference: ArtifactReference }) {
  const [open, setOpen] = useState(false);
  const [content, setContent] = useState<unknown>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    if (!open) return;
    let active = true;
    setError("");
    const params = new URLSearchParams({
      artifact_id: ref.artifact_id,
      version: String(ref.version),
    });
    void api<{ content: unknown }>(`/tasks/${ref.task_id}/artifacts?${params}`)
      .then((value) => {
        if (active) setContent(value.content);
      })
      .catch((err) => {
        if (active) setError(message(err));
      });
    return () => {
      active = false;
    };
  }, [open, ref.task_id, ref.artifact_id, ref.version, retry]);
  return (
    <details
      className="artifact-card"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <span>
          {artifactNames[ref.kind]}
          <small>v{ref.version}</small>
        </span>
        <span>展开内容 ↗</span>
      </summary>
      {open && (
        <div className="artifact-content">
          {error ? (
            <p role="alert">
              {error}
              <button
                className="button small"
                onClick={() => setRetry((value) => value + 1)}
              >
                重新读取
              </button>
            </p>
          ) : content === null ? (
            <p role="status">正在读取结果…</p>
          ) : (
            <ArtifactContent reference={ref} content={content} />
          )}
        </div>
      )}
    </details>
  );
}

function ArtifactContent({
  reference: ref,
  content,
}: {
  reference: ArtifactReference;
  content: unknown;
}) {
  if (ref.kind === "ideas")
    return (
      <>
        {(content as Idea[]).map((idea, index) => (
          <div className="artifact-idea" key={index}>
            <small>方向 {index}</small>
            <h4>{idea.title}</h4>
            <p>{idea.why}</p>
            <p>角度：{idea.angle}</p>
            <p>面向：{idea.audience}</p>
            {idea.required_materials.length > 0 && (
              <p>需要素材：{idea.required_materials.join("；")}</p>
            )}
          </div>
        ))}
      </>
    );
  if (ref.kind === "draft") {
    const draft = content as Draft;
    return (
      <>
        <h4>{draft.title}</h4>
        <div className="artifact-prose">{draft.body}</div>
        <p>{draft.tags.map((tag) => "#" + tag).join(" ")}</p>
        <small>阅读此版本；编辑与批准请打开当前草稿。</small>
      </>
    );
  }
  if (ref.kind === "review") {
    const report = content as {
      issues: { severity: string; message: string }[];
    };
    return report.issues.length ? (
      <ul>
        {report.issues.map((issue, i) => (
          <li key={i}>
            <strong>{issue.severity === "error" ? "需处理" : "提醒"}</strong> ·{" "}
            {issue.message}
          </li>
        ))}
      </ul>
    ) : (
      <p>本次检查未发现问题，发布前仍需人工审核。</p>
    );
  }
  if (ref.kind === "brief") {
    const brief = content as {
      topic: string;
      angles: string[];
      supplied_materials: string[];
      pending_questions: string[];
      sources: { title: string; url: string }[];
    };
    return (
      <>
        <h4>{brief.topic}</h4>
        <h5>写作角度</h5>
        {brief.angles.map((value, i) => (
          <p key={i}>{value}</p>
        ))}
        <h5>已有素材</h5>
        {brief.supplied_materials.map((value, i) => (
          <p key={i}>{value}</p>
        ))}
        {brief.pending_questions.length > 0 && (
          <>
            <h5>待核实</h5>
            <ul>
              {brief.pending_questions.map((value, i) => (
                <li key={i}>{value}</li>
              ))}
            </ul>
          </>
        )}
        {brief.sources.map((source, i) => (
          <p key={i}>
            {safeLink(source.url) ? (
              <a href={safeLink(source.url)} target="_blank" rel="noreferrer">
                {source.title} ↗
              </a>
            ) : (
              source.title
            )}
          </p>
        ))}
      </>
    );
  }
  return (
    <div className="artifact-prose">
      {typeof content === "string" ? content : ref.summary}
    </div>
  );
}
