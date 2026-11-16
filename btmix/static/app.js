"use strict";
const $ = (id) => document.getElementById(id);
const api = async (url, opt) => {
  const r = await fetch(url, opt);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
  return r.json();
};
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "–" :
  (Math.abs(v) >= 1e8 ? "∞" : Number(v).toLocaleString("vi-VN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const sgn = (v, d = 2) => `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${fmt(v, d)}</span>`;
const holdTxt = (h) => (h === 0 ? "∞" : h >= 60 && h % 60 === 0 ? `${h / 60}h` : `${h}m`);
const modeTxt = (m) => (m === "single" ? "1 lệnh/coin" : m === "multi" ? "Nhiều lệnh/coin" : String(m).toUpperCase());
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const tsTxt = (ms) => new Date(ms).toLocaleString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", hour12: false });

let FILES = [], RES = null, SORT = { k: "net", d: -1 }, DETAIL = null, charts = {}, RUN_ID = null, HIST = [];
// Bản gộp: 2 chế độ chạy – "bt" = backtest chiến lược trên dữ liệu sàn, "replay" = chạy lại đúng lệnh thật trong file CSV.
// Kết quả của cả hai cùng khuôn (rows + summary) nên bảng xếp hạng / heatmap / chi tiết / lịch sử dùng chung.
let MODE = localStorage.getItem("mixMode") === "replay" ? "replay" : "bt";
const isBt = () => !!(RES && RES.kind === "bt");
const hasActual = () => !!(RES && RES.kind === "replay");
const rowCfgTxt = (r) => (isBt() ? `${String(r.mode).toUpperCase()} · ${r.params || ""}`
  : `${modeTxt(r.mode)} · TP ${fmt(r.tp)}% · ${r.sl ? "SL " + fmt(r.sl, 1) + "%" : "không SL"} · giữ ${holdTxt(r.hold)}`);
const runQ = () => (RUN_ID ? `&run=${encodeURIComponent(RUN_ID)}` : "");

// ---------------------------------------------------------------- cấu hình
async function loadFiles(select) {
  const d = await api("/api/files");
  FILES = d.files;
  $("exchange").innerHTML = `<option value="">Tự nhận theo tên file</option>` + d.exchanges.map((e) => `<option>${e}</option>`).join("");
  $("file").innerHTML = FILES.map((f) => `<option value="${esc(f.name)}">${esc(f.name)}${f.n ? ` – ${f.n} lệnh` : ""}</option>`).join("");
  if (select) $("file").value = select;
  onFile();
}

function onFile() {
  const f = FILES.find((x) => x.name === $("file").value);
  if (!f) { $("fileInfo").innerHTML = "Chưa có file CSV trong thư mục Actual Performance"; return; }
  if (f.error) { $("fileInfo").innerHTML = `<span class="neg">${esc(f.error)}</span>`; return; }
  $("exchange").value = f.exchange || "";
  $("maker").value = f.fees[0]; $("taker").value = f.fees[1];
  const g = f.guess || {};
  $("aTp").value = g.tp ?? 1; $("aSl").value = g.sl ?? 10; $("aHold").value = g.hold ?? 120;
  if (g.notional) $("notional").value = g.notional;
  if (g.tp_order) $("tpOrder").value = g.tp_order;
  if (g.sizing) $("sizing").value = g.sizing;
  const r = f.reasons || {};
  $("fileInfo").innerHTML = `<b>${f.n}</b> lệnh · ${f.coins} coin · ${tsTxt(f.start_ms)} → ${tsTxt(f.end_ms)}<br>
    Chiến lược: ${esc(f.strategy)} · TP ${r.TP || 0} · SL ${r.SL || 0} · Hết giờ ${r.TIMEOUT || 0}${r.MANUAL ? ` · Đóng tay ${r.MANUAL}` : ""}
    · PnL thật <b>${sgn(f.pnl)}</b> USDT<br>
    Cấu hình thật ${g.reliable ? "đoán từ file" : "<span class='muted'>(không đoán được từ cột Entry – mặc định, hãy sửa nếu khác)</span>"}:
    TP ${g.tp}% · SL ${g.sl}% · giữ ${g.hold} phút${g.notional ? `<br>Khối lượng thật ~<b>${fmt(g.notional, 1)}</b> USDT/lệnh · phí 2 chiều lệnh TP ~${fmt(g.fee_pct, 3)}% → TP ${g.tp_order === "trigger" ? "phí taker" : "phí maker"} (đã tự điền ở mục 5)` : ""}`;
  $("coinList").innerHTML = (f.coins_list || []).map((c) => `<option value="${esc(c)}">`).join("");
  const day = (ms) => new Date(ms + 7 * 3600e3).toISOString().slice(0, 10);
  $("dateFrom").value = day(f.start_ms); $("dateTo").value = day(f.end_ms);
  countCombos();
}

function parseList(s) {
  let n = 0;
  for (const part of String(s).split(/[,;]/).map((x) => x.trim()).filter(Boolean)) {
    const m = part.match(/^([\d.]+)[mh]?\s*-\s*([\d.]+)[mh]?\s*:\s*([\d.]+)[mh]?$/);
    n += m ? Math.floor((+m[2] - +m[1]) / +m[3] + 1e-9) + 1 : 1;
  }
  return n;
}
function countCombos() {
  const modes = (+$("mSingle").checked) + (+$("mMulti").checked);
  const n = parseList($("gTp").value) * parseList($("gSl").value) * parseList($("gHold").value);
  $("comboCount").textContent = `${n.toLocaleString("vi-VN")} cấu hình × ${modes} chế độ`;
}
const PRESETS = {
  quick: ["0.5, 1, 1.5, 2, 3", "3, 5, 10, 0", "60, 120"],
  wide: ["0.3-5:0.1", "2-20:1, 0", "15, 30, 60, 120, 240, 0"],
  fine: () => {
    const tp = +$("aTp").value || 1, sl = +$("aSl").value || 10, h = +$("aHold").value || 120;
    const r = (x) => Math.round(x * 100) / 100;
    return [`${r(tp * 0.5)}-${r(tp * 2)}:${r(tp * 0.1)}`, `${r(sl * 0.5)}-${r(sl * 1.5)}:${r(Math.max(sl * 0.1, 0.5))}, 0`,
      `${Math.round(h / 2)}, ${h}, ${h * 2}`];
  },
};

function settings() {
  const modes = [];
  if ($("mSingle").checked) modes.push("single");
  if ($("mMulti").checked) modes.push("multi");
  return {
    file: $("file").value, exchange: $("exchange").value, date_from: $("dateFrom").value, date_to: $("dateTo").value,
    coins: $("coins").value, entry_mode: $("entryMode").value,
    actual: { tp: $("aTp").value, sl: $("aSl").value, hold: $("aHold").value },
    grid: { tp: $("gTp").value, sl: $("gSl").value, hold: $("gHold").value },
    settings: {
      modes, max_per_coin: +$("maxPerCoin").value || 0, max_total: +$("maxTotal").value || 0, cooldown_min: +$("cooldown").value || 0,
      notional: +$("notional").value, leverage: +$("leverage").value, sizing: $("sizing").value,
      maker_pct: +$("maker").value, taker_pct: +$("taker").value, tp_order: $("tpOrder").value, tp_fill: $("tpFill").value,
      sl_slip_pct: +$("slSlip").value || 0, exit_slip_pct: +$("exitSlip").value || 0,
      funding: $("funding").checked, liquidation: $("liq").checked, skip_1m: $("skip1m").checked,
      auto_slip: $("autoSlip").checked, direction: $("direction").value,
    },
  };
}

const FORM_IDS = ["entryMode", "gTp", "gSl", "gHold", "mSingle", "mMulti", "maxPerCoin", "maxTotal", "cooldown", "notional",
  "leverage", "sizing", "tpOrder", "tpFill", "slSlip", "exitSlip", "funding", "liq", "skip1m", "autoSlip", "direction"];
function syncSlip() { $("slSlip").disabled = $("exitSlip").disabled = $("autoSlip").checked; }
function saveForm() {
  const o = {};
  for (const id of FORM_IDS) o[id] = $(id).type === "checkbox" ? $(id).checked : $(id).value;
  try { localStorage.setItem("replayForm", JSON.stringify(o)); } catch (e) {}
}
function restoreForm() {
  let o = null;
  try { o = JSON.parse(localStorage.getItem("replayForm") || "null"); } catch (e) {}
  if (!o) return;
  for (const id of FORM_IDS) if (id in o) { if ($(id).type === "checkbox") $(id).checked = o[id]; else $(id).value = o[id]; }
}

// ---------------------------------------------------------------- chạy
let polling = null;
async function run() {
  let body;
  if (MODE === "bt") {
    body = btRequest();                       // config.js – cấu hình chiến lược đầy đủ
    if (!body) return;
  } else {
    body = { kind: "replay", ...settings() };
    if (!body.settings.modes.length) { alert("Chọn ít nhất 1 chế độ vị thế"); return; }
    saveForm();
  }
  let r;
  try {
    r = await api("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch (e) { alert(e.message); return; }
  $("prog").hidden = false; $("log").hidden = false;
  if (r.queued) toast("Đang có lượt chạy khác – lượt này vào hàng chờ");
  if (!polling) polling = setInterval(poll, 700);
  poll();
}
function toast(msg) {
  const el = $("toast"); if (!el) return;
  el.textContent = msg; el.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => (el.hidden = true), 4000);
}
async function poll() {
  let s;
  try { s = await api("/api/status"); } catch (e) { return; }
  const running = s.status === "running";
  const pct = s.total ? Math.round(((s.done || 0) / s.total) * 100) : 0;
  $("progBar").style.width = pct + "%";
  $("progBar").parentElement.classList.toggle("indet", !s.total);
  if (running) $("progText").textContent = `${s.exchange ? s.exchange.toUpperCase() + " · " : ""}${s.stage || "Chuẩn bị"}` +
    `${s.total ? ` ${fmt(s.done, 0)}/${fmt(s.total, 0)}` : ""}${s.elapsed ? ` · ${Math.round(s.elapsed)}s` : ""}`;
  $("runBtn").disabled = running;
  $("cancelBtn").hidden = !running;
  $("hdrStatus").textContent = running ? `Đang chạy: ${esc(s.title || "")}` : "";
  $("log").textContent = s.log.join("\n");
  $("log").scrollTop = 1e9;
  renderQueue(s.queue || []);
  if (!running) {
    clearInterval(polling); polling = null;
    if (s.status === "done" && s.run_id && s.run_id !== RUN_ID) { $("prog").hidden = true; await loadResult(s.run_id); }
    else if (s.status === "error") $("progText").innerHTML = `<span class="neg">Lỗi: ${esc(String(s.error || "").split("\n")[0])}</span>`;
    else if (s.status === "cancelled") $("progText").textContent = "Đã huỷ";
    loadHistory();
  }
}

// Hàng chờ: lượt mới bấm Chạy khi đang bận sẽ xếp hàng, đổi thứ tự / bỏ được (giữ nguyên sau khi khởi động lại máy chủ).
function renderQueue(q) {
  const el = $("queue"); if (!el) return;
  el.hidden = !q.length;
  if (!q.length) return;
  el.innerHTML = `<b>Hàng chờ (${q.length})</b>` + q.map((j, i) => `<div class="qrow"><span>${i + 1}. ${esc(j.title)}
      <span class="muted">${j.kind === "bt" ? "backtest" : "replay"}</span></span>
    <span class="nowrap"><button class="btn small ghost" data-q="-1" data-id="${j.id}" ${i ? "" : "disabled"}>↑</button>
      <button class="btn small ghost" data-q="1" data-id="${j.id}" ${i === q.length - 1 ? "disabled" : ""}>↓</button>
      <button class="btn small ghost danger" data-q="x" data-id="${j.id}" title="Bỏ khỏi hàng chờ">✕</button></span></div>`).join("");
}

// ---------------------------------------------------------------- kết quả
async function loadResult(id) {
  try { RES = await api("/api/result" + (id ? `?run=${encodeURIComponent(id)}` : "")); }
  catch (e) { alert(e.message); return; }
  RUN_ID = (RES.meta && RES.meta.id) || id || null;
  const S = RES.summary;
  RES.kind = RES.kind || S.kind || "replay";
  RES.modes = RES.modes || S.modes || [...new Set(RES.rows.map((r) => r.mode))];
  if (!RES.combos) { const c = []; RES.rows.forEach((r) => (c[r.ci] = [r.tp, r.sl, r.hold])); RES.combos = c; }
  // các tham số khác TP/SL/giữ lệnh cũng được quét (chỉ chế độ backtest) -> chọn một bộ để xem heatmap
  EXTRA_KEYS = (S.keys || []).filter((k) => !["exit.take_profit", "exit.stop_loss", "exit.max_hold"].includes(k));
  $("results").hidden = false;
  const m = RES.meta || {};
  const head = isBt()
    ? `${esc(S.title || S.name || "")} · ${(S.exchanges || RES.modes).map((x) => x.toUpperCase()).join(", ")}
       · ${esc(String(S.start || "").slice(0, 10))} → ${esc(String(S.end || "").slice(0, 10))}
       · ${fmt(S.n_combos, 0)} cấu hình${S.grid_mode === "smart" ? ` <span class="muted">(tìm kiếm thông minh trong ${fmt(S.grid_total, 0)})</span>` : ""}`
    : `${esc(S.file)} (${S.exchange}) · ${S.actual.n} lệnh · ${fmt(S.n_combos, 0)} cấu hình × ${RES.modes.length} chế độ` +
      (isRev() ? ` · <span class="revtag">⇄ NGƯỢC HƯỚNG bot thật</span>` : "");
  $("viewing").innerHTML = `Đang xem lượt chạy <b>${m.created ? tsTxt(m.created) : ""}</b> · ` + head;
  $("gridCsv").href = `/api/export/grid.csv?x=1${runQ()}`;
  renderHistory();
  if (hasActual()) {
    const ac = S.actual_cfg;
    RES.rows.forEach((r) => { r.isActual = r.mode === "single" && Math.abs(r.tp - ac.tp) < 1e-9 && Math.abs(r.sl - ac.sl) < 1e-9 && r.hold === ac.hold; });
  }
  COLS = BASE_COLS.concat(RES.rows.some((r) => r.oos_net != null) ? OOS_COLS : []);
  document.querySelectorAll(".replay-only").forEach((x) => (x.hidden = isBt()));
  document.querySelectorAll(".bt-only").forEach((x) => (x.hidden = !isBt()));
  resetDates();
  const uniq = (f) => [...new Set(RES.rows.map(f))].filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
  const holds = uniq((r) => r.hold), tps = uniq((r) => r.tp), sls = uniq((r) => r.sl);
  const opt = (arr, f = (x) => x) => arr.map((x) => `<option value="${x}">${f(x)}</option>`).join("");
  $("fHold").innerHTML = `<option value="">Tất cả</option>` + opt(holds, holdTxt);
  $("hHold").innerHTML = opt(holds, holdTxt); $("dHold").innerHTML = opt(holds, holdTxt);
  $("hMode").innerHTML = opt(RES.modes, modeTxt); $("dMode").innerHTML = opt(RES.modes, modeTxt);
  $("fMode").innerHTML = `<option value="">Tất cả</option>` + opt(RES.modes, modeTxt);
  $("dTp").innerHTML = opt(tps); $("dSl").innerHTML = opt(sls, (x) => (x ? x : "không SL"));
  const extras = [...new Set(RES.rows.map(extraKey))];
  $("hExtraBox").hidden = extras.length < 2;
  $("hExtra").innerHTML = extras.map((k) => `<option value="${esc(k)}">${esc(extraLabel(k))}</option>`).join("");
  $("hMetric").querySelector('[value=diff]').disabled = RES.modes.length < 2 || isBt();
  if (hasActual() && holds.includes(RES.summary.actual_cfg.hold)) $("hHold").value = RES.summary.actual_cfg.hold;
  renderOverview(); renderTable(); renderHeat();
  const best = bestRow();
  if (best) openDetail(best.mode, best.ci, false);
  showTab("overview");
  $("results").scrollIntoView({ behavior: "smooth" });
}
// bộ tham số "ngoài lưới TP/SL/giữ" của một dòng (chỉ chế độ backtest có)
let EXTRA_KEYS = [];
const extraKey = (r) => (EXTRA_KEYS.length ? JSON.stringify(EXTRA_KEYS.map((k) => (r.values || {})[k])) : "");
const extraLabel = (key) => {
  if (!key) return "";
  const vals = JSON.parse(key), lab = RES.summary.labels || [];
  return EXTRA_KEYS.map((k, i) => `${lab[(RES.summary.keys || []).indexOf(k)] || k} ${vals[i]}`).join(" · ");
};
const bestRow = (mode) => RES.rows.filter((r) => r.n > 0 && (!mode || r.mode === mode)).sort((a, b) => b.net - a.net)[0];

function kpi(k, v, s = "") { return `<div class="kpi"><div class="k">${k}</div><div class="v">${v}</div>${s ? `<div class="s">${s}</div>` : ""}</div>`; }

const isRev = () => !!(RES && RES.summary && RES.summary.direction === "reverse");
function renderOverview() {
  const h = document.querySelector("#tab-overview .card h3");
  if (h) h.textContent = isBt() ? "Cấu hình tốt nhất mỗi sàn" : "Thực tế vs cấu hình tốt nhất";
  if (isBt()) return renderOverviewBt();
  const a = RES.summary.actual_stats, S = RES.summary, v = RES.validation;
  const cols = [["Thực tế (bot thật)", a, null]];
  const rv = isRev() ? " ⇄ ngược hướng" : "";
  for (const m of RES.modes) { const b = bestRow(m); if (b) cols.push([`Tốt nhất – ${modeTxt(m)}${rv}`, b, b]); }
  const act = RES.rows.find((r) => r.isActual);
  if (act) cols.splice(1, 0, [`Cấu hình thật – mô phỏng${rv}`, act, act]);
  $("kpis").innerHTML = `<div class="tablewrap"><table class="data"><thead><tr><th class="l"></th>${cols.map((c) => `<th>${c[0]}${c[2] ? `<br><span class="muted">TP ${c[2].tp}% · ${c[2].sl ? "SL " + c[2].sl + "%" : "không SL"} · ${holdTxt(c[2].hold)}</span>` : ""}</th>`).join("")}</tr></thead><tbody>
    ${[["Số lệnh", (x) => fmt(x.n, 0)], ["Lãi/lỗ ròng (USDT)", (x) => sgn(x.net)], ["TB / lệnh", (x) => sgn(x.avg, 3)],
       ["Win rate", (x) => fmt(x.win_rate, 1) + "%"], ["TP / SL / hết giờ", (x) => `${x.n_tp} / ${x.n_sl + (x.n_liq || 0)} / ${x.n_timeout}`],
       ["Max drawdown", (x) => fmt(x.max_dd)], ["Lãi / Max DD", (x) => fmt(x.ret_dd)], ["Profit factor", (x) => fmt(x.pf)],
       ["Giữ lệnh TB (phút)", (x) => fmt(x.avg_hold, 1)], ["Lệnh tệ nhất", (x) => sgn(x.worst)],
       ["Bị bỏ (đang có lệnh mở)", (x) => (x.skipped === undefined ? "–" : fmt(x.skipped, 0))]]
      .map(([k, f]) => `<tr><td class="l">${k}</td>${cols.map((c) => `<td>${f(c[1])}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>
    <p class="hint">Bấm "Bảng xếp hạng" để xem toàn bộ ${S.n_combos.toLocaleString("vi-VN")} cấu hình × ${RES.modes.length} chế độ.</p>`;

  if (!v || !v.n) { $("valid").innerHTML = "<p class='muted'>Không đủ lệnh để kiểm định.</p>"; }
  else {
    const reasons = ["TP", "SL", "TIMEOUT"];
    const conf = `<table class="conf"><tr><th>thật \\ mô phỏng</th>${reasons.map((r) => `<th>${r}</th>`).join("")}</tr>
      ${reasons.map((a) => `<tr><th>${a}</th>${reasons.map((s) => `<td>${(v.confusion[a] || {})[s] || 0}</td>`).join("")}</tr>`).join("")}</table>`;
    const pr = v.pnl_by_reason || {};
    $("valid").innerHTML = `<p>Chạy lại <b>${v.n}</b> lệnh thật với đúng cấu hình bot (TP ${v.tp}% · SL ${v.sl}% · giữ ${v.hold} phút), mỗi lệnh độc lập:</p>
      <div class="vgrid">
        ${kpi("Lý do thoát khớp", fmt(v.reason_match, 1) + "%", "TP/SL/hết giờ giống thật")}
        ${kpi("Thời điểm thoát ≤ 10s", fmt(v.exit_within_10s, 1) + "%", `≤ 60s: ${fmt(v.exit_within_60s, 1)}%`)}
        ${kpi("Bot ghi nhận trễ", fmt(v.delay_median, 1) + "s", "trung vị, so với tick chạm mức")}
        ${kpi("Giá thoát lệch", fmt(v.exit_px_diff_median, 3) + "%", `≤ 0.1%: ${fmt(v.exit_px_within_01, 1)}% lệnh`)}
        ${kpi("PnL mô phỏng / thật", `${sgn(v.pnl_sim)} / ${sgn(v.pnl_actual)}`, v.pnl_sim_raw != null ? `chưa hiệu chỉnh trượt giá: ${fmt(v.pnl_sim_raw)}` : "USDT")}
        ${kpi("Có tick", fmt(v.tick_pct, 1) + "%", `giá Entry file: ${fmt(v.entry_csv_pct, 0)}%`)}
      </div>
      <div class="grid2"><div>${conf}</div><div><table class="conf"><tr><th>PnL theo lý do thật</th><th>Lệnh</th><th>Mô phỏng</th><th>Thật</th></tr>
        ${Object.entries(pr).map(([k, x]) => `<tr><th>${k}</th><td>${x.n}</td><td>${sgn(x.sim)}</td><td>${sgn(x.actual)}</td></tr>`).join("")}</table></div></div>
      ${calibHtml(v)}
      ${bridgeHtml(v)}
      <details><summary>${v.mismatches.length} lệnh có lý do thoát khác thực tế</summary>
        <p class="hint">Thường là lúc giá đã chạm TP nhưng lệnh TP của bot thật không khớp (đặt lệnh trễ / lỗi), hoặc OKX không biết chính xác giá khớp của lệnh SL/hết giờ.</p>
        <div class="tablewrap"><table class="data"><thead><tr><th>#</th><th class="l">Coin</th><th class="l">Vào lệnh</th><th>Thật</th><th>Mô phỏng</th><th class="l">Thoát thật</th><th class="l">Thoát mô phỏng</th><th>Giá thật</th><th>Giá MP</th><th>PnL thật</th><th>PnL MP</th></tr></thead>
        <tbody>${v.mismatches.map((m) => `<tr><td>${m.no}</td><td class="l">${esc(m.coin)}</td><td class="l">${m.entry_t}</td><td><span class="badge ${m.a}">${m.a}</span></td><td><span class="badge ${m.s}">${m.s}</span></td>
          <td class="l">${m.a_exit_t}</td><td class="l">${m.s_exit_t}</td><td>${fmt(m.ax, 6)}</td><td>${fmt(m.sx, 6)}</td><td>${sgn(m.ap, 3)}</td><td>${sgn(m.sp, 3)}</td></tr>`).join("")}</tbody></table></div>
        <a class="btn small ghost" href="/api/export/validation.csv?x=1${runQ()}">Tải CSV</a>
      </details>`;
  }
  const res = Object.entries(S.resolution).map(([k, n]) => `${k}: ${n}`).join(" · ");
  const SRC = { csv: "cột Entry", tp: "suy từ giá TP", sl: "suy từ giá SL", tick: "tick giữa giây", "1m": "nến 1m" };
  const src = Object.entries(S.entry_src).map(([k, n]) => `${(SRC[k.replace("*", "")] || k) + (k.endsWith("*") ? " (đã chỉnh cho khớp KQ thật)" : "")}: ${n}`).join(" · ");
  $("dataInfo").innerHTML = `<p>${S.file} · sàn <b>${S.exchange}</b> · ${S.actual.n} lệnh thật · cửa sổ giá ${S.window_min} phút sau khi vào · ${S.elapsed_s}s</p>
    <p>Đường giá: ${res}<br>Giá vào lệnh: ${src}${S.csv_trusted ? "" : " <span class='muted'>(cột Entry của file không phải giá khớp)</span>"}</p>
    ${S.missing.length ? `<details><summary>${S.missing.length} lệnh không có dữ liệu giá</summary><ul>${S.missing.map((m) => `<li>${esc(m.coin)} ${tsTxt(m.t)} – ${esc(m.note)}</li>`).join("")}</ul></details>` : ""}`;
}

// Tổng quan của chế độ backtest: so các sàn / cấu hình tốt nhất, và vì sao tín hiệu bị bỏ.
function renderOverviewBt() {
  const S = RES.summary;
  const cols = RES.modes.map((m) => [m, bestRow(m)]).filter((c) => c[1]);
  const rows = [["Số lệnh", (x) => fmt(x.n, 0)], ["Lãi/lỗ ròng (USDT)", (x) => sgn(x.net)],
    ["Lãi trên vốn", (x) => fmt(x.ret_pct, 1) + "%"], ["TB / lệnh", (x) => sgn(x.avg, 3)],
    ["Win rate", (x) => fmt(x.win_rate, 1) + "%"], ["TP / SL / hết giờ", (x) => `${x.n_tp} / ${x.n_sl + (x.n_liq || 0)} / ${x.n_timeout}`],
    ["Max drawdown", (x) => `${fmt(x.max_dd)} <span class="muted">(${fmt(x.max_dd_pct, 1)}%)</span>`],
    ["Lãi / Max DD", (x) => fmt(x.ret_dd)], ["Profit factor", (x) => fmt(x.pf)],
    ["Giữ lệnh TB (phút)", (x) => fmt(x.avg_hold, 1)], ["Lệnh tệ nhất", (x) => sgn(x.worst)],
    ["Tín hiệu bị bỏ (giới hạn)", (x) => fmt(x.skipped, 0)], ["Số giờ có lãi", (x) => fmt(x.hours_pos, 0) + "/24"]];
  $("kpis").innerHTML = cols.length ? `<div class="tablewrap"><table class="data"><thead><tr><th class="l"></th>
      ${cols.map(([m, b]) => `<th>${m.toUpperCase()} – tốt nhất<br><span class="muted">${esc(b.params || "")}</span></th>`).join("")}</tr></thead>
      <tbody>${rows.map(([k, f]) => `<tr><td class="l">${k}</td>${cols.map((c) => `<td>${f(c[1])}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    <p class="hint">Xếp hạng theo <b>${esc(S.objective_label || "lãi ròng")}</b>. Bấm "Bảng xếp hạng" để xem toàn bộ
      ${fmt(S.n_combos, 0)} cấu hình, hoặc "Chi tiết cấu hình" để xem từng lệnh của một cấu hình bất kỳ.</p>`
    : `<p class="muted">Không có cấu hình nào vào được lệnh – xem phần "Dữ liệu" bên dưới để biết tín hiệu bị bỏ vì sao.</p>`;
  const per = S.per_exchange || {};
  $("dataInfo").innerHTML = `<p>${esc(S.title || "")} · ${fmt(S.n_combos, 0)} cấu hình · vốn ban đầu ${fmt(S.initial_balance, 0)} USDT · ${S.elapsed_s}s</p>` +
    Object.entries(per).map(([ex, d]) => `<details open><summary><b>${ex.toUpperCase()}</b> – ${fmt(d.n_signals, 0)} tín hiệu vào được lệnh
        · ${fmt(d.n_symbols, 0)} coin · ${d.elapsed_s}s</summary>
      ${(d.rejects || []).length ? `<table class="conf"><tr><th class="l">Tín hiệu bị bỏ</th><th>Số lần</th></tr>
        ${d.rejects.map((r) => `<tr><td class="l">${esc(r.label)}</td><td>${fmt(r.n, 0)}</td></tr>`).join("")}</table>
        <p class="hint">Cộng dồn trên mọi cấu hình trong lưới (một tín hiệu bị bỏ ở nhiều cấu hình sẽ đếm nhiều lần).</p>` : ""}
      </details>`).join("");
}

function calibHtml(v) {
  const c = v.calibration, s = v.slip || {};
  if (!c) return `<p class="hint">Trượt giá đang dùng (nhập tay): SL ${fmt(s.sl, 2)}% · đóng hết giờ ${fmt(s.exit, 2)}%.
    Bật "Tự hiệu chỉnh trượt giá" ở mục 5 để tool tự ước lượng từ lệnh thật.</p>`;
  const one = (x, n) => (n >= c.min_n ? `<b>${x > 0 ? "+" : ""}${fmt(x, 2)}%</b> <span class="muted">(trung vị ${n} lệnh)</span>` : `<span class="muted">0 (chỉ ${n} lệnh, cần ≥ ${c.min_n})</span>`);
  return `<div class="calib"><b>Hiệu chỉnh trượt giá từ lệnh thật</b> (đã áp dụng cho mọi cấu hình trong lưới):
    SL ${one(c.sl_slip, c.n_sl)} · đóng hết giờ ${one(c.exit_slip, c.n_timeout)}
    <div class="hint">&gt; 0: thực tế khớp tệ hơn giá tick kích hoạt (lệnh market trong lúc giá chạy mạnh); &lt; 0: tốt hơn (vd sàn cắt lỗ theo mark price, sớm hơn).
    Tính trên các lệnh mô phỏng ra cùng lý do thoát, so lãi/lỗ gộp (trước phí).</div></div>`;
}
function bridgeHtml(v) {
  if (!v.bridge || !v.bridge.length) return "";
  const tot = v.bridge.reduce((a, b) => ({ n: a.n + b.n, sim: a.sim + b.sim, actual: a.actual + b.actual, gap: a.gap + b.gap }), { n: 0, sim: 0, actual: 0, gap: 0 });
  const share = v.gross_tp_actual ? Math.abs(tot.gap) / v.gross_tp_actual * 100 : null;
  return `<h3 style="margin-top:14px">Vì sao tổng PnL mô phỏng khác thật?</h3>
    <p class="hint">Lãi ròng là hiệu của 2 số lớn: lãi các lệnh TP thật <b>${sgn(v.gross_tp_actual)}</b> và lỗ SL + hết giờ <b>${sgn(v.loss_actual)}</b>.
      Tổng chênh ${sgn(tot.gap)} USDT chỉ bằng ${share == null ? "–" : fmt(share, 1) + "%"} lãi TP (${fmt(tot.gap / Math.max(tot.n, 1), 4)} USDT/lệnh), nhưng đủ để đổi dấu kết quả khi lãi ròng gần 0.</p>
    <div class="tablewrap"><table class="data bridge"><thead><tr><th class="l">Nhóm lệnh</th><th>Lệnh</th><th>Mô phỏng</th><th>Thật</th><th>Chênh</th><th>Chênh/lệnh</th><th class="l">Giải thích</th></tr></thead><tbody>
    ${v.bridge.map((b) => `<tr><td class="l">${esc(b.label)}</td><td>${fmt(b.n, 0)}</td><td>${sgn(b.sim)}</td><td>${sgn(b.actual)}</td><td><b>${sgn(b.gap)}</b></td><td>${sgn(b.per_trade, 4)}</td><td class="l wrap">${esc(b.note)}</td></tr>`).join("")}
    <tr class="tot"><td class="l"><b>Tổng</b></td><td>${fmt(tot.n, 0)}</td><td>${sgn(tot.sim)}</td><td>${sgn(tot.actual)}</td><td><b>${sgn(tot.gap)}</b></td><td>${sgn(tot.gap / Math.max(tot.n, 1), 4)}</td><td></td></tr>
    </tbody></table></div>
    <p class="hint">3 dòng đầu đo sai số của mô phỏng (sau hiệu chỉnh); 2 dòng cuối là lệnh mà kết quả thật do bot ghi sai / khớp lỗi.
      Khi so sánh cấu hình, chênh lệch nhỏ hơn mức "Chênh/lệnh" của 3 dòng đầu × số lệnh thì chưa đủ tin cậy.</p>`;
}

// ---- độ ổn định (pre = "" mọi giờ, "h8_" chỉ 8 giờ tốt nhất). Phần tử thứ 5 = tooltip.
const r2Txt = (v) => (v == null ? "–" : `<span class="${v >= 0.8 ? "pos" : v < 0.3 ? "neg" : ""}">${fmt(v, 2)}</span>`);
function stabCols(pre, cls) {
  const sfx = pre ? " 8h" : "";
  return [
    [pre + "r2", "R² vốn" + sfx, cls, (r) => r2Txt(r[pre + "r2"]),
      () => "R² đường vốn (lãi cộng dồn theo thời gian) so với đường thẳng: 1 = lãi tăng đều, gần 0 = giật cục / đi ngang, âm = đi xuống"],
    [pre + "day_pos", "% ngày lãi" + sfx, pre ? "h8" : "", (r) => (r[pre + "day_pos"] == null ? "–" : fmt(r[pre + "day_pos"], 0) + "%"),
      (r) => r[pre + "day_pos"] == null ? "" : `${Math.round(r[pre + "day_pos"] * r[pre + "n_days"] / 100)}/${r[pre + "n_days"]} ngày có lãi` +
        (r[pre + "top3_day"] == null ? "" : ` · 3 ngày tốt nhất = ${fmt(r[pre + "top3_day"], 0)}% lãi ròng`)],
    [pre + "net_xcoin", "Net bỏ coin top" + sfx, pre ? "h8" : "", (r) => (r[pre + "net_xcoin"] == null ? "–" : sgn(r[pre + "net_xcoin"])),
      (r) => (r[pre + "best_coin"] ? `Bỏ ${r[pre + "best_coin"]} (coin lãi nhiều nhất: ${fmt(r[pre + "best_coin_pnl"])} USDT)` : "")],
    [pre + "nb_net", "Net TB lân cận" + sfx, pre ? "h8" : "", (r) => (r[pre + "nb_net"] == null ? "–" : sgn(r[pre + "nb_net"])),
      (r) => (r[pre + "nb_net"] == null ? "" : `Net trung bình ${r.nb_cnt} ô kề trên heatmap (TP ±1 nấc, SL ±1 nấc, cùng giữ lệnh) · thấp nhất ${fmt(r[pre + "nb_min"])}`)],
  ];
}
const BASE_COLS = [
  ["mode", "Chế độ", "l", (r) => modeTxt(r.mode) + (r.isActual ? " ★" : "")], ["tp", "TP %", "", (r) => fmt(r.tp)],
  ["sl", "SL %", "", (r) => (r.sl ? fmt(r.sl, 1) : "–")], ["hold", "Giữ", "", (r) => holdTxt(r.hold)],
  ["n", "Lệnh", "", (r) => fmt(r.n, 0)], ["skipped", "Bỏ", "", (r) => fmt(r.skipped, 0)],
  ["n_tp", "TP", "", (r) => r.n_tp], ["n_sl", "SL", "", (r) => r.n_sl + (r.n_liq || 0)], ["n_timeout", "Hết giờ", "", (r) => r.n_timeout],
  ["win_rate", "Win %", "", (r) => fmt(r.win_rate, 1)], ["net", "Net USDT", "", (r) => sgn(r.net)], ["avg", "TB/lệnh", "", (r) => sgn(r.avg, 3)],
  ["pf", "PF", "", (r) => fmt(r.pf)], ["max_dd", "Max DD", "", (r) => fmt(r.max_dd)], ["ret_dd", "Net/DD", "", (r) => fmt(r.ret_dd)],
  ["avg_hold", "Giữ TB (ph)", "", (r) => fmt(r.avg_hold, 1)], ["worst", "Tệ nhất", "", (r) => sgn(r.worst)],
  ...stabCols("", "sep"),
  // chỉ vào lệnh trong 8 giờ (giờ vào, VN) có lãi/lỗ tốt nhất của chính cấu hình đó – bấm để mở chi tiết với 8 giờ này
  ["h8_net", "Net 8h tốt nhất", "h8 sep", (r) => (r.h8_net == null ? "–" : sgn(r.h8_net))],
  ["h8_dd", "Max DD 8h", "h8", (r) => fmt(r.h8_dd)], ["h8_ret", "Net/DD 8h", "h8", (r) => fmt(r.h8_ret)],
  ["h8_pf", "PF 8h", "h8", (r) => fmt(r.h8_pf), () => "Profit factor = tổng lãi các lệnh thắng / tổng lỗ các lệnh thua (chỉ 8 giờ tốt nhất)"],
  ...stabCols("h8_", "h8"),
  ["h8_hours", "8 giờ", "h8 l muted", (r) => (r.h8_hours ? r.h8_hours.map(hh).join(" ") : "–")],
];
// Kiểm tra ngoài mẫu (chỉ chế độ backtest, khi đặt % cuối giai đoạn): xếp hạng tính trên phần ĐẦU,
// các cột này cho biết cấu hình đó làm ăn ra sao ở phần CUỐI chưa dùng để chọn.
const OOS_COLS = [
  ["is_net", "Net trong mẫu", "oos sep", (r) => (r.is_net == null ? "–" : sgn(r.is_net)),
    (r) => `Phần đầu giai đoạn (dùng để xếp hạng) · ${fmt(r.is_n, 0)} lệnh`],
  ["oos_net", "Net ngoài mẫu", "oos", (r) => (r.oos_net == null ? "–" : sgn(r.oos_net)),
    (r) => `Phần cuối giai đoạn, không dùng để chọn cấu hình · ${fmt(r.oos_n, 0)} lệnh`],
  ["oos_n", "Lệnh ngoài mẫu", "oos", (r) => fmt(r.oos_n, 0)],
  ["oos_win_rate", "Win % ngoài mẫu", "oos", (r) => (r.oos_win_rate == null ? "–" : fmt(r.oos_win_rate, 1))],
];
let COLS = BASE_COLS;
// ---- lọc theo cột kiểu Excel: nhiều điều kiện, tất cả phải thoả (AND)
const FOPS = [[">", ">"], [">=", "≥"], ["<", "<"], ["<=", "≤"], ["=", "="], ["!=", "≠"], ["between", "trong khoảng"], ["outside", "ngoài khoảng"]];
let FCONDS = [];
try { FCONDS = JSON.parse(localStorage.getItem("replayGridFilters") || "[]") || []; } catch (e) { FCONDS = []; }
const fCols = () => COLS.filter(([k]) => !["mode", "h8_hours"].includes(k))
  .map(([k, t]) => [k, k === "hold" ? "Giữ (phút)" : t]);
const fVal = (r, k) => (k === "n_sl" ? r.n_sl + (r.n_liq || 0) : r[k]);
const fNum = (s) => { const t = String(s ?? "").trim().replace(/\s/g, "").replace(",", "."); return t === "" ? null : Number(t); };
function fTest(c, r) {
  const a = fNum(c.v), b = fNum(c.v2);
  if (a === null || Number.isNaN(a)) return true;                 // chưa nhập / nhập sai -> bỏ qua điều kiện
  const x = fVal(r, c.k);
  if (x === null || x === undefined || Number.isNaN(x)) return false;   // ô trống (lượt chạy cũ) không khớp
  const eq = Math.abs(x - a) <= 1e-9 * Math.max(1, Math.abs(a));
  switch (c.op) {
    case ">": return x > a;
    case ">=": return x >= a || eq;
    case "<": return x < a;
    case "<=": return x <= a || eq;
    case "=": return eq;
    case "!=": return !eq;
    case "between": case "outside": {
      if (b === null || Number.isNaN(b)) return true;
      const inside = x >= Math.min(a, b) && x <= Math.max(a, b);
      return c.op === "between" ? inside : !inside;
    }
  }
  return true;
}
function saveConds() { try { localStorage.setItem("replayGridFilters", JSON.stringify(FCONDS)); } catch (e) {} }
function renderConds() {
  const cols = fCols();
  $("fConds").innerHTML = FCONDS.map((c, i) => {
    const bad = (s) => { const n = fNum(s); return n !== null && Number.isNaN(n); };
    const two = c.op === "between" || c.op === "outside";
    return `<div class="fcond" data-i="${i}">
      <select data-f="k">${cols.map(([k, t]) => `<option value="${k}"${k === c.k ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>
      <select data-f="op">${FOPS.map(([o, t]) => `<option value="${o}"${o === c.op ? " selected" : ""}>${t}</option>`).join("")}</select>
      <input data-f="v" value="${esc(c.v)}" placeholder="giá trị" class="${bad(c.v) ? "bad" : ""}">
      ${two ? `<span class="muted">và</span><input data-f="v2" value="${esc(c.v2)}" placeholder="giá trị" class="${bad(c.v2) ? "bad" : ""}">` : ""}
      <button class="btn small ghost" data-f="del" title="Bỏ điều kiện này">✕</button></div>`;
  }).join("");
  $("fClear").hidden = !FCONDS.length;
}
function onCondInput(e) {
  const el = e.target.closest("[data-f]"), box = e.target.closest(".fcond");
  if (!el || !box) return;
  const i = +box.dataset.i, f = el.dataset.f;
  if (f === "del") { if (e.type !== "click") return; FCONDS.splice(i, 1); renderConds(); }
  else if (e.type === "click") return;
  else {
    FCONDS[i][f] = el.value;
    if (f === "op" || f === "k") renderConds();                   // đổi số ô nhập (khoảng) / làm mới
    else { const n = fNum(el.value); el.classList.toggle("bad", n !== null && Number.isNaN(n)); }
  }
  saveConds();
  if (RES) renderTable();
}

// ---- bộ lọc đã lưu (trên máy chủ): điều kiện lọc cột + thanh công cụ + sắp xếp
let SAVED = [];
function filterState() {
  return { conds: FCONDS.map((c) => ({ ...c })), mode: $("fMode").value, hold: $("fHold").value,
    min: $("fMin").value, top: $("fTop").value, favGroups: [...FAVSEL], dfrom: $("fFrom").value, dto: $("fTo").value, sort: { ...SORT } };
}
function filterSummary(s) {
  const lbl = Object.fromEntries(fCols()), op = Object.fromEntries(FOPS);
  const parts = (s.conds || []).filter((c) => String(c.v ?? "").trim() !== "").map((c) =>
    `${lbl[c.k] || c.k} ${c.op === "between" || c.op === "outside" ? `${op[c.op]} ${c.v} – ${c.v2}` : `${op[c.op] || c.op} ${c.v}`}`);
  if (s.mode) parts.push(modeTxt(s.mode));
  if (s.hold) parts.push("giữ " + holdTxt(+s.hold));
  if (+s.min) parts.push(`số lệnh ≥ ${s.min}`);
  if (s.dfrom || s.dto) parts.push(`ngày ${dmy(s.dfrom) || "đầu"} → ${dmy(s.dto) || "cuối"}`);
  if (s.favGroups && s.favGroups.length) parts.push(`nhóm yêu thích: ${s.favGroups.join(", ")}`);
  else if (s.fav) parts.push("chỉ yêu thích");
  const sc = s.sort && (COLS.find(([k]) => k === s.sort.k) || [])[1];
  if (sc) parts.push(`sắp xếp: ${sc} ${s.sort.d < 0 ? "▼" : "▲"}`);
  return parts.join(" · ") || "không có điều kiện";
}
function renderSaved(sel) {
  const cur = sel ?? $("fSaved").value;
  $("fSaved").innerHTML = `<option value="">${SAVED.length ? `Bộ lọc đã lưu (${SAVED.length})…` : "Chưa có bộ lọc đã lưu"}</option>` +
    SAVED.map((f) => `<option value="${esc(f.name)}" title="${esc(filterSummary(f.state))}">${esc(f.name)}</option>`).join("");
  $("fSaved").value = SAVED.some((f) => f.name === cur) ? cur : "";
  $("fDel").hidden = !$("fSaved").value;
  $("fSaved").title = $("fSaved").value ? filterSummary(SAVED.find((f) => f.name === $("fSaved").value).state) : "Bộ lọc đã lưu – chọn để áp dụng lại";
}
async function loadSaved() {
  try { SAVED = (await api("/api/filters")).filters; } catch (e) { SAVED = []; }
  renderSaved();
}
function applySaved(name) {
  const f = SAVED.find((x) => x.name === name);
  renderSaved(name);
  if (!f) return;
  const s = f.state || {};
  FCONDS = (s.conds || []).map((c) => ({ k: c.k, op: c.op, v: c.v ?? "", v2: c.v2 ?? "" }));
  saveConds(); renderConds();
  const setSel = (id, v) => { $(id).value = v ?? ""; if ($(id).value !== String(v ?? "")) $(id).value = ""; };   // giá trị không có ở lượt này -> Tất cả
  setSel("fMode", s.mode); setSel("fHold", s.hold);
  $("fMin").value = s.min ?? 0;
  const names = FGROUPS.map((g) => g.name);
  FAVSEL = new Set(s.favGroups ? s.favGroups.filter((n) => names.includes(n)) : s.fav ? names : []);   // bản cũ: fav=true -> mọi nhóm
  favBtnTxt();
  if (s.top) setSel("fTop", s.top);
  if (s.sort && s.sort.k) SORT = { k: s.sort.k, d: s.sort.d < 0 ? -1 : 1 };
  const df = s.dfrom || "", dt = s.dto || "";
  if (df !== $("fFrom").value || dt !== $("fTo").value) { $("fFrom").value = df; $("fTo").value = dt; applyDates(); return; }
  if (RES) renderTable();
}
async function saveCurrent() {
  const cur = $("fSaved").value;
  const name = (prompt("Tên bộ lọc (trùng tên sẽ ghi đè):", cur || "") || "").trim();
  if (!name) return;
  if (name !== cur && SAVED.some((f) => f.name === name) && !confirm(`Đã có bộ lọc "${name}". Ghi đè?`)) return;
  try {
    SAVED = (await api("/api/filters", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, state: filterState() }) })).filters;
  } catch (e) { alert(e.message); return; }
  renderSaved(name);
}
async function deleteSaved() {
  const name = $("fSaved").value;
  if (!name || !confirm(`Xoá bộ lọc đã lưu "${name}"? Không khôi phục được.`)) return;
  try {
    SAVED = (await api("/api/filters/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }) })).filters;
  } catch (e) { alert(e.message); return; }
  renderSaved("");
}

// ---- cấu hình yêu thích theo NHÓM (lưu trên máy chủ; cấu hình = chế độ + TP + SL + giữ lệnh, dùng chung mọi lượt chạy)
let FGROUPS = [], FAVMAP = new Map(), FAVSEL = new Set(), FAVPOP = null;
const favKey = (r) => `${r.mode}|${+r.tp}|${+r.sl}|${+r.hold}|${r.params || ""}`;
const favGroupsOf = (r) => FAVMAP.get(favKey(r)) || [];
const favId = (r) => ({ mode: r.mode, tp: r.tp, sl: r.sl, hold: r.hold, params: r.params || "", kind: RES && RES.kind });
const isFav = (r) => favGroupsOf(r).length > 0;
const gColor = (name) => (FGROUPS.find((g) => g.name === name) || {}).color || "var(--neg)";
const postJ = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const cfgTxt = (r) => rowCfgTxt(r);

function setFavs(d) {
  FGROUPS = d.groups || [];
  FAVMAP = new Map((d.items || []).map((x) => [favKey(x), x.groups || []]));
  const names = new Set(FGROUPS.map((g) => g.name));
  FAVSEL = new Set([...FAVSEL].filter((n) => names.has(n)));      // nhóm đã xoá / đổi tên -> bỏ khỏi lựa chọn
  favBtnTxt();
}
function favCounts(name) {
  const total = [...FAVMAP.values()].filter((gs) => gs.includes(name)).length;
  const here = RES ? gridRows().filter((r) => favGroupsOf(r).includes(name)).length : 0;
  return { total, here };
}
function favBtnTxt() {
  const b = $("favFilterBtn");
  const sel = FGROUPS.filter((g) => FAVSEL.has(g.name));
  b.classList.toggle("on", sel.length > 0);
  b.innerHTML = sel.length
    ? `♥ ${sel.length <= 2 ? sel.map((g) => `<span class="dot" style="background:${g.color}"></span>${esc(g.name)}`).join(", ") : `${sel.length} nhóm`} ▾`
    : `♥ Nhóm yêu thích${FGROUPS.length ? ` <span class="muted">(${FGROUPS.length})</span>` : ""} ▾`;
}
async function loadFavs() {
  try { setFavs(await api("/api/favorites")); } catch (e) { setFavs({}); }
  if (RES) renderTable();
}
async function favCall(url, body) {
  try { setFavs(await postJ(url, body)); } catch (e) { alert(e.message); await loadFavs(); return false; }
  if (RES) renderTable();
  if (FAVPOP) FAVPOP.render();
  return true;
}

// ---- popup dùng chung (menu nhóm của 1 dòng, bảng chọn nhóm để lọc)
function openPop(anchor, render) {
  closePop();
  const el = document.createElement("div");
  el.className = "favpop";
  document.body.appendChild(el);
  FAVPOP = { el, render: () => { el.innerHTML = render(); } };   // vị trí chỉ đặt lúc mở (dòng của bảng có thể đã vẽ lại)
  const place = () => {
    const a = anchor.getBoundingClientRect(), w = el.offsetWidth, h = el.offsetHeight;
    let x = a.left + window.scrollX, y = a.bottom + window.scrollY + 4;
    x = Math.max(8, Math.min(x, window.scrollX + document.documentElement.clientWidth - w - 8));
    if (a.bottom + h + 8 > window.innerHeight && a.top - h - 4 > 0) y = a.top + window.scrollY - h - 4;
    el.style.left = x + "px"; el.style.top = y + "px";
  };
  FAVPOP.render();
  place();
  return el;
}
function closePop() { if (FAVPOP) { FAVPOP.el.remove(); FAVPOP = null; } }
document.addEventListener("mousedown", (e) => {
  if (FAVPOP && !FAVPOP.el.contains(e.target) && !e.target.closest(".favbtn, #favFilterBtn")) closePop();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePop(); });

// menu ♡ của 1 dòng: tick nhóm để thêm / bỏ, tạo nhóm mới
function openFavMenu(r, anchor) {
  const el = openPop(anchor, () => {
    const gs = favGroupsOf(r);
    return `<div class="pophead">Lưu vào nhóm yêu thích<div class="muted">${esc(cfgTxt(r))}</div></div>
      ${FGROUPS.map((g) => `<label class="popitem"><input type="checkbox" data-g="${esc(g.name)}"${gs.includes(g.name) ? " checked" : ""}>
        <span class="dot" style="background:${g.color}"></span><span class="gname">${esc(g.name)}</span><span class="muted">${favCounts(g.name).total}</span></label>`).join("")}
      <form class="popnew"><input name="g" placeholder="${FGROUPS.length ? "Nhóm mới…" : "Tên nhóm, vd Yêu thích"}" maxlength="40"><button class="btn small">+ Thêm</button></form>`;
  });
  el.addEventListener("change", (e) => {
    const g = e.target.dataset.g;
    if (g !== undefined) favCall("/api/favorites/set", { ...favId(r), group: g, on: e.target.checked });
  });
  el.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = e.target.g.value.trim() || (FGROUPS.length ? "" : "Yêu thích");
    if (name) await favCall("/api/favorites/set", { ...favId(r), group: name, on: true });
  });
  const inp = el.querySelector(".popnew input"); if (!FGROUPS.length && inp) inp.focus();
}

// bảng chọn nhóm để lọc (chọn 1 hoặc nhiều nhóm = hiện cấu hình thuộc BẤT KỲ nhóm nào đã chọn) + đổi tên / xoá nhóm
function openFavPanel() {
  if (FAVPOP && FAVPOP.panel) { closePop(); return; }
  const el = openPop($("favFilterBtn"), () => `<div class="pophead">Xem cấu hình yêu thích theo nhóm
      <div class="muted">Chọn 1 hoặc nhiều nhóm – bảng hiện cấu hình thuộc bất kỳ nhóm nào đã chọn. Số = có trong lượt này / tổng đã lưu.</div></div>
    ${FGROUPS.length ? `<div class="popacts"><button class="btn small ghost" data-a="all">Chọn tất cả</button><button class="btn small ghost" data-a="none">Bỏ chọn (xem mọi cấu hình)</button></div>` : ""}
    ${FGROUPS.map((g) => { const c = favCounts(g.name); return `<div class="popitem"><label><input type="checkbox" data-sel="${esc(g.name)}"${FAVSEL.has(g.name) ? " checked" : ""}>
      <span class="dot" style="background:${g.color}"></span><span class="gname">${esc(g.name)}</span><span class="muted">${c.here}/${c.total}</span></label>
      <button class="iconbtn" data-a="ren" data-g="${esc(g.name)}" title="Đổi tên nhóm">✎</button><button class="iconbtn danger" data-a="del" data-g="${esc(g.name)}" title="Xoá nhóm">🗑</button></div>`; }).join("")
      || `<div class="muted" style="padding:6px 2px">Chưa có nhóm nào – tạo nhóm ở dưới, hoặc bấm ♡ ở đầu dòng để lưu cấu hình vào nhóm.</div>`}
    <form class="popnew"><input name="g" placeholder="Tạo nhóm mới…" maxlength="40"><button class="btn small">+ Tạo nhóm</button></form>`);
  FAVPOP.panel = true;
  const apply = () => { favBtnTxt(); if (RES) renderTable(); FAVPOP && FAVPOP.render(); };
  el.addEventListener("change", (e) => {
    const n = e.target.dataset.sel; if (n === undefined) return;
    if (e.target.checked) FAVSEL.add(n); else FAVSEL.delete(n);
    apply();
  });
  el.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-a]"); if (!b) return;
    const a = b.dataset.a, g = b.dataset.g;
    if (a === "all") { FAVSEL = new Set(FGROUPS.map((x) => x.name)); apply(); }
    else if (a === "none") { FAVSEL = new Set(); apply(); }
    else if (a === "ren") {
      const nn = (prompt(`Đổi tên nhóm "${g}" thành:`, g) || "").trim();
      if (nn && nn !== g) {
        const was = FAVSEL.has(g);
        if (await favCall("/api/favorites/groups", { action: "rename", name: g, new_name: nn }) && was) { FAVSEL.add(nn); apply(); }
      }
    } else if (a === "del") {
      const c = favCounts(g);
      if (confirm(`Xoá nhóm "${g}" (${c.total} cấu hình)? Cấu hình chỉ thuộc nhóm này sẽ bị bỏ khỏi yêu thích. Không khôi phục được.`))
        favCall("/api/favorites/groups", { action: "delete", name: g });
    }
  });
  el.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = e.target.g.value.trim();
    if (name) await favCall("/api/favorites/groups", { action: "create", name });
  });
}

function favCell(r) {
  const gs = favGroupsOf(r);
  const tip = gs.length ? `Nhóm: ${gs.join(", ")} – bấm để sửa nhóm` : "Lưu vào nhóm yêu thích";
  return `<button class="favbtn${gs.length ? " on" : ""}" title="${esc(tip)}"${gs.length ? ` style="color:${gColor(gs[0])}"` : ""}>${gs.length ? "♥" : "♡"}</button>${
    gs.length > 1 ? `<span class="dots">${gs.slice(1, 4).map((g) => `<span class="dot" style="background:${gColor(g)}"></span>`).join("")}</span>` : ""}`;
}
// ---- lọc theo ngày vào lệnh: máy chủ tính lại bảng xếp hạng chỉ với lệnh trong khoảng (bot "chỉ chạy" khoảng đó)
let DROWS = null, DINFO = null, dateTimer = null, dateTok = 0;
const gridRows = () => DROWS || (RES ? RES.rows : []);
const dateQ = () => ($("fFrom").value ? `&date_from=${$("fFrom").value}` : "") + ($("fTo").value ? `&date_to=${$("fTo").value}` : "");
const vnDay = (ms) => new Date(ms).toLocaleDateString("en-CA", { timeZone: "Asia/Ho_Chi_Minh" });   // YYYY-MM-DD
const dmy = (s) => (s ? s.split("-").reverse().join("/") : "");
function markActual(rows) {
  const ac = RES.summary.actual_cfg;
  rows.forEach((r) => { r.isActual = r.mode === "single" && Math.abs(r.tp - ac.tp) < 1e-9 && Math.abs(r.sl - ac.sl) < 1e-9 && r.hold === ac.hold; });
  return rows;
}
function dateBanner() {
  const f = $("fFrom").value, t = $("fTo").value, on = !!(f || t);
  $("fDateClear").hidden = !on;
  $("dateBanner").hidden = !on;
  if (on) $("dateBanner").innerHTML = DINFO
    ? `📅 Đang lọc ngày vào lệnh <b>${f ? dmy(f) : "đầu"} → ${t ? dmy(t) : "cuối"}</b>: ${DINFO.n_trades.toLocaleString("vi-VN")} / ${DINFO.n_total.toLocaleString("vi-VN")} lệnh – áp dụng cho bảng xếp hạng, heatmap, chi tiết cấu hình (tab Tổng quan vẫn là toàn bộ giai đoạn). <a href="#" id="dateBannerClear">Bỏ lọc ngày</a>`
    : `📅 Đang tính lại theo ngày ${f ? dmy(f) : "đầu"} → ${t ? dmy(t) : "cuối"}…`;
}
async function applyDates() {
  if (!RES) return;
  const tok = ++dateTok, q = dateQ();
  if (!q) { DROWS = null; DINFO = null; }
  else {
    dateBanner();
    let d;
    try { d = await api(`/api/rows?x=1${q}${runQ()}`); } catch (e) { if (tok === dateTok) alert(e.message); return; }
    if (tok !== dateTok) return;                                // đã đổi ngày trong lúc tải
    DROWS = markActual(d.rows); DINFO = d;
  }
  dateBanner();
  renderTable(); renderHeat();
  if (DCUR) openDetail(DCUR.mode, DCUR.ci, false);
}
function datesChanged() { clearTimeout(dateTimer); dateTimer = setTimeout(applyDates, 300); }
function resetDates() {
  const a = RES.summary.actual || {};
  for (const id of ["fFrom", "fTo"]) {
    $(id).value = "";
    if (a.start_ms) { $(id).min = vnDay(a.start_ms); $(id).max = vnDay(a.end_ms); }
  }
  DROWS = null; DINFO = null; dateBanner();
}

function renderTable() {
  const fm = $("fMode").value, fh = $("fHold").value, mn = +$("fMin").value || 0, top = +$("fTop").value, fav = FAVSEL.size > 0;
  let rows = gridRows().filter((r) => (!fm || r.mode === fm) && (fh === "" || r.hold === +fh) && r.n >= mn);
  const total = rows.length;
  if (fav) rows = rows.filter((r) => favGroupsOf(r).some((g) => FAVSEL.has(g)));
  if (FCONDS.length) rows = rows.filter((r) => FCONDS.every((c) => fTest(c, r)));
  $("fCount").textContent = FCONDS.length || fav ? `${rows.length.toLocaleString("vi-VN")} / ${total.toLocaleString("vi-VN")} cấu hình khớp lọc` : "";
  rows.sort((a, b) => ((a[SORT.k] > b[SORT.k]) - (a[SORT.k] < b[SORT.k])) * SORT.d);
  const act = rows.filter((r) => r.isActual);
  rows = rows.slice(0, top);
  for (const r of act) if (!rows.includes(r)) rows.push(r);
  const empty = fav && !rows.length ? `<tr><td class="l muted" colspan="${COLS.length + 2}">Lượt chạy này không có cấu hình nào trong nhóm đã chọn khớp bộ lọc. Bấm ♡ ở đầu dòng để lưu cấu hình vào nhóm.</td></tr>` : "";
  // ♥, #, Chế độ, TP, SL, Giữ: neo cố định bên trái khi kéo ngang (class stk + left tính ở pinGrid)
  const pin = (i) => (i < PIN_COLS ? ` stk stk${i + 2}` : "");
  $("grid").innerHTML = `<thead><tr><th class="l fav stk stk0" title="Cấu hình yêu thích">♥</th><th class="l stk stk1">#</th>${COLS.map(([k, t, c], i) => `<th class="${c}${pin(i)}" data-k="${k}">${t}${SORT.k === k ? (SORT.d < 0 ? " ▼" : " ▲") : ""}</th>`).join("")}</tr></thead>
    <tbody>${empty}${rows.map((r, i) => `<tr class="click ${r.isActual ? "actual" : ""}" data-m="${r.mode}" data-ci="${r.ci}"><td class="l fav stk stk0">${favCell(r)}</td><td class="l stk stk1">${i + 1}</td>${COLS.map(([k, , c, f, t], j) => {
      const h8 = c.includes("h8") && r.h8_hours ? `Chỉ vào lệnh lúc ${r.h8_hours.map(hh).join(", ")}h (${r.h8_n} lệnh) – bấm để mở chi tiết với 8 giờ này` : "";
      const tip = [t ? t(r) : "", h8].filter(Boolean).join("\n");
      return `<td class="${c}${pin(j)}"${tip ? ` title="${esc(tip)}"` : ""}>${f(r)}</td>`;
    }).join("")}</tr>`).join("")}</tbody>`;
  pinGrid();
}
const PIN_COLS = 4;                     // số cột đầu của COLS được neo (Chế độ, TP %, SL %, Giữ)
function pinGrid() {
  if ($("tab-table").hidden) return;            // đang ẩn: không đo được, đo lại khi mở tab
  // vị trí left của từng cột neo = tổng bề rộng các cột neo đứng trước (đo trên hàng tiêu đề)
  const ths = [...$("grid").querySelectorAll("thead th.stk")];
  let x = 0;
  const css = ths.map((th, i) => { const r = `#grid .stk${i}{left:${x}px}`; x += th.getBoundingClientRect().width; return r; });
  css.push(`#grid .stk${ths.length - 1}{box-shadow: inset -1px 0 0 var(--line), 6px 0 8px -6px rgba(0,0,0,.45)}`);
  let st = $("gridPinCss");
  if (!st) { st = document.createElement("style"); st.id = "gridPinCss"; document.head.appendChild(st); }
  st.textContent = css.join("\n");
}

function heatColor(v, lo, hi, good = true) {
  if (v === null || v === undefined || !isFinite(v)) return "transparent";
  let t;
  if (lo < 0 && hi > 0) t = v >= 0 ? v / hi : -v / lo;     // -1..1
  else t = hi > lo ? ((v - lo) / (hi - lo)) * 2 - 1 : 0;
  if (!good) t = -t;
  const a = Math.min(Math.abs(t), 1) * 0.75 + 0.06;
  return t >= 0 ? `color-mix(in srgb, var(--pos) ${a * 100}%, transparent)` : `color-mix(in srgb, var(--neg) ${a * 100}%, transparent)`;
}
function renderHeat() {
  const mode = $("hMode").value, hold = +$("hHold").value, met = $("hMetric").value;
  const sel = $("hExtra").value || "";
  const uniq = (f) => [...new Set(gridRows().map(f))].filter((v) => v !== null && v !== undefined).sort((a, b) => a - b);
  const tps = uniq((r) => r.tp), sls = uniq((r) => r.sl);
  const ac = RES.summary.actual_cfg || {};
  const get = (m, tp, sl) => gridRows().find((r) => r.mode === m && r.tp === tp && r.sl === sl && r.hold === hold &&
    (!EXTRA_KEYS.length || extraKey(r) === sel));
  const val = (tp, sl) => {
    if (met === "diff") { const a = get("multi", tp, sl), b = get("single", tp, sl); return a && b ? a.net - b.net : null; }
    const r = get(mode, tp, sl);
    if (!r) return null;
    if (met === "h8_gain") return r.h8_net == null ? null : r.h8_net - r.net;
    return r[met] ?? null;
  };
  const h8 = met.startsWith("h8_");
  $("heatH8").hidden = !h8;
  const vals = tps.flatMap((tp) => sls.map((sl) => val(tp, sl))).filter((x) => x !== null && isFinite(x) && Math.abs(x) < 1e8);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const good = met !== "max_dd" && met !== "h8_dd";
  const d = ["n", "h8_n", "day_pos", "h8_day_pos"].includes(met) ? 0 : met === "win_rate" ? 1 : 2;
  $("heat").innerHTML = `<table class="heat"><tr><th>TP \\ SL</th>${sls.map((s) => `<th>${s ? s + "%" : "không SL"}</th>`).join("")}</tr>
    ${tps.map((tp) => `<tr><th>${tp}%</th>${sls.map((sl) => {
      const v = val(tp, sl);
      const isAct = tp === ac.tp && sl === ac.sl && hold === ac.hold;
      const r = get(met === "diff" ? "multi" : mode, tp, sl);
      return `<td class="${isAct ? "act" : ""}" style="background:${heatColor(v, lo, hi, good)}" data-m="${met === "diff" ? "multi" : mode}" data-ci="${r ? r.ci : ""}" title="TP ${tp}% · SL ${sl}% · ${r ? (h8 && r.h8_hours ? `${r.h8_n} lệnh trong 8 giờ: ${r.h8_hours.map(hh).join(", ")}h · mọi giờ: net ${fmt(r.net)} / ${r.n} lệnh` : r.n + " lệnh") : ""}">${fmt(v, d)}</td>`;
    }).join("")}</tr>`).join("")}</table>`;
}

// ---------------------------------------------------------------- chi tiết
let HOURS = new Set(Array.from({ length: 24 }, (_, i) => i)), DCUR = null, hourTimer = null;
const TSORT = { byHour: { k: "hour", d: 1 }, byCoin: { k: "sim", d: -1 }, trades: { k: "entry_ms", d: 1 } };
const hh = (h) => String(h).padStart(2, "0");
const hoursQ = () => (HOURS.size === 24 ? "" : `&hours=${HOURS.size ? [...HOURS].sort((a, b) => a - b).join(",") : "none"}`);

async function openDetail(mode, ci, switchTab = true) {
  const c = RES.combos[ci] || [null, null, 0];
  DCUR = { mode, ci };
  $("dMode").value = mode; $("dTp").value = c[0]; $("dSl").value = c[1]; $("dHold").value = c[2];
  const row = RES.rows.find((r) => r.mode === mode && r.ci === ci);
  $("dParams").textContent = row ? rowCfgTxt(row) : "";
  if (row && EXTRA_KEYS.length) $("hExtra").value = extraKey(row);
  $("dCsv").href = `/api/export/trades.csv?mode=${mode}&ci=${ci}${hoursQ()}${dateQ()}${runQ()}`;
  if (switchTab) showTab("detail");
  renderHourChips();
  $("dKpis").innerHTML = `<span class="muted">Đang tải chi tiết…</span>`;
  const q = hoursQ() + dateQ();
  let d;
  try { d = await api(`/api/detail?mode=${mode}&ci=${ci}${q}${runQ()}`); }
  catch (e) { $("dKpis").innerHTML = `<span class="neg">${esc(e.message)}</span>`; return; }
  if (!DCUR || DCUR.mode !== mode || DCUR.ci !== ci || q !== hoursQ() + dateQ()) return;   // đã đổi cấu hình / giờ trong lúc tải
  DETAIL = d;
  renderDetail();
}
function detailFromSelects() {
  const tp = +$("dTp").value, sl = +$("dSl").value, h = +$("dHold").value, m = $("dMode").value;
  const cur = DCUR && RES.rows.find((r) => r.mode === DCUR.mode && r.ci === DCUR.ci);
  const same = (r) => !EXTRA_KEYS.length || !cur || extraKey(r) === extraKey(cur);
  const row = RES.rows.find((r) => r.mode === m && r.tp === tp && r.sl === sl && r.hold === h && same(r)) ||
    RES.rows.find((r) => r.mode === m && r.tp === tp && r.sl === sl && r.hold === h);
  if (row) openDetail(m, row.ci, false);
}

// ---- bộ lọc giờ vào lệnh
function renderHourChips() {
  $("hChips").innerHTML = Array.from({ length: 24 }, (_, i) =>
    `<button class="hchip${HOURS.has(i) ? " on" : ""}" data-h="${i}" title="${hh(i)}:00 – ${hh(i)}:59">${hh(i)}</button>`).join("");
  $("hSelInfo").textContent = HOURS.size === 24 ? "đang tính mọi giờ" : `${HOURS.size}/24 giờ – bot chỉ vào lệnh trong các giờ đã chọn`;
}
function hoursChanged() {
  renderHourChips();
  clearTimeout(hourTimer);
  hourTimer = setTimeout(() => { if (DCUR) openDetail(DCUR.mode, DCUR.ci, false); }, 350);
}

// ---- bảng sắp xếp được: bấm tiêu đề cột để đổi chiều (lần đầu: lớn → nhỏ)
function cmpVal(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "boolean") return (a ? 1 : 0) - (b ? 1 : 0);
  return String(a).localeCompare(String(b), "vi", { numeric: true });
}
function sortedRows(id, rows) {
  const s = TSORT[id];
  if (!s) return rows;
  return rows.slice().sort((x, y) => {
    const a = x[s.k], b = y[s.k];
    const na = a === null || a === undefined || Number.isNaN(a), nb = b === null || b === undefined || Number.isNaN(b);
    if (na || nb) return na === nb ? 0 : na ? 1 : -1;             // ô trống luôn ở cuối
    return cmpVal(a, b) * s.d;
  });
}
function thead(id, cols, group) {
  const s = TSORT[id];
  const g = group ? `<tr class="grp">${group.map(([lbl, n, cls]) => `<th colspan="${n}" class="${cls || ""}">${lbl}</th>`).join("")}</tr>` : "";
  return `<thead>${g}<tr>${cols.map((c) => {
    const on = s && s.k === c.k;
    return `<th data-sk="${c.k}" class="${c.l ? "l" : ""}${on ? " sorted" : ""}" title="Bấm để sắp xếp">${c.label}<span class="arr">${on ? (s.d < 0 ? "▼" : "▲") : "↕"}</span></th>`;
  }).join("")}</tr></thead>`;
}
function sortClick(e) {
  const th = e.target.closest("th[data-sk]");
  if (!th || !DETAIL) return;
  const id = e.currentTarget.id, k = th.dataset.sk, s = TSORT[id];
  TSORT[id] = { k, d: s && s.k === k ? -s.d : -1 };
  ({ byHour: renderHourTable, byCoin: renderCoinTable, trades: renderTrades })[id]();
}

// ---- cột chung cho bảng theo giờ & theo coin: mô phỏng vs thực tế, tách TP / SL / hết giờ
const wrTxt = (v) => (v === null ? "–" : fmt(v, 1) + "%");
const GROUP_COLS = [
  { k: "n", label: "Lệnh", f: (r) => r.n }, { k: "n_tp", label: "TP", f: (r) => r.n_tp }, { k: "n_sl", label: "SL", f: (r) => r.n_sl },
  { k: "n_timeout", label: "Hết giờ", f: (r) => r.n_timeout }, { k: "wr", label: "Win", f: (r) => wrTxt(r.wr) },
  { k: "sim", label: "Lãi/lỗ", f: (r) => sgn(r.sim) }, { k: "avg", label: "TB / lệnh", f: (r) => (r.avg === null ? "–" : sgn(r.avg, 3)) },
  { k: "a_n", label: "Lệnh", f: (r) => r.a_n }, { k: "a_tp", label: "TP", f: (r) => r.a_tp }, { k: "a_sl", label: "SL", f: (r) => r.a_sl },
  { k: "a_timeout", label: "Hết giờ", f: (r) => r.a_timeout }, { k: "a_wr", label: "Win", f: (r) => wrTxt(r.a_wr) },
  { k: "actual", label: "Lãi/lỗ", f: (r) => sgn(r.actual) }, { k: "diff", label: "MP − thật", f: (r) => sgn(r.diff) },
];
const SUM_KEYS = ["n", "win", "n_tp", "n_sl", "n_timeout", "sim", "a_n", "a_win", "a_tp", "a_sl", "a_timeout", "actual"];
function derive(r) {
  return { ...r, wr: r.n ? r.win / r.n * 100 : null, avg: r.n ? r.sim / r.n : null,
    a_wr: r.a_n ? r.a_win / r.a_n * 100 : null, diff: r.sim - r.actual };
}
function groupTable(id, first, rows) {
  const cols = [first, ...GROUP_COLS.filter((c) => hasActual() || !/^(a_|actual|diff)/.test(c.k))];
  const zero = Object.fromEntries(SUM_KEYS.map((k) => [k, 0]));
  const tot = derive(rows.reduce((t, r) => { SUM_KEYS.forEach((k) => (t[k] += r[k] || 0)); return t; }, zero));
  const sepAt = hasActual() ? [8, 14] : [];
  const tr = (r, cls = "") => `<tr class="${cls}">${cols.map((c, i) => `<td class="${i === 0 ? "l" : ""}${sepAt.includes(i) ? " sep" : ""}">${c.f(r)}</td>`).join("")}</tr>`;
  $(id).innerHTML = thead(id, cols, hasActual() ? [["", 1], ["Mô phỏng", 7], ["Thực tế", 6, "sep"], ["", 1, "sep"]] : null) +
    `<tbody>${sortedRows(id, rows).map((r) => tr(r)).join("")}</tbody><tfoot>${tr({ ...tot, _total: true }, "tot")}</tfoot>`;
}
function renderHourTable() {
  const rows = (DETAIL.by_hour || []).filter((h) => HOURS.has(h.hour)).map(derive);
  groupTable("byHour", { k: "hour", label: "Giờ vào", l: true, f: (r) => (r._total ? "<b>Tổng</b>" : `${hh(r.hour)}:00 – ${hh(r.hour)}:59`) }, rows);
}
function renderCoinTable() {
  const rows = (DETAIL.by_coin || []).map(derive);
  $("cCount").textContent = `${rows.length} coin`;
  groupTable("byCoin", { k: "coin", label: "Coin", l: true, f: (r) => (r._total ? "<b>Tổng</b>" : esc(r.coin)) }, rows);
}

function renderDetail() {
  const s = DETAIL.stats, a = hasActual() ? (DETAIL.actual_stats || RES.summary.actual_stats) : null;
  const vs = (txt) => (a ? txt : "");
  const hNote = (HOURS.size === 24 ? "" : ` <span class="muted">· ${HOURS.size}/24 giờ</span>`) +
    ($("fFrom").value || $("fTo").value ? ` <span class="muted">· ${dmy($("fFrom").value) || "đầu"}→${dmy($("fTo").value) || "cuối"}</span>` : "");
  $("dKpis").innerHTML = [
    kpi("Lãi/lỗ ròng" + hNote, sgn(s.net), vs(a && `thật: ${fmt(a.net)}`)),
    kpi("Số lệnh", fmt(s.n, 0), a ? `bị bỏ ${s.skipped} · thật ${a.n}` : `tín hiệu bị bỏ: ${fmt(s.skipped, 0)}`),
    kpi("Win rate", fmt(s.win_rate, 1) + "%", vs(a && `thật: ${fmt(a.win_rate, 1)}%`)),
    kpi("TP / SL / hết giờ", `${s.n_tp} / ${s.n_sl + s.n_liq} / ${s.n_timeout}`, vs(a && `thật: ${a.n_tp} / ${a.n_sl} / ${a.n_timeout}`)),
    kpi("Max drawdown", fmt(s.max_dd), a ? `thật: ${fmt(a.max_dd)}` : `${fmt(s.max_dd_pct, 1)}% vốn`),
    kpi("Lãi / Max DD", fmt(s.ret_dd), vs(a && `thật: ${fmt(a.ret_dd)}`)),
    kpi("Profit factor", fmt(s.pf), vs(a && `thật: ${fmt(a.pf)}`)),
    kpi("Giữ lệnh TB", fmt(s.avg_hold, 1) + " ph", a ? `thật: ${fmt(a.avg_hold, 1)} ph` : `TB/lệnh ${fmt(s.avg, 3)}`),
  ].join("");
  const css = getComputedStyle(document.documentElement);
  const col = (n) => css.getPropertyValue(n).trim();
  Chart.defaults.color = col("--muted"); Chart.defaults.borderColor = col("--line");
  for (const k in charts) charts[k].destroy();
  const xfmt = (v) => new Date(v).toLocaleDateString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh", day: "2-digit", month: "2-digit" });
  charts.eq = new Chart($("eqChart"), {
    type: "line",
    data: { datasets: [
      { label: "Mô phỏng", data: DETAIL.equity.map(([x, y]) => ({ x, y })), borderColor: col("--accent"), pointRadius: 0, borderWidth: 2, stepped: true },
      ...(a ? [{ label: "Thực tế", data: (DETAIL.actual_equity || []).map(([x, y]) => ({ x, y })), borderColor: col("--warn"), pointRadius: 0, borderWidth: 1.5, stepped: true }] : []),
    ] },
    options: { animation: false, maintainAspectRatio: false, interaction: { mode: "nearest", intersect: false },
      scales: { x: { type: "linear", ticks: { callback: xfmt, maxTicksLimit: 8 } } },
      plugins: { tooltip: { callbacks: { title: (it) => tsTxt(it[0].parsed.x), label: (it) => `${it.dataset.label}: ${fmt(it.parsed.y)}` } } } },
  });
  charts.day = new Chart($("dayChart"), {
    type: "bar",
    data: { labels: DETAIL.by_day.map((d) => d.day.slice(5)), datasets: [
      { label: "Mô phỏng", data: DETAIL.by_day.map((d) => d.sim), backgroundColor: col("--accent") },
      ...(a ? [{ label: "Thực tế", data: DETAIL.by_day.map((d) => d.actual), backgroundColor: col("--warn") }] : []),
    ] },
    options: { animation: false, maintainAspectRatio: false },
  });
  const H = (DETAIL.by_hour || []).filter((h) => HOURS.has(h.hour));
  charts.hour = new Chart($("hourChart"), {
    type: "bar",
    data: { labels: H.map((h) => `${hh(h.hour)}h`), datasets: [
      { label: "Mô phỏng", data: H.map((h) => h.sim), backgroundColor: col("--accent") },
      ...(a ? [{ label: "Thực tế", data: H.map((h) => h.actual), backgroundColor: col("--warn") }] : []),
    ] },
    options: { animation: false, maintainAspectRatio: false,
      plugins: { tooltip: { callbacks: {
        title: (it) => `${hh(H[it[0].dataIndex].hour)}:00 – ${hh(H[it[0].dataIndex].hour)}:59`,
        label: (it) => { const h = H[it.dataIndex];
          return it.datasetIndex === 0 ? `Mô phỏng: ${fmt(it.parsed.y)} USDT · ${h.n} lệnh (TP ${h.n_tp} / SL ${h.n_sl} / hết giờ ${h.n_timeout})`
            : `Thực tế: ${fmt(it.parsed.y)} USDT · ${h.a_n} lệnh (TP ${h.a_tp} / SL ${h.a_sl} / hết giờ ${h.a_timeout})`; } } } } },
  });
  renderHourTable();
  renderTrades();
  renderCoinTable();
}

const TRADE_COLS_REPLAY = (() => {
  const srcTxt = { csv: "file", tp: "TP", tick: "tick", "1m": "1m" };
  return [
    { k: "id", label: "#", l: true, f: (t) => t.id }, { k: "coin", label: "Coin", l: true, f: (t) => esc(t.coin) },
    { k: "entry_ms", label: "Vào lệnh (tới ms)", l: true, f: (t) => t.entry_t }, { k: "hour", label: "Giờ", f: (t) => hh(t.hour) },
    { k: "entry_px", label: "Giá vào", f: (t) => fmt(t.entry_px, 6) },
    { k: "entry_src", label: "Nguồn", l: true, f: (t) => `<span title="${esc(t.note)}">${srcTxt[t.entry_src] || t.entry_src}${t.res !== "tick" ? ` · ${t.res}` : ""}</span>` },
    { k: "taken", label: "Vào?", f: (t) => (t.reason === "NA" ? "–" : t.taken ? "✓" : "bỏ") },
    { k: "reason", label: "Mô phỏng", f: (t) => `<span class="badge ${t.reason}">${t.reason}</span>` },
    { k: "exit_t", label: "Thoát MP", l: true, f: (t) => t.exit_t.slice(5) }, { k: "exit_px", label: "Giá thoát", f: (t) => fmt(t.exit_px, 6) },
    { k: "hold_min", label: "Giữ (ph)", f: (t) => fmt(t.hold_min, 1) }, { k: "pnl", label: "PnL MP", f: (t) => (t.pnl === null ? "–" : sgn(t.pnl, 3)) },
    { k: "a_reason", label: "Thật", f: (t) => `<span class="badge ${t.a_reason}">${t.a_reason}</span>` },
    { k: "a_exit_t", label: "Thoát thật", l: true, f: (t) => t.a_exit_t.slice(5) }, { k: "a_hold_min", label: "Giữ thật", f: (t) => fmt(t.a_hold_min, 1) },
    { k: "a_pnl", label: "PnL thật", f: (t) => (t.a_pnl === null ? "–" : sgn(t.a_pnl, 3)) },
  ];
})();
// Backtest: mỗi dòng là một lệnh đã vào trong mô phỏng (tín hiệu bị giới hạn chặn không nằm trong bảng).
const TRADE_COLS_BT = [
  { k: "id", label: "#", l: true, f: (t) => t.id }, { k: "coin", label: "Coin", l: true, f: (t) => esc(t.coin) },
  { k: "side", label: "Chiều", f: (t) => `<span class="badge ${t.side === "LONG" ? "TP" : "SL"}">${t.side}</span>` },
  { k: "signal_t", label: "Tín hiệu", l: true, f: (t) => String(t.signal_t || "").slice(5) },
  { k: "entry_ms", label: "Vào lệnh (tới ms)", l: true, f: (t) => t.entry_t }, { k: "hour", label: "Giờ", f: (t) => hh(t.hour) },
  { k: "move_pct", label: "Biến động %", f: (t) => fmt(t.move_pct, 2) },
  { k: "entry_px", label: "Giá vào", f: (t) => fmt(t.entry_px, 6) },
  { k: "res", label: "Dữ liệu", l: true, f: (t) => t.res },
  { k: "reason", label: "Lý do thoát", f: (t) => `<span class="badge ${t.reason}">${t.reason}</span>` },
  { k: "exit_t", label: "Thoát", l: true, f: (t) => String(t.exit_t || "").slice(5) },
  { k: "exit_px", label: "Giá thoát", f: (t) => fmt(t.exit_px, 6) },
  { k: "hold_min", label: "Giữ (ph)", f: (t) => fmt(t.hold_min, 1) },
  { k: "pnl", label: "PnL", f: (t) => (t.pnl === null ? "–" : sgn(t.pnl, 3)) },
  { k: "roe", label: "ROE %", f: (t) => (t.roe == null ? "–" : sgn(t.roe, 1)) },
  { k: "fee", label: "Phí", f: (t) => fmt(t.fee, 3) }, { k: "funding", label: "Funding", f: (t) => fmt(t.funding, 3) },
];
const tradeCols = () => (hasActual() ? TRADE_COLS_REPLAY : TRADE_COLS_BT);
function renderTrades() {
  if (!DETAIL) return;
  const COL = tradeCols();
  const f = $("tFilter").value, q = $("tCoin").value.trim().toUpperCase();
  const rows = DETAIL.trades.filter((t) => (!q || t.coin.includes(q)) && (
    f === "all" || (f === "taken" && t.taken) || (f === "skipped" && !t.taken && t.reason !== "NA") ||
    (f === "diff" && t.taken && t.reason !== t.a_reason) || (t.taken && t.reason === f)));
  $("tCount").textContent = `${rows.length} lệnh`;
  $("trades").innerHTML = thead("trades", COL) + `<tbody>${sortedRows("trades", rows).slice(0, 3000).map((t) =>
    `<tr class="${t.taken ? "" : "skip"}">${COL.map((c) => `<td class="${c.l ? "l" : ""}">${c.f(t)}</td>`).join("")}</tr>`).join("")}</tbody>`;
}

// ---------------------------------------------------------------- tabs & sự kiện
function showTab(t) {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  document.querySelectorAll(".tab").forEach((x) => (x.hidden = x.id !== "tab-" + t));
  if (t === "table" && RES) pinGrid();          // bảng vẽ lúc tab đang ẩn -> đo lại bề rộng cột neo
}
window.addEventListener("resize", () => { if (RES && !$("tab-table").hidden) pinGrid(); });
document.querySelector(".tabs").addEventListener("click", (e) => { if (e.target.dataset.tab) showTab(e.target.dataset.tab); });
$("file").addEventListener("change", onFile);
["gTp", "gSl", "gHold", "mSingle", "mMulti"].forEach((id) => $(id).addEventListener("input", countCombos));
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", () => {
  const p = PRESETS[b.dataset.preset]; const v = typeof p === "function" ? p() : p;
  $("gTp").value = v[0]; $("gSl").value = v[1]; $("gHold").value = v[2]; countCombos();
}));
$("runBtn").addEventListener("click", run);
$("cancelBtn").addEventListener("click", () => {
  if (!confirm("Huỷ lượt đang chạy?")) return;
  api("/api/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }).catch(() => {});
});
$("upload").addEventListener("change", async (e) => {
  const f = e.target.files[0]; if (!f) return;
  try {
    const r = await api("/api/upload", { method: "POST", headers: { "X-Filename": encodeURIComponent(f.name).replace(/%20/g, " ") }, body: await f.arrayBuffer() });
    await loadFiles(r.name);
  } catch (err) { alert(err.message); }
  e.target.value = "";
});
["fMode", "fHold", "fMin", "fTop"].forEach((id) => $(id).addEventListener("input", renderTable));
$("favFilterBtn").addEventListener("click", openFavPanel);
["fFrom", "fTo"].forEach((id) => $(id).addEventListener("change", datesChanged));
$("fDateClear").addEventListener("click", () => { $("fFrom").value = ""; $("fTo").value = ""; applyDates(); });
$("dateBanner").addEventListener("click", (e) => { if (e.target.id === "dateBannerClear") { e.preventDefault(); $("fDateClear").click(); } });
$("fAdd").addEventListener("click", () => {
  FCONDS.push({ k: "net", op: ">", v: "", v2: "" }); saveConds(); renderConds();
  const ins = $("fConds").querySelectorAll('input[data-f="v"]'); if (ins.length) ins[ins.length - 1].focus();
});
$("fClear").addEventListener("click", () => { FCONDS = []; saveConds(); renderConds(); if (RES) renderTable(); });
["input", "change", "click"].forEach((ev) => $("fConds").addEventListener(ev, onCondInput));
renderConds();
$("fSaved").addEventListener("change", (e) => applySaved(e.target.value));
$("fSave").addEventListener("click", saveCurrent);
$("fDel").addEventListener("click", deleteSaved);
loadSaved();
loadFavs();
$("grid").addEventListener("click", (e) => {
  const th = e.target.closest("th[data-k]");
  if (th) { const k = th.dataset.k; SORT = { k, d: SORT.k === k ? -SORT.d : -1 }; renderTable(); return; }
  const tr = e.target.closest("tr[data-ci]"); if (!tr) return;
  if (e.target.closest(".favbtn")) {                          // ♡ / ♥: lưu / bỏ yêu thích, không mở chi tiết
    const r = gridRows().find((x) => x.mode === tr.dataset.m && x.ci === +tr.dataset.ci);
    if (r) openFavMenu(r, e.target.closest(".favbtn"));
    return;
  }
  // bấm vào nhóm cột "8h tốt nhất" -> mở chi tiết với đúng 8 giờ đó; cột khác -> giữ bộ lọc giờ đang dùng
  const row = e.target.closest("td.h8") && gridRows().find((r) => r.mode === tr.dataset.m && r.ci === +tr.dataset.ci);
  if (row && row.h8_hours) HOURS = new Set(row.h8_hours);
  openDetail(tr.dataset.m, +tr.dataset.ci);
});
["hMode", "hHold", "hMetric"].forEach((id) => $(id).addEventListener("input", renderHeat));
$("heat").addEventListener("click", (e) => {
  const td = e.target.closest("td[data-ci]"); if (!td || td.dataset.ci === "") return;
  // đang xem chỉ số 8h tốt nhất -> mở chi tiết với đúng 8 giờ của ô đó
  if ($("hMetric").value.startsWith("h8_")) {
    const r = gridRows().find((x) => x.mode === td.dataset.m && x.ci === +td.dataset.ci);
    if (r && r.h8_hours) HOURS = new Set(r.h8_hours);
  }
  openDetail(td.dataset.m, +td.dataset.ci);
});
["dMode", "dTp", "dSl", "dHold"].forEach((id) => $(id).addEventListener("input", detailFromSelects));
["tFilter", "tCoin"].forEach((id) => $(id).addEventListener("input", renderTrades));
["byHour", "byCoin", "trades"].forEach((id) => $(id).addEventListener("click", sortClick));
$("hChips").addEventListener("click", (e) => {
  const b = e.target.closest("[data-h]"); if (!b) return;
  const h = +b.dataset.h; if (HOURS.has(h)) HOURS.delete(h); else HOURS.add(h); hoursChanged();
});
$("hAll").addEventListener("click", () => { HOURS = new Set(Array.from({ length: 24 }, (_, i) => i)); hoursChanged(); });
$("hNone").addEventListener("click", () => { HOURS = new Set(); hoursChanged(); });
$("hReset").addEventListener("click", () => {
  HOURS = new Set(Array.from({ length: 24 }, (_, i) => i)); $("tFilter").value = "all"; $("tCoin").value = ""; hoursChanged();
});
renderHourChips();
document.querySelectorAll("[data-toggle]").forEach((b) => b.addEventListener("click", () => {
  const el = $(b.dataset.toggle); el.hidden = !el.hidden;
  b.textContent = el.hidden ? "Hiện bảng" : "Ẩn bảng";
}));

// ---------------------------------------------------------------- lịch sử
async function loadHistory() {
  try { HIST = (await api("/api/history")).runs; } catch (e) { return; }
  renderHistory();
}
function renderHistory() {
  $("histCount").textContent = HIST.length ? `(${HIST.length} lượt)` : "";
  $("histDelAll").disabled = !HIST.length;
  if (!HIST.length) { $("hist").innerHTML = `<tbody><tr><td class="l muted">Chưa có lượt chạy nào.</td></tr></tbody>`; updateSel(); return; }
  const day = (ms) => new Date(ms).toLocaleDateString("vi-VN", { timeZone: "Asia/Ho_Chi_Minh" });
  const cfgOf = (b) => (b.params ? esc(b.params) : `TP ${b.tp}% · ${b.sl ? "SL " + b.sl + "%" : "không SL"} · ${holdTxt(b.hold)}`);
  const bestTxt = (h) => Object.entries(h.best || {}).filter(([, b]) => b)
    .map(([m, b]) => `<div><span class="muted">${h.kind === "bt" ? m.toUpperCase() : modeTxt(m)}:</span> ${sgn(b.net)}
      <span class="muted">${cfgOf(b)}</span></div>`).join("") || "–";
  const gridTxt = (g) => {
    if (!g) return "";
    if (g.tp !== undefined) return `${esc(g.tp || "")} / ${esc(g.sl || "")} / ${esc(g.hold || "")}`;
    return Object.entries(g).map(([k, v]) => `${esc(k.split(".").pop())}: ${esc(Array.isArray(v) ? v.join(", ") : JSON.stringify(v))}`).join(" · ");
  };
  $("hist").innerHTML = `<thead><tr><th class="l"><input type="checkbox" id="histAll"></th><th class="l"></th>
    <th class="l">Thời điểm chạy</th><th class="l">Chế độ</th><th class="l">Chiến lược / file</th><th class="l">Giai đoạn</th>
    <th class="l">Lưới</th><th>Cấu hình</th><th class="l">Tốt nhất</th><th class="l">Ghi chú</th></tr></thead>
    <tbody>${HIST.map((h) => `<tr class="${h.id === RUN_ID ? "cur" : ""}" data-id="${h.id}">
      <td class="l"><input type="checkbox" class="hsel" value="${h.id}"></td>
      <td class="l nowrap"><button class="btn small act" data-act="view">Xem</button><button class="btn small ghost act" data-act="reuse">Dùng lại</button><button class="btn small ghost danger act" data-act="del" title="Xoá lượt này">🗑</button></td>
      <td class="l">${tsTxt(h.created)}</td>
      <td class="l"><span class="kindtag ${h.kind}">${h.kind === "bt" ? "Backtest" : "Replay"}</span></td>
      <td class="l" title="${esc(h.title)}">${esc(h.title)} <span class="muted">${esc((h.exchanges || [h.exchange]).filter(Boolean).join(", "))}</span></td>
      <td class="l">${h.kind === "bt" ? `${esc(String(h.start || "").slice(0, 10))} → ${esc(String(h.end || "").slice(0, 10))}`
        : `${day(h.start_ms)} → ${day(h.end_ms)}`}</td>
      <td class="l muted">${gridTxt(h.grid)}</td>
      <td>${fmt(h.n_combos, 0)}${h.direction === "reverse" ? ` <span class="revtag" title="Ngược hướng bot thật">⇄</span>` : ""}</td>
      <td class="l">${bestTxt(h)}</td>
      <td class="l muted">${h.kind === "bt" ? `${fmt(h.n_signals, 0)} tín hiệu${h.objective ? " · " + esc(h.objective) : ""}`
        : `${fmt(h.n_trades, 0)} lệnh thật · PnL ${sgn(h.actual_net)}${h.reason_match == null ? "" : ` · khớp ${fmt(h.reason_match, 1)}%`}`}</td>
    </tr>`).join("")}</tbody>`;
  updateSel();
}
function selectedIds() { return [...document.querySelectorAll(".hsel:checked")].map((x) => x.value); }
function updateSel() {
  const n = selectedIds().length;
  $("histDelSel").disabled = !n;
  $("histDelSel").textContent = n ? `Xoá đã chọn (${n})` : "Xoá đã chọn";
}
async function deleteRuns(ids, all = false) {
  const msg = all ? `Xoá toàn bộ ${HIST.length} lượt trong lịch sử?` : ids.length === 1 ? "Xoá lượt chạy này khỏi lịch sử?" : `Xoá ${ids.length} lượt đã chọn?`;
  if (!confirm(msg + " Không khôi phục được.")) return;
  try {
    await api("/api/history/delete", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(all ? { all: true } : { ids }) });
  } catch (e) { alert(e.message); return; }
  if (RUN_ID && (all || ids.includes(RUN_ID))) { RUN_ID = null; RES = null; $("results").hidden = true; }
  loadHistory();
}
async function reuseRun(id) {
  let d;
  try { d = await api(`/api/history/${encodeURIComponent(id)}/request`); } catch (e) { alert(e.message); return; }
  const b = d.request || d;
  if ((b.kind || (d.meta && d.meta.kind)) === "bt") {
    setMode("bt");
    applyConfig(b.config, b.exchanges);           // config.js
    $("cfgPanel").scrollIntoView({ behavior: "smooth" });
    return;
  }
  setMode("replay");
  if (FILES.some((f) => f.name === b.file)) { $("file").value = b.file; onFile(); }
  else alert(`File "${b.file}" không còn trong thư mục Actual Performance – vẫn điền cấu hình, hãy chọn file.`);
  const set = (k, v) => { if (v !== undefined && v !== null && $(k)) { if ($(k).type === "checkbox") $(k).checked = !!v; else $(k).value = v; } };
  set("exchange", b.exchange); set("dateFrom", b.date_from); set("dateTo", b.date_to); set("coins", b.coins); set("entryMode", b.entry_mode);
  const a = b.actual || {}, g = b.grid || {}, s = b.settings || {};
  set("aTp", a.tp); set("aSl", a.sl); set("aHold", a.hold); set("gTp", g.tp); set("gSl", g.sl); set("gHold", g.hold);
  set("mSingle", (s.modes || []).includes("single")); set("mMulti", (s.modes || []).includes("multi"));
  set("maxPerCoin", s.max_per_coin); set("maxTotal", s.max_total); set("cooldown", s.cooldown_min); set("notional", s.notional);
  set("leverage", s.leverage); set("sizing", s.sizing); set("maker", s.maker_pct); set("taker", s.taker_pct); set("tpOrder", s.tp_order);
  set("tpFill", s.tp_fill); set("slSlip", s.sl_slip_pct); set("exitSlip", s.exit_slip_pct); set("funding", s.funding);
  set("liq", s.liquidation); set("skip1m", s.skip_1m); set("autoSlip", !!s.auto_slip); set("direction", s.direction || "same"); syncSlip();
  countCombos();
  $("cfgPanel").scrollIntoView({ behavior: "smooth" });
}
$("hist").addEventListener("click", (e) => {
  if (e.target.id === "histAll") { document.querySelectorAll(".hsel").forEach((x) => (x.checked = e.target.checked)); updateSel(); return; }
  if (e.target.classList.contains("hsel")) { updateSel(); return; }
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = b.closest("tr").dataset.id;
  if (b.dataset.act === "view") loadResult(id).then(() => $("results").scrollIntoView({ behavior: "smooth" }));
  else if (b.dataset.act === "reuse") reuseRun(id);
  else if (b.dataset.act === "del") deleteRuns([id]);
});
$("histDelSel").addEventListener("click", () => deleteRuns(selectedIds()));
$("histDelAll").addEventListener("click", () => deleteRuns([], true));

// ---------------------------------------------------------------- chuyển chế độ & khởi động
function setMode(m) {
  MODE = m === "replay" ? "replay" : "bt";
  try { localStorage.setItem("mixMode", MODE); } catch (e) {}
  document.querySelectorAll("[data-setmode]").forEach((b) => b.classList.toggle("active", b.dataset.setmode === MODE));
  $("btForm").hidden = MODE !== "bt";
  $("replayForm").hidden = MODE !== "replay";
  // phần tóm tắt / báo lỗi của thanh chạy chỉ dành cho form backtest
  document.querySelectorAll(".formbt").forEach((x) => (x.hidden = MODE !== "bt"));
  $("runBtn").textContent = MODE === "bt" ? "Chạy backtest" : "Chạy replay";
  if (MODE === "bt" && typeof changed === "function" && META) changed();
}
document.querySelectorAll("[data-setmode]").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.setmode)));
$("queue").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-q]"); if (!b) return;
  const H = { "Content-Type": "application/json" };
  try {
    if (b.dataset.q === "x") await api("/api/cancel", { method: "POST", headers: H, body: JSON.stringify({ id: b.dataset.id }) });
    else await api(`/api/queue/${encodeURIComponent(b.dataset.id)}/move`, { method: "POST", headers: H, body: JSON.stringify({ delta: +b.dataset.q }) });
  } catch (err) { alert(err.message); }
  poll();
});

restoreForm();
syncSlip();
$("autoSlip").addEventListener("change", syncSlip);
setMode(MODE);
loadHistory();
initConfig();                                  // config.js: nạp sàn / cấu hình mẫu cho chế độ backtest
loadFiles().catch(() => {});
api("/api/status").then((s) => {
  if (s.status === "running" || (s.queue && s.queue.length)) {
    $("prog").hidden = false; $("log").hidden = false;
    polling = setInterval(poll, 700); poll();
  } else if (s.run_id) loadResult(s.run_id);
}).catch(() => {});
