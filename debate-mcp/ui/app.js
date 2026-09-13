// MAGI — ClaMi: cliente de la interfaz.
// Triángulo y estética de TomaszRewak/MAGI (MIT, © 2023 Tomasz Rewak).
// Interacción simplificada a pedido del operador: una sola caja estilo CLI,
// conversación limpia (sin metadata de kinds/ids), indicador "thinking…",
// todo el texto en inglés. El estado llega por SSE (LISTEN/NOTIFY).

const SLOTS = ["melchior", "balthasar", "casper"];
// color de IDENTIDAD por asiento (en el triángulo el color es el del voto;
// acá es fijo para reconocer quién habla)
const SEAT_COLORS = {
  melchior: "#52e691",
  balthasar: "#ff8d00",
  casper: "#3caee0",
  adrian: "#d8d8d8",
  magi: "#ff8d00",
};
const POSITION_COLORS = {
  yes: "#52e691",
  no: "#a41413",
  info: "#3caee0",
  conditional: "repeating-linear-gradient(56deg, rgb(82, 230, 145) 0px, rgb(82, 230, 145) 30px, #82cd68 30px, #82cd68 60px)",
  pending: "black",
};

let state = null;
let focusedId = null;
let uiMode = "council";   // council | chat

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function focusPool() {
  return state?.decisions ?? [];
}

function focused() {
  const pool = focusPool();
  if (!pool.length) return null;
  if (!focusedId || !pool.find(d => d.id === focusedId)) {
    // default: la más reciente abierta; si no, la última actividad
    const open = pool.filter(d => d.status === "open" || d.status === "split");
    focusedId = (open[0] ?? pool[0]).id;
  }
  return pool.find(d => d.id === focusedId);
}

function thinkingSeats(d) {
  if (!d || d.status !== "open") return [];
  return d.seats.filter(s => !s.voted).map(s => s.seat);
}

// ------------------------------------------------------------- magi

function renderMagi(d) {
  const magi = document.getElementById("magi");
  magi.querySelectorAll(".wise-man, .response, .system-status, .title").forEach(e => e.remove());

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "MAGI";
  magi.appendChild(title);

  const thinking = thinkingSeats(d);

  const status = document.createElement("div");
  status.className = "system-status";
  const ext = d ? `${String(d.protocol).toUpperCase()} · ROUND ${d.round}` : "STANDBY";
  status.innerHTML = `<div>${esc(ext)}</div>`;
  magi.appendChild(status);

  (d?.seats ?? []).slice(0, 3).forEach((seat, i) => {
    const slot = SLOTS[i];
    const isThinking = thinking.includes(seat.seat);
    const color = seat.voted ? POSITION_COLORS[seat.position] : POSITION_COLORS.pending;
    const outer = document.createElement("div");
    outer.className = `wise-man ${slot}`;
    const inner = document.createElement("div");
    inner.className = "inner" + (seat.voted ? "" : " flicker");
    inner.style.background = color;
    inner.textContent = `${seat.seat.toUpperCase()} • ${i + 1}`;
    outer.appendChild(inner);
    if (isThinking) {
      const tag = document.createElement("div");
      tag.className = "thinking-tag";
      tag.textContent = "THINKING";
      outer.appendChild(tag);
    }
    outer.addEventListener("click", () => openModal(seat));
    magi.appendChild(outer);
  });

  const resp = document.createElement("div");
  resp.className = "response" + (d && d.badge.flicker ? " flicker" : "");
  resp.style.color = d ? d.badge.color : "#ff8d00";
  resp.style.borderColor = d ? d.badge.color : "#ff8d00";
  const inner = document.createElement("div");
  inner.className = "inner";
  inner.textContent = d ? d.badge.text : "STANDBY";
  resp.appendChild(inner);
  magi.appendChild(resp);
}

// ------------------------------------------------------------- conversación

