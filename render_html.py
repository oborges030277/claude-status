import re
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    _BERLIN = ZoneInfo("Europe/Berlin")
except Exception:
    _BERLIN = None

_TIME_AMPM_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])m\b", re.IGNORECASE)
_RESET_RE = re.compile(r"Resets\s+(\d{1,2})(?::(\d{2}))?\s*([ap])m", re.IGNORECASE)


def _next_reset_iso(reset_str: str) -> str:
    # "Resets 4:30pm (Europe/Berlin)" -> next future occurrence of 16:30 in Berlin,
    # returned as UTC ISO. Session resets are ~5h apart so this is always within today/tomorrow.
    if not reset_str or _BERLIN is None:
        return ""
    m = _RESET_RE.search(reset_str)
    if not m:
        return ""
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    is_pm = m.group(3).lower() == "p"
    if is_pm and hour != 12:
        hour += 12
    elif not is_pm and hour == 12:
        hour = 0
    now_berlin = datetime.now(timezone.utc).astimezone(_BERLIN)
    cand = now_berlin.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= now_berlin:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc).isoformat(timespec="seconds")


def _ampm_to_24h(match: re.Match) -> str:
    hour = int(match.group(1))
    minute = match.group(2)
    is_pm = match.group(3).lower() == "p"
    if is_pm:
        if hour != 12:
            hour += 12
    else:
        if hour == 12:
            hour = 0
    return f"{hour:02d}:{minute if minute else '00'}"


def _to_24h(text: str) -> str:
    return _TIME_AMPM_RE.sub(_ampm_to_24h, text)


def _bucket(pct: int) -> str:
    if pct <= 10:
        return "low"
    if pct <= 40:
        return "mlow"
    if pct <= 65:
        return "med"
    if pct <= 89:
        return "high"
    return "crit"


def _card(section: dict) -> str:
    title = escape(section["title"])
    percent = max(0, min(100, int(section.get("percent") or 0)))
    reset_raw = section.get("reset") or ""
    reset = escape(_to_24h(reset_raw))
    reset_html = f'<div class="reset">{reset}</div>' if reset else ""
    countdown_html = ""
    if section.get("title") == "Current session":
        target = _next_reset_iso(reset_raw)
        if target:
            cls = "countdown" + (" countdown-crit" if percent >= 100 else "")
            countdown_html = (
                f'<div class="{cls}" data-target="{escape(target)}">'
                f'<span class="cd-label">Nächster Session-Reset in</span>'
                f'<span class="cd-value">–</span></div>'
            )
    return (
        f'<article class="card pct-{_bucket(percent)}">'
        f'<div class="row"><h2>{title}</h2><span class="pct">{percent}%</span></div>'
        f'<div class="bar"><div class="fill" style="width:{percent}%"></div></div>'
        f'{reset_html}{countdown_html}</article>'
    )


