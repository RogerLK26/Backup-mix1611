"use strict";
/* Form cấu hình chiến lược (chế độ backtest).

   Bố cục: **Cơ bản** (mọi thứ cần để chạy, nhìn hết trong một màn) + **Nâng cao** (phần còn lại, chia nhóm, mỗi nhóm
   báo đang đặt khác mặc định mấy mục). Tất cả các ô đều khai báo trong một bảng duy nhất `FIELDS`
   {k: đường dẫn trong YAML, label, type, d: mặc định} nên đọc/ghi cấu hình, lưu preset YAML, "dùng lại cấu hình"
   của lịch sử đều dùng chung một chỗ – thêm một tham số chỉ là thêm một dòng. */

// Danh sách giá trị hợp lệ do server (engine) cung cấp – ở đây chỉ có nhãn tiếng Việt cho từng giá trị,
// nên không bao giờ chào được giá trị mà engine không nhận.
let TF = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"];
const WEEKDAYS = [["mon", "T2"], ["tue", "T3"], ["wed", "T4"], ["thu", "T5"], ["fri", "T6"], ["sat", "T7"], ["sun", "CN"]];
const VI = {
  "exit.unit": { price: "% giá", roe: "% ROE (đã nhân đòn bẩy)" },
  "exit.tp_order": { limit: "Limit (phí maker)", market: "Market (phí taker)" },
  "exit.tp_fill": { touch: "Chạm là khớp", through: "Phải vượt qua (khắt khe)" },
  "position.margin_mode": { fixed: "USDT", percent: "% số dư" },
  "data.resolution": { auto: "Tự động (dùng tick khi có)", tick: "Bắt buộc tick", "1m": "Chỉ nến 1m (nhanh, kém chính xác)" },
  "data.intrabar_fallback": { conservative: "Bất lợi (an toàn)", ohlc: "Theo thứ tự O-H-L-C", optimistic: "Thuận lợi" },
  "limits.timeout_counts_as": { pnl: "Có – lãi tính là TP, lỗ tính là SL", none: "Không tính" },
  "universe.market_cap_source": { none: "Không lọc", supply: "Cung lưu hành × giá lúc có tín hiệu (nhanh)",
    coingecko: "CoinGecko theo từng ngày (chậm)", current: "Vốn hoá hiện tại của CoinGecko" },
  "universe.unknown_market_cap": { exclude: "Bỏ qua", include: "Vẫn chạy" },
  direction: { up: "tăng", down: "giảm", both: "tăng hoặc giảm" },
  trigger: { touch: "ngay khi chạm ngưỡng", close: "khi nến đóng" },
  side: { reverse: "ngược chiều", follow: "thuận chiều", long: "Long", short: "Short" },
};
const viOpts = (key, values) => (values || []).map((v) => [v, (VI[key] || {})[v] || v]);
const DEF_SIGNAL = { timeframe: "5m", change_pct: 3, direction: "up", trigger: "touch", side: "reverse" };
let META = null, PRESETS_LIST = [], SIGNALS = [], EXTRA_SWEEP = [], CURRENT_RAW = {}, vTimer = null;

