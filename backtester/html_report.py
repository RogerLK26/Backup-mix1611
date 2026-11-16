"""Báo cáo HTML tự chứa (không cần internet): KPI, đường vốn, lãi/lỗ theo ngày & giờ, bảng lệnh."""
from __future__ import annotations

import html
import json
import math

from .stats import LABELS_VI, by_day, by_hour, by_symbol
from .utils import fmt_ms

REASON_VI = {"TP": "Chốt lời", "SL": "Cắt lỗ", "LIQ": "Thanh lý", "TIMEOUT": "Hết giờ"}


def _num(x):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return float(x)


def _public_config(cfg) -> dict:
    """Cấu hình thực tế của lượt chạy này (đã áp tham số khi quét nhiều chiến lược), bỏ khoá API."""
    raw = json.loads(json.dumps(cfg.raw or {}, default=str))
    raw.pop("sweep", None)
    raw.pop("optimize", None)
    raw["exchange"] = cfg.exchange
    if isinstance(raw.get("universe"), dict):
        raw["universe"].pop("coingecko_api_key", None)
    return raw


def report_data(res, s: dict) -> dict:
    """Toàn bộ dữ liệu kết quả dạng JSON (dùng cho report.html và giao diện web)."""
    cfg = res.cfg
    tz = cfg.timezone
    t = res.trades
    eq = [[int(a), round(float(b), 4)] for a, b in res.equity]
    if len(t):
        eq = [[cfg.start_ms, cfg.initial_balance]] + [[int(a), round(float(b), 4)]
                                                      for a, b in t.sort_values("exit_t")[["exit_t", "balance_after"]].values]
    dd = by_day(t, tz, cfg.day_reset_hour)
    hh = by_hour(t, tz)
    sym = by_symbol(t)
    trades = []
    for r in t.itertuples() if len(t) else []:
        trades.append([r.symbol, r.side, REASON_VI.get(r.reason, r.reason), fmt_ms(r.entry_t, tz, True),
                       fmt_ms(r.exit_t, tz, True), _num(r.hold_s), _num(r.entry_px), _num(r.exit_px), _num(r.net_pnl),
                       _num(r.roe_pct), _num(r.move_pct), r.resolution + (" ⚠" if r.ambiguous else ""),
                       _num(r.entry_fee + r.exit_fee), _num(r.funding)])
    return {
        "summary": {k: (_num(v) if isinstance(v, float) else v) for k, v in s.items()},
        "equity": eq,
        "days": [[k, _num(v), int(n)] for (k, v), n in zip(dd["net_pnl"].items(), dd["trades"])] if len(dd) else [],
        "hours": [[int(k), _num(v), int(n)] for (k, v), n in zip(hh["net_pnl"].items(), hh["trades"])] if len(hh) else [],
        "symbols": [[str(k), int(r.trades), int(r.tp), int(r.sl), _num(r.win_rate_pct), _num(r.net_pnl), _num(r.avg_hold_s)]
                    for k, r in sym.iterrows()] if len(sym) else [],
        "trades": trades,
        "notes": list(res.universe.notes),
        "rejected": [[k, LABELS_VI.get(k, k), v] for k, v in (s.get("rejected") or {}).items()],
        "tz": tz,
        "config": _public_config(cfg),
    }


