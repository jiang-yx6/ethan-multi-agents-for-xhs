export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers:
      options.body instanceof FormData
        ? options.headers
        : { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const details = Array.isArray(body.errors)
      ? body.errors
          .slice(0, 3)
          .map(
            (item: { loc?: string[]; msg?: string }) =>
              `${item.loc?.slice(1).join(".") || "字段"}: ${item.msg || "格式无效"}`,
          )
          .join("；")
      : "";
    const message =
      (typeof body.detail === "string"
        ? body.detail
        : "操作失败，请刷新状态后重试") + (details ? "：" + details : "");
    throw new ApiError(message, response.status);
  }
  return response.json();
}
export const post = <T>(path: string, payload: unknown) =>
  api<T>(path, { method: "POST", body: JSON.stringify(payload) });
export const put = <T>(path: string, payload: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(payload) });
export const requestId = () => crypto.randomUUID();
export const isBusy = (job?: { status: string } | null) =>
  Boolean(job && ["queued", "running"].includes(job.status));
export const safeLink = (value: string) => {
  try {
    const parsed = new URL(value);
    return ["http:", "https:"].includes(parsed.protocol)
      ? parsed.href
      : undefined;
  } catch {
    return undefined;
  }
};