// type: num | text | check | select | dur (30s/15m/2h) | list (danh sách cách nhau dấu phẩy) | datetime
// d: giá trị mặc định của engine – dùng để báo "đang đặt khác mặc định" ở mỗi nhóm Nâng cao
const GROUPS = [
  {
    id: "coin", title: "Lọc coin", hint: "Bỏ trống hết = chạy mọi coin USDT perpetual của sàn.", fields: [
      { k: "universe.symbols", label: "Chỉ chạy các coin", type: "list", w: 2, ph: "PEPEUSDT, WIFUSDT – trống = tất cả", d: [] },
      { k: "universe.exclude", label: "Loại trừ coin", type: "list", w: 2, ph: "BTCUSDT, ETHUSDT", d: [] },
      { k: "universe.min_volume_24h", label: "Volume 24h ≥ (USDT)", type: "num", hint: "tính tới đúng phút có tín hiệu", d: null },
      { k: "universe.max_volume_24h", label: "Volume 24h ≤", type: "num", d: null },
      { k: "universe.min_max_leverage", label: "Đòn bẩy tối đa của sàn ≥", type: "num", hint: "bỏ coin sàn chỉ cho đòn bẩy thấp", d: null },
      { k: "universe.min_market_cap", label: "Vốn hoá ≥ (USD)", type: "num", d: null },
      { k: "universe.max_market_cap", label: "Vốn hoá ≤ (USD)", type: "num", d: null },
      { k: "universe.market_cap_source", label: "Nguồn vốn hoá", type: "select", d: "supply", opts: [] },
      { k: "universe.unknown_market_cap", label: "Coin không tra được vốn hoá", type: "select", d: "exclude", opts: [] },
      { k: "universe.coingecko_api_key", label: "CoinGecko API key", type: "text", d: "" },
    ],
  },
  {
    id: "entry", title: "Vào lệnh", hint: "Mô phỏng độ trễ và trượt giá lúc bot đặt lệnh.", fields: [
      { k: "entry.latency_ms", label: "Độ trễ bot (ms)", type: "num", hint: "từ lúc chạm ngưỡng tới khi lệnh tới sàn", d: 0 },
      { k: "entry.slippage_pct", label: "Trượt giá vào lệnh (%)", type: "num", d: 0 },
    ],
  },
  {
    id: "exit", title: "Thoát lệnh & phí", hint: "TP/SL lấy từ lưới ở phần Cơ bản; đây là cách khớp và phí.", fields: [
      { k: "exit.unit", label: "TP/SL tính theo", type: "select", d: "price", opts: [] },
      { k: "exit.tp_order", label: "Lệnh chốt lời", type: "select", d: "limit", opts: [] },
      { k: "exit.tp_fill", label: "Khớp limit TP", type: "select", d: "touch", opts: [] },
      { k: "exit.sl_slippage_pct", label: "Trượt giá khi cắt lỗ (%)", type: "num", d: 0 },
      { k: "exit.exit_slippage_pct", label: "Trượt giá khi đóng hết giờ (%)", type: "num", d: 0 },
      { k: "fees.maker_pct", label: "Phí maker (%)", type: "num", d: 0.02 },
      { k: "fees.taker_pct", label: "Phí taker (%)", type: "num", d: 0.05 },
      { k: "fees.funding", label: "Tính phí funding thật", type: "check", d: true },
      { k: "exit.liquidation", label: "Mô phỏng thanh lý (isolated)", type: "check", d: true },
      { k: "exit.maintenance_margin_pct", label: "Ký quỹ duy trì (%)", type: "num", d: 0.5 },
    ],
  },
  {
    id: "limits", title: "Giới hạn rủi ro", hint: "Bỏ trống = không giới hạn.", fields: [
      { k: "limits.max_open_positions", label: "Lệnh mở đồng thời tối đa", type: "num", d: null },
      { k: "limits.max_open_per_coin", label: "Lệnh mở tối đa / coin", type: "num", d: 1 },
      { k: "limits.max_trades_per_day", label: "Lệnh tối đa / ngày", type: "num", d: null },
      { k: "limits.max_tp_per_day", label: "Số TP tối đa / ngày", type: "num", d: null },
      { k: "limits.max_sl_per_day", label: "Số SL tối đa / ngày", type: "num", d: null },
      { k: "limits.max_tp_per_coin_per_day", label: "TP tối đa / coin / ngày", type: "num", d: null },
      { k: "limits.max_sl_per_coin_per_day", label: "SL tối đa / coin / ngày", type: "num", d: null },
      { k: "limits.cooldown_after_tp_coin", label: "Nghỉ coin sau TP", type: "dur", ph: "10m", d: null },
      { k: "limits.cooldown_after_sl_coin", label: "Nghỉ coin sau SL", type: "dur", ph: "30m", d: null },
      { k: "limits.cooldown_after_trade_coin", label: "Nghỉ coin sau mọi lệnh", type: "dur", d: null },
      { k: "limits.cooldown_after_sl_global", label: "Nghỉ cả chiến lược sau SL", type: "dur", d: null },
      { k: "limits.max_consecutive_sl", label: "Số SL liên tiếp tối đa", type: "num", d: null },
      { k: "limits.pause_after_consecutive_sl", label: "Nghỉ khi chạm số SL liên tiếp", type: "dur", d: null },
      { k: "limits.daily_loss_limit", label: "Giới hạn lỗ / ngày (USDT)", type: "num", d: null },
      { k: "limits.max_runtime", label: "Thời gian chạy tối đa mỗi ngày", type: "dur", d: null },
      { k: "limits.day_reset_hour", label: "Giờ bắt đầu ngày mới", type: "num", d: 0 },
      { k: "limits.timeout_counts_as", label: "Lệnh hết giờ tính vào bộ đếm TP/SL", type: "select", d: "pnl", opts: [] },
    ],
  },
  {
    id: "schedule", title: "Lịch chạy", hint: "Mặc định chạy mọi giờ, mọi ngày.", fields: [
      { k: "schedule.sessions", label: "Khung giờ vào lệnh", type: "list", w: 2, ph: "00:00-24:00, 22:00-02:00",
        hint: "nhiều khoảng cách nhau dấu phẩy, hỗ trợ khung qua đêm", d: ["00:00-24:00"] },
    ],
    weekdays: true,
  },
  {
    id: "rank", title: "Xếp hạng & kiểm tra ngoài mẫu", hint: "Cách chọn cấu hình tốt nhất trong lưới.", fields: [
      { k: "optimize.objective", label: "Xếp hạng theo", type: "select", opts: [], d: "net_pnl" },
      { k: "optimize.objective2", label: "Tiêu chí thứ 2", type: "select", opts: [], d: null },
      { k: "optimize.min_trades", label: "Bỏ qua cấu hình ít hơn … lệnh", type: "num", d: 10 },
      { k: "optimize.max_drawdown_pct", label: "Bỏ qua nếu sụt vốn quá (%)", type: "num", ph: "không giới hạn", d: null },
      { k: "optimize.oos_pct", label: "Kiểm tra ngoài mẫu (% cuối giai đoạn)", type: "num",
        hint: "xếp hạng trên phần đầu, xem kết quả phần cuối", d: 0 },
      { k: "optimize.max_combos", label: "Số cấu hình tối đa", type: "num",
        hint: "lưới lớn hơn thì bot tìm kiếm thông minh", d: 20000 },
    ],
  },
  {
    id: "data", title: "Dữ liệu & thông tin chung", fields: [
      { k: "name", label: "Tên chiến lược", type: "text", w: 2, d: "strategy" },
      { k: "timezone", label: "Múi giờ", type: "select", opts: ["Asia/Ho_Chi_Minh", "UTC"], d: "Asia/Ho_Chi_Minh" },
      { k: "initial_balance", label: "Vốn ban đầu (USDT)", type: "num", d: 1000 },
      { k: "data.resolution", label: "Độ phân giải", type: "select", d: "auto", opts: [] },
      { k: "data.intrabar_fallback", label: "Khi chỉ có nến 1m", type: "select", d: "conservative", opts: [] },
      { k: "data.workers", label: "Số luồng tải dữ liệu", type: "num", d: 8 },
    ],
  },
];
// các ô của phần Cơ bản (đặt trong khung riêng, xem index.html)
const BASIC = [
  { k: "position.margin", label: "Ký quỹ mỗi lệnh", type: "num", d: 10 },
  { k: "position.margin_mode", label: "Tính theo", type: "select", d: "fixed", opts: [] },
  { k: "position.leverage", label: "Đòn bẩy", type: "num", d: 10 },
];
const ALL_FIELDS = () => BASIC.concat(...GROUPS.map((g) => g.fields));