def render_html(res, s: dict, data: dict | None = None) -> str:
    cfg = res.cfg
    data = data or report_data(res, s)
    sym = by_symbol(res.trades).head(50)

    def f(x, d=2, suf=""):
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "–"
        if isinstance(x, float) and math.isinf(x):
            return "∞"
        return f"{x:,.{d}f}{suf}"

    n = s.get("trades", 0)
    kpis = [
        ("Lãi/lỗ ròng", f(s.get("net_pnl")) + " USDT", f(s.get("return_pct"), 2, "%"), (s.get("net_pnl") or 0) >= 0),
        ("Số lệnh", f"{n}", f"{f(s.get('trades_per_day'), 1)} lệnh/ngày" if n else "", None),
        ("Win rate", f(s.get("win_rate_pct"), 1, "%") if n else "–", f"{s.get('wins', 0)} thắng / {s.get('losses', 0)} thua", None),
        ("Profit factor", f(s.get("profit_factor")) if n else "–", f"Kỳ vọng {f(s.get('expectancy_per_trade'), 3)} USDT/lệnh" if n else "", None),
        ("Max drawdown", f(s.get("max_drawdown")) + " USDT" if n else "–", f(s.get("max_drawdown_pct"), 2, "%") if n else "", None),
        ("Phí + funding", f((s.get("fees") or 0)) + " USDT" if n else "–", f"Funding {f(s.get('funding'), 4)}" if n else "", None),
        ("Giữ lệnh TB", f(s.get("avg_hold_s"), 1, " s") if n else "–", f"Trung vị {f(s.get('median_hold_s'), 1)} s" if n else "", None),
        ("Độ chính xác", f(s.get("resolution_tick_pct"), 1, "% tick") if n else "–", f"{s.get('ambiguous_trades', 0)} lệnh mơ hồ (1m)" if n else "", None),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kl">{html.escape(a)}</div><div class="kv">{html.escape(b)}</div>'
        f'<div class="ks">{"<span class=%s>%s</span>" % ("pos" if good else "neg", "▲" if good else "▼") if good is not None else ""}'
        f' {html.escape(c)}</div></div>' for a, b, c, good in kpis)

    reasons = "".join(f"<tr><td>{REASON_VI[k]}</td><td class=n>{s.get(v, 0)}</td></tr>"
                      for k, v in [("TP", "tp"), ("SL", "sl"), ("LIQ", "liquidations"), ("TIMEOUT", "timeouts")])
    rej = "".join(f"<tr><td>{html.escape(LABELS_VI.get(k, k))}</td><td class=n>{v}</td></tr>"
                  for k, v in (s.get("rejected") or {}).items()) or "<tr><td colspan=2>Không có</td></tr>"
    sym_rows = "".join(
        f"<tr><td>{html.escape(str(k))}</td><td class=n>{int(r.trades)}</td><td class=n>{int(r.tp)}</td><td class=n>{int(r.sl)}</td>"
        f"<td class=n>{r.win_rate_pct:.1f}%</td><td class='n {'pos' if r.net_pnl >= 0 else 'neg'}'>{r.net_pnl:,.3f}</td>"
        f"<td class=n>{r.avg_hold_s:,.1f}</td></tr>" for k, r in sym.iterrows()) or "<tr><td colspan=7>Không có lệnh</td></tr>"
    notes = "".join(f"<li>{html.escape(x)}</li>" for x in res.universe.notes)
    cfg_txt = html.escape(json.dumps({k: v for k, v in cfg.raw.items() if k != "sweep"}, ensure_ascii=False, indent=2, default=str))

    return TEMPLATE.replace("__TITLE__", html.escape(f"Backtest · {cfg.name} · {cfg.exchange}")) \
        .replace("__NAME__", html.escape(cfg.name)).replace("__EX__", html.escape(cfg.exchange.upper())) \
        .replace("__PERIOD__", html.escape(s.get("period", ""))) \
        .replace("__META__", html.escape(f"{s.get('symbols_in_universe', 0)} coin hợp lệ · {s.get('signals_found', 0)} tín hiệu · "
                                         f"số dư {f(cfg.initial_balance)} → {f(s.get('final_balance'))} USDT")) \
        .replace("__KPIS__", kpi_html).replace("__REASONS__", reasons).replace("__REJ__", rej) \
        .replace("__SYMS__", sym_rows).replace("__NOTES__", f"<ul class=notes>{notes}</ul>" if notes else "") \
        .replace("__CFG__", cfg_txt).replace("__DATA__", json.dumps(data, ensure_ascii=False, default=str))


