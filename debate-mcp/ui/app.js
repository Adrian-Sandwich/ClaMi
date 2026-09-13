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
      ? "STALEMATE — your ruling closes it, or write 'seguí' (+ context) for another round"
      : "Ask the council anything… Enter to send, Shift+Enter for a new line";
  } else {
    input.placeholder = "Ask the council anything… Enter to send, Shift+Enter for a new line";
  }
  if (!msgs.length) {
    el.innerHTML = `<div class="empty">the council awaits — ask anything below</div>`;
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
  el.innerHTML = list.map(d =>
    `<a href="#" data-id="${d.id}" style="color:${d.badge.color}">#${d.id} ${esc(d.badge.text)}</a>`
  ).join(" · ");
  el.querySelectorAll("a").forEach(a => a.addEventListener("click", ev => {
    ev.preventDefault();
    focusedId = Number(a.dataset.id);
    uiMode = "council";
    syncModes();
    render();
  }));
}

function render() {
  const d = focused();
  renderMagi(d);
  renderStatusBar(d);
  renderConversation(d);
  renderHistory();
  const abortBtn = document.getElementById("c-abort");
  abortBtn.hidden = !(uiMode === "council" && d && ["open", "split", "executing"].includes(d.status));
}

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

async function send() {
  const status = document.getElementById("c-status");
  const input = document.getElementById("c-input");
  const body = input.value.trim();
  if (!body) return;
  const payload = { mode: uiMode, body };
  // producción: repo + flag viajan con la consulta que abre la decisión
  const repo = document.getElementById("c-repo").value.trim();
  if (uiMode === "council" && repo) {
    payload.artifact = repo;
    payload.production = document.getElementById("c-prod").checked;
  }
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