const enumOf = (field) => (META && META.enums && META.enums.signal[field]) ||
  ({ direction: ["up", "down", "both"], trigger: ["touch", "close"], side: ["long", "short", "follow", "reverse"] })[field];
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
  if (f.type === "check") {
    return `<div class="fld chkfld"><label class="chk"><input type="checkbox" id="${id}"> ${esc(f.label)}</label>
      ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""}</div>`;
  }
  const input = f.type === "select"
    ? `<select id="${id}">${(f.opts || []).map((o) => (Array.isArray(o) ? o : [o, o]))
        .map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>`
    : `<input type="${f.type === "num" ? "number" : "text"}" id="${id}"${f.type === "num" ? ' step="any"' : ""}${ph}>`;
  return `<div class="fld${f.w ? " w" + f.w : ""}"><label for="${id}">${esc(f.label)}</label>${input}
    ${f.hint ? `<div class="hint">${esc(f.hint)}</div>` : ""}</div>`;
}

function groupHtml(g) {
  return `<details class="sec" id="sec_${g.id}"><summary>${esc(g.title)}<span class="badge-off" data-badge="${g.id}"></span></summary>
    ${g.hint ? `<p class="hint">${esc(g.hint)}</p>` : ""}
    <div class="grid">${g.fields.map(fieldHtml).join("")}</div>
    ${g.weekdays ? `<div class="fld" style="margin-top:8px"><label>Ngày trong tuần</label><div id="weekdays" class="chkrow"></div></div>` : ""}
  </details>`;
}

