"use strict";
const POLL_MS = 2500;
const VIEWER_CLIENT_KEY = "ah.viewerClient";
const PROJECT_HASH_KEY = "project";
// This endpoint lives on the computer running the browser + SSH terminal, not
// on the Agent Hub server reached through the tunnel.
const CLIENT_FOCUS_URL = "http://127.0.0.1:18788/focus";

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

// --- small DOM helpers ------------------------------------------------------
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else if (v === true) n.setAttribute(k, "");
    else if (v !== false && v != null) n.setAttribute(k, v);
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
}

const STATUS_LABEL = { active: "工作中", waiting: "等待输入", done: "已完成", idle: "空闲",
  exited: "已退出", unknown: "状态未知", unstarted: "未启动" };
// Attention first (waiting is more urgent than done), then working, then the
// quiet states. Drives the ordering of the colored-dot chips.
const STATUS_ORDER = ["waiting", "done", "active", "idle", "unstarted", "exited", "unknown"];

// A registered-but-never-started seat is "未启动", not "状态未知" — the DB's
// initial unknown only means the sampler has nothing to look at yet.
function displayStatus(seat) {
  return (!seat.removed_at && !seat.started_at) ? "unstarted" : seat.status;
}

function countStatuses(seats) {
  const c = {};
  for (const s of seats) { const st = displayStatus(s); c[st] = (c[st] || 0) + 1; }
  return c;
}

// Compact colored-dot chips: ●2 ●1 … (skip zero counts). withLabel adds the
// status name for the global bar; bare numbers keep project rows short.
function statusChips(counts, withLabel = false) {
  return STATUS_ORDER.filter((st) => counts[st]).map((st) =>
    el("span", { class: `dotcount ${st}`, title: STATUS_LABEL[st] },
      withLabel ? `${STATUS_LABEL[st]} ${counts[st]}` : String(counts[st])));
}

