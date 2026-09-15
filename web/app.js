/* 뉴스레터 에이전트 — 프런트엔드 */

const $ = (id) => document.getElementById(id);
const api = async (path, opts) => {
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
};
const postJSON = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const STAGE_LABELS = {
  collect: "수집", curate: "선별", research: "취재", verify: "검수", publish: "발행",
};
const STAGE_ORDER = ["collect", "curate", "research", "verify", "publish"];
const FACT_MARK = { supported: "✅", contradicted: "❌", unverified: "❔" };

let config = null;
let currentStatus = "idle";

/* ───────────────────────────────────────── 탭 */

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("is-active", t === tab));
    const name = tab.dataset.tab;
    document.querySelectorAll(".panel").forEach((p) => p.classList.toggle("is-active", p.id === `panel-${name}`));
    if (name === "history") loadHistory();
    if (name === "settings") { loadConfig(); loadSecrets(); }
  });
});

/* ───────────────────────────────────────── 단계 표시 */

function renderStepper(upto) {
  const limit = STAGE_ORDER.indexOf(upto || "publish");
  $("stepper").innerHTML = STAGE_ORDER.map((s, i) => `
    <li class="step${i > limit ? " skipped" : ""}" data-step="${s}">
      <span class="step-num">${i + 1}</span><span>${STAGE_LABELS[s]}</span>
    </li>`).join("");
}

function markStep(stage, phase) {
  const el = document.querySelector(`.step[data-step="${stage}"]`);
  if (!el) return;
  el.classList.remove("active", "done", "skipped");
  if (phase === "start") el.classList.add("active");
  else if (phase === "done") el.classList.add("done");
  else if (phase === "skipped") el.classList.add("skipped");
}

/* ───────────────────────────────────────── 로그 */

const logEl = $("log");
function appendLog(item) {
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  const line = document.createElement("div");
  const isEvent = item.kind !== "log";
  line.className = `log-line log-${item.level || "INFO"}${isEvent ? " log-event" : ""}`;
  line.innerHTML = `<span class="log-time">${esc(item.time || "")}</span><span class="log-msg">${esc(
    item.message || eventText(item)
  )}</span>`;
  logEl.appendChild(line);
  while (logEl.childElementCount > 800) logEl.removeChild(logEl.firstChild);
  if (atBottom) logEl.scrollTop = logEl.scrollHeight;
}

function eventText(item) {
  switch (item.kind) {
    case "started":
      return `▶ 실행 시작 · ${item.run_id} · ${STAGE_LABELS[item.stage] || item.stage}까지 · LLM ${item.provider}${
        item.dry_run ? " · 연습 실행" : " · 실제 발행"}`;
    case "stage":
      return `${item.phase === "start" ? "┌" : item.phase === "done" ? "└" : "×"} ${STAGE_LABELS[item.stage]} ${
        { start: "시작", done: "완료", skipped: "건너뜀" }[item.phase] || item.phase}`;
    case "awaiting_approval":
      return `⏸ 발행 대기 — ${item.count}건. 확인 후 발행 버튼을 누르세요.`;
    case "discarded":
      return "✕ 발행을 취소했습니다.";
    case "error":
      return `✕ ${item.message}`;
    case "finished":
      return "■ 종료";
    default:
      return JSON.stringify(item);
  }
}

$("clearLog").addEventListener("click", () => (logEl.innerHTML = ""));

/* ───────────────────────────────────────── SSE */

function connectStream() {
  const source = new EventSource("/api/stream");
  source.onmessage = (ev) => {
    const item = JSON.parse(ev.data);
    appendLog(item);
    if (item.kind === "stage") markStep(item.stage, item.phase);
    if (item.kind === "started") renderStepper(item.stage);
    if (item.kind === "finished" || item.kind === "awaiting_approval" || item.kind === "error") refreshStatus();
  };
  source.onerror = () => {
    source.close();
    setTimeout(connectStream, 2500);
  };
}

/* ───────────────────────────────────────── 상태 */

function setStatus(status, session) {
  currentStatus = status;
  const pill = $("statusPill");
  const map = {
    idle: ["대기", "pill-idle"],
    running: ["실행 중", "pill-running"],
    awaiting_approval: ["발행 대기", "pill-await"],
    done: ["완료", "pill-done"],
    error: ["오류", "pill-error"],
  };
  const [text, cls] = map[status] || map.idle;
  pill.textContent = text;
  pill.className = `pill ${cls}`;

  const busy = status === "running";
  $("runBtn").disabled = busy || status === "awaiting_approval";
  $("runBtnText").textContent = busy ? "실행 중…" : "실행";
  $("approvalBar").hidden = status !== "awaiting_approval";

  $("errorBox").hidden = !(status === "error" && session?.error);
  if (session?.error) $("errorBox").textContent = session.error;
}

function updateSchedulePill(sch) {
  const el = $("schedPill");
  if (!el) return;
  if (!sch || !sch.enabled) {
    el.textContent = "⏰ 스케줄 꺼짐";
    el.className = "pill pill-idle";
  } else {
    const rem = sch.remaining_seconds ? `${Math.round(sch.remaining_seconds / 60)}분 후` : "곧 실행";
    el.textContent = `⏰ 자동 실행: ${sch.next_run_display || ""} (${rem})`;
    el.className = "pill on";
  }
}

async function refreshStatus() {
  try {
    const { session, result, schedule } = await api("/api/status");
    setStatus(session.status, session);
    renderResult(result);
    updateSchedulePill(schedule);
    if (session.status === "awaiting_approval") {
      $("approvalCount").textContent = `${result.results.length}건을 보낼 준비가 됐습니다. 아래에서 확인하세요.`;
    }
  } catch (e) {
    console.error(e);
  }
}

/* ───────────────────────────────────────── 결과 렌더 */