// "đang đặt khác mặc định mấy mục" của mỗi nhóm – để không ai bỏ sót thứ mình đã đổi rồi quên
const sameVal = (a, b) => {
  if (Array.isArray(a) || Array.isArray(b)) return JSON.stringify(a || []) === JSON.stringify(b || []);
  if (a === null || a === undefined || a === "") return b === null || b === undefined || b === "";
  if (typeof b === "number") return Number(a) === b;
  return String(a) === String(b);
};
function refreshBadges() {
  for (const g of GROUPS) {
    const n = g.fields.filter((f) => !sameVal(readField(f), f.d)).length;
    const el = document.querySelector(`[data-badge="${g.id}"]`);
    if (el) {
      el.textContent = n ? `${n} mục khác mặc định` : "";
      el.classList.toggle("on", !!n);
    }
  }
}

function readField(f) {
  const el = $(fid(f.k));
  if (!el) return undefined;
  if (f.type === "check") return el.checked;
  const t = el.value.trim();
  if (t === "") return f.type === "list" ? [] : null;
  if (f.type === "num") return Number(t);
  if (f.type === "list") return t.split(f.k === "schedule.sessions" ? /[,;]+/ : /[,;\s]+/).map((x) => x.trim()).filter(Boolean);
  return t;
}

// ---------------------------------------------------------------- tín hiệu: viết thành câu, chi tiết ẩn trong ⚙
function signalHtml(s, i) {
  const sel = (key, list, cur) => `<select data-s="${key}">` + list.map((o) => {
    const [v, t] = Array.isArray(o) ? o : [o, o];
    return `<option value="${esc(v)}"${String(cur) === String(v) ? " selected" : ""}>${esc(t)}</option>`;
  }).join("") + `</select>`;
  const open = s._open ? " open" : "";
  return `<div class="sigrow" data-i="${i}">
    <div class="sent">
      <span>Khi giá</span>
      ${sel("direction", viOpts("direction", enumOf("direction")), s.direction || "up")}
      <span>≥</span><input type="number" step="any" data-s="change_pct" value="${s.change_pct ?? 3}" class="num3">
      <span>% trong nến</span>
      ${sel("timeframe", TF, s.timeframe || "5m")}
      <span>→ vào lệnh</span>
      ${sel("side", viOpts("side", enumOf("side")), s.side || "reverse")}
      ${sel("trigger", viOpts("trigger", enumOf("trigger")), s.trigger || "touch")}
      <span class="spacer"></span>
      <button class="btn small ghost sigmore" title="Biến động tối đa, chờ giá hồi">⚙ Lọc thêm</button>
      <button class="btn small ghost danger sigdel" title="Xoá tín hiệu">✕</button>
    </div>
    <div class="sigmore-box"${open ? "" : " hidden"}>
      <div class="grid">
        <div class="fld"><label>Biến động tối đa (%)</label><input type="number" step="any" data-s="max_change_pct"
          value="${s.max_change_pct ?? ""}" placeholder="không giới hạn"></div>
        <div class="fld"><label>Chờ giá hồi (%)</label><input type="number" step="any" data-s="pullback_pct"
          value="${s.pullback_pct ?? ""}" placeholder="không chờ"></div>
        <div class="fld"><label>Theo dõi hồi trong</label><input type="text" data-s="pullback_window"
          value="${esc(s.pullback_window ?? "")}" placeholder="10m"></div>
      </div>
    </div></div>`;
}
function renderSignals() {
  $("signals").innerHTML = SIGNALS.map(signalHtml).join("");
  refreshSummary();
}
function readSignals() {
  return [...document.querySelectorAll("#signals .sigrow")].map((row) => {
    const o = { _open: !row.querySelector(".sigmore-box").hidden };
    row.querySelectorAll("[data-s]").forEach((el) => {
      const k = el.dataset.s, v = el.value.trim();
      if (v === "") return;
      o[k] = ["change_pct", "max_change_pct", "pullback_pct"].includes(k) ? +v : v;
    });
    return o;
  });
}
const cleanSignal = (s) => {
  const o = { ...s };
  delete o._open;
  if (o.pullback_pct == null) delete o.pullback_window;
  return o;
};
const signalTxt = (s) => `${s.timeframe} ${{ up: "tăng", down: "giảm", both: "±" }[s.direction] || ""} ≥${s.change_pct}%` +
  `${s.trigger === "close" ? " (nến đóng)" : ""}${s.pullback_pct ? ` (hồi ${s.pullback_pct}%)` : ""}` +
  ` → ${{ reverse: "ngược", follow: "thuận", long: "Long", short: "Short" }[s.side] || s.side}`;

