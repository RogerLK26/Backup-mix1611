"use strict";
/* Form cấu hình chiến lược (chế độ backtest) – đầy đủ tham số như bản "Backtest Strategy", nhưng gọn trong
   một bảng khai báo: mỗi ô là một dòng {k: đường dẫn trong YAML, label, type}. Nhờ vậy đọc/ghi cấu hình,
   lưu preset YAML và "dùng lại cấu hình" của lịch sử đều dùng chung một chỗ. */

const TF = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1D"];
const WEEKDAYS = [["mon", "T2"], ["tue", "T3"], ["wed", "T4"], ["thu", "T5"], ["fri", "T6"], ["sat", "T7"], ["sun", "CN"]];
let META = null, PRESETS_LIST = [], SIGNALS = [], EXTRA_SWEEP = [];

// type: num | text | check | select | dur (30s/15m/2h) | list (danh sách cách nhau dấu phẩy) | datetime
const SECTIONS = [
  {
    id: "chung", title: "1. Chiến lược, sàn & giai đoạn", open: true, fields: [
      { k: "name", label: "Tên chiến lược", type: "text", w: 2 },
      { k: "start", label: "Từ", type: "datetime", hint: "theo múi giờ bên dưới" },
      { k: "end", label: "Đến", type: "datetime" },
      { k: "timezone", label: "Múi giờ", type: "select", opts: ["Asia/Ho_Chi_Minh", "UTC"] },
      { k: "initial_balance", label: "Vốn ban đầu (USDT)", type: "num" },
      { k: "data.resolution", label: "Độ phân giải", type: "select", opts: [["auto", "Tự động (tick khi có)"], ["tick", "Bắt buộc tick"], ["1m", "Chỉ nến 1m (nhanh, kém chính xác)"]] },
      { k: "data.intrabar_fallback", label: "Khi chỉ có nến 1m", type: "select", opts: [["conservative", "Bất lợi (an toàn)"], ["ohlc", "Theo thứ tự O-H-L-C"], ["optimistic", "Thuận lợi"]] },
      { k: "data.workers", label: "Số luồng tải dữ liệu", type: "num" },
    ],
  },
  {
    id: "coin", title: "2. Lọc coin", fields: [
      { k: "universe.symbols", label: "Chỉ chạy các coin (để trống = tất cả)", type: "list", w: 2, ph: "PEPEUSDT, WIFUSDT" },
      { k: "universe.exclude", label: "Loại trừ coin", type: "list", w: 2, ph: "BTCUSDT, ETHUSDT" },
      { k: "universe.min_volume_24h", label: "Volume 24h tối thiểu (USDT)", type: "num", hint: "tính rolling tới đúng phút có tín hiệu" },
      { k: "universe.max_volume_24h", label: "Volume 24h tối đa", type: "num" },
      { k: "universe.min_max_leverage", label: "Đòn bẩy tối đa của sàn ≥", type: "num", hint: "bỏ coin sàn chỉ cho đòn bẩy thấp" },
      { k: "universe.min_market_cap", label: "Vốn hoá tối thiểu (USD)", type: "num" },
      { k: "universe.max_market_cap", label: "Vốn hoá tối đa (USD)", type: "num" },
      { k: "universe.market_cap_source", label: "Nguồn vốn hoá", type: "select", opts: [["none", "Không lọc"], ["supply", "Cung lưu hành × giá (nhanh)"], ["coingecko", "CoinGecko theo ngày (chậm)"]] },
      { k: "universe.unknown_market_cap", label: "Coin không tra được vốn hoá", type: "select", opts: [["exclude", "Bỏ qua"], ["include", "Vẫn chạy"]] },
      { k: "universe.coingecko_api_key", label: "CoinGecko API key", type: "text" },
    ],
  },
  {
    id: "entry", title: "4. Vào lệnh & vị thế", fields: [
      { k: "entry.latency_ms", label: "Độ trễ bot (ms)", type: "num", hint: "từ lúc chạm ngưỡng tới lúc lệnh tới sàn" },
      { k: "entry.slippage_pct", label: "Trượt giá vào lệnh (%)", type: "num" },
      { k: "position.margin", label: "Ký quỹ mỗi lệnh", type: "num", hint: "USDT, hoặc % số dư nếu chọn bên cạnh" },
      { k: "position.margin_mode", label: "Cách tính ký quỹ", type: "select", opts: [["fixed", "USDT cố định"], ["percent", "% số dư"]] },
      { k: "position.leverage", label: "Đòn bẩy", type: "num" },
    ],
  },
  {
    id: "exit", title: "5. Thoát lệnh & phí", fields: [
      { k: "exit.unit", label: "TP/SL tính theo", type: "select", opts: [["price", "% giá"], ["roe", "% ROE (đã nhân đòn bẩy)"]] },
      { k: "exit.tp_order", label: "Lệnh chốt lời", type: "select", opts: [["limit", "Limit (phí maker)"], ["market", "Market (phí taker)"]] },
      { k: "exit.tp_fill", label: "Khớp limit TP", type: "select", opts: [["touch", "Chạm là khớp"], ["through", "Phải vượt qua (khắt khe)"]] },
      { k: "exit.sl_slippage_pct", label: "Trượt giá khi cắt lỗ (%)", type: "num" },
      { k: "exit.exit_slippage_pct", label: "Trượt giá khi đóng hết giờ (%)", type: "num" },
      { k: "exit.liquidation", label: "Mô phỏng thanh lý (isolated)", type: "check" },
      { k: "exit.maintenance_margin_pct", label: "Ký quỹ duy trì (%)", type: "num" },
      { k: "fees.maker_pct", label: "Phí maker (%)", type: "num" },
      { k: "fees.taker_pct", label: "Phí taker (%)", type: "num" },
      { k: "fees.funding", label: "Tính phí funding thật", type: "check" },
      { k: "exit.take_profit", label: "Chốt lời (%) – khi không quét lưới", type: "num", hint: "để trống nếu lưới bên dưới đã có TP" },
      { k: "exit.stop_loss", label: "Cắt lỗ (%) – khi không quét lưới", type: "num" },
      { k: "exit.max_hold", label: "Giữ tối đa – khi không quét lưới", type: "dur", ph: "2h" },
    ],
  },
  {
    id: "limits", title: "6. Giới hạn rủi ro", fields: [
      { k: "limits.max_open_positions", label: "Lệnh mở đồng thời tối đa", type: "num" },
      { k: "limits.max_open_per_coin", label: "Lệnh mở tối đa / coin", type: "num" },
      { k: "limits.max_trades_per_day", label: "Lệnh tối đa / ngày", type: "num" },
      { k: "limits.max_tp_per_day", label: "Số TP tối đa / ngày", type: "num" },
      { k: "limits.max_sl_per_day", label: "Số SL tối đa / ngày", type: "num" },
      { k: "limits.max_tp_per_coin_per_day", label: "TP tối đa / coin / ngày", type: "num" },
      { k: "limits.max_sl_per_coin_per_day", label: "SL tối đa / coin / ngày", type: "num" },
      { k: "limits.cooldown_after_tp_coin", label: "Nghỉ coin sau TP", type: "dur", ph: "10m" },
      { k: "limits.cooldown_after_sl_coin", label: "Nghỉ coin sau SL", type: "dur", ph: "30m" },
      { k: "limits.cooldown_after_trade_coin", label: "Nghỉ coin sau mọi lệnh", type: "dur" },
      { k: "limits.cooldown_after_sl_global", label: "Nghỉ cả chiến lược sau SL", type: "dur" },
      { k: "limits.max_consecutive_sl", label: "Số SL liên tiếp tối đa", type: "num" },
      { k: "limits.pause_after_consecutive_sl", label: "Nghỉ sau khi chạm số SL liên tiếp", type: "dur" },
      { k: "limits.daily_loss_limit", label: "Giới hạn lỗ / ngày (USDT)", type: "num" },
      { k: "limits.max_runtime", label: "Thời gian chạy tối đa mỗi ngày", type: "dur" },
      { k: "limits.day_reset_hour", label: "Giờ bắt đầu ngày mới", type: "num" },
      { k: "limits.timeout_counts_as", label: "Lệnh hết giờ tính là", type: "select", opts: [["pnl", "TP/SL theo lãi lỗ"], ["none", "Không tính"], ["tp", "TP"], ["sl", "SL"]] },
    ],
  },
];