TEMPLATE = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light;--bg:#f6f6f4;--surface-1:#fcfcfb;--border:#e4e3df;--grid:#ecebe7;--text-primary:#0b0b0b;--text-secondary:#52514e;--text-muted:#7a7974;
--series-1:#2a78d6;--pos:#2a78d6;--neg:#e34948;--pos-text:#1c5cab;--neg-text:#b52f2e}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#121211;--surface-1:#1a1a19;--border:#2e2e2b;--grid:#262624;
--text-primary:#f0efec;--text-secondary:#c3c2b7;--text-muted:#8f8e86;--series-1:#3987e5;--pos:#3987e5;--neg:#e66767;--pos-text:#6da7ec;--neg-text:#f08a8a}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#121211;--surface-1:#1a1a19;--border:#2e2e2b;--grid:#262624;--text-primary:#f0efec;--text-secondary:#c3c2b7;
--text-muted:#8f8e86;--series-1:#3987e5;--pos:#3987e5;--neg:#e66767;--pos-text:#6da7ec;--neg-text:#f08a8a}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text-primary);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:24px 16px 48px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:15px;margin:0 0 12px;font-weight:600}
.sub{color:var(--text-secondary)}.muted{color:var(--text-muted);font-size:12px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;margin-top:16px}
.kpi{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.kl{color:var(--text-secondary);font-size:12px}.kv{font-size:20px;font-weight:650;margin:2px 0;font-variant-numeric:tabular-nums}.ks{font-size:12px;color:var(--text-secondary)}
.pos{color:var(--pos-text)}.neg{color:var(--neg-text)}
.grid2{display:grid;grid-template-columns:2fr 1fr;gap:16px}@media(max-width:800px){.grid2{grid-template-columns:1fr}}
.chart{position:relative;width:100%}.chart svg{display:block;width:100%;height:auto;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--surface-1);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;
box-shadow:0 2px 8px rgba(0,0,0,.12);white-space:nowrap;display:none;z-index:2;font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{padding:5px 8px;border-bottom:1px solid var(--grid);text-align:left;white-space:nowrap}
th{color:var(--text-secondary);font-weight:600;position:sticky;top:0;background:var(--surface-1);cursor:pointer}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow:auto;max-height:520px}.legend{display:flex;gap:14px;font-size:12px;color:var(--text-secondary);margin-bottom:6px}
.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
input[type=search]{background:var(--bg);color:var(--text-primary);border:1px solid var(--border);border-radius:6px;padding:6px 10px;width:220px;max-width:100%}
pre{font-size:12px;overflow:auto;max-height:400px;background:var(--bg);padding:12px;border-radius:8px}.notes{color:var(--text-secondary);margin:8px 0 0;padding-left:18px}
</style></head><body><div class="wrap">
<h1>__NAME__ <span class="sub">· __EX__</span></h1>
<div class="sub">__PERIOD__</div><div class="muted">__META__</div>__NOTES__
<div class="kpis">__KPIS__</div>
<div class="card"><h2>Đường vốn (số dư sau mỗi lệnh đóng, USDT)</h2><div class="chart" id="eq"></div></div>
<div class="grid2">
 <div class="card"><h2>Lãi/lỗ ròng theo ngày (USDT)</h2><div class="legend"><span><i class="sw" style="background:var(--pos)"></i>Lãi</span><span><i class="sw" style="background:var(--neg)"></i>Lỗ</span></div><div class="chart" id="days"></div></div>
 <div class="card"><h2>Kết quả thoát lệnh</h2><table>__REASONS__</table><h2 style="margin-top:18px">Tín hiệu bị bỏ qua</h2><table>__REJ__</table></div>
</div>
<div class="card"><h2>Lãi/lỗ ròng theo giờ vào lệnh (giờ địa phương)</h2><div class="legend"><span><i class="sw" style="background:var(--pos)"></i>Lãi</span><span><i class="sw" style="background:var(--neg)"></i>Lỗ</span></div><div class="chart" id="hours"></div></div>
<div class="card"><h2>Theo coin (top 50)</h2><div class="scroll"><table><thead><tr><th>Coin</th><th class=n>Lệnh</th><th class=n>TP</th><th class=n>SL</th><th class=n>Win rate</th><th class=n>Lãi/lỗ</th><th class=n>Giữ TB (s)</th></tr></thead><tbody>__SYMS__</tbody></table></div></div>
<div class="card"><h2>Danh sách lệnh</h2><input type="search" id="q" placeholder="Lọc theo coin / chiều / kết quả..."> <span class="muted" id="cnt"></span>
<div class="scroll" style="margin-top:8px"><table id="tt"><thead><tr><th>Coin</th><th>Chiều</th><th>Kết quả</th><th>Vào lệnh</th><th>Thoát lệnh</th><th class=n>Giữ (s)</th><th class=n>Giá vào</th><th class=n>Giá ra</th><th class=n>Lãi/lỗ</th><th class=n>ROE %</th><th class=n>Biến động %</th><th>Dữ liệu</th></tr></thead><tbody></tbody></table></div></div>
<div class="card"><h2>Cấu hình đã chạy</h2><pre>__CFG__</pre></div>
</div>
<script>
const D=__DATA__;
const css=n=>getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt=(v,d=2)=>v==null?'–':v.toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d});
const fdt=ms=>{const d=new Date(ms);return d.toLocaleString('sv-SE',{timeZone:D.tz}).slice(0,16)};
const NS='http://www.w3.org/2000/svg';
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function ticks(lo,hi,n){if(lo===hi){lo-=1;hi+=1}const s=Math.pow(10,Math.floor(Math.log10((hi-lo)/n)));const m=[1,2,5,10].find(m=>(hi-lo)/(s*m)<=n)*s;
 const out=[];for(let v=Math.ceil(lo/m)*m;v<=hi+1e-9;v+=m)out.push(+v.toFixed(10));return out}