function renderStatusBar(d) {
  const el = document.getElementById("statusbar");
  if (uiMode === "chat") {
    el.textContent = "OPEN CONVERSATION — the three heads answer in turn";
    return;
  }
  if (!d) { el.textContent = "MAGI SYSTEM — STANDBY"; return; }
  const conf = d.confidence != null ? ` · CONFIDENCE ${d.confidence}` : "";
  el.textContent = `#${d.id} ${d.badge.text}${conf} — ${d.title}`;
}

function renderConversation(d) {
  const el = document.getElementById("conversation");
  const input = document.getElementById("c-input");
  let msgs = [];
  if (uiMode === "chat") {
    msgs = state?.chat ?? [];
    input.placeholder = "Talk to the three heads — Enter to send";
  } else if (d) {
    msgs = d.journal ?? [];
    input.placeholder = d.status === "split"
      ? "your ruling closes it, or write 'seguí' (+ context) for another round"
      : "Ask the council anything… Enter to send, Shift+Enter for a new line";
  } else {
    input.placeholder = "Ask the council anything… Enter to send, Shift+Enter for a new line";
  }
  if (!msgs.length) {
    el.innerHTML = uiMode === "chat"
      ? '<div class="welcome">Talk to the three heads — each answers from its own angle:<br>' +
        '<b>MELCHIOR</b> technical truth · <b>BALTHASAR</b> risk &amp; care · <b>CASPER</b> what you really want.<br>Just type below and press Enter.</div>'
      : '<div class="welcome">This is the <b>COUNCIL</b> — ask anything and three heads investigate, ' +
        'debate and vote:<br><b>MELCHIOR</b> what do the facts say · <b>BALTHASAR</b> who gets hurt if we\'re wrong · ' +
        '<b>CASPER</b> what do we actually want.<br>You get a verdict with confidence and dissent. Type below and press Enter. ' +
        'Switch to <b>CHAT</b> for open conversation without a vote.</div>';
    return;
  }
  el.innerHTML = msgs.map(m => {
    const color = SEAT_COLORS[m.author] ?? "#d8d8d8";
    const who = m.author === "adrian" ? "YOU" : m.author.toUpperCase();
    return `<div class="msg"><span class="who" style="color:${color}">${esc(who)}</span>` +
           `<span class="body" style="border-color:${color}">${esc(m.body || "")}</span></div>`;
  }).join("");
  el.scrollTop = el.scrollHeight;

  const thinking = uiMode === "chat" ? [] : thinkingSeats(d);
  if (thinking.length) {
    const t = document.createElement("div");
    t.className = "thinking-line";
    t.textContent = "· " + thinking.map(s => s.toUpperCase() + " is thinking…").join(" · ");
    el.appendChild(t);
  }
}

function renderHistory() {
  const el = document.getElementById("history");
  const list = (state?.decisions ?? []).filter(d => d.id !== focusedId);
  if (!list.length) { el.innerHTML = ""; return; }
  el.innerHTML = '<span class="h-label">HISTORY&nbsp;</span>' + list.map(d =>
    `<a href="#" data-id="${d.id}" style="color:${d.badge.color}">#${d.id} ${esc(d.badge.text)}` +
    ` <span class="h-title">— ${esc(d.title.slice(0, 32))}${d.title.length > 32 ? "…" : ""}</span></a>`
  ).join(" &middot; ");
  el.querySelectorAll("a").forEach(a => a.addEventListener("click", ev => {
    ev.preventDefault();
    focusedId = Number(a.dataset.id);
    uiMode = "council";
    syncModes();
    render();
  }));
}