function statTile(label, value, sub) {
  return `<div class="stat"><div class="stat-label">${esc(label)}</div>
    <div class="stat-value">${esc(value)}</div>
    ${sub ? `<div class="stat-sub">${esc(sub)}</div>` : ""}</div>`;
}

function renderStats(stats) {
  const row = $("statsRow");
  if (!stats || !Object.keys(stats).length) { row.hidden = true; return; }
  const c = stats.collect || {}, cu = stats.curate || {}, r = stats.research || {},
        v = stats.verify || {}, p = stats.publish || {}, llm = stats.llm || {};
  const tiles = [];
  if (c.unique !== undefined) tiles.push(statTile("수집", c.unique, `소스 ${c.sources_enabled}개 · 실패 ${c.sources_failed}`));
  if (cu.selected !== undefined) tiles.push(statTile("선별", cu.selected,
    `후보 ${cu.candidates}${cu.skipped_already_published ? ` · 기발행 ${cu.skipped_already_published} 제외` : ""}`));
  if (r.briefs !== undefined) tiles.push(statTile("취재", r.briefs, r.degraded ? `폴백 ${r.degraded}건` : "전부 정상"));
  if (v.verified !== undefined) {
    tiles.push(statTile("평균 출처", `${v.avg_sources ?? "-"}곳`, `신뢰도 ${v.avg_confidence ?? "-"}`));
    const extra = [];
    if (v.peer_bodies_fetched) extra.push(`본문 ${v.peer_bodies_fetched}`);
    if (v.peers_collapsed_as_reprint) extra.push(`전재제외 ${v.peers_collapsed_as_reprint}`);
    if (v.merged_same_event) extra.push(`병합 ${v.merged_same_event}`);
    tiles.push(statTile("검수", v.verified, extra.join(" · ") || "—"));
  }
  if (p.published !== undefined) tiles.push(statTile("발행", p.published,
    p.dry_run ? "연습 실행" : `${(p.delivered || []).join(", ") || "전송 실패"}`));
  if (llm.calls) tiles.push(statTile("LLM 호출", llm.calls,
    llm.input_tokens ? `${(llm.input_tokens + llm.output_tokens).toLocaleString()} 토큰` : "mock"));
  row.innerHTML = tiles.join("");
  row.hidden = false;
}

