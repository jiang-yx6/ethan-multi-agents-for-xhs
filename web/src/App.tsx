import { useCallback, useEffect, useRef, useState } from "react";
import { api, isBusy, post } from "./api";
import type {
  Health,
  Job,
  ProfileRecord,
  TaskDetail,
  TaskSummary,
} from "./types";
import { Heading, Icon, message } from "./ui";
import Workspace from "./Workspace";
import {
  AccountsView,
  ConfirmModal,
  NewTask,
  ProfileView,
  PublishModal,
  SettingsView,
} from "./Views";

type Page = "workspace" | "archive" | "profile" | "accounts" | "settings";
const labels: Record<Page, string> = {
  workspace: "创作工作台",
  archive: "内容归档",
  profile: "创作画像",
  accounts: "发布账号",
  settings: "技能与模型",
};
function readRoute() {
  const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
  return {
    page: (path && path in labels ? path : "workspace") as Page,
    task: new URLSearchParams(query || "").get("task") || "",
  };
}

export default function App() {
  const initial = readRoute();
  const [page, setPage] = useState<Page>(initial.page),
    [selected, setSelected] = useState(initial.task);
  const [tasks, setTasks] = useState<TaskSummary[]>([]),
    [total, setTotal] = useState(0),
    [detail, setDetail] = useState<TaskDetail | null>(null);
  const [health, setHealth] = useState<Health | null>(null),
    [profiles, setProfiles] = useState<ProfileRecord[]>([]);
  const [query, setQuery] = useState(""),
    [filter, setFilter] = useState(
      initial.page === "archive" ? "published" : "all",
    );
  const [pending, setPending] = useState(false),
    [loading, setLoading] = useState(true),
    [dirty, setDirty] = useState(false);
  const [modal, setModal] = useState<"new" | "publish" | "confirm" | null>(
      null,
    ),
    [toast, setToast] = useState(""),
    [error, setError] = useState("");
  const selectedRef = useRef(selected),
    detailRef = useRef(detail),
    queryRef = useRef(query),
    filterRef = useRef(filter),
    dirtyRef = useRef(dirty);
  selectedRef.current = selected;
  detailRef.current = detail;
  queryRef.current = query;
  filterRef.current = filter;
  dirtyRef.current = dirty;
  const notify = useCallback((value: string) => setToast(value), []);
  const detailSequence = useRef(0);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(""), 4500);
    return () => clearTimeout(timer);
  }, [toast]);

  const refreshDetail = useCallback(async (id = selectedRef.current) => {
    if (!id) return;
    const sequence = ++detailSequence.current;
    const result = await api<TaskDetail>(`/tasks/${id}`);
    if (selectedRef.current === id && sequence === detailSequence.current)
      setDetail(result);
  }, []);
  const refreshTasks = useCallback(
    async (append = false) => {
      const criteria = queryRef.current + ":" + filterRef.current;
      const params = new URLSearchParams({
        query: queryRef.current,
        limit: "100",
      });
      if (filterRef.current === "published") params.set("status", "published");
      if (append) params.set("offset", String(tasks.length));
      const response = await api<{ items: TaskSummary[]; total: number }>(
        `/tasks?${params}`,
      );
      if (criteria !== queryRef.current + ":" + filterRef.current) return;
      setTasks((current) =>
        append
          ? [
              ...current,
              ...response.items.filter(
                (next) =>
                  !current.some((item) => item.task_id === next.task_id),
              ),
            ]
          : response.items,
      );
      setTotal(response.total);
      if (!selectedRef.current && response.items[0]) {
        setSelected(response.items[0].task_id);
        selectedRef.current = response.items[0].task_id;
      }
    },
    [tasks.length],
  );
  const refreshTasksRef = useRef(refreshTasks);
  refreshTasksRef.current = refreshTasks;
  const reloadProfiles = useCallback(
    async () => setProfiles(await api<ProfileRecord[]>("/profiles")),
    [],
  );
  async function refreshHealth() {
    setHealth(await api<Health>("/health"));
  }

  useEffect(() => {
    let active = true;
    Promise.all([api<Health>("/health"), api<ProfileRecord[]>("/profiles")])
      .then(async ([info, items]) => {
        if (!active) return;
        setHealth(info);
        setProfiles(items);
        await refreshTasksRef.current();
        setError("");
      })
      .catch((err) => {
        if (active) setError(message(err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    if (!selected) {
      setDetail(null);
      return;
    }
    setDetail(null);
    void refreshDetail(selected).catch((err) => setError(message(err)));
  }, [selected, refreshDetail]);
  useEffect(() => {
    const timer = setTimeout(
      () =>
        void refreshTasksRef.current().catch((err) => setError(message(err))),
      200,
    );
    return () => clearTimeout(timer);
  }, [query, filter]);
  useEffect(() => {
    const timer = setInterval(() => {
      void refreshTasksRef.current().catch((err) => setError(message(err)));
      if (selectedRef.current)
        void refreshDetail().catch((err) => setError(message(err)));
    }, 2500);
    return () => clearInterval(timer);
  }, [refreshDetail]);
  const jobId = detail?.job?.job_id,
    jobStatus = detail?.job?.status;
  useEffect(() => {
    if (!jobId || !isBusy(detail?.job)) return;
    const source = new EventSource(`/api/jobs/${jobId}/events`);
    source.onmessage = (event) => {
      const update = JSON.parse(event.data) as {
        job: Job;
        events: TaskDetail["events"];
        workflow: TaskDetail["workflow"];
      };
      setDetail((current) =>
        current?.job?.job_id === jobId
          ? {
              ...current,
              job: update.job,
              events: update.events,
              workflow: update.workflow,
              allowed_actions: [],
            }
          : current,
      );
      void refreshDetail().catch(() => {});
      if (!isBusy(update.job)) {
        source.close();
        void refreshTasksRef.current().catch(() => {});
      }
    };
    return () => source.close();
  }, [jobId, jobStatus, refreshDetail]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);
  useEffect(() => {
    const handler = () => {
      const next = readRoute();
      if (dirtyRef.current && !window.confirm("放弃尚未保存的编辑并切换页面？"))
        return;
      setDirty(false);
      setPage(next.page);
      if (next.task) setSelected(next.task);
      setFilter(next.page === "archive" ? "published" : "all");
    };
    window.addEventListener("hashchange", handler);
    return () => window.removeEventListener("hashchange", handler);
  }, []);
  function navigate(next: Page, id = selectedRef.current) {
    if (
      dirtyRef.current &&
      !window.confirm("当前编辑尚未保存，是否放弃后切换？")
    )
      return;
    setDirty(false);
    dirtyRef.current = false;
    setPage(next);
    setSelected(id);
    setFilter(next === "archive" ? "published" : "all");
    const hash = `#/${next}${id && ["workspace", "archive"].includes(next) ? "?task=" + encodeURIComponent(id) : ""}`;
    history.replaceState(null, "", hash);
  }
  function chooseTask(id: string) {
    navigate("workspace", id);
  }
  async function waitForJob(job: Job) {
    let current = job;
    while (isBusy(current)) {
      await new Promise((resolve) => setTimeout(resolve, 350));
      const response = await api<{ job: Job }>(`/jobs/${current.job_id}`);
      current = response.job;
      if (current.task_id === selectedRef.current) await refreshDetail();
    }
    if (current.status !== "succeeded")
      throw new Error(current.error || "后台作业未完成");
  }
  async function action(
    kind: string,
    params: Record<string, unknown> = {},
  ): Promise<boolean> {
    const current = detailRef.current;
    if (!current || pending) return false;
    setPending(true);
    setError("");
    try {
      const job = await post<Job>(`/tasks/${current.task.task_id}/${kind}`, {
        request_id: crypto.randomUUID(),
        expected_version: current.task.current_version,
        ...([
          "select",
          "drafts",
          "changes",
          "approve",
          "cancel",
          "publish",
        ].includes(kind)
          ? { interrupt_id: current.workflow.interrupts[0]?.id }
          : {}),
        ...params,
      });
      await refreshDetail();
      await waitForJob(job);
      await Promise.all([refreshTasksRef.current(), refreshDetail()]);
      notify(
        kind === "publish"
          ? "提交操作已结束，请查看实际回执。"
          : "操作已完成，已读取服务端最新状态。",
      );
      return true;
    } catch (err) {
      notify(message(err));
      void refreshDetail().catch(() => {});
      return false;
    } finally {
      setPending(false);
    }
  }
  async function create(body: Record<string, unknown>) {
    if (pending) return;
    setPending(true);
    try {
      const job = await post<Job>("/tasks", body);
      setModal(null);
      setDirty(false);
      navigate("workspace", job.task_id!);
      selectedRef.current = job.task_id!;
      await refreshTasksRef.current();
      await refreshDetail(job.task_id!);
      notify("任务已开始，进度会持续保存在服务端。");
    } catch (err) {
      notify(message(err));
    } finally {
      setPending(false);
    }
  }
  const visibleTasks =
    filter === "attention"
      ? tasks.filter((item) =>
          [
            "pending",
            "ideating",
            "pending_review",
            "approved",
            "submitted",
            "unknown",
            "failed",
            "submitting",
          ].includes(item.status),
        )
      : tasks;
  return (
    <>
      <a
        className="skip-link"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById("main")?.focus();
        }}
      >
        跳到主要内容
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#/workspace"
          onClick={(event) => {
            event.preventDefault();
            navigate("workspace");
          }}
          aria-label="创作间首页"
        >
          <span className="brand-mark">作</span>
          <span>
            创作间<small>CONTENT STUDIO</small>
          </span>
        </a>
        <p className="sidebar-label">你的内容，有自己的节奏</p>
        <nav aria-label="主导航">
          {Object.entries(labels).map(([value, label], index) => (
            <div key={value}>
              {index === 2 && <div className="nav-divider">工作区设置</div>}
              <button
                className={`nav-item ${page === value ? "active" : ""}`}
                aria-current={page === value ? "page" : undefined}
                onClick={() => navigate(value as Page)}
              >
                <Icon name={value} />
                <span>{label}</span>
              </button>
            </div>
          ))}
        </nav>
        <div className="sidebar-note">
          <span className="small-star">✳</span>
          <p>
            先写你想表达的，
            <br />
            再找到合适的读者。
          </p>
          <span>需求优先 · 热点可选</span>
        </div>
        <div className="sidebar-bottom">
          <span className="avatar">作</span>
          <div>
            我的创作空间<small>本地单人工作区</small>
          </div>
          <span className="live-dot" />
        </div>
      </aside>
      <div className="app-shell">
        <header className="topbar">
          <div>
            <span className="breadcrumb">工作空间</span>
            <span className="slash">/</span>
            <span>{labels[page]}</span>
          </div>
          <div className="topbar-actions">
            <span className={`connection ${health ? "" : "offline"}`}>
              {health ? "已连接本地服务" : "正在连接服务"}
            </span>
            <button
              className="icon-button"
              aria-label="刷新工作区"
              onClick={() => {
                void refreshTasksRef
                  .current()
                  .catch((err) => notify(message(err)));
                void refreshDetail().catch((err) => notify(message(err)));
              }}
            >
              <Icon name="refresh" />
            </button>
          </div>
        </header>
        <div className="demo-banner">
          <span className="banner-dot" />
          选题：{health?.ideation_backend === "mock"
            ? "离线示例"
            : "真实模型"}{" "}
          · 资料与创作：Mock · 审核：基本规则；发布需明确选择账号并确认。
        </div>
        <main id="main" tabIndex={-1}>
          {error && (
            <div className="notice error" role="alert">
              {error}
              <button
                className="button small"
                onClick={() => location.reload()}
              >
                重新连接
              </button>
            </div>
          )}
          {["workspace", "archive"].includes(page) && (
            <>
              <Heading
                eyebrow={
                  page === "archive"
                    ? "COLLECTION / 内容归档"
                    : "WORKSPACE / 创作工作台"
                }
                title={
                  page === "archive"
                    ? "好内容，值得被留下。"
                    : "把一个想法，写成一篇好内容。"
                }
                note="从你的真实需求出发，保留每一次创作的判断与选择。"
                action={
                  <button
                    className="button primary"
                    disabled={pending || !profiles.length}
                    onClick={() =>
                      dirty ? notify("请先保存当前修改。") : setModal("new")
                    }
                  >
                    <Icon name="plus" />
                    新建创作
                  </button>
                }
              />
              <Workspace
                tasks={visibleTasks}
                total={total}
                hasMore={total > tasks.length}
                selected={selected}
                detail={detail}
                loading={pending || loading}
                query={query}
                filter={filter}
                onQuery={setQuery}
                onFilter={(value) =>
                  dirty ? notify("请先保存当前修改。") : setFilter(value)
                }
                onSelect={chooseTask}
                onAction={action}
                onPublish={() => setModal("publish")}
                onConfirm={() => setModal("confirm")}
                onSettings={() => navigate("settings")}
                onDirty={setDirty}
                notify={notify}
                onMore={() =>
                  void refreshTasksRef
                    .current(true)
                    .catch((err) => notify(message(err)))
                }
              />
            </>
          )}
          {page === "profile" && (
            <ProfileView
              records={profiles}
              reload={reloadProfiles}
              notify={notify}
            />
          )}{" "}
          {page === "accounts" && <AccountsView notify={notify} />}{" "}
          {page === "settings" && (
            <SettingsView
              notify={notify}
              onSaved={() => void refreshHealth()}
            />
          )}
        </main>
      </div>
      {modal === "new" && (
        <NewTask
          profiles={profiles}
          pending={pending}
          onClose={() => setModal(null)}
          onCreate={create}
        />
      )}{" "}
      {modal === "publish" && detail && (
        <PublishModal
          detail={detail}
          onClose={() => setModal(null)}
          onSubmit={(params) => action("publish", params)}
        />
      )}{" "}
      {modal === "confirm" && detail && (
        <ConfirmModal
          onClose={() => setModal(null)}
          onSubmit={(params) => action("confirm-published", params)}
        />
      )}
      <div
        id="toast"
        className={toast ? "visible" : ""}
        role="status"
        aria-live="polite"
      >
        {toast}
      </div>
    </>
  );
}