function frame(id,h){const box=document.getElementById(id);const W=Math.max(320,box.clientWidth||800),H=h;
 const svg=el('svg',{viewBox:`0 0 ${W} ${H}`},box);const tip=document.createElement('div');tip.className='tip';box.appendChild(tip);return{box,svg,W,H,tip}}
function yaxis(svg,W,y,tk,L,R){for(const v of tk){const yy=y(v);el('line',{x1:L,x2:W-R,y1:yy,y2:yy,stroke:css(v===0?'--text-muted':'--grid'),'stroke-width':1},svg);
 const t=el('text',{x:L-6,y:yy+4,'text-anchor':'end','font-size':11,fill:css('--text-secondary')},svg);t.textContent=fmt(v,Math.abs(tk[1]-tk[0])<1?2:0)}}
function showTip(f,html,x,y){f.tip.innerHTML=html;f.tip.style.display='block';const bw=f.box.clientWidth,tw=f.tip.offsetWidth;
 f.tip.style.left=Math.min(Math.max(0,x+12),bw-tw)+'px';f.tip.style.top=Math.max(0,y-40)+'px'}
function lineChart(){const P=D.equity;if(P.length<2){document.getElementById('eq').innerHTML='<div class=muted>Chưa có lệnh</div>';return}
 const f=frame('eq',280),L=64,R=12,T=10,B=26;const xs=P.map(p=>p[0]),ys=P.map(p=>p[1]);
 const x0=xs[0],x1=Math.max(xs[xs.length-1],x0+1);let lo=Math.min(...ys),hi=Math.max(...ys);const pad=(hi-lo)*0.08||1;lo-=pad;hi+=pad;
 const tk=ticks(lo,hi,5);lo=Math.min(lo,tk[0]);hi=Math.max(hi,tk[tk.length-1]);
 const x=v=>L+(v-x0)/(x1-x0)*(f.W-L-R),y=v=>T+(hi-v)/(hi-lo)*(f.H-T-B);yaxis(f.svg,f.W,y,tk,L,R);
 for(let i=0;i<5;i++){const v=x0+(x1-x0)*i/4;const t=el('text',{x:x(v),y:f.H-6,'text-anchor':i==0?'start':i==4?'end':'middle','font-size':11,fill:css('--text-secondary')},f.svg);t.textContent=(x1-x0)<3*864e5?fdt(v).slice(5,16):fdt(v).slice(0,10)}
 const d=P.map((p,i)=>`${i?'L':'M'}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
 el('path',{d,fill:'none',stroke:css('--series-1'),'stroke-width':2,'stroke-linejoin':'round'},f.svg);
 const cross=el('line',{y1:T,y2:f.H-B,stroke:css('--text-muted'),'stroke-dasharray':'3 3',visibility:'hidden'},f.svg);
 const dot=el('circle',{r:4,fill:css('--series-1'),stroke:css('--surface-1'),'stroke-width':2,visibility:'hidden'},f.svg);
 const hit=el('rect',{x:L,y:T,width:f.W-L-R,height:f.H-T-B,fill:'transparent'},f.svg);
 hit.addEventListener('mousemove',ev=>{const r=f.svg.getBoundingClientRect(),mx=(ev.clientX-r.left)*f.W/r.width;const tv=x0+(mx-L)/(f.W-L-R)*(x1-x0);
  let i=xs.findIndex(v=>v>tv);i=i<=0?(i===0?0:xs.length-1):(tv-xs[i-1]<xs[i]-tv?i-1:i);const px=x(xs[i]),py=y(ys[i]);
  cross.setAttribute('x1',px);cross.setAttribute('x2',px);cross.setAttribute('visibility','visible');dot.setAttribute('cx',px);dot.setAttribute('cy',py);dot.setAttribute('visibility','visible');
  const dlt=i?ys[i]-ys[i-1]:0;showTip(f,`<b>${fmt(ys[i])} USDT</b><br>${fdt(xs[i])}${i?`<br>Lệnh này: <span class="${dlt>=0?'pos':'neg'}">${dlt>=0?'+':''}${fmt(dlt,3)}</span>`:''}`,px*r.width/f.W,py*r.height/f.H)});
 hit.addEventListener('mouseleave',()=>{cross.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');f.tip.style.display='none'})}
function barChart(id,items,label){if(!items.length){document.getElementById(id).innerHTML='<div class=muted>Chưa có lệnh</div>';return}
 const f=frame(id,240),L=56,R=8,T=10,B=26;const vs=items.map(i=>i[1]||0);let lo=Math.min(0,...vs),hi=Math.max(0,...vs);if(lo===hi)hi=1;
 const tk=ticks(lo,hi,5);lo=Math.min(lo,tk[0]);hi=Math.max(hi,tk[tk.length-1]);const y=v=>T+(hi-v)/(hi-lo)*(f.H-T-B);yaxis(f.svg,f.W,y,tk,L,R);
 const n=items.length,bw=(f.W-L-R)/n,gap=Math.max(Math.min(2+bw*0.2,10),bw-48),every=Math.ceil(n/12);
 items.forEach((it,i)=>{const v=it[1]||0,x0=L+i*bw+gap/2,w=Math.max(1,bw-gap),y0=y(Math.max(v,0)),h=Math.max(Math.abs(y(v)-y(0)),v?1:0);
  const rr=Math.min(4,w/2,h);const top=v>=0;
  const d=top?`M${x0},${y0+h}V${y0+rr}Q${x0},${y0} ${x0+rr},${y0}H${x0+w-rr}Q${x0+w},${y0} ${x0+w},${y0+rr}V${y0+h}Z`
             :`M${x0},${y0}V${y0+h-rr}Q${x0},${y0+h} ${x0+rr},${y0+h}H${x0+w-rr}Q${x0+w},${y0+h} ${x0+w},${y0+h-rr}V${y0}Z`;
  const p=el('path',{d,fill:css(top?'--pos':'--neg')},f.svg);
  if(i%every===0){const t=el('text',{x:x0+w/2,y:f.H-6,'text-anchor':'middle','font-size':11,fill:css('--text-secondary')},f.svg);t.textContent=label(it,true)}
  const hb=el('rect',{x:L+i*bw,y:T,width:bw,height:f.H-T-B,fill:'transparent'},f.svg);
  hb.addEventListener('mousemove',ev=>{const r=f.svg.getBoundingClientRect();p.setAttribute('opacity',.8);
   showTip(f,`<b>${label(it,false)}</b><br>Lãi/lỗ: <span class="${v>=0?'pos':'neg'}">${fmt(v,3)} USDT</span>${it[2]!=null?`<br>${it[2]} lệnh`:''}`,(ev.clientX-r.left),(ev.clientY-r.top))});
  hb.addEventListener('mouseleave',()=>{p.removeAttribute('opacity');f.tip.style.display='none'})})}
function tradesTable(){const tb=document.querySelector('#tt tbody'),q=document.getElementById('q'),cnt=document.getElementById('cnt');let sortK=null,asc=true;
 const render=()=>{const s=q.value.trim().toLowerCase();let rows=D.trades.filter(r=>!s||(r[0]+' '+r[1]+' '+r[2]+' '+r[11]).toLowerCase().includes(s));
  if(sortK!=null)rows=[...rows].sort((a,b)=>(a[sortK]>b[sortK]?1:a[sortK]<b[sortK]?-1:0)*(asc?1:-1));
  cnt.textContent=rows.length+' / '+D.trades.length+' lệnh';
  tb.innerHTML=rows.slice(0,3000).map(r=>`<tr><td>${r[0]}</td><td>${r[1]==='LONG'?'Long':'Short'}</td><td>${r[2]}</td><td>${r[3]}</td><td>${r[4]}</td><td class=n>${fmt(r[5],1)}</td><td class=n>${+r[6].toPrecision(6)}</td><td class=n>${+r[7].toPrecision(6)}</td><td class="n ${r[8]>=0?'pos':'neg'}">${fmt(r[8],4)}</td><td class=n>${fmt(r[9],2)}</td><td class=n>${fmt(r[10],2)}</td><td>${r[11]}</td></tr>`).join('')};
 document.querySelectorAll('#tt th').forEach((th,i)=>th.onclick=()=>{asc=sortK===i?!asc:true;sortK=i;render()});q.oninput=render;render()}
function draw(){for(const id of['eq','days','hours'])document.getElementById(id).innerHTML='';lineChart();
 barChart('days',D.days,(it,short)=>short?it[0].slice(5):it[0]);barChart('hours',D.hours,(it,short)=>short?String(it[0]):`${it[0]}:00–${it[0]}:59`)}
draw();tradesTable();let rt;addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(draw,150)});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',draw);
</script></body></html>"""