// Qué va a pasar con el próximo Enter, en palabras. Es la respuesta a "no sé
// qué hará mi mensaje": la UI anticipa la acción antes de que la escribas.
function renderIntent(d) {
  const el = document.getElementById("c-intent");
  const repo = document.getElementById("c-repo").value.trim();
  let txt;
  if (uiMode === "chat") {
    txt = "↳ Enter talks to the three heads in the open thread — no vote, just their takes.";
  } else if (!d) {
    txt = repo
      ? `↳ Enter opens a PRODUCTION decision on ${repo} — the council votes your plan, an executor implements it there, the council reviews the diff.`
      : "↳ Enter opens a NEW decision — the council investigates and votes. Put a repo folder below to also have the approved plan executed there.";
  } else if (d.status === "open") {
    txt = `↳ Enter adds CONTEXT to #${d.id} — the heads read it on their next turn (${d.round}° round).`;
  } else if (d.status === "split") {
    txt = `↳ Enter closes #${d.id} with YOUR ruling — o escribí "seguí" para otra ronda.`;
  } else if (d.status === "executing") {
    txt = `↳ Enter adds context to #${d.id} — the executor is working; the council will review the diff after.`;
  } else {
    txt = "↳ Enter opens a NEW decision — this one is already closed (see HISTORY below).";
  }
  el.textContent = txt;
  // acciones accionables para el STALEMATE: no dejar la decisión en "y ahora qué"
  const sa = document.getElementById("stalemate-actions");
  sa.hidden = !(uiMode === "council" && d && d.status === "split");
}

function render() {
  const d = focused();
  renderMagi(d);
  renderStatusBar(d);
  renderConversation(d);
  renderHistory();
  renderIntent(d);
  const abortBtn = document.getElementById("c-abort");
  abortBtn.hidden = !(uiMode === "council" && d && ["open", "split", "executing"].includes(d.status));
  // NEW abre decisión nueva salteando la heurística; en CHAT no aplica
  document.getElementById("c-new").hidden = uiMode !== "council";
}

// --- NEW: abrir una decisión NUEVA con lo que hay en la caja, aunque haya
// otra abierta (sin esto, el council siempre mandaba el mensaje a la abierta)
document.getElementById("c-new").addEventListener("click", async () => {
  const input = document.getElementById("c-input");
  const status = document.getElementById("c-status");
  if (!input.value.trim()) {
    status.textContent = "write the new question first — NEW sends what's in the box as a fresh decision";
    input.focus();
    return;
  }
  await send(true);
});

// --- acciones de STALEMATE: botones en vez de recordar la convención
document.getElementById("sa-segui").addEventListener("click", async () => {
  const input = document.getElementById("c-input");
  input.value = "seguí";
  await send();
});
document.getElementById("sa-ruling").addEventListener("click", () => {
  const input = document.getElementById("c-input");
  input.placeholder = "your ruling and why — Enter closes the decision";
  input.focus();
});

// --- toggle production: el repo y la explicación sólo aparecen cuando aplica
document.getElementById("c-repo").addEventListener("input", render);

// --- mini-explorador de carpetas: elegir el repo sin tipear paths
let fsCurrent = null;

async function fsLoad(path) {
  const url = path ? `/fs?path=${encodeURIComponent(path)}` : "/fs";
  const resp = await fetch(url);
  const data = await resp.json();
  if (!resp.ok) throw new Error(data.error || resp.statusText);
  fsCurrent = data.path;
  document.getElementById("fs-path").textContent = data.path;
  document.getElementById("fs-up").hidden = !data.parent;
  document.getElementById("fs-list").innerHTML = data.dirs.length
    ? data.dirs.map(d =>
        `<span class="dir" data-path="${esc(d.path)}">${d.git ? '<span class="git-star">★</span>' : "▸"} ${esc(d.name)}</span>`
      ).join("")
    : '<span class="dir">(sin subcarpetas)</span>';
  document.querySelectorAll("#fs-list .dir[data-path]").forEach(el =>
    el.addEventListener("click", () => fsLoad(el.dataset.path).catch(err => {
      document.getElementById("c-status").textContent = `error: ${err.message}`;
    })));
}

document.getElementById("c-browse").addEventListener("click", async () => {
  const panel = document.getElementById("fs-panel");
  panel.hidden = !panel.hidden;
  if (!panel.hidden) {
    try {
      await fsLoad(document.getElementById("c-repo").value.trim() || null);
    } catch (err) {
      panel.hidden = true;
      document.getElementById("c-status").textContent = `error: ${err.message}`;
    }
  }
});

