/* Web shell client — zero dependências (fetch + polling). */

const term = document.getElementById("term");
const cmd = document.getElementById("cmd");
const statusEl = document.getElementById("status");
const memoryEl = document.getElementById("memory");

const state = {
  jobs: new Map(),   // id -> {out, status, exit}
  polling: false,
  auth: "",
};

/* Base64 UTF-8 seguro: `btoa` nativo lança InvalidCharacterError para
   caracteres > U+00FF (ex.: senha com acento/emoji) — codifica os bytes
   UTF-8 antes. */
function b64Encode(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/* Obtém as credenciais Basic: sempre pede ao usuário via prompt — NÃO
   persiste em localStorage (B3: sem credenciais em armazenamento).
   Retorna "" se o usuário cancelar/não informar. */
function getCredentials() {
  const user = prompt("Usuário de acesso do harness:");
  if (!user) {
    print("[erro] usuário de acesso necessário — recarregue a página e informe.", "err");
    return "";
  }
  const pwd = prompt("Senha de acesso do harness:");
  if (!pwd) {
    print("[erro] senha de acesso necessária — recarregue a página e informe.", "err");
    return "";
  }
  return b64Encode(user + ":" + pwd);
}

function print(text, cls) {
  const div = document.createElement("div");
  if (cls) div.className = cls;
  div.textContent = text;
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;
}

async function api(path, opts, retried) {
  const o = opts || {};
  const headers = Object.assign(
    { "Authorization": "Basic " + state.auth },
    o.headers || {}
  );
  let res;
  try {
    res = await fetch(path, Object.assign({}, o, { headers }));
  } catch (err) {
    // rede indisponível / servidor caiu: nunca deixa promise rejeitada
    return { error: "falha de rede: " + err.message };
  }
  if (res.status === 401 && !retried) {
    // credenciais ausentes/inválidas: zera (sem localStorage) e pede de novo
    state.auth = "";
    state.auth = getCredentials();
    if (!state.auth) {
      setStatus("autenticação necessária");
      return { error: "credenciais de acesso ausentes ou inválidas" };
    }
    return api(path, o, true);
  }
  // servidor pode responder não-JSON (404 estático) ou corpo vazio
  try {
    const texto = await res.text();
    return texto ? JSON.parse(texto) : {};
  } catch (err) {
    return { error: "resposta inválida do servidor", status: res.status };
  }
}

function setStatus(text) {
  statusEl.textContent = text;
}

async function exec(line) {
  if (!line.trim()) return;
  print("$ " + line, "ok");
  cmd.value = "";
  const data = await api("/api/exec", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command: line }),
  });
  if (data.error) {
    print(`[erro] ${data.error}`, "err");
    return;
  }
  if (data.status === "blocked") {
    print(`[bloqueado] ${data.reason}`, "err");
    return;
  }
  if (data.status === "error") {
    print(`[erro] ${data.reason}`, "err");
    return;
  }
  if (!data.id) {
    print("[erro] resposta sem id do job.", "err");
    return;
  }
  state.jobs.set(data.id, { out: "", status: "running", exit: null });
  if (!state.polling) {
    state.polling = true;
    pollJobs();
  }
}

async function pollJobs() {
  const running = [...state.jobs.entries()].filter(([, j]) => j.status === "running");
  if (running.length === 0) {
    state.polling = false;
    return;
  }
  await Promise.all(running.map(async ([id, job]) => {
    const snap = await api(`/api/job?id=${id}`);
    if (!snap || snap.error) return;
    if (snap.output && snap.output !== job.out) {
      const delta = snap.output.slice(job.out.length);
      job.out = snap.output;
      print(delta, snap.status === "finished" && snap.exit_code === 0 ? "" : "err");
    }
    if (snap.status !== "running") {
      job.status = snap.status;
      if (snap.status === "stopped") print("[interrompido]", "err");
      else if (snap.exit_code !== 0) print(`[exit ${snap.exit_code}]`, "err");
      else print(`[exit 0]`, "dim");
    }
  }));
  setTimeout(pollJobs, 250);
}

async function refreshMemory() {
  const data = await api("/api/memory");
  if (!data || data.error) return;
  if (!data.records) return;
  memoryEl.innerHTML = "";
  const core = document.createElement("p");
  core.textContent = "core.md: " + (data.core.split("\n")[1] || "").trim();
  core.style.color = "#8b949e";
  memoryEl.appendChild(core);
  data.records.slice().reverse().forEach(rec => {
    const det = document.createElement("details");
    const sum = document.createElement("summary");
    const meta = rec.meta || {};
    const kw = (meta.keywords || "[]").replace(/[\[\]"']/g, "");
    sum.textContent = `${rec.id}  ·  ${kw}`;
    det.appendChild(sum);
    const pre = document.createElement("pre");
    pre.textContent = rec.body;
    det.appendChild(pre);
    memoryEl.appendChild(det);
  });
}

function clearScreen() {
  term.innerHTML = "";
}

cmd.addEventListener("keydown", (e) => {
  if (e.key === "Enter") exec(cmd.value);
});

document.getElementById("btn-stop").addEventListener("click", async () => {
  for (const [id, job] of state.jobs) {
    if (job.status === "running") {
      await api("/api/job/stop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
    }
  }
});

document.getElementById("btn-refresh").addEventListener("click", refreshMemory);
document.getElementById("btn-clear").addEventListener("click", clearScreen);

(async function init() {
  // As credenciais NÃO vêm mais de /api/health (endpoint público): pede ao usuário.
  state.auth = getCredentials();
  if (!state.auth) {
    setStatus("autenticação necessária");
    return;
  }
  const h = await api("/api/health");
  if (h && h.ok) {
    // /api/health não expõe mais o root do projeto (A1)
    setStatus("pronto");
    print(`[harness] web shell conectado. Digite um comando abaixo.`, "dim");
  } else {
    setStatus("servidor indisponível");
    print("[erro] não foi possível conectar ao servidor.", "err");
  }
  refreshMemory();
})();