function timeAgo(iso) {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return iso;
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 60) return `${s}秒前`;
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)}小时前`;
  return `${Math.floor(s / 86400)}天前`;
}

// --- incremental rendering --------------------------------------------------
// The poll refreshes state every few seconds. Rather than rebuild the whole
// board (which reset scroll, collapsed the removed-toggle, and would clobber the
// notes box), we keep DOM nodes keyed by id and update them in place.
const openRemoved = new Set();     // project ids whose removed-toggle is open
const projectNodes = new Map();    // pid -> project node refs
const pendingNotes = new Map();    // pid -> unsaved note text (guards refresh)
const notesTimers = new Map();     // pid -> debounce timer
const NOTES_DEBOUNCE_MS = 600;
let lastState = null;              // latest /api/state, for reorder permutations
let lastPipelines = [];
let lastNavigationIntentId = null;

// The selected Project is encoded in the URL rather than only in transient
// browser state.  That makes a project's view bookmarkable and lets Back /
// Forward move between the Projects a user has entered.
function projectIdFromLocation(projects) {
  const raw = new URLSearchParams(window.location.hash.slice(1)).get(PROJECT_HASH_KEY);
  return raw && projects.some((project) => project.id === raw) ? raw : null;
}

function renderProjectNavigation(projects) {
  const nav = document.getElementById("project-nav");
  const selectedId = projectIdFromLocation(projects);
  const all = el("button", {
    class: `project-nav-item${selectedId ? "" : " selected"}`,
    type: "button",
    "aria-current": selectedId ? null : "page",
    onclick: () => selectProject(null),
  },
    el("span", { class: "project-nav-name" }, "全部项目"),
    el("span", { class: "project-nav-count" }, `${projects.length} 个`),
  );
  const items = projects.map((project) => {
    const isSelected = project.id === selectedId;
    return el("button", {
      class: `project-nav-item${isSelected ? " selected" : ""}`,
      type: "button",
      title: project.root_dir,
      "aria-current": isSelected ? "page" : null,
      onclick: () => selectProject(project.id),
    },
      el("span", { class: "project-nav-name" }, project.name),
      el("span", { class: "project-nav-meta" },
        el("span", { class: "project-nav-count" }, `${project.sessions.length} 席位`),
        ...statusChips(countStatuses(project.sessions))),
    );
  });
  nav.replaceChildren(all, ...items);
}

function selectProject(projectId) {
  const url = new URL(window.location.href);
  url.hash = projectId ? `${PROJECT_HASH_KEY}=${encodeURIComponent(projectId)}` : "";
  history.pushState(null, "", url);
  renderCurrentProjectView();
}

function applyNavigationIntent(intent, projects) {
  if (!intent || intent.id === lastNavigationIntentId) return;
  lastNavigationIntentId = intent.id;
  if (!projects.some((project) => project.id === intent.project_id)) return;
  if (projectIdFromLocation(projects) === intent.project_id) return;
  const url = new URL(window.location.href);
  url.hash = `${PROJECT_HASH_KEY}=${encodeURIComponent(intent.project_id)}`;
  // This is an external handoff, not another choice in this tab's own history.
  history.replaceState(null, "", url);
}

function renderCurrentProjectView() {
  if (!lastState) return;
  renderProjectNavigation(lastState.projects);
  render(lastState);
  renderPipelines(lastPipelines);
}

// Each browser chooses its own tmux client.  That is deliberately localStorage
// rather than server state: the dashboard may be open on the Mac and through an
// SSH tunnel at the same time, and each browser must keep a different target.
function selectedViewerClient() {
  return document.getElementById("viewer-client").value || null;
}

function renderViewerClients(clients) {
  const select = document.getElementById("viewer-client");
  const saved = localStorage.getItem(VIEWER_CLIENT_KEY) || "";
  const signature = JSON.stringify(clients);
  // Do not rebuild an open select every 2.5 seconds when nothing changed.
  if (select.dataset.signature === signature && select.dataset.saved === saved) return;

  const options = [el("option", { value: "", text: "自动（最宽终端）" })];
  for (const client of clients) {
    const tty = (client.tty || client.name).replace(/^\/dev\//, "");
    const size = client.width && client.height ? `${client.width}×${client.height}` : "未知尺寸";
    const session = client.session || "未选择 session";
    options.push(el("option", {
      value: client.name,
      text: `${tty} · ${size} · ${session}`,
    }));
  }
  if (saved && !clients.some((client) => client.name === saved)) {
    options.push(el("option", { value: saved, text: `已断开 · ${saved}`, disabled: true }));
  }
  select.replaceChildren(...options);
  select.value = saved;
  select.dataset.signature = signature;
  select.dataset.saved = saved;
}

// Collapsed projects (title bar only). Persisted per-browser in localStorage.
const collapsed = new Set((() => {
  try { return JSON.parse(localStorage.getItem("ah.collapsed") || "[]"); }
  catch (_) { return []; }
})());
function saveCollapsed() { localStorage.setItem("ah.collapsed", JSON.stringify([...collapsed])); }

// Collapsed pipeline cards (title row only). Separate set — pipeline cards are
// fully rebuilt each poll, so the collapse state must live here, not in the DOM.
const plCollapsed = new Set((() => {
  try { return JSON.parse(localStorage.getItem("ah.plCollapsed") || "[]"); }
  catch (_) { return []; }
})());
function savePlCollapsed() { localStorage.setItem("ah.plCollapsed", JSON.stringify([...plCollapsed])); }

// ---- seat card ----
function makeSeatNode() {
  const name = el("span", { class: "name" });
  const prov = el("span", { class: "prov" });
  const badge = el("span", { class: "badge" });
  const dir = el("div", { class: "dir" });
  const out = el("pre", { class: "out" });
  const when = el("div", { class: "when" });
  const pc = el("div", { class: "pc-link", hidden: true });
  const actions = el("div", { class: "actions" });
  const node = el("div", { class: "seat" },
    el("div", { class: "head" }, name, prov, badge),
    dir, out, when, pc, actions,
  );
  const ref = { node, name, prov, badge, dir, out, when, pc, actions,
    _out: null, _key: null, _pcKey: null, _seat: null };
  // Click anywhere on the card to jump — same idea as the project header row.
  // Opt-outs: the buttons (they run their own action), the output box (.out is
  // scrollable/selectable), and an in-progress text selection (drag to copy the
  // path/name). The small "跳到终端" button still works as before. Only jumpable
  // seats respond, so clicking a not-started / exited / removed card does nothing.
  node.addEventListener("click", (e) => {
    const seat = ref._seat;
    if (!seat) return;
    if (e.target.closest("button, a, .out")) return;
    const sel = window.getSelection && window.getSelection();
    if (sel && sel.type === "Range" && String(sel).trim()) return;
    if (seat.removed_at || !seat.started_at || seat.status === "exited") return;
    jump(seat);
  });
  return ref;
}

function seatActions(seat, removed, started) {
  const b = [];
  if (removed) {
    b.push(el("button", {
      class: "btn primary", title: "新对话，并注入恢复时最新的 brief 与 Context Pack",
      onclick: () => restore(seat, "fresh"),
    }, "最新上下文重开"));
    if (seat.started_at && ["claude", "codex", "ds4", "ds4-co"].includes(seat.provider))
      b.push(el("button", {
        class: "btn", title: "保留旧聊天记录，同时注入恢复时最新的项目上下文",
        onclick: () => restore(seat, "resume"),
      }, "继续旧对话"));
    b.push(el("button", { class: "btn danger", onclick: () => purge(seat) }, "彻底删除"));
  } else {
    if (started && seat.status !== "exited")
      b.push(el("button", { class: "btn jump", onclick: () => jump(seat) }, "跳到终端"));
    if (!started || seat.status === "exited")
      b.push(el("button", { class: "btn primary", onclick: () => start(seat) }, started ? "重新启动" : "启动"));
    b.push(el("button", { class: "btn danger", onclick: () => remove(seat) }, "移除"));
    b.push(el("button", { class: "btn icon", title: "前移", onclick: () => moveSeat(seat, -1) }, "‹"));
    b.push(el("button", { class: "btn icon", title: "后移", onclick: () => moveSeat(seat, 1) }, "›"));
  }
  return b;
}

function projectCoreMetadata(seat) {
  try {
    const value = JSON.parse(seat.project_core_json || "{}");
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  } catch (_) { return {}; }
}

function pcCandidateLabel(candidate) {
  const project = candidate.project_title || "Project";
  const workstream = candidate.workstream_title || "Workstream";
  return `${project} › ${workstream}`;
}

const AGENT_ROLE_LABEL = {
  general: "通用", plan: "规划", implement: "实现", review: "审核",
};

function renderProjectCoreLink(ref, seat, started) {
  const metadata = projectCoreMetadata(seat);
  const inferredRegistered = !metadata.registration_status && metadata.context_pack_id;
  const status = inferredRegistered ? "registered" : metadata.registration_status;
  const key = JSON.stringify([
    status, metadata.candidates || [], started,
    seat.project_core_lifecycle, seat.project_core_report_warning,
    seat.project_core_dead_letter_count, seat.project_core_dead_letter_error,
    metadata.project_title, metadata.workstream_title, metadata.seat_role,
    metadata.desired_project_title, metadata.desired_workstream_title,
    metadata.desired_project_id, metadata.desired_record_id, seat.status,
  ]);
  if (ref._pcKey === key) return;
  ref._pcKey = key;
  ref.pc.replaceChildren();
  ref.pc.hidden = false;

  if (status === "registered") {
    const lifecycle = {
      registered: "已关联，等待启动", active: "正在追踪",
      finished: "已关闭", abandoned: "异常结束",
    }[seat.project_core_lifecycle] || "已关联";
    ref.pc.append(el("span", { class: "pc-state linked" }, `Project Core · ${lifecycle}`));
    const project = metadata.project_title || metadata.project_id || "Project";
    const workstream = metadata.workstream_title || metadata.record_id || "Workstream";
    const role = AGENT_ROLE_LABEL[metadata.seat_role || seat.agent_role] || "通用";
    ref.pc.append(el("span", { class: "pc-note" }, `${project} › ${workstream} · ${role}`));
    if (started && seat.status !== "exited" && !seat.removed_at) {
      ref.pc.append(
        el("div", { class: "pc-actions" },
          el("button", { class: "btn", onclick: () => reinjectProjectCore(seat) }, "重新注入当前快照"),
        ),
        el("span", { class: "pc-note" }, "seat 内 /new 或 /clear 后使用；只重发已注册快照，不刷新 Project Core 状态。"),
      );
    }
    if (seat.project_core_report_warning) {
      ref.pc.append(el("span", { class: "pc-note bad" }, seat.project_core_report_warning));
    }
    if (seat.project_core_dead_letter_count) {
      ref.pc.append(
        el("span", { class: "pc-note bad" },
          `${seat.project_core_dead_letter_count} 条 Project Core 投递失败：${seat.project_core_dead_letter_error || "未知错误"}`),
        el("div", { class: "pc-actions" },
          el("button", { class: "btn", onclick: () => retryProjectCoreDeliveries(seat) }, "重试投递"),
        ),
      );
    }
    return;
  }
  if (status === "suggested" || status === "ambiguous") {
    const candidates = metadata.candidates || [];
    const select = el("select", { class: "pc-candidates" },
      ...candidates.map((candidate) => el("option", {
        value: candidate.candidate_id, text: pcCandidateLabel(candidate),
      })));
    const message = status === "suggested"
      ? "发现一个可能相关的 Workstream。只有你确认后才会追踪。"
      : "发现多个相关 Workstream，请选择；Agent Hub 不会自行猜测。";
    const accept = el("button", {
      class: "btn primary", disabled: !candidates.length,
      onclick: () => linkProjectCore(seat, select.value),
    }, "关联 Project Core");
    const ignore = el("button", {
      class: "btn ghost", onclick: () => ignoreProjectCore(seat),
    }, "本席位不跟踪");
    ref.pc.append(
      el("div", { class: "pc-message" }, message),
      ...(candidates.length > 1 ? [select] : []),
      el("div", { class: "pc-actions" }, accept, ignore),
    );
    if (started) ref.pc.append(el("div", { class: "pc-note" }, "关联后，Context Pack 和汇报约定会注入当前会话。"));
    return;
  }
  if (status === "unassigned") {
    ref.pc.append(
      el("span", { class: "pc-state" }, "Project Core · 未找到关联候选"),
      el("button", { class: "btn", onclick: () => retryProjectCore(seat) }, "重新查找"),
      el("button", { class: "btn ghost", onclick: () => ignoreProjectCore(seat) }, "不再提示"),
    );
    return;
  }
  if (status === "target_unavailable") {
    const project = metadata.desired_project_title || metadata.desired_project_id || "Project";
    const workstream = metadata.desired_workstream_title || metadata.desired_record_id || "Workstream";
    ref.pc.append(
      el("span", { class: "pc-state" }, "Project Core · 所选节点当前不可注册"),
      el("span", { class: "pc-note" }, `${project} › ${workstream}`),
      el("button", { class: "btn", onclick: () => retryProjectCore(seat) }, "按原目标重试"),
      el("button", { class: "btn ghost", onclick: () => ignoreProjectCore(seat) }, "改为不追踪"),
    );
    return;
  }
  if (status === "unavailable" && seat.project_core_tracking !== "off") {
    const project = metadata.desired_project_title || metadata.desired_project_id;
    const workstream = metadata.desired_workstream_title || metadata.desired_record_id;
    ref.pc.append(
      el("span", { class: "pc-state" }, "Project Core · 当前不可用，追踪席位尚不能启动"),
      ...(project || workstream ? [
        el("span", { class: "pc-note" }, `${project || "Project"} › ${workstream || "Workstream"}`),
      ] : []),
      el("button", { class: "btn", onclick: () => retryProjectCore(seat) }, "重试关联"),
      el("button", { class: "btn ghost", onclick: () => ignoreProjectCore(seat) }, "改为不追踪"),
    );
    return;
  }
  ref.pc.hidden = true;
}

function updateSeat(ref, seat) {
  const started = !!seat.started_at;
  const removed = !!seat.removed_at;
  ref._seat = seat;                                   // read by the card-click jump handler
  // Only a live, non-exited seat is jumpable; the class drives the pointer cursor + hover.
  ref.node.classList.toggle("jumpable", started && !removed && seat.status !== "exited");
  ref.name.textContent = seat.name;
  ref.prov.textContent = seat.model ? `${seat.provider} · ${seat.model}` : seat.provider;
  const st = displayStatus(seat);
  ref.badge.className = `badge ${st}`;
  ref.badge.textContent = STATUS_LABEL[st] || st;
  ref.dir.textContent = seat.working_dir;
  ref.when.textContent = `最后活动：${timeAgo(seat.last_activity_at)} · ${seat.tmux_session}`;
  renderProjectCoreLink(ref, seat, started);

  // Only touch the output when it actually changed — leaving it alone keeps the
  // user's scroll from snapping. On change, pin to the newest (bottom) line.
  const outText = seat.last_output || (started ? "（暂无输出）" : "（未启动）");
  if (ref._out !== outText) {
    ref._out = outText;
    ref.out.textContent = outText;
    ref.out.scrollTop = ref.out.scrollHeight;
  }

  // Rebuild the buttons only when the applicable set changes.
  const key = `${removed}|${started}|${seat.status}`;
  if (ref._key !== key) {
    ref._key = key;
    ref.actions.replaceChildren(...seatActions(seat, removed, started));
  }
}

// Reconcile a container's seat cards against a list, keyed by seat id.
function reconcileSeats(container, refMap, seats) {
  const seen = new Set();
  seats.forEach((seat, i) => {
    seen.add(seat.id);
    let ref = refMap.get(seat.id);
    if (!ref) { ref = makeSeatNode(); refMap.set(seat.id, ref); }
    updateSeat(ref, seat);
    if (container.children[i] !== ref.node) container.insertBefore(ref.node, container.children[i] || null);
  });
  for (const [id, ref] of refMap)
    if (!seen.has(id)) { ref.node.remove(); refMap.delete(id); }
}

// ---- project card ----
function makeProjectNode(pid) {
  const name = el("span", { class: "pname" });
  const meta = el("span", { class: "meta" });
  const chips = el("span", { class: "pchips" });
  const root = el("span", { class: "root" });
  const addBtn = el("button", { class: "btn" }, "+ 新建席位");
  const toggle = () => {
    collapsed.has(pid) ? collapsed.delete(pid) : collapsed.add(pid);
    saveCollapsed();
    if (lastState) render(lastState);
  };
  const tgl = el("span", { class: "chev", title: "折叠/展开" });  // indicator only
  const up = el("button", { class: "btn icon", title: "上移", onclick: () => moveProject(pid, -1) }, "↑");
  const down = el("button", { class: "btn icon", title: "下移", onclick: () => moveProject(pid, 1) }, "↓");
  // edit/delete get the project object at update time (see updateProject).
  const edit = el("button", { class: "btn icon", title: "编辑项目（改名 / 改工作目录）" }, "✎");
  const del = el("button", { class: "btn icon danger", title: "删除项目" }, "🗑");
  // The WHOLE title row toggles collapse; the buttons on the right opt out.
  const header = el("header", {
    onclick: (e) => { if (!e.target.closest(".pctl")) toggle(); },
  },
    el("div", { class: "pinfo" }, tgl, name, meta, chips, root),
    el("div", { class: "pctl" }, up, down, edit, del, addBtn),
  );

  const notes = el("textarea", {
    class: "notes", rows: 2,
    placeholder: "随手笔记：记录当前做到哪了…（自动保存）",
    oninput: (e) => scheduleNotesSave(pid, e.target.value),
  });

  const seatsEl = el("div", { class: "seats" });
  const removedWrap = el("div", { class: "removed-wrap" });
  const section = el("section", { class: "project" }, header, notes, seatsEl, removedWrap);

  return {
    section, name, meta, chips, root, addBtn, edit, del, tgl, notes, seatsEl, removedWrap,
    seatRefs: new Map(), removedRefs: new Map(),
    emptyEl: null, details: null, summary: null, grid: null, notesInit: false,
  };
}

function updateProject(ref, p) {
  ref.name.textContent = p.name;
  const pcProject = p.project_core_project_title || p.project_core_project_id;
  ref.meta.textContent = `${p.sessions.length} 席位${pcProject ? ` · Project Core: ${pcProject}` : " · 未关联 Project Core"}`;
  ref.chips.replaceChildren(...statusChips(countStatuses(p.sessions)));
  ref.root.textContent = p.root_dir;
  ref.addBtn.onclick = () => openSeatDialog(p);
  ref.edit.onclick = () => openProjectDialog(p);
  ref.del.onclick = () => deleteProject(p);

  const isCollapsed = collapsed.has(p.id);
  ref.section.classList.toggle("collapsed", isCollapsed);
  ref.tgl.textContent = isCollapsed ? "▸" : "▾";

  // Notes: fill once; afterwards only sync from the server when the user isn't
  // editing and has nothing pending, so we never clobber what they're typing.
  const notes = p.notes || "";
  if (!ref.notesInit) { ref.notes.value = notes; ref.notesInit = true; }
  else if (document.activeElement !== ref.notes && !pendingNotes.has(p.id) && ref.notes.value !== notes)
    ref.notes.value = notes;

  // Active seats
  if (p.sessions.length === 0) {
    ref.seatRefs.forEach((r) => r.node.remove());
    ref.seatRefs.clear();
    if (!ref.emptyEl) ref.emptyEl = el("div", { class: "empty small", text: "还没有席位" });
    if (!ref.emptyEl.parentNode) ref.seatsEl.append(ref.emptyEl);
  } else {
    if (ref.emptyEl && ref.emptyEl.parentNode) ref.emptyEl.remove();
    reconcileSeats(ref.seatsEl, ref.seatRefs, p.sessions);
  }

  // Removed-seats <details>
  const removed = p.removed_sessions || [];
  if (removed.length === 0) {
    if (ref.details) { ref.details.remove(); ref.details = null; ref.removedRefs.clear(); }
  } else {
    if (!ref.details) {
      ref.grid = el("div", { class: "seats" });
      ref.summary = el("summary", {});
      ref.details = el("details", {
        class: "removed",
        open: openRemoved.has(p.id),
        ontoggle: (e) => { e.target.open ? openRemoved.add(p.id) : openRemoved.delete(p.id); },
      }, ref.summary, ref.grid);
      ref.removedWrap.append(ref.details);
    }
    ref.summary.textContent = `已手动移除席位（${removed.length}）`;
    reconcileSeats(ref.grid, ref.removedRefs, removed);
  }
}

function render(state) {
  const board = document.getElementById("board");
  const selectedId = projectIdFromLocation(state.projects);
  const projects = selectedId
    ? state.projects.filter((project) => project.id === selectedId)
    : state.projects;
  if (!projects.length) {
    projectNodes.forEach((ref) => ref.section.remove());
    projectNodes.clear();
    if (!board._empty) board._empty = el("div", { class: "empty", text: "还没有项目。点右上角「新建项目」开始。" });
    if (!board._empty.parentNode) board.replaceChildren(board._empty);
    return;
  }
  if (board._empty && board._empty.parentNode) board._empty.remove();

  const seen = new Set();
  projects.forEach((p, i) => {
    seen.add(p.id);
    let ref = projectNodes.get(p.id);
    if (!ref) { ref = makeProjectNode(p.id); projectNodes.set(p.id, ref); }
    updateProject(ref, p);
    if (board.children[i] !== ref.section) board.insertBefore(ref.section, board.children[i] || null);
  });
  for (const [pid, ref] of projectNodes)
    if (!seen.has(pid)) { ref.section.remove(); projectNodes.delete(pid); }
}

// ---- notes autosave ----
function scheduleNotesSave(pid, value) {
  pendingNotes.set(pid, value);
  clearTimeout(notesTimers.get(pid));
  notesTimers.set(pid, setTimeout(() => saveNotes(pid), NOTES_DEBOUNCE_MS));
}

async function saveNotes(pid) {
  const value = pendingNotes.get(pid);
  try {
    await api(`/api/projects/${pid}`, { method: "PATCH", body: JSON.stringify({ notes: value }) });
  } catch (_) { return; }              // keep local; the next edit retries
  if (pendingNotes.get(pid) === value) pendingNotes.delete(pid);
}

// --- actions ----------------------------------------------------------------
function swapAt(ids, id, delta) {
  const i = ids.indexOf(id), j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return null;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  return ids;
}

async function moveProject(pid, delta) {
  if (!lastState) return;
  const ids = swapAt(lastState.projects.map((p) => p.id), pid, delta);
  if (!ids) return;
  try { await api("/api/projects/reorder", { method: "POST", body: JSON.stringify({ ids }) }); await poll(); }
  catch (e) { alert("排序失败：" + e.message); }
}

async function moveSeat(seat, delta) {
  if (!lastState) return;
  const p = lastState.projects.find((x) => x.id === seat.project_id);
  if (!p) return;
  const ids = swapAt(p.sessions.map((s) => s.id), seat.id, delta);
  if (!ids) return;
  try { await api(`/api/projects/${p.id}/sessions/reorder`, { method: "POST", body: JSON.stringify({ ids }) }); await poll(); }
  catch (e) { alert("排序失败：" + e.message); }
}

async function start(seat) {
  try { await api(`/api/sessions/${seat.id}/start`, { method: "POST" }); await poll(); }
  catch (e) { alert("启动失败：" + e.message); }
}

async function linkProjectCore(seat, candidateId) {
  try {
    await api(`/api/sessions/${seat.id}/project-core/register`, {
      method: "POST", body: JSON.stringify({ candidate_id: candidateId || null }),
    });
    await poll();
  } catch (e) { alert("Project Core 关联失败：" + e.message); }
}

async function ignoreProjectCore(seat) {
  try {
    await api(`/api/sessions/${seat.id}/project-core/ignore`, { method: "POST" });
    await poll();
  } catch (e) { alert("更新 Project Core 追踪状态失败：" + e.message); }
}

async function retryProjectCore(seat) {
  try {
    await api(`/api/sessions/${seat.id}/project-core/retry`, { method: "POST" });
    await poll();
  } catch (e) { alert("Project Core 重新关联失败：" + e.message); }
}

async function reinjectProjectCore(seat) {
  try {
    await api(`/api/sessions/${seat.id}/project-core/reinject`, { method: "POST" });
    toast("已把当前 Project Core 快照重新注入该席位");
  } catch (e) { alert("Project Core 上下文注入失败：" + e.message); }
}

async function retryProjectCoreDeliveries(seat) {
  try {
    const result = await api(`/api/sessions/${seat.id}/project-core/deliveries/retry`, { method: "POST" });
    await poll();
    if (result.remaining_dead_letters) {
      alert(`仍有 ${result.remaining_dead_letters} 条 Project Core 投递失败；请查看席位上的最新原因。`);
    } else {
      toast(result.requeued ? "Project Core 投递已重试并送达" : "没有需要重试的 Project Core 投递");
    }
  } catch (e) { alert("Project Core 投递重试失败：" + e.message); }
}

async function remove(seat) {
  const ok = confirm(`移除席位「${seat.name}」？\n\n将停止 tmux 会话 ${seat.tmux_session}，\n但不会删除工作目录或任何项目文件。`);
  if (!ok) return;
  try { await api(`/api/sessions/${seat.id}/remove`, { method: "POST" }); await poll(); }
  catch (e) { alert("移除失败：" + e.message); }
}

async function restore(seat, conversationMode = "fresh") {
  try {
    await api(`/api/sessions/${seat.id}/restore`, {
      method: "POST", body: JSON.stringify({ conversation_mode: conversationMode }),
    });
    toast(conversationMode === "fresh" ? "已恢复；启动时将以最新上下文新开对话" : "已恢复；启动时将继续旧对话并注入最新上下文");
    await poll();
  }
  catch (e) { alert("恢复失败：" + e.message); }
}

async function purge(seat) {
  const ok = confirm(`彻底删除席位「${seat.name}」？\n\n将从看板永久删除该席位记录，无法恢复。\n（不会删除工作目录或任何项目文件。）`);
  if (!ok) return;
  try { await api(`/api/sessions/${seat.id}`, { method: "DELETE" }); await poll(); }
  catch (e) { alert("删除失败：" + e.message); }
}

async function jump(seat) {
  // Start the client-side raise immediately. Over SSH, waiting for the server's
  // AppleScript tty lookup to fail before contacting this helper adds a visible
  // pause. The tmux switch and iTerm2 activation are independent and safe to
  // run in parallel.
  const clientFocus = focusClientIterm();
  try {
    const r = await api(`/api/sessions/${seat.id}/jump`, {
      method: "POST",
      body: JSON.stringify({ client: selectedViewerClient() }),
    });
    // A successful server-side raise needs no helper result. Otherwise the
    // request has already been running while tmux switched, so there is no
    // extra serial wait in the normal SSH case.
    const clientFocused = r.focused ? false : await clientFocus;
    showJump(r, clientFocused);
  } catch (e) { alert("跳转失败：" + e.message); }
}

let toastTimer = null;
function toast(text, ms = 2600) {
  let n = document.getElementById("toast");
  if (!n) { n = el("div", { id: "toast" }); document.body.append(n); }
  n.textContent = text;
  n.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => n.classList.remove("show"), ms);
}

async function focusClientIterm() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 2500);
  try {
    const response = await fetch(CLIENT_FOCUS_URL, {
      method: "POST",
      mode: "cors",
      cache: "no-store",
      headers: { "Content-Type": "text/plain" },
      body: "focus",
      signal: controller.signal,
    });
    if (!response.ok) return false;
    const result = await response.json();
    return result && result.ok === true;
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

function showJump(r, clientFocused = false) {
  if (r.ok && r.jumped) {
    // Full success is silent — the terminal window coming to the front IS the
    // feedback. The SSH client's loopback helper was started in parallel with
    // the server jump, so reaching this branch no longer incurs a second wait.
    if (!r.focused && clientFocused) return;
    if (!r.focused && r.explicit_client)
      toast(`已切换 ${r.client}；客户端未安装 iTerm2 置前 helper`, 6000);
    else if (!r.focused)
      toast("已切换,但未能自动置前终端——首次使用需在 系统设置→隐私与安全→自动化 里允许", 6000);
    return;
  }
  const dlg = document.getElementById("dlg-jump");
  const title = document.getElementById("j-title");
  const msg = document.getElementById("j-msg");
  const cmd = document.getElementById("j-cmd");
  const copy = document.getElementById("j-copy");
  cmd.hidden = true; copy.hidden = true;
  if (r.ok && !r.jumped) {
    title.textContent = "还没有取景器终端";
    msg.textContent = r.hint || "在一个终端里运行下面的命令，然后再点一次跳转：";
    cmd.textContent = r.attach_command; cmd.hidden = false; copy.hidden = false;
    copy.onclick = () => navigator.clipboard.writeText(r.attach_command);
  } else {
    title.textContent = "无法跳转";
    msg.textContent = r.reason || "未知原因。";
    if (r.attach_command) { cmd.textContent = r.attach_command; cmd.hidden = false; copy.hidden = false;
      copy.onclick = () => navigator.clipboard.writeText(r.attach_command); }
  }
  if (!dlg.open) dlg.showModal();
}

// --- dialogs ----------------------------------------------------------------
let editingProjectId = null;   // null => the dialog is in "create" mode
let editingProject = null;

function replaceOptions(select, items, selected = "") {
  select.replaceChildren(...items.map((item) => el("option", {
    value: item.value, text: item.label,
    "data-title": item.title || "",
    "data-project": item.project || "",
    "data-project-title": item.projectTitle || "",
  })));
  select.value = items.some((item) => item.value === selected) ? selected : "";
}

function openProjectDialog(p = null) {
  editingProjectId = p ? p.id : null;
  editingProject = p;
  const dlg = document.getElementById("dlg-project");
  document.getElementById("p-title").textContent = p ? "编辑项目" : "新建项目";
  document.getElementById("p-ok").textContent = p ? "保存" : "创建";
  document.getElementById("p-name").value = p ? p.name : "";
  document.getElementById("p-root").value = p ? p.root_dir : "";
  const pcSelect = document.getElementById("p-pc-project");
  const currentId = p?.project_core_project_id || "";
  replaceOptions(pcSelect, [
    { value: "", label: "不关联 Project Core" },
    ...(currentId ? [{
      value: currentId,
      label: p.project_core_project_title || currentId,
      title: p.project_core_project_title || currentId,
    }] : []),
  ], currentId);
  document.getElementById("p-pc-status").textContent = currentId
    ? "已保存绑定；可重新查找。" : "先填写根目录，再查找有权访问的 Project。";
  document.getElementById("p-err").textContent = "";
  dlg.showModal();
}

async function submitProject(ev) {
  ev.preventDefault();
  const name = document.getElementById("p-name").value.trim();
  const root = document.getElementById("p-root").value.trim();
  const pcOption = document.getElementById("p-pc-project").selectedOptions[0];
  const projectCoreProjectId = pcOption?.value || "";
  const projectCoreProjectTitle = pcOption?.dataset.title || pcOption?.textContent || "";
  try {
    if (editingProjectId) {
      // Editing an existing project: change the name and/or repoint the working
      // directory (seats under the old root are relocated server-side).
      await api(`/api/projects/${editingProjectId}`, {
        method: "PATCH", body: JSON.stringify({
          name, root_dir: root,
          project_core_project_id: projectCoreProjectId,
          project_core_project_title: projectCoreProjectTitle,
        }),
      });
    } else {
      const created = await api("/api/projects", { method: "POST", body: JSON.stringify({
        name, root_dir: root,
        project_core_project_id: projectCoreProjectId,
        project_core_project_title: projectCoreProjectTitle,
      }) });
      // A new project is usually the next thing the user wants to work in;
      // enter it immediately instead of leaving it buried in the all-projects
      // overview while the next poll catches up.
      selectProject(created.id);
    }
    document.getElementById("dlg-project").close();
    await poll();
  } catch (e) { document.getElementById("p-err").textContent = e.message; }
}

async function resolveProjectTargets() {
  const root = document.getElementById("p-root").value.trim();
  const status = document.getElementById("p-pc-status");
  if (!root) { status.textContent = "请先填写根目录。"; return; }
  status.textContent = "正在查找…";
  try {
    const result = await api("/api/project-core/targets", {
      method: "POST", body: JSON.stringify({ working_dir: root }),
    });
    const projects = new Map();
    for (const candidate of result.candidates || [])
      if (!projects.has(candidate.project_ref))
        projects.set(candidate.project_ref, candidate.project_title || candidate.project_ref);
    const current = document.getElementById("p-pc-project").value;
    replaceOptions(document.getElementById("p-pc-project"), [
      { value: "", label: "不关联 Project Core" },
      ...[...projects].map(([value, title]) => ({ value, title, label: title })),
    ], current || editingProject?.project_core_project_id || "");
    status.textContent = projects.size
      ? `找到 ${projects.size} 个 Project，请明确选择。`
      : "没有找到与这个目录绑定且当前 principal 可访问的 Project。";
  } catch (e) { status.textContent = "查找失败：" + e.message; }
}

async function deleteProject(p) {
  const n = p.sessions.length + p.removed_sessions.length;
  const warn = n
    ? `删除项目「${p.name}」将永久移除它的 ${n} 个席位并结束其运行中的会话，且无法恢复。确定？`
    : `删除项目「${p.name}」？无法恢复。`;
  if (!confirm(warn)) return;
  try {
    await api(`/api/projects/${p.id}`, { method: "DELETE" });
    await poll();
  } catch (e) { toast("删除失败：" + e.message); }
}

let seatProjectId = null;
let seatProject = null;
let providerOptions = {};
function refreshSeatModels(selected = "") {
  const provider = document.getElementById("s-provider").value;
  const models = providerOptions[provider] || [];
  replaceOptions(document.getElementById("s-model"), [
    { value: "", label: "默认模型（由 CLI 配置决定）" },
    ...models.map((value) => ({ value, label: value })),
  ], selected);
  document.getElementById("s-model-label").hidden = models.length === 0;
}
function openSeatDialog(p) {
  seatProjectId = p.id;
  seatProject = p;
  document.getElementById("s-name").value = "";
  document.getElementById("s-dir").value = p.root_dir;
  document.getElementById("s-cmd").value = "";
  document.getElementById("s-role").value = "general";
  refreshSeatModels();
  document.getElementById("s-prompt").value = "";
  replaceOptions(document.getElementById("s-pc-workstream"), [
    { value: "", label: "不追踪 Project Core" },
  ]);
  // The Agent Hub project's own binding is a default, not a gate: what a seat
  // may track is decided by the working directory Project Core resolves.
  const bound = p.project_core_project_id;
  document.getElementById("s-pc-resolve").disabled = false;
  document.getElementById("s-pc-status").textContent = bound
    ? `绑定 Project：${p.project_core_project_title || bound}；正在读取节点…`
    : "正在按工作目录读取可追踪的节点…";
  document.getElementById("s-err").textContent = "";
  document.getElementById("dlg-seat").showModal();
  resolveSeatTargets();
}

async function resolveSeatTargets() {
  if (!seatProject) return;
  const expectedProjectId = seatProject.id;
  const status = document.getElementById("s-pc-status");
  const workingDir = document.getElementById("s-dir").value.trim();
  if (!workingDir) { status.textContent = "请先填写工作目录。"; return; }
  status.textContent = "正在读取 Workstream…";
  try {
    const result = await api("/api/project-core/targets", {
      method: "POST", body: JSON.stringify({ working_dir: workingDir }),
    });
    if (seatProject?.id !== expectedProjectId) return;
    // One directory can be bound to several Project Core Projects. Listing only
    // the one this Agent Hub project is bound to made the others unpickable,
    // so every candidate is offered and the seat carries the Project its chosen
    // Workstream belongs to.
    const candidates = result.candidates || [];
    const projectCount = new Set(candidates.map((c) => c.project_ref)).size;
    const workstreams = new Map();
    for (const candidate of candidates) {
      if (workstreams.has(candidate.workstream_ref)) continue;
      const title = candidate.workstream_title || candidate.workstream_ref;
      const projectTitle = candidate.project_title || candidate.project_ref;
      workstreams.set(candidate.workstream_ref, {
        value: candidate.workstream_ref,
        title,
        project: candidate.project_ref,
        projectTitle,
        label: projectCount > 1
          ? `${projectTitle} · ${title} · ${candidate.horizon || "later"}`
          : `${title} · ${candidate.horizon || "later"}`,
      });
    }
    const selected = document.getElementById("s-pc-workstream").value;
    replaceOptions(document.getElementById("s-pc-workstream"), [
      { value: "", label: "不追踪 Project Core" }, ...workstreams.values(),
    ], selected);
    status.textContent = workstreams.size
      ? `找到 ${workstreams.size} 个节点${projectCount > 1 ? `（分属 ${projectCount} 个 Project）` : ""}；请选择本席位唯一目标。`
      : "该目录下没有找到可用的 Workstream。";
  } catch (e) { status.textContent = "读取失败：" + e.message; }
}

async function submitSeat(ev) {
  ev.preventDefault();
  const workstreamOption = document.getElementById("s-pc-workstream").selectedOptions[0];
  const body = {
    name: document.getElementById("s-name").value.trim(),
    provider: document.getElementById("s-provider").value,
    model: document.getElementById("s-model").value,
    working_dir: document.getElementById("s-dir").value.trim(),
    launch_command: document.getElementById("s-cmd").value.trim(),
    agent_role: document.getElementById("s-role").value,
    initial_prompt: document.getElementById("s-prompt").value.trim(),
    project_core_workstream_id: workstreamOption?.value || "",
    project_core_workstream_title: workstreamOption?.dataset.title || "",
    project_core_project_id: workstreamOption?.dataset.project || "",
    project_core_project_title: workstreamOption?.dataset.projectTitle || "",
  };
  try {
    await api(`/api/projects/${seatProjectId}/sessions`, { method: "POST", body: JSON.stringify(body) });
    document.getElementById("dlg-seat").close();
    await poll();
  } catch (e) { document.getElementById("s-err").textContent = e.message; }
}

// --- poll loop --------------------------------------------------------------
// ---- pipelines (linear orchestration) --------------------------------------
let providersList = [];
let templatesCatalog = [];
// Global default agent for NEW pipeline steps (add-step + outline import). Per-step
// selectors still override it; template steps keep their own provider.
let defaultProvider = localStorage.getItem("ah.defaultProvider") || null;

const PHASE_LABEL = { pending: "待开始", starting: "启动中", running: "进行中",
  awaiting_approval: "待批准", done: "已完成" };
const PL_LABEL = { running: "运行中", done: "已完成", aborted: "已中止", failed: "失败" };

function renderPipelines(pipelines) {
  const section = document.getElementById("pipelines");
  const list = document.getElementById("pipelines-list");
  const selectedId = projectIdFromLocation((lastState && lastState.projects) || []);
  const visible = selectedId
    ? pipelines.filter((pipeline) => pipeline.project_id === selectedId)
    : pipelines;
  section.hidden = visible.length === 0;
  list.replaceChildren(...visible.map(pipelineCard));
}

function pipelineCard(pl) {
  const cur = pl.phases[pl.phase_index];
  const canApprove = pl.status === "running" && cur && cur.status === "awaiting_approval";

  const ctl = el("div", { class: "pctl" });
  if (canApprove)
    ctl.append(el("button", { class: "btn primary", onclick: () => approvePhase(pl.id) },
      `批准 → ${pl.phases[pl.phase_index + 1] ? pl.phases[pl.phase_index + 1].role : "完成"} ▶`));
  if (pl.status === "running")
    ctl.append(el("button", { class: "btn", onclick: () => abortPipeline(pl.id) }, "中止"));
  ctl.append(el("button", { class: "btn icon danger", title: "删除流水线（含 worktree）",
    onclick: () => deletePipeline(pl) }, "🗑"));

  const phases = el("ol", { class: "pl-phases" }, ...pl.phases.map((ph, i) => {
    const isCur = i === pl.phase_index && pl.status === "running";
    const seat = ph.seat;
    const st = seat ? displayStatus(seat) : "unknown";
    const row = el("li", { class: `pl-phase ${isCur ? "cur" : ""} ph-${ph.status}` },
      el("span", { class: "pl-role" }, `${i + 1}. ${ph.role}`),
      el("span", { class: "pl-pstatus" }, PHASE_LABEL[ph.status] || ph.status),
      seat ? el("span", { class: `chip s-${st}` }, `${seat.provider}·${STATUS_LABEL[st] || st}`) : null,
      el("button", { class: "btn", onclick: () => showPhaseLog(pl.id, i, ph.role) }, "日志"),
      seat ? el("button", { class: "btn jump", onclick: () => jump(seat) }, "跳到终端") : null,
    );
    return row;
  }));

  const isCol = plCollapsed.has(pl.id);
  const chev = el("span", { class: "pl-chev", title: "折叠/展开" }, isCol ? "▸" : "▾");
  const header = el("header", {
    onclick: (e) => {                       // whole title row toggles; the control buttons opt out
      if (e.target.closest(".pctl")) return;
      const nowCol = !plCollapsed.has(pl.id);
      nowCol ? plCollapsed.add(pl.id) : plCollapsed.delete(pl.id);
      savePlCollapsed();
      card.classList.toggle("collapsed", nowCol);
      chev.textContent = nowCol ? "▸" : "▾";
    },
  },
    el("div", { class: "pinfo" }, chev,
      el("span", { class: "pl-name" }, pl.name),
      el("span", { class: "pl-tpl" }, pl.template),
      pl.auto_advance ? el("span", { class: "pl-tpl" }, "全自动") : null,
      el("span", { class: `pl-status s-${pl.status}` }, PL_LABEL[pl.status] || pl.status)),
    ctl);

  const card = el("div", { class: `pipeline s-${pl.status}${isCol ? " collapsed" : ""}` },
    header,
    el("div", { class: "pl-meta" }, `worktree: ${pl.worktree_path}　分支 ${pl.branch}（基线 ${pl.base_branch}）`),
    phases,
  );
  // When a phase is awaiting your approval, show what that agent last produced.
  if (canApprove && cur.seat && cur.seat.last_output)
    card.append(el("pre", { class: "pl-output" }, cur.seat.last_output));
  return card;
}

async function showPhaseLog(pid, idx, role) {
  const dlg = document.getElementById("dlg-log");
  document.getElementById("log-title").textContent = `步骤日志：${idx + 1}. ${role}`;
  const body = document.getElementById("log-body");
  body.textContent = "加载中…";
  dlg.showModal();
  try {
    const r = await api(`/api/pipelines/${pid}/phases/${idx}/log`);
    body.textContent = r.log && r.log.trim() ? r.log : "(还没有日志——该步可能尚未开始)";
    body.scrollTop = body.scrollHeight;
  } catch (e) { body.textContent = "读取日志失败：" + e.message; }
}

async function approvePhase(pid) {
  try { await api(`/api/pipelines/${pid}/approve`, { method: "POST" }); await poll(); }
  catch (e) { toast("批准失败：" + e.message); }
}
async function abortPipeline(pid) {
  if (!confirm("中止这条流水线？会结束它的三个 agent，worktree/分支保留供你查看。")) return;
  try { await api(`/api/pipelines/${pid}/abort`, { method: "POST" }); await poll(); }
  catch (e) { toast("中止失败：" + e.message); }
}
async function deletePipeline(pl) {
  if (!confirm(`删除流水线「${pl.name}」？会结束并清除它的 agent，并移除 worktree（分支保留）。`)) return;
  try { await api(`/api/pipelines/${pl.id}`, { method: "DELETE" }); await poll(); }
  catch (e) { toast("删除失败：" + e.message); }
}

let plSteps = [];         // the editable step list: [{role, provider, prompt}]
let plOutlinePath = null; // set when steps came from parsing an outline file

function openPipelineDialog() {
  const projSel = document.getElementById("pl-project");
  const projects = (lastState && lastState.projects) || [];
  const selectedId = projectIdFromLocation(projects);
  projSel.replaceChildren(...projects.map((p) => el("option", {
    value: p.id, text: p.name, selected: p.id === selectedId,
  })));
  const tplSel = document.getElementById("pl-template");
  tplSel.replaceChildren(...templatesCatalog.map((t) => el("option", { value: t.id, text: t.label })));
  document.getElementById("pl-name").value = "";
  document.getElementById("pl-task").value = "";
  document.getElementById("pl-outline-path").value = "";
  document.getElementById("pl-err").textContent = "";
  document.getElementById("pl-auto").checked = false;
  plOutlinePath = null;
  const dpSel = document.getElementById("pl-default-provider");
  dpSel.replaceChildren(...providersList.map((p) => el("option", { value: p, text: p, selected: p === defaultProvider })));
  dpSel.onchange = () => { defaultProvider = dpSel.value; localStorage.setItem("ah.defaultProvider", defaultProvider); };
  // wire controls (idempotent — set each open)
  document.querySelectorAll("input[name=pl-src]").forEach((r) => { r.onchange = () => setPipelineSource(r.value); });
  tplSel.onchange = prefillFromTemplate;
  document.getElementById("pl-task").oninput = prefillFromTemplate;   // live-bake {task}
  document.getElementById("pl-parse").onclick = parseOutlineIntoSteps;
  document.getElementById("pl-add-step").onclick = () => {
    syncStepsFromDOM();
    plSteps.push({ role: "", provider: defaultProvider || providersList[0] || "claude", prompt: "" });
    renderSteps();
  };
  document.querySelector("input[name=pl-src][value=template]").checked = true;
  setPipelineSource("template");
  document.getElementById("dlg-pipeline").showModal();
}

function setPipelineSource(src) {
  document.getElementById("pl-src-template").hidden = src !== "template";
  document.getElementById("pl-src-outline").hidden = src !== "outline";
  // Only the template source prefills steps; switching to outline clears the
  // lingering template steps so you start empty and fill them by parsing.
  if (src === "template") prefillFromTemplate();
  else { plSteps = []; plOutlinePath = null; renderSteps(); }
}

function prefillFromTemplate() {
  const tpl = templatesCatalog.find((t) => t.id === document.getElementById("pl-template").value);
  if (!tpl) return;
  const task = document.getElementById("pl-task").value.trim();
  plOutlinePath = null;
  // NOTE: re-typing the task rebuilds the rows (bakes {task}); do task first,
  // then hand-edit steps — later edits win because they come after.
  plSteps = tpl.phases.map((ph) => ({
    role: ph.role, provider: ph.provider,
    prompt: ph.prompt.split("{task}").join(task || "{task}"),
  }));
  renderSteps();
}

async function parseOutlineIntoSteps() {
  const path = document.getElementById("pl-outline-path").value.trim();
  if (!path) { document.getElementById("pl-err").textContent = "请填大纲文件路径"; return; }
  try {
    const r = await api("/api/parse-outline", { method: "POST", body: JSON.stringify({ path }) });
    plSteps = r.steps.map((s) => ({ role: s.role, provider: defaultProvider || s.provider || "claude", prompt: s.prompt }));
    plOutlinePath = r.outline_path;
    document.getElementById("pl-err").textContent = "";
    renderSteps();
  } catch (e) { document.getElementById("pl-err").textContent = e.message; }
}

function renderSteps() {
  const box = document.getElementById("pl-steps");
  box.replaceChildren(...plSteps.map((st, i) => el("div", { class: "pl-step-row" },
    el("div", { class: "pl-step-top" },
      el("span", { class: "pl-step-num" }, `${i + 1}`),
      el("input", { class: "pl-step-role", value: st.role, placeholder: "步骤名称（可选）" }),
      el("select", { class: "pl-step-provider" },
        ...providersList.map((p) => el("option", { value: p, text: p, selected: p === st.provider }))),
      el("button", { class: "btn icon", type: "button", title: "上移", onclick: () => moveStep(i, -1) }, "↑"),
      el("button", { class: "btn icon", type: "button", title: "下移", onclick: () => moveStep(i, 1) }, "↓"),
      el("button", { class: "btn icon danger", type: "button", title: "删除", onclick: () => removeStep(i) }, "✕")),
    el("textarea", { class: "pl-step-prompt", rows: 3, text: st.prompt,
      placeholder: "这一步发给 agent 的 prompt…" }),
  )));
}

function syncStepsFromDOM() {
  plSteps = Array.from(document.querySelectorAll("#pl-steps .pl-step-row")).map((row) => ({
    role: row.querySelector(".pl-step-role").value.trim(),
    provider: row.querySelector(".pl-step-provider").value,
    prompt: row.querySelector(".pl-step-prompt").value,
  }));
}
function moveStep(i, d) {
  syncStepsFromDOM();
  const j = i + d;
  if (j < 0 || j >= plSteps.length) return;
  [plSteps[i], plSteps[j]] = [plSteps[j], plSteps[i]];
  renderSteps();
}
function removeStep(i) { syncStepsFromDOM(); plSteps.splice(i, 1); renderSteps(); }

async function submitPipeline(ev) {
  ev.preventDefault();
  syncStepsFromDOM();
  const steps = plSteps.filter((s) => s.prompt.trim());
  if (!steps.length) { document.getElementById("pl-err").textContent = "至少要有一步（且带 prompt）"; return; }
  const src = document.querySelector("input[name=pl-src]:checked").value;
  const body = {
    project_id: document.getElementById("pl-project").value,
    name: document.getElementById("pl-name").value.trim() || null,
    steps,
    outline_path: src === "outline" ? plOutlinePath : null,
    auto_advance: document.getElementById("pl-auto").checked,
  };
  try {
    await api("/api/pipelines", { method: "POST", body: JSON.stringify(body) });
    document.getElementById("dlg-pipeline").close();
    await poll();
  } catch (e) { document.getElementById("pl-err").textContent = e.message; }
}

async function poll() {
  const conn = document.getElementById("conn");
  try {
    const state = await api("/api/state", { headers: { "X-Agent-Hub-Dashboard": "1" } });
    lastState = state;
    applyNavigationIntent(state.navigation_intent, state.projects);
    renderViewerClients(state.tmux_clients || []);
    renderProjectNavigation(state.projects);
    render(state);
    try {
      lastPipelines = await api("/api/pipelines");
      renderPipelines(lastPipelines);
    } catch (_) {}
    // Global status bar (topbar) + tab badge, from all seats across projects.
    const all = countStatuses(state.projects.flatMap((p) => p.sessions));
    document.getElementById("summary").replaceChildren(...statusChips(all, true));
    const sp = document.getElementById("status-panel");
    if (sp && !sp.hidden) renderStatusPanel();     // keep the expanded roster live
    const parts = [];
    if (all.waiting) parts.push(`${all.waiting}⚠`);
    if (all.done) parts.push(`${all.done}✓`);
    document.title = (parts.length ? `(${parts.join(" ")}) ` : "") + "Agent Hub";
    conn.textContent = state.tmux_available ? "已连接" : "已连接（未检测到 tmux！）";
    conn.className = state.tmux_available ? "conn ok" : "conn bad";
  } catch (e) {
    conn.textContent = "连接断开：" + e.message;
    conn.className = "conn bad";
  }
}

// ---- topbar status panel (click the summary to expand a grouped roster) ----
function toggleStatusPanel() {
  const panel = document.getElementById("status-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) renderStatusPanel();
}

// Three fixed columns: 工作中 / 需要输入·已完成 / 空闲. Every status maps into
// exactly one column, so no agent is dropped. Within a column the sub-statuses
// keep their own colored-dot header, and every seat row shows its last-activity
// time. Live seats stay click-to-jump.
const STATUS_COLUMNS = [
  { title: "工作中", statuses: ["active"] },
  { title: "需要输入 / 已完成", statuses: ["waiting", "done"] },
  { title: "空闲", statuses: ["idle", "unstarted", "exited", "unknown"], sortRecent: true },
];

// Epoch millis of a seat's last activity (0 when never active → sorts to bottom).
function seatTime(seat) {
  const t = seat.last_activity_at ? new Date(seat.last_activity_at).getTime() : 0;
  return isNaN(t) ? 0 : t;
}

function renderStatusPanel() {
  const panel = document.getElementById("status-panel");
  const seats = (lastState ? lastState.projects : []).flatMap((p) =>
    (p.sessions || []).map((s) => ({ seat: s, proj: p.name })));
  const inner = el("div", { class: "sp-inner" });
  if (!seats.length) {
    inner.append(el("div", { class: "sp-empty" }, "还没有任何 agent"));
    panel.replaceChildren(inner);
    return;
  }
  for (const col of STATUS_COLUMNS) {
    const colSeats = seats.filter((x) => col.statuses.includes(displayStatus(x.seat)));
    const colEl = el("div", { class: "sp-col" },
      el("div", { class: "sp-col-hd" }, `${col.title} · ${colSeats.length}`));
    if (!colSeats.length) {
      colEl.append(el("div", { class: "sp-col-empty" }, "—"));
    } else {
      for (const st of col.statuses) {
        const members = colSeats.filter((x) => displayStatus(x.seat) === st);
        if (!members.length) continue;
        if (col.sortRecent) members.sort((a, b) => seatTime(b.seat) - seatTime(a.seat));
        colEl.append(el("div", { class: `sp-group-hd dotcount ${st}` },
          `${STATUS_LABEL[st] || st} · ${members.length}`));
        for (const { seat, proj } of members) {
          const jumpable = seat.started_at && !seat.removed_at && seat.status !== "exited";
          const row = el("div", { class: "sp-seat" + (jumpable ? " clickable" : "") },
            el("span", { class: "sp-name" }, seat.name),
            el("span", { class: "sp-proj" }, proj),
            el("span", { class: "sp-when", title: "最后活动时间" }, timeAgo(seat.last_activity_at)));
          if (jumpable) row.addEventListener("click", () => { panel.hidden = true; jump(seat); });
          colEl.append(row);
        }
      }
    }
    inner.append(colEl);
  }
  panel.replaceChildren(inner);
}

async function boot() {
  try {
    providersList = await api("/api/providers");
    const sel = document.getElementById("s-provider");
    providersList.forEach((p) => sel.append(el("option", { value: p, text: p })));
  } catch (_) {}
  try {
    const options = await api("/api/provider-options");
    providerOptions = Object.fromEntries(options.map((item) => [item.name, item.models || []]));
  } catch (_) {}
  refreshSeatModels();
  if (!defaultProvider || !providersList.includes(defaultProvider))
    defaultProvider = providersList[0] || "claude";
  try { templatesCatalog = await api("/api/pipeline-templates"); } catch (_) {}
  document.getElementById("btn-new-project").addEventListener("click", () => openProjectDialog());
  document.getElementById("btn-new-pipeline").addEventListener("click", () => openPipelineDialog());
  document.getElementById("pl-ok").addEventListener("click", submitPipeline);
  document.getElementById("p-ok").addEventListener("click", submitProject);
  document.getElementById("p-pc-resolve").addEventListener("click", resolveProjectTargets);
  document.getElementById("s-ok").addEventListener("click", submitSeat);
  document.getElementById("s-provider").addEventListener("change", () => refreshSeatModels());
  document.getElementById("s-pc-resolve").addEventListener("click", resolveSeatTargets);
  document.getElementById("j-close").addEventListener("click", () => document.getElementById("dlg-jump").close());
  document.getElementById("viewer-client").addEventListener("change", (e) => {
    if (e.target.value) localStorage.setItem(VIEWER_CLIENT_KEY, e.target.value);
    else localStorage.removeItem(VIEWER_CLIENT_KEY);
    e.target.dataset.saved = e.target.value;
  });
  document.getElementById("summary").addEventListener("click", toggleStatusPanel);
  window.addEventListener("popstate", renderCurrentProjectView);
  window.addEventListener("hashchange", renderCurrentProjectView);
  document.addEventListener("click", (e) => {          // click outside closes the roster
    const panel = document.getElementById("status-panel");
    if (!panel || panel.hidden) return;
    if (e.target.closest("#status-panel, #summary")) return;
    panel.hidden = true;
  });
  await poll();
  setInterval(poll, POLL_MS);
}

boot();