document.getElementById("fs-up").addEventListener("click", async () => {
  const resp = await fetch(`/fs?path=${encodeURIComponent(fsCurrent)}`);
  const data = await resp.json();
  if (data.parent) await fsLoad(data.parent);
});

document.getElementById("fs-use").addEventListener("click", () => {
  document.getElementById("c-repo").value = fsCurrent;
  document.getElementById("fs-panel").hidden = true;
  render();
});

async function abortDecision() {
  const d = focused();
  if (!d) return;
  if (!confirm(`Abort decision #${d.id}? The heads' running turns get killed. This closes it as ABORTED.`)) return;
  const status = document.getElementById("c-status");
  status.textContent = "aborting…";
  try {
    const resp = await fetch("/abort", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision_id: d.id }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    status.textContent = `decision #${d.id} aborted — running turns killed`;
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  }
}

document.getElementById("c-abort").addEventListener("click", abortDecision);

// ------------------------------------------------------------- modal

function openModal(seat) {
  document.getElementById("modal-title").textContent =
    `${seat.seat.toUpperCase()} — ${seat.voted ? "POSITION: " + seat.position.toUpperCase() : "thinking…"}`;
  const cond = seat.conditions?.length ? `<br>CONDITIONS: ${esc(seat.conditions.join("; "))}` : "";
  document.getElementById("modal-content").innerHTML =
    `<div>position:</div><div>${seat.voted ? esc(seat.position) : "no vote yet this round"}${cond}</div>` +
    `<div>reasoning:</div><div style="white-space:pre-wrap">${esc(seat.body || "(still processing — this can take minutes on local models)")}</div>`;
  document.getElementById("modal").hidden = false;
}

document.getElementById("modal-close").addEventListener("click", () => {
  document.getElementById("modal").hidden = true;
});

// ------------------------------------------------------------- composer

function syncModes() {
  document.querySelectorAll("#modes button").forEach(b =>
    b.classList.toggle("active", b.dataset.mode === uiMode));
}

document.querySelectorAll("#modes button").forEach(b =>
  b.addEventListener("click", () => { uiMode = b.dataset.mode; syncModes(); render(); }));

async function send(forceNew = false) {
  const status = document.getElementById("c-status");
  const input = document.getElementById("c-input");
  const body = input.value.trim();
  if (!body) return;
  const payload = { mode: uiMode, body };
  // con repo, production es siempre: lo aprobado se ejecuta ahí
  const repo = document.getElementById("c-repo").value.trim();
  if (uiMode === "council" && repo) {
    payload.artifact = repo;
    payload.production = true;
  }
  if (forceNew) payload.force_new = true;
  status.textContent = "sending…";
  try {
    const resp = await fetch("/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    if (data.action === "opened") {
      focusedId = data.decision_id;
      status.textContent = data.production
        ? `production decision #${data.decision_id} opened — approve the plan and an executor implements it`
        : `decision #${data.decision_id} opened — the council is deliberating`;
    } else if (data.action === "reopened") {
      status.textContent = `decision #${data.decision_id} reopened — the heads recast with your context`;
    } else if (data.action === "hint") {
      status.textContent = data.message;
      input.value = "";
      return;
    } else if (data.action === "arbitrated") {
      status.textContent = `decision #${data.decision_id} closed with your ruling`;
    } else if (data.action === "context") {
      status.textContent = "context added — the heads will see it on their next turn";
    } else {
      status.textContent = "sent — the council answers in turn";
    }
    input.value = "";
  } catch (err) {
    status.textContent = `error: ${err.message}`;
  }
}

document.getElementById("c-input").addEventListener("keydown", ev => {
  if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); send(); }
});

// ------------------------------------------------------------- events

const events = new EventSource("/events");
events.onmessage = e => { state = JSON.parse(e.data); render(); };
events.onerror = () => { /* EventSource reintenta solo */ };