const path = (o, k) => k.split(".").reduce((x, p) => (x == null ? undefined : x[p]), o);
function setPath(o, k, v) {
  const ks = k.split(".");
  let cur = o;
  ks.slice(0, -1).forEach((p) => { if (typeof cur[p] !== "object" || cur[p] === null) cur[p] = {}; cur = cur[p]; });
  cur[ks[ks.length - 1]] = v;
}
const fid = (k) => "f_" + k.replace(/\./g, "_");

function fieldHtml(f) {
  const id = fid(f.k), ph = f.ph ? ` placeholder="${esc(f.ph)}"` : "";
  let input;
  if (f.type === "check") input = `<label class="chk"><input type="checkbox" id="${id}"> ${esc(f.label)}</label>`;
  else if (f.type === "select") {
    const opts = f.opts.map((o) => (Array.isArray(o) ? o : [o, o])).map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("");
    input = `<label for="${id}">${esc(f.label)}</label><select id="${id}">${opts}</select>`;
  } else {
    const t = f.type === "num" ? "number" : f.type === "datetime" ? "text" : "text";
    const step = f.type === "num" ? ' step="any"' : "";
    input = `<label for="${id}">${esc(f.label)}</label><input type="${t}" id="${id}"${step}${ph}>`;
  }
  return `<div class="fld${f.w ? " w" + f.w : ""}">${input}${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""}</div>`;
}