def render(data: dict) -> str:
    sections = [s for s in data["sections"] if "sonnet" not in s.get("title", "").lower()]
    cards = "\n".join(_card(s) for s in sections)
    updated = data["updated_utc"]
    try:
        dt = datetime.fromisoformat(updated)
        dt_local = dt.astimezone(_BERLIN) if _BERLIN is not None else dt.astimezone()
        local = dt_local.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        local = updated
    hist_file = "history.jsonl"
    sess_file = "sessions.json"
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<meta http-equiv="Expires" content="0">
<title>Claude Usage</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
html,body{{height:100%}}
body{{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  color:#eef1f7;min-height:100vh;padding:48px 20px;
  background:
    radial-gradient(1200px 800px at 10% 10%,#5b5ff5 0%,transparent 60%),
    radial-gradient(1000px 700px at 90% 90%,#c86dd7 0%,transparent 55%),
    radial-gradient(900px 600px at 50% 50%,#1b2140 0%,#0b0e1b 80%);
  background-attachment:fixed;
}}
.wrap{{max-width:720px;margin:0 auto}}
header{{margin-bottom:32px;text-align:center}}
header h1{{font-size:28px;font-weight:600;letter-spacing:-.01em}}
header p{{opacity:.7;font-size:14px;margin-top:6px}}
.grid{{display:grid;gap:16px}}
.card{{
  padding:22px 24px;border-radius:18px;
  background:rgba(255,255,255,.07);
  backdrop-filter:blur(18px) saturate(140%);
  -webkit-backdrop-filter:blur(18px) saturate(140%);
  border:1px solid rgba(255,255,255,.12);
  box-shadow:0 8px 32px rgba(0,0,0,.25);
}}
.card h2{{font-size:13px;font-weight:500;opacity:.8;letter-spacing:.04em;text-transform:uppercase}}
.age{{margin:0 auto 24px;max-width:720px;padding:18px 24px;border-radius:18px;text-align:center;
  font-size:22px;font-weight:700;letter-spacing:.01em;
  background:rgba(255,255,255,.09);border:1px solid rgba(255,255,255,.18);
  box-shadow:0 8px 32px rgba(0,0,0,.25)}}
.age.fresh{{background:linear-gradient(90deg,rgba(90,200,120,.25),rgba(90,200,180,.25));border-color:rgba(120,220,160,.5)}}
.age.stale{{background:linear-gradient(90deg,rgba(240,160,60,.3),rgba(240,90,90,.3));border-color:rgba(240,160,90,.6)}}
.age small{{display:block;font-size:12px;font-weight:400;opacity:.75;margin-top:4px;text-transform:none;letter-spacing:0}}
.row{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:12px}}
.pct{{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-.01em}}
.bar{{position:relative;height:10px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden}}
.fill{{height:100%;border-radius:999px;background:linear-gradient(90deg,#7aa2ff,#b388ff);transition:width .4s ease}}
.card.pct-low .pct{{color:#4ade80}}
.card.pct-mlow .pct{{color:#a3e635}}
.card.pct-med .pct{{color:#facc15}}
.card.pct-high .pct{{color:#fb923c}}
.card.pct-crit .pct{{color:#f87171}}
.card.pct-low .fill{{background:linear-gradient(90deg,#86efac,#22c55e)}}
.card.pct-mlow .fill{{background:linear-gradient(90deg,#bef264,#84cc16)}}
.card.pct-med .fill{{background:linear-gradient(90deg,#fde047,#eab308)}}
.card.pct-high .fill{{background:linear-gradient(90deg,#fdba74,#f97316)}}
.card.pct-crit .fill{{background:linear-gradient(90deg,#fca5a5,#ef4444)}}
.reset{{margin-top:14px;font-size:13px;color:#eef1f7;opacity:.95;font-weight:500}}
.countdown{{margin-top:10px;display:flex;align-items:baseline;justify-content:space-between;gap:10px;
  padding:10px 14px;border-radius:12px;background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.12)}}
.countdown .cd-label{{font-size:12px;opacity:.8;text-transform:uppercase;letter-spacing:.04em}}
.countdown .cd-value{{font-size:18px;font-weight:700;font-variant-numeric:tabular-nums;color:#7aa2ff}}
.countdown.countdown-crit{{background:linear-gradient(90deg,rgba(248,113,113,.22),rgba(248,113,113,.10));
  border-color:rgba(248,113,113,.5)}}
.countdown.countdown-crit .cd-value{{color:#fca5a5;font-size:22px}}
.countdown.countdown-crit .cd-label{{color:#fecaca;opacity:1}}
.extra{{margin-top:12px;font-size:13px;opacity:.75}}
footer{{margin-top:28px;text-align:center;font-size:12px;opacity:.5}}
.charts{{margin-top:32px;display:grid;gap:16px}}
.chart-card{{
  padding:22px 24px;border-radius:18px;
  background:rgba(255,255,255,.07);
  backdrop-filter:blur(18px) saturate(140%);
  -webkit-backdrop-filter:blur(18px) saturate(140%);
  border:1px solid rgba(255,255,255,.12);
  box-shadow:0 8px 32px rgba(0,0,0,.25);
}}
.chart-card h2{{font-size:13px;font-weight:500;opacity:.8;letter-spacing:.04em;text-transform:uppercase;margin-bottom:14px}}
.chart-card .empty{{opacity:.55;font-size:13px;padding:24px 0;text-align:center}}
.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:8px}}
@media (max-width:640px){{.tiles{{grid-template-columns:repeat(2,1fr)}}}}
.tile{{
  padding:18px 14px;border-radius:14px;
  background:rgba(255,255,255,.06);
  border:1px solid rgba(255,255,255,.10);
  text-align:center;
}}
.tile .val{{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.01em;color:#b388ff}}
.tile .lbl{{font-size:11px;opacity:.7;margin-top:6px;text-transform:uppercase;letter-spacing:.04em}}
.heatmap{{display:grid;grid-template-columns:28px repeat(24,1fr);gap:2px;font-size:10px}}
.heatmap .hd{{opacity:.55;text-align:center;line-height:16px}}
.heatmap .rl{{opacity:.55;line-height:18px;text-align:right;padding-right:4px}}
.heatmap .cell{{height:18px;border-radius:3px;background:rgba(255,255,255,.05)}}
.hm-legend{{margin-top:12px;display:flex;align-items:center;gap:10px;font-size:11px;opacity:.8}}
.hm-legend .bar{{flex:1;height:10px;border-radius:999px;
  background:linear-gradient(90deg,
    rgba(255,255,255,.06) 0%,
    hsla(130,70%,55%,.45) 12%,
    hsla(80,70%,55%,.65) 35%,
    hsla(45,70%,55%,.85) 60%,
    hsla(0,70%,55%,1) 100%)}}
.hm-legend .lbl{{font-variant-numeric:tabular-nums;opacity:.75;min-width:28px;text-align:center}}
.uplot,.u-wrap,.u-over{{background:transparent!important}}
.u-legend{{color:#eef1f7!important;font-size:12px}}
.u-legend .u-label,.u-legend .u-value{{color:#eef1f7!important}}
.u-axis{{color:#b8bdd0}}
</style>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css">
<script src="https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js"></script>
</head>
<body>
<div class="wrap">
<div id="age" class="age" data-updated="{escape(updated)}">
  <span id="age-text">Datenalter wird berechnet…</span>
  <small>Letzte Aktualisierung: {escape(local)}</small>
</div>
<header>
  <h1>Claude Usage</h1>
</header>
<section class="grid">
{cards}
</section>
<section class="charts">
  <div class="chart-card">
    <h2>Summary</h2>
    <div class="tiles" id="tiles">
      <div class="tile"><div class="val" id="t-norm">–</div><div class="lbl">Ø Wochenkosten / voll genutzte Session</div></div>
      <div class="tile"><div class="val" id="t-heavy">–</div><div class="lbl">Voll genutzte Sessions / Woche</div></div>
      <div class="tile"><div class="val" id="t-remaining">–</div><div class="lbl">Volle Sessions noch übrig (diese Woche)</div></div>
      <div class="tile"><div class="val" id="t-current">–</div><div class="lbl">Aktuelle Session – hochgerechnet</div></div>
    </div>
    <div style="opacity:.6;font-size:11px;margin-top:10px;line-height:1.5">
      <b>Ø Wochenkosten / voll genutzte Session</b>: Durchschnitt über die letzten 14 Tage — wieviel Wochenkontingent eine voll ausgeschöpfte Session typischerweise kostet.<br>
      <b>Aktuelle Session – hochgerechnet</b>: Wenn du die gerade laufende Session zu 100% durchziehst, kostet sie dich so viel Wochenkontingent.<br>
      <span style="opacity:.7">Formel: Δ Woche / SessionPeak × 100. Damit zählen auch teilweise genutzte Sessions.</span>
    </div>
  </div>
  <div class="chart-card">
    <h2>Wochenkontingent – Ist vs. Ideallinie (letzte 3 Wochen)</h2>
    <div id="chart-weekly"></div>
    <div class="empty" id="empty-weekly" style="display:none">noch keine Daten</div>
    <div style="opacity:.55;font-size:11px;margin-top:10px;line-height:1.4">
      Die Ideallinie ist das lineare Soll: bei jedem Wochen-Reset bei 0%, am Ende der Woche bei 100%. Die Ist-Linie zeigt deinen tatsächlichen Wochenkontingent-Verbrauch über die Kalendertage.
    </div>
  </div>
  <div class="chart-card">
    <h2>Aktivität – Tag × Stunde</h2>
    <div id="heatmap" class="heatmap"></div>
    <div class="hm-legend" id="hm-legend" style="display:none">
      <span class="lbl">0%</span>
      <span class="lbl" style="text-align:center;flex:0 0 auto;padding:0 4px;opacity:.55">Ø Session-% in diesem Slot</span>
      <span class="lbl">100%</span>
    </div>
    <div class="hm-legend" id="hm-legend-bar" style="display:none">
      <span class="lbl" style="visibility:hidden">0%</span>
      <span class="bar"></span>
      <span class="lbl" style="visibility:hidden">100%</span>
    </div>
    <div class="empty" id="empty-heatmap" style="display:none">noch keine Daten</div>
  </div>
</section>
<footer>auto-updated every 5 min</footer>
</div>
<script>
(function(){{
  var el=document.getElementById('age'),txt=document.getElementById('age-text');
  var ts=Date.parse(el.getAttribute('data-updated'));
  function tick(){{
    var diff=Math.max(0,Date.now()-ts);
    var m=Math.floor(diff/60000),h=Math.floor(m/60);m=m%60;
    var s=h>0?('Daten sind '+h+' Std. '+m+' Min. alt'):('Daten sind '+m+' Min. alt');
    txt.textContent=s;
    el.classList.toggle('fresh',diff<15*60000);
    el.classList.toggle('stale',diff>=30*60000);
  }}
  tick();setInterval(tick,30000);
  var CHECK_MS=60000;
  function checkForUpdate(){{
    if(document.hidden) return;
    fetch('index.html?t='+Date.now(),{{cache:'no-store'}})
      .then(function(r){{return r.ok?r.text():null;}})
      .then(function(text){{
        if(!text) return;
        var m=text.match(/id="age"[^>]*data-updated="([^"]+)"/);
        if(m && m[1]!==el.getAttribute('data-updated')){{
          var u=new URL(location.href); u.searchParams.set('cb',Date.now()); location.replace(u.toString());
        }}
      }})
      .catch(function(){{}});
  }}
  setInterval(checkForUpdate,CHECK_MS);
  document.addEventListener('visibilitychange',function(){{
    if(!document.hidden) checkForUpdate();
  }});
}})();
(function(){{
  function parseJsonl(text){{
    var out=[];
    (text||'').split(/\\r?\\n/).forEach(function(ln){{
      if(!ln.trim()) return;
      try{{ out.push(JSON.parse(ln)); }}catch(e){{}}
    }});
    return out;
  }}
  function fmtRatio(r){{ return (r==null)?'–':r.toFixed(2); }}
  function fmtPct(n){{ return (n==null)?'–':(Math.round(n*10)/10)+'%'; }}
  function show(id){{ var el=document.getElementById(id); if(el) el.style.display=''; }}
  function hide(id){{ var el=document.getElementById(id); if(el) el.style.display='none'; }}
  function loadAll(){{
    return Promise.all([
      fetch('{hist_file}?t='+Date.now(),{{cache:'no-store'}}).then(function(r){{return r.ok?r.text():'';}}).catch(function(){{return '';}}),
      fetch('{sess_file}?t='+Date.now(),{{cache:'no-store'}}).then(function(r){{return r.ok?r.json():null;}}).catch(function(){{return null;}})
    ]);
  }}
  function detectWeekResets(rows){{
    // A week reset = consecutive rows where week% drops steeply into single digits.
    // Returns timestamps (unix seconds) where a new week started (use the "after" row).
    var out=[];
    for(var i=1;i<rows.length;i++){{
      var a=rows[i-1], b=rows[i];
      if(a.week-b.week>=30 && b.week<=15){{ out.push(Date.parse(b.t)/1000); }}
    }}
    return out;
  }}
  function buildWeekly(rows){{
    var host=document.getElementById('chart-weekly');
    if(!host) return;
    if(!rows.length || typeof uPlot==='undefined'){{ show('empty-weekly'); return; }}
    var nowSec=Date.now()/1000;
    var cutoff=nowSec-21*86400;
    var inWin=rows.filter(function(r){{ var t=Date.parse(r.t)/1000; return !isNaN(t) && t>=cutoff; }});
    if(!inWin.length){{ show('empty-weekly'); return; }}
    // Build list of week-start timestamps inside window.
    // Start with detected resets; if none, assume the earliest row is the start of the current week.
    var resets=detectWeekResets(rows).filter(function(t){{return t>=cutoff;}});
    var firstT=Date.parse(inWin[0].t)/1000;
    var starts=resets.slice();
    if(!starts.length || starts[0]>firstT+3600){{
      // Prepend the window's first row as the implicit start of the earliest visible week.
      starts.unshift(firstT);
    }}
    // Build ideal sawtooth: for each segment [starts[i], starts[i+1]] go from 0% to 100%.
    // For the last (ongoing) segment, project 7 days forward.
    var WEEK=7*86400;
    var idealX=[], idealY=[];
    for(var i=0;i<starts.length;i++){{
      var s=starts[i];
      var e=(i+1<starts.length)?starts[i+1]:(s+WEEK);
      idealX.push(s); idealY.push(0);
      idealX.push(e); idealY.push(100);
      // insert gap so uPlot doesn't connect back to 0 of next segment
      if(i+1<starts.length){{
        idealX.push(e+1); idealY.push(null);
      }}
    }}
    // Actual line: all rows in window
    var actualX=inWin.map(function(r){{return Date.parse(r.t)/1000;}});
    var actualY=inWin.map(function(r){{return r.week;}});
    // Align to single x-axis: uPlot needs one shared x-array. Merge sorted unique.
    var xset={{}};
    idealX.forEach(function(x){{if(x!=null) xset[x]=1;}});
    actualX.forEach(function(x){{xset[x]=1;}});
    var xs=Object.keys(xset).map(Number).sort(function(a,b){{return a-b;}});
    // Build ideal and actual series aligned to xs (use null where no value)
    function interpIdeal(t){{
      for(var i=0;i<starts.length;i++){{
        var s=starts[i];
        var e=(i+1<starts.length)?starts[i+1]:(s+WEEK);
        if(t>=s && t<=e){{
          return (t-s)/(e-s)*100;
        }}
      }}
      return null;
    }}
    var ideal=xs.map(interpIdeal);
    // Actual: only at real sample points; null elsewhere to keep line only where we have data
    var actMap={{}};
    for(var j=0;j<actualX.length;j++){{ actMap[actualX[j]]=actualY[j]; }}
    var actual=xs.map(function(x){{return (x in actMap)?actMap[x]:null;}});
    var w=host.clientWidth||680;
    var opts={{
      width:w, height:260,
      scales:{{ x:{{time:true, range:[Math.max(cutoff,xs[0]), Math.max(nowSec, xs[xs.length-1])]}}, y:{{range:[0,105]}} }},
      series:[
        {{}},
        {{label:'Ideallinie (linear)', stroke:'rgba(248,113,113,.7)', width:1.5, dash:[4,4], points:{{show:false}}, spanGaps:false}},
        {{label:'Ist-Verbrauch %',     stroke:'#7aa2ff', width:2, points:{{show:false}}, spanGaps:true}}
      ],
      axes:[
        {{stroke:'#b8bdd0', grid:{{stroke:'rgba(255,255,255,.06)'}}}},
        {{stroke:'#b8bdd0', grid:{{stroke:'rgba(255,255,255,.06)'}}, values:function(u,v){{return v.map(function(x){{return x+'%';}});}}}}
      ],
      hooks:{{
        draw:[function(u){{
          if(!starts.length) return;
          var ctx=u.ctx;
          ctx.save();
          ctx.strokeStyle='rgba(255,255,255,.18)';
          ctx.setLineDash([2,3]);
          starts.forEach(function(m){{
            if(m<u.scales.x.min||m>u.scales.x.max) return;
            var x=u.valToPos(m,'x',true);
            ctx.beginPath();
            ctx.moveTo(x,u.bbox.top);
            ctx.lineTo(x,u.bbox.top+u.bbox.height);
            ctx.stroke();
          }});
          ctx.restore();
        }}]
      }}
    }};
    new uPlot(opts,[xs,ideal,actual],host);
  }}
  function buildHeatmap(rows){{
    var host=document.getElementById('heatmap');
    if(!host) return;
    if(!rows.length){{ show('empty-heatmap'); return; }}
    var leg1=document.getElementById('hm-legend');
    var leg2=document.getElementById('hm-legend-bar');
    if(leg1) leg1.style.display='flex';
    if(leg2) leg2.style.display='flex';
    var cutoff=(Date.now()/1000)-30*86400;
    var sums=new Array(7*24).fill(0);
    var counts=new Array(7*24).fill(0);
    rows.forEach(function(r){{
      var t=Date.parse(r.t)/1000;
      if(isNaN(t)||t<cutoff) return;
      var d=new Date(t*1000);
      var dow=(d.getDay()+6)%7; // Mon=0
      var hr=d.getHours();
      var idx=dow*24+hr;
      sums[idx]+=r.sess;
      counts[idx]++;
    }});
    var means=sums.map(function(s,i){{return counts[i]?s/counts[i]:null;}});
    var days=['Mo','Di','Mi','Do','Fr','Sa','So'];
    var html='<div class="hd"></div>';
    for(var h=0;h<24;h++) html+='<div class="hd">'+(h%3===0?h:'')+'</div>';
    for(var d=0;d<7;d++){{
      html+='<div class="rl">'+days[d]+'</div>';
      for(var h2=0;h2<24;h2++){{
        var v=means[d*24+h2];
        var bg='rgba(255,255,255,.05)';
        if(v!=null){{
          var a=Math.max(0.08,Math.min(1,v/80));
          // gradient low=green, high=red
          var hue=Math.max(0, 130 - Math.round(v*1.3));
          bg='hsla('+hue+',70%,55%,'+a.toFixed(2)+')';
        }}
        html+='<div class="cell" style="background:'+bg+'" title="'+days[d]+' '+h2+':00 → '+(v==null?'–':Math.round(v)+'%')+'"></div>';
      }}
    }}
    host.innerHTML=html;
  }}
  function renderSummary(summary){{
    if(!summary) return;
    document.getElementById('t-norm').textContent=fmtPct(summary.avg_norm_week);
    document.getElementById('t-heavy').textContent=(summary.heavy_per_week==null?'–':summary.heavy_per_week);
    document.getElementById('t-remaining').textContent=(summary.remaining_full_sessions==null?'–':summary.remaining_full_sessions.toFixed(1));
    document.getElementById('t-current').textContent=fmtPct(summary.current_norm_week);
  }}
  loadAll().then(function(res){{
    var rows=parseJsonl(res[0]);
    var payload=res[1]||{{}};
    if(payload && payload.summary) renderSummary(payload.summary);
    buildWeekly(rows);
    buildHeatmap(rows);
  }});
}})();
(function(){{
  // Live countdown to session reset. Data source: data-target ISO UTC on .countdown elements.
  function pad(n){{return n<10?('0'+n):(''+n);}}
  function fmt(remSec){{
    if(remSec<=0) return '00:00:00';
    var h=Math.floor(remSec/3600);
    var m=Math.floor((remSec%3600)/60);
    var s=remSec%60;
    return pad(h)+':'+pad(m)+':'+pad(s);
  }}
  function tick(){{
    var nodes=document.querySelectorAll('.countdown[data-target]');
    var now=Date.now();
    nodes.forEach(function(node){{
      var ts=Date.parse(node.getAttribute('data-target'));
      if(isNaN(ts)) return;
      var rem=Math.max(0, Math.floor((ts-now)/1000));
      var val=node.querySelector('.cd-value');
      if(val) val.textContent=fmt(rem);
    }});
  }}
  tick();
  setInterval(tick, 1000);
}})();
</script>
</body>
</html>
"""


def write_html(path: Path, data: dict) -> None:
    path.write_text(render(data), encoding="utf-8")
