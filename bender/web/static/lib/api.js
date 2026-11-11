// Обращение к ядру Бендера. Любая ошибка приходит как ApiError с человеческим текстом.

export class ApiError extends Error {
  constructor(error, status) {
    super(error?.title || "Ошибка");
    this.error = error || { code: "unknown", title: "Ошибка", hint: "" };
    this.status = status;
    this.code = this.error.code;
  }
}

function networkError(e) {
  return new ApiError({ code: "network", title: "Нет связи с Бендером",
    hint: "Ядро не отвечает. Проверь, что Бендер запущен (окно терминала или служба), и обнови страницу.", detail: String(e) }, 0);
}

export async function api(method, path, body, { signal } = {}) {
  const opts = { method, headers: {"X-Bender-Agent": sessionStorage.getItem("bender.agent") || "main"}, credentials: "same-origin", signal };
  if (method !== "GET") opts.headers["X-Bender"] = "1";
  if (body !== undefined && body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let resp;
  try { resp = await fetch(path, opts); } catch (e) { if (e.name === "AbortError") throw e; throw networkError(e); }
  const type = resp.headers.get("content-type") || "";
  if (!resp.ok) {
    let err = null;
    if (type.includes("json")) { try { err = (await resp.json()).error; } catch { /* пусто */ } }
    if (!err) err = { code: `http_${resp.status}`, title: `Ядро ответило ${resp.status}`, hint: "Обнови страницу.", detail: await resp.text().catch(() => "") };
    throw new ApiError(err, resp.status);
  }
  return type.includes("json") ? resp.json() : resp;
}

export const get = (p, o) => api("GET", p, null, o);
export const post = (p, b, o) => api("POST", p, b ?? {}, o);
export const put = (p, b, o) => api("PUT", p, b ?? {}, o);
export const patch = (p, b, o) => api("PATCH", p, b ?? {}, o);
export const del = (p, o) => api("DELETE", p, null, o);

// Поток событий с переподключением.
export function listen(path, onEvent, { onOpen, onError } = {}) {
  let es = null, closed = false, retry = 500;
  const open = () => {
    if (closed) return;
    const scopedPath=path+(path.includes("?")?"&":"?")+"agent="+encodeURIComponent(sessionStorage.getItem("bender.agent")||"main");
    es = new EventSource(scopedPath, { withCredentials: true });
    es.onopen = () => { retry = 500; onOpen && onOpen(); };
    es.onmessage = (m) => { try { onEvent(JSON.parse(m.data)); } catch { /* пропускаем */ } };
    es.onerror = () => { es.close(); onError && onError(); if (!closed) setTimeout(open, retry); retry = Math.min(retry * 2, 8000); };
  };
  open();
  return () => { closed = true; es && es.close(); };
}
