import { useEffect, useState, type ChangeEvent } from "react";
import { api, isBusy, safeLink } from "./api";
import type { Draft, ImageReference, TaskActions, TaskDetail } from "./types";
import { Badge, Icon, Modal, message } from "./ui";

export default function DraftEditor(
  props: TaskActions & { detail: TaskDetail },
) {
  const { detail, onAction, notify } = props;
  const task = detail.task,
    busy = isBusy(detail.job),
    can = (action: string) =>
      detail.allowed_actions.includes(action) && !props.loading;
  const [tab, setTab] = useState("edit"),
    [dirty, setDirty] = useState(false);
  const [title, setTitle] = useState(task.draft?.title || ""),
    [body, setBody] = useState(task.draft?.body || ""),
    [tags, setTags] = useState(task.draft?.tags.join(", ") || "");
  const [images, setImages] = useState<ImageReference[]>(
      detail.images.map((image) => ({
        existing_index: image.index,
        description: image.description,
        url: image.available ? image.url : undefined,
      })),
    ),
    [baseVersion, setBaseVersion] = useState(task.current_version);
  const [baseInterrupt, setBaseInterrupt] = useState(
    detail.workflow.interrupts[0]?.id,
  );
  const [feedback, setFeedback] = useState(false),
    [history, setHistory] = useState<Draft | null>(null),
    [uploading, setUploading] = useState(false);
  function reset() {
    setTitle(task.draft?.title || "");
    setBody(task.draft?.body || "");
    setTags(task.draft?.tags.join(", ") || "");
    setImages(
      detail.images.map((image) => ({
        existing_index: image.index,
        description: image.description,
        url: image.available ? image.url : undefined,
      })),
    );
    setBaseVersion(task.current_version);
    setBaseInterrupt(detail.workflow.interrupts[0]?.id);
    setDirty(false);
    props.onDirty(false);
  }
  useEffect(() => {
    if (!dirty) reset();
  }, [
    task.current_version,
    task.task_id,
    detail.workflow.interrupts[0]?.id,
    dirty,
  ]);
  useEffect(() => {
    props.onDirty(dirty);
  }, [dirty]);
  const stale =
    baseVersion !== task.current_version ||
    (dirty && baseInterrupt !== detail.workflow.interrupts[0]?.id);
  const change = (setter: (value: string) => void, value: string) => {
    setter(value);
    setDirty(true);
  };
  const tagList = tags
    .split(/[,，\n]+/)
    .map((tag) => tag.trim().replace(/^#+/, ""))
    .filter(Boolean);
  const length =
    body.length +
    (tagList.length ? 1 + tagList.map((tag) => "#" + tag).join(" ").length : 0);
  async function save() {
    if (!title.trim() || !body.trim()) return notify("标题和正文都需要填写。");
    try {
      const saved = await onAction("drafts", {
        expected_version: baseVersion,
        interrupt_id: baseInterrupt,
        title,
        body,
        tags: tagList,
        images: images.map(({ description, media_id, existing_index }) => ({
          description,
          media_id,
          existing_index,
        })),
        content_type: task.draft?.content_type || task.request.content_type,
      });
      if (saved) {
        setDirty(false);
        props.onDirty(false);
      }
    } catch {
      /* App 显示错误；保留本地修改。 */
    }
  }
  async function upload(event: ChangeEvent<HTMLInputElement>) {
    const files = [...(event.target.files || [])];
    event.target.value = "";
    if (images.filter((image) => image.url).length + files.length > 9)
      return notify("首版小红书图文最多使用 9 张图片。");
    setUploading(true);
    try {
      const added: ImageReference[] = [];
      for (const file of files) {
        const data = new FormData();
        data.append("file", file);
        const media = await api<{
          media_id: string;
          url: string;
          filename: string;
        }>("/media", { method: "POST", body: data });
        added.push({
          media_id: media.media_id,
          description: media.filename,
          url: media.url,
          name: media.filename,
        });
      }
      setImages((current) => [
        ...current.filter((image) => image.url),
        ...added,
      ]);
      setDirty(true);
    } catch (error) {
      notify(message(error));
    } finally {
      setUploading(false);
    }
  }
  async function showHistory(version: number) {
    try {
      setHistory(await api<Draft>(`/tasks/${task.task_id}/drafts/${version}`));
    } catch (error) {
      notify(message(error));
    }
  }
  const pause =
    detail.workflow.status === "waiting_input"
      ? detail.workflow.interrupts[0]
      : null;
  const waitMessage =
    pause &&
    (
      {
        ask_user: "父 Agent 有一个问题需要你确认，请在下方回复",
        review: "等待你审核当前草稿",
        publish_gate: "等待你确认发布",
      } as Record<string, string>
    )[pause.kind];
  const progress = [...detail.events]
    .reverse()
    .find(
      (event) =>
        event.kind === "graph_step" ||
        event.kind === "model_call" ||
        event.kind === "skill_tool",
    );
  const executionError =
    task.last_error ||
    (detail.job?.status === "failed" ? detail.job.error : null) ||
    detail.workflow.error ||
    (task.publish_result?.status === "failed"
      ? task.publish_result.reason
      : null);
  return (
    <article className="editor-panel" aria-label="创作详情">
      <div className="editor-heading">
        <div className="task-context">
          <span>小红书 · {task.request.content_type}</span>
          <span className="dot">/</span>
          <span>v{task.current_version}</span>
          <Badge
            status={task.status}
            busy={busy}
            mock={Boolean(task.published && !task.publish_result?.platform)}
          />
        </div>
        <h2>{task.draft?.title || task.topic}</h2>
        <p className="task-goal">创作目标：{task.request.goal}</p>
      </div>
      {waitMessage && (
        <div className="notice panel-notice workflow-notice" role="status">
          <strong>{waitMessage}</strong>
          <span>进度已保存，可以稍后继续。</span>
        </div>
      )}
      {executionError && (
        <div className="notice error panel-notice" role="alert">
          {executionError}
        </div>
      )}
      {busy && (
        <div className="running-strip">
          <span className="spinner" />
          <span>
            {progress?.message ||
              (
                {
                  create: "正在准备候选方向",
                  draft: "正在保存新版本并审核",
                  publish: "正在向目标账号提交",
                } as Record<string, string>
              )[detail.job!.kind] ||
              "正在处理本次操作"}
          </span>
        </div>
      )}
      {!task.draft && pause?.kind !== "ask_user" && (
        <div className="editor-body progress-box">
          {busy ? (
            <>
              <div className="spinner" />
              <h3>正在把你的想法整理成方向…</h3>
              <p>可以离开页面，任务进度会保留在服务端。</p>
            </>
          ) : (
            <>
              <h3>
                {task.status === "failed"
                  ? "这次执行需要处理一个问题"
                  : "等待任务执行"}
              </h3>
              <p>检查模型配置后，可继续恢复这次任务。</p>
              <button className="button" onClick={props.onSettings}>
                打开模型设置
              </button>
            </>
          )}
        </div>
      )}
      {task.draft && (
        <>
          <div className="editor-tabs">
            {[
              ["edit", "正文编辑"],
              ["materials", "素材与依据"],
              ["history", "版本记录"],
            ].map(([value, label]) => (
              <button
                key={value}
                className={tab === value ? "active" : ""}
                onClick={() =>
                  dirty ? notify("请先保存修改，再切换标签。") : setTab(value)
                }
              >
                {label}
              </button>
            ))}
            <span className="version-label">{task.history.length} 个版本</span>
          </div>
          <div className="editor-body">
            {stale && dirty && (
              <div className="notice">
                内容或当前待办已更新，请先复制需要保留的修改，再加载最新状态。
                <button className="button small" onClick={reset}>
                  加载最新内容
                </button>
              </div>
            )}
            {tab === "edit" && (
              <>
                {["unknown", "submitted"].includes(task.status) && (
                  <div className="notice">
                    {task.status === "unknown"
                      ? "上次提交结果不明。"
                      : "内容已提交。"}
                    请先到平台后台核实，当前版本不会直接再次提交。
                  </div>
                )}
                {task.status === "published" && (
                  <div className="notice success">
                    {task.publish_result?.platform
                      ? `已确认发布 · 核实方式：${task.publish_result.verification === "manual" ? "人工" : "平台"}`
                      : "这是本地模拟发布记录"}
                    {safeLink(task.publish_result?.url || "") && (
                      <p>
                        <a
                          href={safeLink(task.publish_result!.url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          查看作品 ↗
                        </a>
                      </p>
                    )}
                  </div>
                )}
                <div className="field-heading">
                  <label htmlFor="draft-title">标题</label>
                  <span
                    className={`counter ${title.length > 20 ? "over" : ""}`}
                  >
                    {title.length} / 20
                  </span>
                </div>
                <input
                  id="draft-title"
                  className="title-input"
                  value={title}
                  readOnly={!can("draft")}
                  onChange={(event) => change(setTitle, event.target.value)}
                />
                <label className="sr-only" htmlFor="draft-body">
                  正文
                </label>
                <textarea
                  id="draft-body"
                  className="body-input"
                  value={body}
                  readOnly={!can("draft")}
                  onChange={(event) => change(setBody, event.target.value)}
                />
                <div className="field-heading">
                  <label htmlFor="draft-tags">话题（用逗号分隔）</label>
                  <span className={`counter ${length > 1000 ? "over" : ""}`}>
                    {length} / 1000
                  </span>
                </div>
                <input
                  id="draft-tags"
                  value={tags}
                  readOnly={!can("draft")}
                  onChange={(event) => change(setTags, event.target.value)}
                />
                <div className="media-gallery">
                  {images.map((image, index) => (
                    <div
                      className="media-tile"
                      key={image.media_id || `existing-${image.existing_index}`}
                    >
                      <div className="media-thumb">
                        {image.url ? (
                          <img src={image.url} alt={image.description} />
                        ) : (
                          "图"
                        )}
                      </div>
                      <small>{image.description}</small>
                      {can("draft") && (
                        <button
                          aria-label={`移除图片 ${index + 1}`}
                          onClick={() => {
                            setImages((current) =>
                              current.filter((_, i) => i !== index),
                            );
                            setDirty(true);
                          }}
                        >
                          移除
                        </button>
                      )}
                    </div>
                  ))}
                </div>
                {can("draft") && (
                  <label className="button small upload-button">
                    {uploading ? "正在上传…" : "+ 添加或替换配图"}
                    <input
                      className="sr-only"
                      type="file"
                      accept="image/png,image/jpeg,image/webp"
                      multiple
                      disabled={uploading}
                      onChange={upload}
                    />
                  </label>
                )}
              </>
            )}
            {tab === "materials" && (
              <>
                <div className="detail-block">
                  <h3>用户素材</h3>
                  {task.request.supplied_materials.map((value, index) => (
                    <p key={index}>{value}</p>
                  ))}
                  {!task.request.supplied_materials.length && (
                    <p>尚未提供具体素材</p>
                  )}
                </div>
                <div className="detail-block">
                  <h3>创作约束</h3>
                  {[
                    ...task.request.constraints,
                    ...task.profile.boundaries,
                  ].map((value, index) => (
                    <p key={index}>{value}</p>
                  ))}
                </div>
                <div className="detail-block">
                  <h3>资料来源</h3>
                  {task.brief?.sources.map((source, index) => (
                    <p key={index}>
                      {safeLink(source.url) ? (
                        <a
                          href={safeLink(source.url)}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {source.title} ↗
                        </a>
                      ) : (
                        source.title
                      )}
                    </p>
                  ))}
                  {!task.brief?.sources.length && <p>当前没有外部资料来源。</p>}
                </div>
                {task.brief?.pending_questions.length ? (
                  <div className="notice">
                    待补充：{task.brief.pending_questions.join("；")}
                  </div>
                ) : null}
              </>
            )}
            {tab === "history" &&
              task.history
                .slice()
                .reverse()
                .map((entry) => (
                  <div className="history-item" key={entry.version}>
                    <time>
                      VERSION {String(entry.version).padStart(2, "0")}
                    </time>
                    <h3>{entry.title}</h3>
                    <p>
                      {entry.approved ? "当前批准版本" : "未批准或旧批准已失效"}
                    </p>
                    <button
                      className="button text"
                      onClick={() => showHistory(entry.version)}
                    >
                      查看该版本 →
                    </button>
                  </div>
                ))}
          </div>
        </>
      )}
      <div className="editor-footer">
        <span className="save-state">
          {dirty
            ? "有未保存修改 · 保存后重新审核"
            : busy
              ? "操作正在服务端执行"
              : task.draft?.approved
                ? "批准已绑定当前版本与素材"
                : pause?.kind === "ask_user"
                  ? "等待回复 · 任务进度已保存"
                  : "修改将保存为新版本"}
        </span>
        <div className="footer-actions">
          {can("export") && (
            <a className="button" href={`/api/tasks/${task.task_id}/export`}>
              导出文案
            </a>
          )}
          {can("cancel") && !dirty && (
            <button
              className="button text"
              onClick={() =>
                window.confirm("取消这次创作？") && void onAction("cancel")
              }
            >
              取消
            </button>
          )}
          {can("changes") && !dirty && (
            <button className="button" onClick={() => setFeedback(true)}>
              提出修改意见
            </button>
          )}
          {can("draft") && (
            <button
              className="button"
              disabled={!dirty || stale || uploading}
              onClick={() => void save()}
            >
              保存修改
            </button>
          )}
          {can("approve") && (
            <button
              className="button primary"
              disabled={dirty}
              onClick={() => void onAction("approve")}
            >
              批准当前版本
            </button>
          )}
          {can("publish") && (
            <button
              className="button primary"
              disabled={dirty}
              onClick={props.onPublish}
            >
              准备发布 <Icon name="arrow" />
            </button>
          )}
          {can("confirm") && (
            <button className="button primary" onClick={props.onConfirm}>
              核实发布结果
            </button>
          )}
          {can("retry") && (
            <button
              className="button primary"
              onClick={() => void onAction("retry")}
            >
              重试任务
            </button>
          )}
          {can("reconcile") && (
            <button
              className="button primary"
              onClick={() => void onAction("reconcile")}
            >
              恢复待办状态
            </button>
          )}
        </div>
      </div>
      {feedback && (
        <Modal title="想怎样调整这篇内容？" onClose={() => setFeedback(false)}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              const value = String(
                new FormData(event.currentTarget).get("comment") || "",
              ).trim();
              if (value)
                void onAction("changes", { comment: value }).then((ok) => {
                  if (ok) setFeedback(false);
                });
            }}
          >
            <label htmlFor="feedback">修改意见</label>
            <textarea
              id="feedback"
              name="comment"
              required
              placeholder="例如：保留轻松的语气，补充交通方式。"
            />
            <p className="form-help">
              修改意见会交给父 Agent 重新安排工作，新版本仍需你的审核。
            </p>
            <div className="modal-foot">
              <button className="button primary" disabled={props.loading}>
                提交修改意见
              </button>
            </div>
          </form>
        </Modal>
      )}
      {history && (
        <Modal
          title={`版本 v${history.version} · 只读`}
          onClose={() => setHistory(null)}
        >
          <h3>{history.title}</h3>
          <div className="prose-preview">{history.body}</div>
        </Modal>
      )}
    </article>
  );
}