function renderCard(item) {
  const a = item.article || {};
  const conf = Math.round((item.confidence || 0) * 100);
  const facts = item.fact_checks || [];
  const supported = facts.filter((f) => f.status === "supported").length;
  const contradicted = facts.filter((f) => f.status === "contradicted").length;

  return `<article class="rcard v-${esc(item.verdict)}">
    <div class="rcard-top">
      <span class="badge v-${esc(item.verdict)}">${esc(item.verdict_label)}</span>
      <span class="conf">
        <span class="conf-bar"><span class="conf-fill" style="width:${conf}%"></span></span>
        <span class="conf-num">${(item.confidence ?? 0).toFixed(2)}</span>
      </span>
      <span class="chip-label">교차 출처 ${item.sources.length}곳</span>
      ${item.degraded ? '<span class="badge">요약 폴백</span>' : ""}
    </div>

    <h3 class="rtitle">${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(item.headline)}</a>`
      : esc(item.headline)}</h3>

    <ul class="bullets">${(item.summary || []).map((b) => `<li>${esc(b)}</li>`).join("")}</ul>

    ${item.why_it_matters ? `<p class="why"><strong>왜 중요한가</strong> ${esc(item.why_it_matters)}</p>` : ""}

    ${item.sources.length ? `<div class="chips"><span class="chip-label">근거</span>${
      item.sources.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>` : ""}

    ${facts.length ? `<details>
      <summary>사실 확인 ${facts.length}건 — 확인 ${supported}${contradicted ? ` · 상충 ${contradicted}` : ""}</summary>
      <ul class="facts">${facts.map((f) => `<li>
        <span class="mark">${FACT_MARK[f.status] || "❔"}</span>
        <span>${esc(f.fact)}${f.evidence ? ` <span class="fact-evi">— ${esc(f.evidence)}</span>` : ""}</span>
      </li>`).join("")}</ul>
    </details>` : ""}

    <div class="rcard-foot">
      <span>${esc(a.source || "")}</span>
      ${a.published_at ? `<span>${esc(String(a.published_at).slice(0, 16).replace("T", " "))}</span>` : ""}
      ${a.matched_interest ? `<span>관심사: ${esc(a.matched_interest)}</span>` : ""}
      ${a.score ? `<span>선별점수 ${a.score}</span>` : ""}
      ${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">원문 →</a>` : ""}
    </div>
  </article>`;
}

function renderResult(result) {
  renderStats(result.stats);
  const items = result.results || [];
  $("resultCount").textContent = items.length ? `${items.length}건` : "";
  $("results").innerHTML = items.map(renderCard).join("");
  $("resultsEmpty").hidden = items.length > 0;

  if (!items.length && result.selected && result.selected.length) {
    // 검수 전 단계까지만 돌린 경우 — 선별 결과라도 보여준다
    $("resultsEmpty").hidden = true;
    $("results").innerHTML = result.selected.map((a) => `<article class="rcard">
      <div class="rcard-top"><span class="badge">선별됨</span>
        <span class="conf"><span class="conf-num">${a.score}</span></span>
        <span class="chip-label">kw ${a.keyword_score} · vec ${a.vector_score}</span></div>
      <h3 class="rtitle"><a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a></h3>
      <div class="chips"><span class="chip-label">${esc(a.matched_interest || "")}</span>
        ${(a.matched_keywords || []).map((k) => `<span class="chip">${esc(k)}</span>`).join("")}</div>
      <div class="rcard-foot"><span>${esc(a.source)}</span><span>${esc(a.domain)}</span></div>
    </article>`).join("");
    $("resultCount").textContent = `선별 ${result.selected.length}건`;
  }
}

/* ───────────────────────────────────────── 실행 */

$("runBtn").addEventListener("click", async () => {
  const options = {
    stage: $("optStage").value,
    dry_run: $("optDryRun").checked,
    approve: $("optApprove").checked,
  };
  const provider = $("optProvider").value;
  if (provider) options.provider = provider;
  const maxArticles = $("optMaxArticles").value;
  if (maxArticles) options.max_articles = Number(maxArticles);

  logEl.innerHTML = "";
  renderStepper(options.stage);
  try {
    await postJSON("/api/run", options);
    setStatus("running");
  } catch (e) {
    setStatus("error", { error: e.message });
  }
});

$("approveBtn").addEventListener("click", async () => {
  $("approveBtn").disabled = true;
  try {
    await postJSON("/api/approve");
    setStatus("running");
  } catch (e) {
    setStatus("error", { error: e.message });
  } finally {
    $("approveBtn").disabled = false;
  }
});

$("discardBtn").addEventListener("click", async () => {
  try {
    await postJSON("/api/discard");
    refreshStatus();
  } catch (e) { console.error(e); }
});

function updateRunHint() {
  const dry = $("optDryRun").checked;
  const approve = $("optApprove").checked;
  const stage = $("optStage").value;
  let hint;
  if (stage !== "publish") {
    hint = `${STAGE_LABELS[stage]} 단계까지만 돌립니다. 발행하지 않습니다.`;
  } else if (dry) {
    hint = "연습 실행입니다. 결과만 보여주고 디스코드로 보내지 않습니다.";
  } else if (approve) {
    hint = "검수까지 돌린 뒤 멈춥니다. 결과를 보고 발행 버튼을 눌러야 전송됩니다.";
  } else {
    hint = "⚠ 실제로 디스코드에 바로 전송됩니다.";
  }
  $("runHint").textContent = hint;
  $("optApprove").disabled = dry || stage !== "publish";
}
["optDryRun", "optApprove", "optStage"].forEach((id) => $(id).addEventListener("change", updateRunHint));

/* ───────────────────────────────────────── 설정 */

async function loadConfig() {
  config = await api("/api/config");
  const c = config;

  $("cfgMode").value = c.curate.mode;
  $("cfgThreshold").value = c.curate.threshold;
  $("cfgThresholdOut").textContent = Number(c.curate.threshold).toFixed(2);
  $("cfgMaxArticles").value = c.curate.max_articles;
  $("cfgLookback").value = c.collect.lookback_hours;

  $("cfgCluster").value = c.verify.cluster_threshold;
  $("cfgClusterOut").textContent = Number(c.verify.cluster_threshold).toFixed(2);
  $("cfgPeerAge").value = c.verify.peer_max_age_hours;
  $("cfgSearchProvider").value = c.verify.search_provider;
  $("cfgFetchPeer").checked = c.verify.fetch_peer_content;

  $("cfgProvider").value = c.llm.provider;
  $("cfgModel").value = c.llm.model[c.llm.provider] || "";
  $("cfgBaseUrl").value = (c.llm.base_url || {}).local || "";
  $("baseUrlField").hidden = c.llm.provider !== "local";
  $("cfgTitle").value = c.publish.title;
  $("cfgUsername").value = c.publish.username;
  $("cfgDryRun").checked = c.publish.dry_run;
  $("cfgHistory").checked = c.history.enabled;
  renderChannels(c.channels, c.publish.targets);

  const sch = c.schedule || {};
  $("cfgSchedEnabled").checked = Boolean(sch.enabled);
  $("cfgSchedMode").value = sch.mode || "interval";
  $("cfgSchedInterval").value = sch.interval_hours || 6;
  $("cfgSchedDailyTime").value = sch.daily_time || "08:30";
  syncScheduleBoxes();

  $("searchHelp").textContent = c.env.naver_hub
    ? "네이버 API HUB 키가 있습니다. 증인 본문까지 읽어 검증합니다."
    : c.env.naver_legacy
      ? "구 방식 네이버 키로 동작합니다. 2027-06-30 까지만 지원되니 API HUB 키로 옮기세요."
      : "네이버 키가 없어 구글뉴스로 동작합니다 — 증인 본문을 못 읽어 제목 대조까지만 가능합니다.";
  applyProviderHelp();

  renderEnvDots(c.env);
  renderInterests(c.curate.interests);
  renderSources(c.sources);

  const warnings = [...(c.warnings || [])];
  if (c.error) warnings.unshift(c.error);
  $("cfgWarnings").hidden = warnings.length === 0;
  $("cfgWarnings").innerHTML = warnings.length
    ? `<strong>확인 필요</strong><ul>${warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : "";
}

function syncScheduleBoxes() {
  const mode = $("cfgSchedMode").value;
  $("schedIntervalBox").hidden = mode !== "interval";
  $("schedDailyBox").hidden = mode !== "daily";
}
$("cfgSchedMode").addEventListener("change", syncScheduleBoxes);

function renderEnvDots(env) {
  const labels = {
    anthropic: "Anthropic", openai: "OpenAI", gemini: "Gemini",
    discord: "Discord", slack: "Slack", telegram: "Telegram", naver_hub: "네이버",
  };
  $("envDots").innerHTML = Object.entries(labels)
    .map(([k, label]) => `<span class="env-dot${env[k] ? " on" : ""}" title="${env[k] ? "키 있음" : "키 없음"}">${label}</span>`)
    .join("");
}

function interestCard(it) {
  const keywords = it.keywords || [];
  const exclude = it.exclude || [];
  return `
    <div class="interest">
      <div class="interest-head">
        <input class="i-name" type="text" value="${esc(it.name || "")}"
               placeholder="관심사 이름 (예: AI 인프라·반도체)">
        <label class="field field-narrow" style="min-width:auto">
          <span class="field-label">가중치</span>
          <input type="number" class="i-weight" step="0.1" min="0" max="3" value="${it.weight ?? 1}">
        </label>
        <button type="button" class="btn btn-tiny i-remove" title="이 관심사를 삭제합니다">삭제</button>
      </div>
      <div class="kw-group">
        <div class="kw-title">포함 키워드</div>
        <div class="kw-list" data-kind="keywords">
          ${keywords.map((k) => kwChip(k, false)).join("")}
          <span class="kw-input">
            <input class="kw-add" data-kind="keywords" placeholder="키워드 입력">
            <button type="button" class="kw-add-btn" title="추가">추가</button>
          </span>
        </div>
      </div>
      <div class="kw-group">
        <div class="kw-title">제외 키워드</div>
        <div class="kw-list" data-kind="exclude">
          ${exclude.map((k) => kwChip(k, true)).join("")}
          <span class="kw-input">
            <input class="kw-add" data-kind="exclude" placeholder="제외할 단어">
            <button type="button" class="kw-add-btn" title="추가">추가</button>
          </span>
        </div>
      </div>
    </div>`;
}

function renderInterests(interests) {
  $("interests").innerHTML = (interests || []).map(interestCard).join("");
}

$("addInterest").addEventListener("click", () => {
  $("interests").insertAdjacentHTML("beforeend", interestCard({ name: "", weight: 1 }));
  const added = $("interests").lastElementChild;
  added.scrollIntoView({ behavior: "smooth", block: "nearest" });
  added.querySelector(".i-name").focus();
});

function kwChip(word, isExclude) {
  return `<span class="kw${isExclude ? " excl" : ""}" data-word="${esc(word)}">${esc(word)}<button type="button" title="삭제">×</button></span>`;
}

$("interests").addEventListener("click", (ev) => {
  const remove = ev.target.closest(".i-remove");
  if (remove) {
    const card = remove.closest(".interest");
    const name = card.querySelector(".i-name").value.trim();
    const filled = card.querySelectorAll(".kw").length > 0 || name;
    if (filled && !confirm(`관심사 '${name || "(이름 없음)"}' 를 삭제할까요?\n저장을 눌러야 파일에 반영됩니다.`)) return;
    card.remove();
    return;
  }
  // 키워드 '추가' 버튼
  const addBtn = ev.target.closest(".kw-add-btn");
  if (addBtn) {
    const input = addBtn.parentElement.querySelector(".kw-add");
    commitKeywords(input);
    input.focus();
    return;
  }
  // 키워드 칩의 × 버튼
  if (ev.target.tagName === "BUTTON" && ev.target.closest(".kw")) {
    ev.target.closest(".kw").remove();
  }
});
/* 키워드 추가는 세 가지 방법을 모두 받는다.
   Enter 하나만 두면 한글 IME 가 그 Enter 를 조합 확정에 써버려서
   "입력했는데 아무 일도 안 일어난다" 가 된다. 실제로 그렇게 막혔다. */

function commitKeywords(input) {
  const list = input.closest(".kw-list");
  const isExclude = list.dataset.kind === "exclude";
  const existing = new Set([...list.querySelectorAll(".kw")].map((k) => k.dataset.word));
  const holder = input.closest(".kw-input");

  // 쉼표·줄바꿈으로 여러 개를 한 번에 넣을 수 있다
  const added = [];
  for (const raw of input.value.split(/[,\n]/)) {
    const word = raw.trim();
    if (!word || existing.has(word)) continue;
    existing.add(word);
    added.push(word);
    holder.insertAdjacentHTML("beforebegin", kwChip(word, isExclude));
  }
  input.value = "";
  return added.length;
}

$("interests").addEventListener("keydown", (ev) => {
  if (!ev.target.classList.contains("kw-add") || ev.key !== "Enter") return;
  // IME 조합 중의 Enter 는 글자를 확정하는 용도다. 여기서 가로채면 안 된다.
  if (ev.isComposing || ev.keyCode === 229) return;
  ev.preventDefault();
  commitKeywords(ev.target);
});

// 입력만 하고 다른 곳을 눌러도 잃지 않는다
$("interests").addEventListener("focusout", (ev) => {
  if (ev.target.classList.contains("kw-add") && ev.target.value.trim()) {
    commitKeywords(ev.target);
  }
});

const SOURCE_TYPES = { rss: "RSS / Atom", api: "JSON API", crawl: "크롤링" };

function kvToText(obj) {
  return Object.entries(obj || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
}

function textToKv(text) {
  const out = {};
  for (const line of String(text || "").split("\n")) {
    const at = line.indexOf(":");
    if (at < 1) continue;
    const key = line.slice(0, at).trim();
    const value = line.slice(at + 1).trim();
    if (!key) continue;
    // 숫자는 숫자로 저장한다. 따옴표 붙은 '50' 이 yaml 에 남으면 보기 나쁘다.
    out[key] = /^-?\d+$/.test(value) ? Number(value) : value;
  }
  return out;
}

function sourceCard(s, open) {
  const fm = s.field_map || {};
  return `
  <div class="source-card${open ? " is-open" : ""}" data-type="${esc(s.type || "rss")}">
    <div class="source-row">
      <label class="switch">
        <input type="checkbox" class="s-enabled" ${s.enabled !== false ? "checked" : ""}>
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </label>
      <span class="source-name">
        <span class="s-title">${esc(s.name || "새 소스")}</span>
        <span class="source-url">${esc(s.url || "주소를 입력하세요")}</span>
      </span>
      <span class="tag s-type-tag">${esc(SOURCE_TYPES[s.type] || s.type || "rss")}</span>
      <span class="tag tag-witness s-role-tag"${s.role === "corroboration" ? "" : " hidden"}>증인 전용</span>
      <button type="button" class="btn btn-tiny s-edit">편집</button>
      <button type="button" class="btn btn-tiny s-remove">삭제</button>
    </div>

    <div class="source-edit">
      <div class="sfield-row">
        <label class="field"><span class="field-label">이름</span>
          <input class="s-name" type="text" value="${esc(s.name || "")}" placeholder="예: AI타임스"></label>
        <label class="field"><span class="field-label">종류</span>
          <select class="s-type">
            ${Object.entries(SOURCE_TYPES).map(([k, v]) =>
              `<option value="${k}"${(s.type || "rss") === k ? " selected" : ""}>${v}</option>`).join("")}
          </select></label>
        <label class="field"><span class="field-label">역할</span>
          <select class="s-role">
            <option value="content"${s.role !== "corroboration" ? " selected" : ""}>본문 — 뉴스레터에 실림</option>
            <option value="corroboration"${s.role === "corroboration" ? " selected" : ""}>증인 전용 — 검증에만 사용</option>
          </select></label>
        <label class="field field-narrow"><span class="field-label">최대 건수</span>
          <input class="s-max" type="number" min="1" max="200" value="${s.max_items || 30}"></label>
        <label class="field field-narrow"><span class="field-label">수집 기간(h)</span>
          <input class="s-lookback" type="number" min="1" max="720" value="${s.lookback_hours || ""}" placeholder="기본"></label>
      </div>

      <label class="field"><span class="field-label">주소 (URL)</span>
        <input class="s-url" type="text" value="${esc(s.url || "")}" placeholder="https://example.com/rss.xml"></label>

      <div class="type-only" data-for="rss">
        <p class="help">RSS 2.0 과 Atom 1.0 을 같은 방식으로 처리합니다. 주소만 넣으면 됩니다.</p>
      </div>

      <div class="type-only" data-for="api">
        <label class="field"><span class="field-label">기사 배열 위치 (items_path)</span>
          <input class="s-items-path" type="text" value="${esc(s.items_path || "")}" placeholder="예: articles 또는 data.items">
          <small class="help">응답 JSON 에서 기사 목록이 있는 경로. 점으로 중첩을 표현합니다.</small></label>
        <div class="sfield-row">
          <label class="field"><span class="field-label">제목 필드</span>
            <input class="s-fm-title" type="text" value="${esc(fm.title || "title")}"></label>
          <label class="field"><span class="field-label">링크 필드</span>
            <input class="s-fm-url" type="text" value="${esc(fm.url || "url")}"></label>
          <label class="field"><span class="field-label">요약 필드</span>
            <input class="s-fm-snippet" type="text" value="${esc(fm.snippet || "description")}"></label>
          <label class="field"><span class="field-label">발행시각 필드</span>
            <input class="s-fm-date" type="text" value="${esc(fm.published_at || "publishedAt")}"></label>
          <label class="field"><span class="field-label">매체명 필드</span>
            <input class="s-fm-source" type="text" value="${esc(fm.source || "")}" placeholder="없으면 비움"></label>
        </div>
        <label class="field"><span class="field-label">쿼리 파라미터</span>
          <textarea class="s-params" rows="3" placeholder="country: kr&#10;pageSize: 50">${esc(kvToText(s.params))}</textarea>
          <small class="help">한 줄에 <code>이름: 값</code> 하나씩.</small></label>
        <label class="field"><span class="field-label">요청 헤더</span>
          <textarea class="s-headers" rows="2" placeholder="X-Api-Key: \${NEWSAPI_KEY}">${esc(kvToText(s.headers))}</textarea>
          <small class="help">API 키는 <code>\${환경변수명}</code> 으로 적고 실제 값은 위의 'API 키' 에 넣으세요.
            여기에 키를 직접 적으면 설정 파일에 그대로 남습니다.</small></label>
      </div>

      <div class="type-only" data-for="crawl">
        <div class="sfield-row">
          <label class="field"><span class="field-label">목록 셀렉터 (필수)</span>
            <input class="s-list-sel" type="text" value="${esc(s.list_selector || "")}" placeholder="예: span.titleline > a"></label>
          <label class="field"><span class="field-label">제목 셀렉터</span>
            <input class="s-title-sel" type="text" value="${esc(s.title_selector || "")}" placeholder="비우면 링크 텍스트"></label>
          <label class="field"><span class="field-label">요약 셀렉터</span>
            <input class="s-snippet-sel" type="text" value="${esc(s.snippet_selector || "")}" placeholder="선택"></label>
          <label class="field field-narrow"><span class="field-label">링크 속성</span>
            <input class="s-link-attr" type="text" value="${esc(s.link_attr || "href")}"></label>
        </div>
        <label class="field"><span class="field-label">상대경로 기준 주소</span>
          <input class="s-base-url" type="text" value="${esc(s.base_url || "")}" placeholder="비우면 위 주소 기준">
          <small class="help">robots.txt 를 확인하고 차단된 경로는 건너뜁니다.</small></label>
      </div>
    </div>
  </div>`;
}

function syncSourceType(card) {
  const type = card.querySelector(".s-type").value;
  card.dataset.type = type;
  card.querySelectorAll(".type-only").forEach((el) => {
    el.hidden = el.dataset.for !== type;
  });
  card.querySelector(".s-type-tag").textContent = SOURCE_TYPES[type] || type;
}

function syncSourceHeader(card) {
  card.querySelector(".s-title").textContent = card.querySelector(".s-name").value.trim() || "새 소스";
  card.querySelector(".s-url-display") // no-op guard
    ?.remove();
  card.querySelector(".source-url").textContent =
    card.querySelector(".s-url").value.trim() || "주소를 입력하세요";
  card.querySelector(".s-role-tag").hidden = card.querySelector(".s-role").value !== "corroboration";
}

function renderSources(sources) {
  $("sources").innerHTML = (sources || []).map((s) => sourceCard(s, false)).join("");
  document.querySelectorAll(".source-card").forEach(syncSourceType);
}

$("addSource").addEventListener("click", () => {
  $("sources").insertAdjacentHTML("beforeend", sourceCard({ type: "rss", enabled: true, max_items: 30 }, true));
  const card = $("sources").lastElementChild;
  syncSourceType(card);
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  card.querySelector(".s-name").focus();
});

$("sources").addEventListener("click", (ev) => {
  const card = ev.target.closest(".source-card");
  if (!card) return;
  if (ev.target.closest(".s-edit")) {
    card.classList.toggle("is-open");
    return;
  }
  if (ev.target.closest(".s-remove")) {
    const name = card.querySelector(".s-name").value.trim();
    if (!confirm(`소스 '${name || "(이름 없음)"}' 를 삭제할까요?\n저장을 눌러야 파일에 반영됩니다.`)) return;
    card.remove();
  }
});

$("sources").addEventListener("change", (ev) => {
  const card = ev.target.closest(".source-card");
  if (!card) return;
  if (ev.target.classList.contains("s-type")) syncSourceType(card);
  syncSourceHeader(card);
});
$("sources").addEventListener("input", (ev) => {
  const card = ev.target.closest(".source-card");
  if (card && (ev.target.classList.contains("s-name") || ev.target.classList.contains("s-url"))) {
    syncSourceHeader(card);
  }
});

function collectSources() {
  return [...document.querySelectorAll(".source-card")].map((card) => {
    const val = (sel) => (card.querySelector(sel)?.value ?? "").trim();
    const type = val(".s-type") || "rss";
    const out = {
      name: val(".s-name"),
      type,
      enabled: card.querySelector(".s-enabled").checked,
      role: val(".s-role") || "content",
      url: val(".s-url"),
      max_items: Number(val(".s-max") || 30),
    };
    const lookback = val(".s-lookback");
    if (lookback) out.lookback_hours = Number(lookback);
    if (type === "api") {
      out.items_path = val(".s-items-path");
      out.field_map = Object.fromEntries(Object.entries({
        title: val(".s-fm-title"), url: val(".s-fm-url"), snippet: val(".s-fm-snippet"),
        published_at: val(".s-fm-date"), source: val(".s-fm-source"),
      }).filter(([, v]) => v));
      out.params = textToKv(card.querySelector(".s-params").value);
      out.headers = textToKv(card.querySelector(".s-headers").value);
    } else if (type === "crawl") {
      out.list_selector = val(".s-list-sel");
      out.title_selector = val(".s-title-sel");
      out.snippet_selector = val(".s-snippet-sel");
      out.link_attr = val(".s-link-attr") || "href";
      out.base_url = val(".s-base-url");
    }
    return out;
  }).filter((s) => s.name);
}

$("cfgThreshold").addEventListener("input", (e) => ($("cfgThresholdOut").textContent = Number(e.target.value).toFixed(2)));
$("cfgCluster").addEventListener("input", (e) => ($("cfgClusterOut").textContent = Number(e.target.value).toFixed(2)));
const PROVIDER_HELP = {
  anthropic: ["ANTHROPIC_API_KEY", "아래 'API 키' 에서 Claude 키를 넣으세요."],
  openai: ["OPENAI_API_KEY", "아래 'API 키' 에서 OpenAI 키를 넣으세요."],
  gemini: ["GOOGLE_API_KEY", "아래 'API 키' 에서 Gemini 키를 넣으세요."],
};
const PROVIDER_ENV_FLAG = { anthropic: "anthropic", openai: "openai", gemini: "gemini" };

function applyProviderHelp() {
  if (!config) return;
  const provider = $("cfgProvider").value;
  let text;
  if (provider === "mock") {
    text = "규칙 기반 백엔드입니다. 키 없이 돌아가지만 요약은 기사 앞 문장을 자른 것이고 의미 판단은 하지 못합니다.";
  } else if (provider === "local") {
    text = "OpenAI 호환 로컬 서버를 부릅니다. 서버가 떠 있어야 하며, 인증이 없으면 키는 비워두세요.";
  } else {
    const [envName, howto] = PROVIDER_HELP[provider];
    text = config.env[PROVIDER_ENV_FLAG[provider]]
      ? `${envName} 확인됨.`
      : `⚠ ${envName} 가 없습니다. ${howto}`;
  }
  $("providerHelp").textContent = text;

  // 백엔드를 바꾸면 그 백엔드의 모델 이름으로 갈아끼운다
  $("cfgModel").value = (config.llm.model || {})[provider] || "";
  $("baseUrlField").hidden = provider !== "local";
}
$("cfgProvider").addEventListener("change", applyProviderHelp);

/* ── 발행 채널 ── */

function renderChannels(channels, selected) {
  const chosen = new Set(selected || []);
  $("channelList").innerHTML = (channels || []).map((ch) => `
    <div class="channel-row">
      <label class="switch">
        <input type="checkbox" class="ch-on" data-id="${esc(ch.id)}" ${chosen.has(ch.id) ? "checked" : ""}>
        <span class="switch-track"><span class="switch-thumb"></span></span>
      </label>
      <span class="channel-name">${esc(ch.label)}</span>
      <span class="channel-state ${ch.ready ? "ready" : "missing"}"
            title="${ch.ready ? "필요한 값이 모두 있습니다" : esc(ch.missing.join(", ")) + " 가 필요합니다"}">${
        ch.ready ? "준비됨" : "키 필요"}</span>
    </div>`).join("");
}

/* ── API 키 ── */

const SECRET_GROUPS = [
  { title: "LLM", keys: ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "LOCAL_LLM_API_KEY"] },
  { title: "발행 채널", keys: ["DISCORD_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"] },
  { title: "교차검증 검색 — 네이버 (신규: API HUB)", keys: ["NAVER_API_KEY_ID", "NAVER_API_KEY"] },
  { title: "교차검증 검색 — 네이버 (구 방식, 기존 키가 있을 때만)", keys: ["NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET"] },
];

const SECRET_HINTS = {
  ANTHROPIC_API_KEY: 'console.anthropic.com 에서 발급',
  OPENAI_API_KEY: 'platform.openai.com/api-keys 에서 발급',
  GOOGLE_API_KEY: 'aistudio.google.com/apikey 에서 발급 (무료 등급 있음)',
  LOCAL_LLM_API_KEY: 'Ollama 등 인증이 없는 서버라면 비워두세요',
  DISCORD_WEBHOOK_URL: '채널 편집 → 연동 → 웹후크 → 새 웹후크 → URL 복사',
  SLACK_WEBHOOK_URL: 'Slack 앱 → Incoming Webhooks 에서 발급',
  TELEGRAM_BOT_TOKEN: '@BotFather 로 봇을 만들고 받은 토큰',
  TELEGRAM_CHAT_ID: '봇과 대화 시작 후 api.telegram.org/bot<토큰>/getUpdates 에서 확인',
  NAVER_API_KEY_ID: '네이버클라우드 플랫폼 콘솔 → NAVER API HUB → 검색(뉴스) 신청 (무료)',
  NAVER_API_KEY: '',
  NAVER_CLIENT_ID: '개발자센터에서 예전에 받은 키. 2027-06-30 까지만 지원됩니다',
  NAVER_CLIENT_SECRET: '',
};

let secretState = {};

function secretRow(key, info) {
  const hint = SECRET_HINTS[key];
  return `<div class="secret-row">
    <span class="secret-label">${esc(info.label)}</span>
    <input type="password" class="secret-input" data-key="${esc(key)}"
           placeholder="${info.set ? "저장됨 — 바꾸려면 새 값 입력" : "입력하세요"}"
           autocomplete="off" spellcheck="false">
    <span class="secret-state ${info.set ? "on" : ""}">${info.set ? esc(info.preview) : "미설정"}</span>
    <button type="button" class="btn btn-tiny secret-clear" data-key="${esc(key)}"
            ${info.set ? "" : "disabled"}>지우기</button>
    ${hint ? `<div class="secret-hint">${esc(hint)}</div>` : ""}
  </div>`;
}

async function loadSecrets() {
  const { secrets } = await api("/api/secrets");
  secretState = secrets;
  const customKeys = Object.keys(secrets).filter((k) => secrets[k].custom);

  $("secretGroups").innerHTML = SECRET_GROUPS.map((group) => `
    <div>
      <div class="secret-group-title">${esc(group.title)}</div>
      ${group.keys.map((key) => secretRow(key, secrets[key] || { label: key, set: false, preview: "" })).join("")}
    </div>`).join("")
    + `<div>
        <div class="secret-group-title">직접 추가한 키 — 커스텀 소스용</div>
        ${customKeys.map((key) => secretRow(key, secrets[key])).join("")}
        <div class="secret-row custom-add">
          <input class="custom-name" type="text" placeholder="변수명 (예: FINNHUB_TOKEN)"
                 autocomplete="off" spellcheck="false">
          <input class="custom-value" type="password" placeholder="값" autocomplete="off" spellcheck="false">
          <span></span>
          <button type="button" id="addCustomSecret" class="btn btn-tiny">추가</button>
          <div class="secret-hint">
            소스 설정의 헤더·파라미터에서 <code>\${변수명}</code> 으로 참조합니다.
            그래야 설정 파일에 키가 남지 않습니다.
          </div>
        </div>
      </div>`;
}

const pendingClear = new Set();
$("secretGroups").addEventListener("click", (ev) => {
  const btn = ev.target.closest(".secret-clear");
  if (!btn) return;
  pendingClear.add(btn.dataset.key);
  btn.disabled = true;
  const state = btn.parentElement.querySelector(".secret-state");
  state.textContent = "저장 시 삭제됨";
  state.classList.remove("on");
});

$("secretGroups").addEventListener("click", (ev) => {
  if (!ev.target.closest("#addCustomSecret")) return;
  const row = ev.target.closest(".custom-add");
  const name = row.querySelector(".custom-name").value.trim().toUpperCase();
  const value = row.querySelector(".custom-value").value.trim();
  const note = $("secretNote");
  if (!name || !value) {
    note.textContent = "변수명과 값을 모두 입력하세요.";
    note.style.color = "var(--disputed)";
    return;
  }
  // 바로 서버로 보낸다 — 임시 입력칸을 들고 있다가 잃어버리지 않게
  postJSON("/api/secrets", { secrets: { [name]: value } })
    .then(async () => {
      row.querySelector(".custom-name").value = "";
      row.querySelector(".custom-value").value = "";
      note.textContent = `${name} 저장됨`;
      note.style.color = "var(--send)";
      await loadSecrets();
      setTimeout(() => (note.textContent = ""), 4000);
    })
    .catch((e) => {
      note.textContent = e.message;
      note.style.color = "var(--disputed)";
    });
});

$("saveSecretsBtn").addEventListener("click", async () => {
  const secrets = {};
  document.querySelectorAll(".secret-input").forEach((input) => {
    const value = input.value.trim();
    if (value) secrets[input.dataset.key] = value;
  });
  const clear = [...pendingClear];
  const note = $("secretNote");

  if (!Object.keys(secrets).length && !clear.length) {
    note.textContent = "변경된 값이 없습니다.";
    note.style.color = "var(--text-dim)";
    setTimeout(() => (note.textContent = ""), 3000);
    return;
  }

  $("saveSecretsBtn").disabled = true;
  try {
    const res = await postJSON("/api/secrets", { secrets, clear });
    pendingClear.clear();
    document.querySelectorAll(".secret-input").forEach((i) => (i.value = ""));
    note.textContent = `저장됨 — ${res.changed.length}개 항목`;
    note.style.color = "var(--send)";
    await loadSecrets();
    await loadConfig();   // 키가 생기면 경고·채널 준비 상태가 달라진다
    setTimeout(() => (note.textContent = ""), 4000);
  } catch (e) {
    note.textContent = e.message;
    note.style.color = "var(--disputed)";
  } finally {
    $("saveSecretsBtn").disabled = false;
  }
});

$("saveBtn").addEventListener("click", async () => {
  // 입력칸에 남아 있는 글자도 저장에 포함한다
  document.querySelectorAll(".kw-add").forEach((i) => i.value.trim() && commitKeywords(i));

  const interests = [...document.querySelectorAll(".interest")].map((el) => ({
    name: el.querySelector(".i-name").value.trim(),
    weight: Number(el.querySelector(".i-weight").value),
    keywords: [...el.querySelectorAll('.kw-list[data-kind="keywords"] .kw')].map((k) => k.dataset.word),
    exclude: [...el.querySelectorAll('.kw-list[data-kind="exclude"] .kw')].map((k) => k.dataset.word),
  })).filter((i) => i.name);

  // 화면이 덜 그려진 상태에서 저장하면 관심사가 통째로 날아간다. 그 전에 막는다.
  if (!interests.length) {
    const note = $("saveNote");
    note.textContent = "이름이 있는 관심사를 최소 하나는 남겨야 합니다.";
    note.style.color = "var(--disputed)";
    return;
  }
  const sources = collectSources();
  if (!sources.length) {
    const note = $("saveNote");
    note.textContent = "수집 소스를 최소 하나는 남겨야 합니다.";
    note.style.color = "var(--disputed)";
    return;
  }

  const patch = {
    curate: {
      mode: $("cfgMode").value,
      threshold: Number($("cfgThreshold").value),
      max_articles: Number($("cfgMaxArticles").value),
      interests,
    },
    collect: { lookback_hours: Number($("cfgLookback").value) },
    verify: {
      cluster_threshold: Number($("cfgCluster").value),
      peer_max_age_hours: Number($("cfgPeerAge").value),
      fetch_peer_content: $("cfgFetchPeer").checked,
      search_provider: $("cfgSearchProvider").value,
    },
    llm: {
      provider: $("cfgProvider").value,
      model: { [$("cfgProvider").value]: $("cfgModel").value.trim() },
      base_url: { local: $("cfgBaseUrl").value.trim() },
    },
    publish: {
      dry_run: $("cfgDryRun").checked,
      title: $("cfgTitle").value,
      username: $("cfgUsername").value,
      targets: [...document.querySelectorAll(".ch-on")].filter((c) => c.checked).map((c) => c.dataset.id),
    },
    history: { enabled: $("cfgHistory").checked },
    schedule: {
      enabled: $("cfgSchedEnabled").checked,
      mode: $("cfgSchedMode").value,
      interval_hours: Number($("cfgSchedInterval").value) || 6,
      daily_time: $("cfgSchedDailyTime").value || "08:30",
    },
    sources,
  };

  const note = $("saveNote");
  $("saveBtn").disabled = true;
  try {
    const res = await postJSON("/api/config", patch);
    note.textContent = `저장됨 — ${res.files.join(", ")}`;
    note.style.color = "var(--send)";
    config = res.config;
    await refreshStatus();
    setTimeout(() => (note.textContent = ""), 4000);
  } catch (e) {
    note.textContent = e.message;
    note.style.color = "var(--disputed)";
  } finally {
    $("saveBtn").disabled = false;
  }
});

/* ───────────────────────────────────────── 이력 */

async function loadHistory() {
  const { runs } = await api("/api/history");
  $("historyEmpty").hidden = runs.length > 0;
  $("historyList").innerHTML = runs.map((r) => `
    <div class="hrow" data-run="${esc(r.run_id)}">
      <span class="hrow-id">${esc(r.run_id)}</span>
      <span class="hrow-flow">수집 ${r.collected} → 선별 ${r.selected} → 발행 ${r.published}</span>
      ${r.dry_run === false ? '<span class="tag">전송됨</span>' : '<span class="tag">연습</span>'}
      <span class="hrow-dist">${Object.entries(r.distribution || {})
        .map(([v, n]) => `<span class="badge v-${esc(v)}">${esc(v)} ${n}</span>`).join("")}</span>
    </div>`).join("");
}

$("refreshHistory").addEventListener("click", loadHistory);

$("historyList").addEventListener("click", async (ev) => {
  const row = ev.target.closest(".hrow");
  if (!row) return;
  const data = await api(`/api/history/${row.dataset.run}`);
  // 저장된 JSON 을 실행 결과와 같은 모양으로 바꿔 렌더한다
  const results = (data.verified || []).map((v) => ({
    headline: v.brief.headline,
    verdict: v.verdict,
    verdict_label: v.verdict,
    confidence: v.confidence,
    sources: v.corroborating_sources || [],
    summary: v.brief.summary || [],
    why_it_matters: v.brief.why_it_matters,
    degraded: v.brief.degraded,
    fact_checks: v.fact_checks || [],
    article: v.brief.article,
  }));
  document.querySelector('.tab[data-tab="run"]').click();
  renderResult({ stats: data.stats || {}, results, selected: [] });
  logEl.innerHTML = "";
  appendLog({ kind: "event", time: "", message: `📂 ${data.run_id} 기록을 불러왔습니다.` });
});

/* ───────────────────────────────────────── 시작 */

renderStepper("publish");
updateRunHint();
connectStream();
refreshStatus();
setInterval(refreshStatus, 30000);
loadConfig().catch((e) => console.error(e));
loadSecrets().catch((e) => console.error(e));
