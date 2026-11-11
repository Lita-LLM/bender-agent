// Общее состояние пульта: маленькое наблюдаемое хранилище без библиотек.
import { useEffect, useState } from "../vendor/ui.js";
import { get } from "./api.js";

export const state = {
  face: "idle",
  expression: "",
  st: null,         // ответ /api/state
  stError: null,
  route: parseHash(),
  sidebar: false,
  connected: false,
  pending: [],      // карточки одобрения и вопросы
};

const subs = new Set();
export function setState(patch) {
  Object.assign(state, typeof patch === "function" ? patch(state) : patch);
  subs.forEach((fn) => fn());
}
export function useApp() {
  const [, force] = useState(0);
  useEffect(() => { const fn = () => force((x) => x + 1); subs.add(fn); return () => subs.delete(fn); }, []);
  return state;
}

export function parseHash() {
  const h = (location.hash || "#/").slice(1);
  const [path, query = ""] = h.split("?");
  return { path, parts: path.split("/").filter(Boolean), query: new URLSearchParams(query) };
}
window.addEventListener("hashchange", () => setState({ route: parseHash(), sidebar: false }));
export function go(hash) { if (location.hash === hash) setState({ route: parseHash() }); else location.hash = hash; }

let refreshing = null;
export function refresh() {
  if (refreshing) return refreshing;
  refreshing = get("/api/state")
    .then((st) => { setState({ st, stError: null, pending: st.pending || [] }); return st; })
    .catch((e) => { setState({ stError: e }); throw e; })
    .finally(() => { refreshing = null; });
  return refreshing;
}

// Событий много (каждый ход каждого агента), а состояние тяжёлое: из событий оно запрашивается не чаще раза в 2 секунды,
// лишние события схлопываются в один запрос в конце окна.
let lastSoon = 0, soonTimer = null;
export function refreshSoon() {
  const wait = 2000 - (Date.now() - lastSoon);
  if (wait <= 0) { lastSoon = Date.now(); return refresh().catch(() => {}); }
  if (!soonTimer) soonTimer = setTimeout(() => { soonTimer = null; lastSoon = Date.now(); refresh().catch(() => {}); }, wait);
  return Promise.resolve();
}

// подписчики на события ядра
const evSubs = new Set();
export function onEvent(fn) { evSubs.add(fn); return () => evSubs.delete(fn); }
export function emit(ev) { evSubs.forEach((fn) => { try { fn(ev); } catch (e) { console.error(e); } }); }
export function useEvents(fn, deps = []) { useEffect(() => onEvent(fn), deps); }