function sectionHtml(s) {
  return `<details class="sec" ${s.open ? "open" : ""}><summary>${esc(s.title)}</summary>
    <div class="grid">${s.fields.map(fieldHtml).join("")}</div></details>`;
}

// ---------------------------------------------------------------- tín hiệu (nhiều rule, HOẶC)
function signalHtml(s, i) {
  const opt = (v, list, cur) => list.map((o) => { const [val, txt] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(val)}"${String(cur) === String(val) ? " selected" : ""}>${esc(txt)}</option>`; }).join("");
  return `<div class="sigrow" data-i="${i}">
    <div class="fld"><label>Khung nến</label><select data-s="timeframe">${opt("", TF, s.timeframe || "5m")}</select></div>
    <div class="fld"><label>Biến động ≥ (%)</label><input type="number" step="any" data-s="change_pct" value="${s.change_pct ?? 3}"></div>
    <div class="fld"><label>Tối đa (%)</label><input type="number" step="any" data-s="max_change_pct" value="${s.max_change_pct ?? ""}" placeholder="không giới hạn"></div>
    <div class="fld"><label>Chiều biến động</label><select data-s="direction">${opt("", [["up", "Tăng"], ["down", "Giảm"], ["both", "Cả hai"]], s.direction || "up")}</select></div>
    <div class="fld"><label>Kích hoạt</label><select data-s="trigger">${opt("", [["touch", "Chạm ngưỡng"], ["close", "Nến đóng"]], s.trigger || "touch")}</select></div>
    <div class="fld"><label>Vào lệnh</label><select data-s="side">${opt("", [["reverse", "Ngược chiều"], ["follow", "Thuận chiều"], ["long", "Luôn Long"], ["short", "Luôn Short"]], s.side || "reverse")}</select></div>
    <div class="fld"><label>Chờ hồi (%)</label><input type="number" step="any" data-s="pullback_pct" value="${s.pullback_pct ?? ""}" placeholder="không chờ"></div>
    <div class="fld"><label>Theo dõi hồi trong</label><input type="text" data-s="pullback_window" value="${esc(s.pullback_window ?? "")}" placeholder="10m"></div>
    <button class="btn small ghost danger sigdel" title="Xoá tín hiệu">✕</button></div>`;
}
function renderSignals() {
  $("signals").innerHTML = SIGNALS.map(signalHtml).join("");
}
function readSignals() {
  return [...document.querySelectorAll("#signals .sigrow")].map((row) => {
    const o = {};
    row.querySelectorAll("[data-s]").forEach((el) => {
      const k = el.dataset.s, v = el.value.trim();
      if (v === "") { if (!["max_change_pct", "pullback_pct", "pullback_window"].includes(k)) return; o[k] = null; }
      else o[k] = ["change_pct", "max_change_pct", "pullback_pct"].includes(k) ? +v : v;
    });
    if (o.pullback_pct == null) { delete o.pullback_pct; delete o.pullback_window; }
    if (o.max_change_pct == null) delete o.max_change_pct;
    return o;
  });
}

// ---------------------------------------------------------------- lưới & xếp hạng
const SWEEPABLE = [
  ["position.leverage", "Đòn bẩy"], ["position.margin", "Ký quỹ"], ["entry.latency_ms", "Độ trễ (ms)"],
  ["entry.slippage_pct", "Trượt giá vào (%)"], ["signals.0.change_pct", "Tín hiệu 1 – biến động (%)"],
  ["signals.1.change_pct", "Tín hiệu 2 – biến động (%)"], ["signals.0.pullback_pct", "Tín hiệu 1 – hồi (%)"],
  ["signals.0.pullback_window", "Tín hiệu 1 – theo dõi hồi"], ["limits.max_open_positions", "Lệnh mở đồng thời"],
  ["limits.max_open_per_coin", "Lệnh mở / coin"], ["limits.max_sl_per_day", "Max SL/ngày"],
  ["limits.cooldown_after_sl_coin", "Nghỉ coin sau SL"], ["limits.cooldown_after_tp_coin", "Nghỉ coin sau TP"],
  ["limits.cooldown_after_sl_global", "Nghỉ sau SL"], ["limits.day_reset_hour", "Giờ bắt đầu ngày"],
];
function renderExtraSweep() {
  $("extraSweep").innerHTML = EXTRA_SWEEP.map((x, i) => `<div class="sigrow" data-i="${i}">
    <div class="fld w2"><label>Tham số</label><select data-x="k">${SWEEPABLE.map(([v, t]) =>
      `<option value="${esc(v)}"${x.k === v ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
    <div class="fld w2"><label>Các giá trị muốn thử</label><input type="text" data-x="v" value="${esc(x.v || "")}" placeholder="3, 5, 10 hoặc 1-10:1"></div>
    <button class="btn small ghost danger xdel" title="Bỏ tham số">✕</button></div>`).join("");
}
function readExtraSweep() {
  const out = {};
  document.querySelectorAll("#extraSweep .sigrow").forEach((row) => {
    const k = row.querySelector('[data-x="k"]').value, v = row.querySelector('[data-x="v"]').value.trim();
    if (k && v) out[k] = v;
  });
  return out;
}

// ---------------------------------------------------------------- đọc / ghi cấu hình
function getConfig() {
  const raw = {};
  for (const s of SECTIONS) {
    for (const f of s.fields) {
      const el = $(fid(f.k));
      if (!el) continue;
      let v;
      if (f.type === "check") v = el.checked;
      else {
        const t = el.value.trim();
        if (t === "") v = null;
        else if (f.type === "num") v = Number(t);
        else if (f.type === "list") v = t.split(/[,;\s]+/).filter(Boolean);
        else v = t;
      }
      if (f.type === "list" && v === null) v = [];
      setPath(raw, f.k, v);
    }
  }
  raw.exchange = selectedExchanges();
  raw.signals = readSignals();
  raw.schedule = {
    sessions: ($("f_schedule_sessions").value.trim() || "00:00-24:00").split(/[,;]+/).map((x) => x.trim()).filter(Boolean),
    weekdays: [...document.querySelectorAll("#weekdays input:checked")].map((x) => x.value),
  };
  const sw = {};
  const g = { "exit.take_profit": $("swTp").value.trim(), "exit.stop_loss": $("swSl").value.trim(), "exit.max_hold": $("swHold").value.trim() };
  for (const [k, v] of Object.entries(g)) if (v) sw[k] = v;
  Object.assign(sw, readExtraSweep());
  raw.sweep = sw;
  raw.optimize = {
    objective: $("objective").value, objective2: $("objective2").value || null,
    min_trades: +$("minTrades").value || 0,
    max_drawdown_pct: $("maxDd").value.trim() === "" ? null : +$("maxDd").value,
    oos_pct: +$("oosPct").value || 0, max_combos: +$("maxCombos").value || 20000,
  };
  return raw;
}

function applyConfig(raw, exchanges) {
  raw = raw || {};
  for (const s of SECTIONS) {
    for (const f of s.fields) {
      const el = $(fid(f.k));
      if (!el) continue;
      let v = path(raw, f.k);
      if (f.type === "check") el.checked = v === undefined ? true : !!v;
      else if (f.type === "list") el.value = Array.isArray(v) ? v.join(", ") : (v || "");
      else el.value = v === null || v === undefined ? "" : String(v);
    }
  }
  const exs = exchanges || raw.exchange || ["binance"];
  const list = Array.isArray(exs) ? exs : String(exs).split(",").map((x) => x.trim());
  document.querySelectorAll("#exchanges input").forEach((x) => (x.checked = list.includes(x.value)));
  SIGNALS = (raw.signals && raw.signals.length ? raw.signals : [{ timeframe: "5m", change_pct: 3, direction: "up", trigger: "touch", side: "reverse" }]).map((x) => ({ ...x }));
  renderSignals();
  const sch = raw.schedule || {};
  $("f_schedule_sessions").value = (sch.sessions || ["00:00-24:00"]).join(", ");
  const wd = sch.weekdays || WEEKDAYS.map((w) => w[0]);
  document.querySelectorAll("#weekdays input").forEach((x) => (x.checked = wd.includes(x.value)));
  const sw = raw.sweep || {};
  $("swTp").value = sweepTxt(sw["exit.take_profit"]);
  $("swSl").value = sweepTxt(sw["exit.stop_loss"]);
  $("swHold").value = sweepTxt(sw["exit.max_hold"]);
  EXTRA_SWEEP = Object.entries(sw).filter(([k]) => !["exit.take_profit", "exit.stop_loss", "exit.max_hold"].includes(k))
    .map(([k, v]) => ({ k, v: sweepTxt(v) }));
  renderExtraSweep();
  const o = raw.optimize || {};
  $("objective").value = o.objective || "net_pnl";
  $("objective2").value = o.objective2 || "";
  $("minTrades").value = o.min_trades ?? 10;
  $("maxDd").value = o.max_drawdown_pct ?? "";
  $("oosPct").value = o.oos_pct ?? 0;
  $("maxCombos").value = o.max_combos ?? 20000;
  countBtCombos();
  saveDraft();
}
const sweepTxt = (v) => {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return `${v.from}-${v.to}:${v.step}`;
  return String(v);
};

function selectedExchanges() {
  return [...document.querySelectorAll("#exchanges input:checked")].map((x) => x.value);
}

// số cấu hình của lưới (dùng chung hàm đếm với chế độ replay)
function countBtCombos() {
  let n = 1, keys = 0;
  for (const id of ["swTp", "swSl", "swHold"]) {
    const v = $(id).value.trim();
    if (v) { n *= parseList(v); keys++; }
  }
  document.querySelectorAll('#extraSweep [data-x="v"]').forEach((el) => {
    if (el.value.trim()) { n *= parseList(el.value); keys++; }
  });
  const max = +$("maxCombos").value || 20000;
  $("btComboCount").innerHTML = keys
    ? `<b>${n.toLocaleString("vi-VN")}</b> cấu hình × ${selectedExchanges().length || 0} sàn` +
      (n > max ? ` <span class="warn">– lớn hơn giới hạn ${max.toLocaleString("vi-VN")}: bot sẽ tìm kiếm thông minh (lấy mẫu + tinh chỉnh quanh nhóm tốt nhất)</span>` : "")
    : `<span class="neg">Chưa có lưới – nhập ít nhất một trong TP / SL / thời gian giữ lệnh</span>`;
}

function btRequest() {
  const config = getConfig();
  const exchanges = selectedExchanges();
  if (!exchanges.length) { alert("Chọn ít nhất 1 sàn"); return null; }
  if (!config.signals.length) { alert("Cần ít nhất 1 tín hiệu"); return null; }
  if (!Object.keys(config.sweep).length) { alert("Nhập lưới TP / SL / thời gian giữ lệnh ở mục 7"); return null; }
  saveDraft();
  return { kind: "bt", config, exchanges, grid: config.sweep };
}

// ---------------------------------------------------------------- preset (file YAML trong configs/)
async function loadPresets(select) {
  PRESETS_LIST = await api("/api/presets");
  $("preset").innerHTML = `<option value="">— Cấu hình đang nhập —</option>` +
    PRESETS_LIST.map((p) => `<option value="${esc(p.file)}">${esc(p.name)}${p.sweep ? "" : " (chưa có lưới)"}</option>`).join("");
  if (select) $("preset").value = select;
}
async function openPreset(file) {
  if (!file) return;
  try { applyConfig(await api(`/api/presets/${encodeURIComponent(file)}`)); }
  catch (e) { alert(e.message); }
}
async function savePreset(asNew) {
  const cfg = getConfig();
  const cur = $("preset").value;
  try {
    if (asNew || !cur) {
      const name = prompt("Tên file cấu hình mới:", (cfg.name || "chien_luoc").replace(/\s+/g, "_"));
      if (!name) return;
      const r = await api("/api/presets", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ file: name, config: cfg }) });
      await loadPresets(r.file);
    } else {
      await api(`/api/presets/${encodeURIComponent(cur)}`, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg) });
      await loadPresets(cur);
    }
    toast("Đã lưu cấu hình");
  } catch (e) { alert(e.message); }
}
async function deletePreset() {
  const cur = $("preset").value;
  if (!cur || !confirm(`Xoá cấu hình "${cur}"?`)) return;
  await api(`/api/presets/${encodeURIComponent(cur)}`, { method: "DELETE" });
  await loadPresets("");
}
async function exportYaml() {
  const d = await api("/api/yaml/dump", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(getConfig()) });
  const blob = new Blob([d.yaml], { type: "text/yaml" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = ((getConfig().name || "chien_luoc") + ".yaml").replace(/\s+/g, "_");
  a.click();
}
async function importYaml(file) {
  const text = await file.text();
  try {
    const d = await api("/api/yaml/parse", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }) });
    applyConfig(d.config);
    toast("Đã nạp cấu hình từ file YAML");
  } catch (e) { alert(e.message); }
}

// bản nháp tự lưu trên trình duyệt
function saveDraft() {
  try { localStorage.setItem("btDraft", JSON.stringify({ config: getConfig(), exchanges: selectedExchanges() })); } catch (e) {}
}

// ---------------------------------------------------------------- dựng form
async function initConfig() {
  META = await api("/api/meta");
  $("exchanges").innerHTML = META.exchanges.map((e) => `<label class="chk" title="Nến 1m: ${esc(e.klines)} · Tick: ${esc(e.ticks)}">
    <input type="checkbox" value="${e.id}"> ${esc(e.name)}</label>`).join("");
  // mỗi mục được đặt vào đúng chỗ của nó trong index.html (xen giữa là tín hiệu, lịch chạy, lưới cấu hình)
  SECTIONS.forEach((s) => { const el = $("sec_" + s.id); if (el) el.innerHTML = sectionHtml(s); });
  $("objective").innerHTML = Object.entries(META.objectives).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("objective2").innerHTML = `<option value="">— không dùng —</option>` +
    Object.entries(META.objectives).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("weekdays").innerHTML = WEEKDAYS.map(([v, t]) => `<label class="chk"><input type="checkbox" value="${v}" checked> ${t}</label>`).join("");
  let draft = null;
  try { draft = JSON.parse(localStorage.getItem("btDraft") || "null"); } catch (e) {}
  applyConfig(draft ? draft.config : (META.default || {}), draft ? draft.exchanges : null);
  await loadPresets();

  $("btForm").addEventListener("input", () => { countBtCombos(); saveDraft(); });
  $("signals").addEventListener("click", (e) => {
    if (!e.target.classList.contains("sigdel")) return;
    SIGNALS = readSignals();
    SIGNALS.splice(+e.target.closest(".sigrow").dataset.i, 1);
    if (!SIGNALS.length) SIGNALS = [{ timeframe: "5m", change_pct: 3, direction: "up", trigger: "touch", side: "reverse" }];
    renderSignals();
  });
  $("sigAdd").addEventListener("click", () => {
    SIGNALS = readSignals();
    SIGNALS.push({ ...(SIGNALS[SIGNALS.length - 1] || { timeframe: "5m", change_pct: 3, direction: "up", trigger: "touch", side: "reverse" }) });
    renderSignals();
  });
  $("extraSweep").addEventListener("click", (e) => {
    if (!e.target.classList.contains("xdel")) return;
    EXTRA_SWEEP = readExtraSweepRows();
    EXTRA_SWEEP.splice(+e.target.closest(".sigrow").dataset.i, 1);
    renderExtraSweep(); countBtCombos();
  });
  $("xAdd").addEventListener("click", () => {
    EXTRA_SWEEP = readExtraSweepRows();
    EXTRA_SWEEP.push({ k: SWEEPABLE[0][0], v: "" });
    renderExtraSweep();
  });
  $("preset").addEventListener("change", (e) => openPreset(e.target.value));
  $("presetSave").addEventListener("click", () => savePreset(false));
  $("presetNew").addEventListener("click", () => savePreset(true));
  $("presetDel").addEventListener("click", deletePreset);
  $("yamlOut").addEventListener("click", exportYaml);
  $("yamlIn").addEventListener("change", (e) => { if (e.target.files[0]) importYaml(e.target.files[0]); e.target.value = ""; });
  document.querySelectorAll("[data-gridpreset]").forEach((b) => b.addEventListener("click", () => {
    const v = { quick: ["0.5, 1, 1.5, 2, 3", "2, 3, 5, 10", "30m, 1h, 2h"],
      wide: ["0.5-5:0.25", "1-20:1", "15m, 30m, 1h, 2h, 4h, 0"],
      tp: ["0.3-3:0.1", "", ""] }[b.dataset.gridpreset];
    $("swTp").value = v[0]; $("swSl").value = v[1]; $("swHold").value = v[2];
    countBtCombos(); saveDraft();
  }));
  countBtCombos();
}
function readExtraSweepRows() {
  return [...document.querySelectorAll("#extraSweep .sigrow")].map((row) => ({
    k: row.querySelector('[data-x="k"]').value, v: row.querySelector('[data-x="v"]').value }));
}