// ---------------------------------------------------------------- lưới quét thêm tham số
// Tham số quét thêm: [khoá trong YAML, tên hiện trên giao diện, gợi ý giá trị].
// Nhóm tín hiệu sinh theo số tín hiệu đang có; mỗi giá trị của nhóm này phải quét lại tín hiệu nên chậm hơn TP/SL/giữ.
const SIG_SWEEP = [
  ["timeframe", "Khung nến", "1m, 5m, 15m, 1h, 4h, 1d"],
  ["change_pct", "Biến động ≥ (%)", "2, 3, 4, 5 hoặc 1-5:0.5"],
  ["max_change_pct", "Biến động tối đa (%)", "5, 10, none"],
  ["direction", "Chiều biến động", "up, down, both"],
  ["side", "Vào lệnh", "reverse, follow"],
  ["trigger", "Kích hoạt", "touch, close"],
  ["pullback_pct", "Chờ giá hồi (%)", "0.5, 1, 2, none"],
  ["pullback_window", "Theo dõi hồi", "5m, 10m, 30m"],
];
const OTHER_SWEEP = [
  ["position.leverage", "Đòn bẩy", "5, 10, 20"],
  ["position.margin", "Ký quỹ mỗi lệnh", "5, 10, 20"],
  ["entry.latency_ms", "Độ trễ bot (ms)", "0, 200, 500"],
  ["entry.slippage_pct", "Trượt giá vào (%)", "0, 0.05, 0.1"],
  ["exit.tp_order", "Lệnh chốt lời", "limit, market"],
  ["exit.sl_slippage_pct", "Trượt giá SL (%)", "0, 0.1, 0.5"],
  ["limits.max_open_positions", "Lệnh mở đồng thời", "3, 5, 10"],
  ["limits.max_open_per_coin", "Lệnh mở / coin", "1, 2"],
  ["limits.max_trades_per_day", "Lệnh / ngày", "5, 10, none"],
  ["limits.max_sl_per_day", "Max SL / ngày", "2, 3, none"],
  ["limits.max_tp_per_day", "Max TP / ngày", "2, 3, none"],
  ["limits.cooldown_after_sl_coin", "Nghỉ coin sau SL", "0, 10m, 30m"],
  ["limits.cooldown_after_tp_coin", "Nghỉ coin sau TP", "0, 10m, 30m"],
  ["limits.cooldown_after_trade_coin", "Nghỉ coin sau mọi lệnh", "0, 10m, 30m"],
  ["limits.cooldown_after_sl_global", "Nghỉ cả chiến lược sau SL", "0, 30m, 2h"],
  ["limits.daily_loss_limit", "Giới hạn lỗ / ngày", "50, 100, none"],
  ["limits.day_reset_hour", "Giờ bắt đầu ngày", "0, 7"],
];
function sweepable() {
  const n = Math.max(1, document.querySelectorAll("#signals .sigrow").length || SIGNALS.length || 1);
  const out = [];
  for (let i = 0; i < n; i++) {
    for (const [f, label, ph] of SIG_SWEEP) {
      out.push([`signals.${i}.${f}`, n > 1 ? `${label} – tín hiệu ${i + 1}` : label, ph]);
    }
  }
  return out.concat(OTHER_SWEEP);
}
const sweepPh = (k) => (sweepable().find(([key]) => key === k) || [])[2] || "3, 5, 10 hoặc 1-10:1";
function renderExtraSweep() {
  const opts = sweepable();
  $("extraSweep").innerHTML = EXTRA_SWEEP.map((x, i) => `<div class="xrow" data-i="${i}">
    <select data-x="k">${opts.map(([v, t]) => `<option value="${esc(v)}"${x.k === v ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>
    <input type="text" data-x="v" value="${esc(x.v || "")}" placeholder="${esc(sweepPh(x.k))}">
    <button class="btn small ghost danger xdel" title="Bỏ tham số">✕</button></div>`).join("");
  if ($("xHint")) $("xHint").hidden = !EXTRA_SWEEP.length;
}
const readExtraRows = () => [...document.querySelectorAll("#extraSweep .xrow")].map((r) => ({
  k: r.querySelector('[data-x="k"]').value, v: r.querySelector('[data-x="v"]').value }));

// ---------------------------------------------------------------- đọc / ghi cấu hình
function getConfig() {
  // giữ lại những khoá cấu hình không có ô trên form (vd TP/SL cố định của preset cũ) để không mất khi lưu lại
  const raw = JSON.parse(JSON.stringify(CURRENT_RAW || {}));
  for (const f of ALL_FIELDS()) {
    const v = readField(f);
    if (v !== undefined) setPath(raw, f.k, v);
  }
  raw.exchange = selectedExchanges();
  raw.start = $("btStart").value.trim();
  raw.end = $("btEnd").value.trim();
  raw.signals = readSignals().map(cleanSignal);
  raw.schedule = Object.assign({}, raw.schedule, {
    sessions: readField(GROUPS.find((g) => g.id === "schedule").fields[0]) || ["00:00-24:00"],
    weekdays: [...document.querySelectorAll("#weekdays input:checked")].map((x) => x.value),
  });
  const sw = {};
  for (const [k, id] of [["exit.take_profit", "swTp"], ["exit.stop_loss", "swSl"], ["exit.max_hold", "swHold"]]) {
    const v = $(id).value.trim();
    if (v) sw[k] = v;
  }
  for (const r of readExtraRows()) if (r.k && r.v.trim()) sw[r.k] = r.v.trim();
  raw.sweep = sw;
  if (!raw.optimize.objective2) raw.optimize.objective2 = null;
  return raw;
}

function applyConfig(rawIn, exchanges) {
  const raw = rawIn || {};
  CURRENT_RAW = JSON.parse(JSON.stringify(raw));
  for (const f of ALL_FIELDS()) {
    const el = $(fid(f.k));
    if (!el) continue;
    let v = path(raw, f.k);
    if (v === undefined) v = f.d;
    if (f.type === "check") el.checked = v === undefined || v === null ? true : !!v;
    else if (f.type === "list") el.value = Array.isArray(v) ? v.join(", ") : (v || "");
    else el.value = v === null || v === undefined ? "" : String(v);
  }
  $("btStart").value = raw.start ? String(raw.start) : "";
  $("btEnd").value = raw.end ? String(raw.end) : "";
  const exs = exchanges || raw.exchange || ["binance"];
  const list = Array.isArray(exs) ? exs : String(exs).split(",").map((x) => x.trim());
  document.querySelectorAll("#exchanges [data-ex]").forEach((b) => b.classList.toggle("on", list.includes(b.dataset.ex)));
  SIGNALS = (raw.signals && raw.signals.length ? raw.signals : [DEF_SIGNAL]).map((x) => ({ ...x }));
  renderSignals();
  const wd = (raw.schedule || {}).weekdays || WEEKDAYS.map((w) => w[0]);
  document.querySelectorAll("#weekdays input").forEach((x) => (x.checked = wd.includes(x.value)));
  const sw = raw.sweep || {};
  $("swTp").value = sweepTxt(sw["exit.take_profit"]);
  $("swSl").value = sweepTxt(sw["exit.stop_loss"]);
  $("swHold").value = sweepTxt(sw["exit.max_hold"]);
  EXTRA_SWEEP = Object.entries(sw).filter(([k]) => !["exit.take_profit", "exit.stop_loss", "exit.max_hold"].includes(k))
    .map(([k, v]) => ({ k, v: sweepTxt(v) }));
  renderExtraSweep();
  changed();
}
const sweepTxt = (v) => {
  if (v === null || v === undefined) return "";
  if (Array.isArray(v)) return v.join(", ");
  if (typeof v === "object") return `${v.from}-${v.to}:${v.step}`;
  return String(v);
};
const selectedExchanges = () => [...document.querySelectorAll("#exchanges [data-ex].on")].map((b) => b.dataset.ex);

// ---------------------------------------------------------------- tóm tắt + kiểm tra cấu hình (thanh chạy)
function comboCount() {
  let n = 1, keys = 0;
  for (const id of ["swTp", "swSl", "swHold"]) {
    const v = $(id).value.trim();
    if (v) { n *= parseList(v); keys++; }
  }
  for (const r of readExtraRows()) if (r.v.trim()) { n *= parseList(r.v); keys++; }
  return keys ? n : 0;
}
function refreshSummary() {
  if (!$("btSummary")) return;
  const exs = selectedExchanges(), n = comboCount(), max = +$(fid("optimize.max_combos")).value || 20000;
  const sigs = readSignals().map(signalTxt).join(" · ");
  const day = (s) => String(s || "").slice(0, 10).split("-").reverse().join("/");
  $("btSummary").innerHTML = exs.length
    ? `<b>${exs.map((e) => e.toUpperCase()).join(", ")}</b> · ${day($("btStart").value)} → ${day($("btEnd").value)}
       · ${esc(sigs)} · <b>${n ? n.toLocaleString("vi-VN") : "0"}</b> cấu hình${exs.length > 1 ? ` × ${exs.length} sàn` : ""}` +
      (n > max ? ` <span class="warn">(lớn hơn ${max.toLocaleString("vi-VN")} → tìm kiếm thông minh)</span>` : "")
    : `<span class="neg">Chưa chọn sàn</span>`;
  $("btCombo").textContent = n ? `${n.toLocaleString("vi-VN")} cấu hình${exs.length > 1 ? ` × ${exs.length} sàn` : ""}` : "";
}
function changed() {
  refreshBadges();
  refreshSummary();
  saveDraft();
  clearTimeout(vTimer);
  vTimer = setTimeout(validateNow, 600);
}
async function validateNow() {
  const box = $("btError");
  if (!box) return;
  if (MODE !== "bt") { box.hidden = true; return; }
  const cfg = getConfig(), exs = selectedExchanges();
  if (!exs.length) { box.innerHTML = `<span class="neg">Chọn ít nhất 1 sàn</span>`; box.hidden = false; return; }
  if (!comboCount()) { box.innerHTML = `<span class="neg">Chưa có lưới TP / SL / thời gian giữ lệnh</span>`; box.hidden = false; return; }
  try {
    const d = await postJ("/api/validate", { config: cfg, exchanges: exs });
    if (!d.ok) { box.innerHTML = `<span class="neg">${esc(d.error)}</span>`; box.hidden = false; return; }
    // cấu hình chạy được, nhưng vài trường hợp dễ nhầm thì nhắc một dòng
    const notes = [];
    if (!$("swTp").value.trim()) notes.push("lưới không có TP – lệnh chỉ thoát khi chạm SL hoặc hết giờ");
    if (!$("swSl").value.trim()) notes.push("lưới không có SL – lệnh chỉ thoát khi chạm TP hoặc hết giờ");
    if (!$("swHold").value.trim()) notes.push("không giới hạn thời gian giữ lệnh");
    box.innerHTML = notes.length ? `<span class="warn">Lưu ý: ${esc(notes.join(" · "))}</span>` : "";
    box.hidden = !notes.length;
  } catch (e) { box.hidden = true; }
}

function btRequest() {
  const config = getConfig(), exchanges = selectedExchanges();
  if (!exchanges.length) { alert("Chọn ít nhất 1 sàn"); return null; }
  if (!config.signals.length) { alert("Cần ít nhất 1 tín hiệu"); return null; }
  if (!Object.keys(config.sweep).length) { alert("Nhập lưới TP / SL / thời gian giữ lệnh ở phần Cơ bản"); return null; }
  saveDraft();
  return { kind: "bt", config, exchanges, grid: config.sweep };
}

// ---------------------------------------------------------------- preset (file YAML trong configs/)
async function loadPresets(select) {
  PRESETS_LIST = await api("/api/presets");
  $("preset").innerHTML = `<option value="">— Cấu hình đang nhập —</option>` +
    PRESETS_LIST.map((p) => `<option value="${esc(p.file)}">${esc(p.name)}</option>`).join("");
  if (select) $("preset").value = select;
}
async function openPreset(file) {
  if (!file) return;
  try { applyConfig(await api(`/api/presets/${encodeURIComponent(file)}`)); }
  catch (e) { alert(e.message); }
}
async function savePreset(asNew) {
  const cfg = getConfig(), cur = $("preset").value;
  try {
    if (asNew || !cur) {
      const name = prompt("Tên file cấu hình mới:", (cfg.name || "chien_luoc").replace(/\s+/g, "_"));
      if (!name) return;
      const r = await postJ("/api/presets", { file: name, config: cfg });
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
  const cfg = getConfig();
  const d = await postJ("/api/yaml/dump", cfg);
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([d.yaml], { type: "text/yaml" }));
  a.download = ((cfg.name || "chien_luoc") + ".yaml").replace(/\s+/g, "_");
  a.click();
}
async function importYaml(file) {
  try {
    const d = await postJ("/api/yaml/parse", { text: await file.text() });
    applyConfig(d.config);
    toast("Đã nạp cấu hình từ file YAML");
  } catch (e) { alert(e.message); }
}
function saveDraft() {
  try { localStorage.setItem("btDraft", JSON.stringify({ config: getConfig(), exchanges: selectedExchanges() })); } catch (e) {}
}

// ---------------------------------------------------------------- khoảng thời gian nhanh
function setRange(days) {
  const pad = (n) => String(n).padStart(2, "0");
  const txt = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} 00:00`;
  const end = new Date(); end.setHours(0, 0, 0, 0);
  const start = new Date(end); start.setDate(start.getDate() - days);
  $("btStart").value = txt(start); $("btEnd").value = txt(end);
  changed();
}

// ---------------------------------------------------------------- dựng form
async function initConfig() {
  META = await api("/api/meta");
  $("exchanges").innerHTML = META.exchanges.map((e) => `<button class="chip" data-ex="${e.id}"
    title="Nến 1m: ${esc(e.klines)} · Tick: ${esc(e.ticks)}">${esc(e.name)}</button>`).join("");
  TF = (META.enums && META.enums.signal.timeframe) || META.timeframes || TF;
  for (const g of GROUPS) {
    for (const f of g.fields) {
      if (f.type === "select" && META.enums && META.enums.params[f.k]) f.opts = viOpts(f.k, META.enums.params[f.k]);
    }
  }
  for (const f of BASIC) {
    if (f.type === "select" && META.enums && META.enums.params[f.k]) f.opts = viOpts(f.k, META.enums.params[f.k]);
  }
  const rank = GROUPS.find((g) => g.id === "rank");
  rank.fields[0].opts = Object.entries(META.objectives);
  rank.fields[1].opts = [["", "— không dùng —"]].concat(Object.entries(META.objectives));
  $("btBasic").innerHTML = BASIC.map(fieldHtml).join("");
  $("btAdvanced").innerHTML = GROUPS.map(groupHtml).join("");
  $("weekdays").innerHTML = WEEKDAYS.map(([v, t]) => `<label class="chk"><input type="checkbox" value="${v}" checked> ${t}</label>`).join("");

  let draft = null;
  try { draft = JSON.parse(localStorage.getItem("btDraft") || "null"); } catch (e) {}
  applyConfig(draft ? draft.config : (META.default || {}), draft ? draft.exchanges : null);
  if (!$("btStart").value) setRange(7);
  await loadPresets();

  $("btForm").addEventListener("input", changed);
  $("btForm").addEventListener("change", changed);
  $("exchanges").addEventListener("click", (e) => {
    const b = e.target.closest("[data-ex]"); if (!b) return;
    b.classList.toggle("on"); changed();
  });
  document.querySelectorAll("[data-range]").forEach((b) => b.addEventListener("click", () => setRange(+b.dataset.range)));
  $("signals").addEventListener("click", (e) => {
    const row = e.target.closest(".sigrow"); if (!row) return;
    if (e.target.classList.contains("sigdel")) {
      SIGNALS = readSignals();
      SIGNALS.splice(+row.dataset.i, 1);
      if (!SIGNALS.length) SIGNALS = [{ ...DEF_SIGNAL }];
      renderSignals(); changed();
    } else if (e.target.classList.contains("sigmore")) {
      const box = row.querySelector(".sigmore-box");
      box.hidden = !box.hidden;
    }
  });
  $("sigAdd").addEventListener("click", () => {
    SIGNALS = readSignals();
    SIGNALS.push({ ...(SIGNALS[SIGNALS.length - 1] || DEF_SIGNAL) });
    renderSignals(); changed();
  });
  $("xAdd").addEventListener("click", () => {
    EXTRA_SWEEP = readExtraRows();
    const used = new Set(EXTRA_SWEEP.map((x) => x.k));
    const next = sweepable().find(([k]) => !used.has(k)) || sweepable()[0];
    EXTRA_SWEEP.push({ k: next[0], v: "" });
    renderExtraSweep(); changed();
  });
  $("extraSweep").addEventListener("click", (e) => {
    if (!e.target.classList.contains("xdel")) return;
    EXTRA_SWEEP = readExtraRows();
    EXTRA_SWEEP.splice(+e.target.closest(".xrow").dataset.i, 1);
    renderExtraSweep(); changed();
  });
  $("extraSweep").addEventListener("change", (e) => {     // đổi tham số -> đổi luôn gợi ý giá trị mẫu
    if (e.target.dataset.x !== "k") return;
    const inp = e.target.closest(".xrow").querySelector("[data-x=v]");
    if (inp) inp.placeholder = sweepPh(e.target.value);
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
    changed();
  }));
  $("advToggle").addEventListener("click", () => {
    const on = $("btAdvanced").hidden;
    $("btAdvanced").hidden = !on;
    $("advToggle").textContent = on ? "Ẩn tuỳ chọn nâng cao" : "Tuỳ chọn nâng cao…";
  });
  changed();
}
