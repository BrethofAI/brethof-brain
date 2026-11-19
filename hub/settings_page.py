"""The hub's settings page: one self-contained HTML page over /admin/api/*.
No build step, no external assets — it runs on a machine with no internet."""

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Brain hub settings</title>
<style>
:root{--bg:#f7f7f5;--card:#fff;--fg:#1c1c1a;--mut:#6b6b66;--line:#e2e2dd;--acc:#2f5bd3;--bad:#b3261e;--ok:#1e7a3c}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--card:#1d1d1b;--fg:#ecece8;--mut:#9a9a94;--line:#33332f;--acc:#7d9cf0;--bad:#f2867e;--ok:#6fcf8f}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
.sub{color:var(--mut);margin:0 0 20px}section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:0 0 16px}
h2{font-size:16px;margin:0 0 10px}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:7px 6px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:500;border-top:0;font-size:13px}button{font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.p,a.p{background:var(--acc);border-color:var(--acc);color:#fff}a.btn{display:inline-block;text-decoration:none;font:inherit;padding:6px 12px;border-radius:7px;border:1px solid var(--line)}button.d{color:var(--bad)}input,select{font:inherit;padding:6px 8px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);width:100%}
label{display:block;font-size:13px;color:var(--mut);margin:8px 0 3px}.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}@media(max-width:600px){.row{grid-template-columns:1fr}}
.code{font:600 18px ui-monospace,monospace;letter-spacing:1px}.mut{color:var(--mut)}.ok{color:var(--ok)}.bad{color:var(--bad)}.note{font-size:13px;color:var(--mut);margin-top:8px}
.msg{margin:8px 0 0;font-size:14px}.hide{display:none}.chk{display:inline-flex;gap:4px;align-items:center;margin-right:10px;width:auto}.chk input{width:auto}
</style></head><body><main>
<h1>Brain hub</h1><p class="sub" id="sub">Settings</p>

<section id="login" class="hide"><h2>Sign in</h2>
<label for="pw">Settings password (printed when the hub was installed)</label>
<input id="pw" type="password" autocomplete="current-password">
<p><button class="p" onclick="login()">Sign in</button></p><p id="loginmsg" class="msg bad"></p></section>

<div id="app" class="hide">
<section><h2>Waiting for approval</h2><div id="pending"></div>
<p class="note">A device shows the same code on its own screen. Approve only codes you recognise.</p></section>

<section><h2>Devices</h2><div id="devices"></div></section>

<section><h2>Licence</h2><div id="licence"></div><p id="licmsg" class="msg"></p></section>

<section><h2>Models</h2>
<div class="row"><div><label>AI model — base URL (OpenAI-compatible, ends in /v1)</label><input id="ai_url" placeholder="http://localhost:11434/v1"></div>
<div><label>AI model — name</label><input id="ai_model" placeholder="model name"></div></div>
<div class="row"><div><label>AI model — key</label><input id="ai_key" type="password" placeholder="unchanged"></div>
<div><label>Reasoning effort (optional, e.g. low)</label><input id="ai_reasoning"></div></div>
<label>Extra request options (optional JSON, e.g. {"chat_template_kwargs": {"enable_thinking": false}})</label><input id="ai_extra">
<label>Decision model</label><select id="decision_style"><option value="">Off</option>
<option value="decision">A decision model endpoint</option><option value="ai">Use the AI model (costs more)</option></select>
<div id="dec" class="hide"><div class="row"><div><label>Decision model — URL</label><input id="decision_url"></div>
<div><label>Decision model — name</label><input id="decision_model"></div></div>
<label>Decision model — key</label><input id="decision_key" type="password" placeholder="unchanged"></div>
<p><button class="p" onclick="saveModels()">Save</button> <button onclick="testModels()">Test</button></p><p id="modmsg" class="msg"></p></section>

<section><h2>Memories</h2><div id="memories"></div>
<div class="row"><div><label>New memory — name</label><input id="mname" placeholder="team"></div>
<div><label>Its address on the hub's network</label><input id="murl" placeholder="http://brain-storage-team:8610"></div></div>
<p><button onclick="addMemory()">Add memory</button></p><p id="memmsg" class="msg"></p></section>
<section><h2>Apps on their own plan</h2>
<p class="note">For an app that lets you set its model's address (Grok Build, Claude Code, Codex…): the app keeps its own sign-in and plan; its calls go through this hub, which adds the memory and keeps the conversation. The app counts as a device.</p>
<div class="row"><div><label>App name</label><input id="appname" placeholder="Grok on my laptop"></div>
<div><label>Its model service</label><select id="appsvc"></select></div></div>
<p><button onclick="addApp()">Connect app</button></p><p id="appmsg" class="msg"></p>
<div class="row"><div><label>Another model service — name</label><input id="svcname" placeholder="mylab"></div>
<div><label>Its base address (without /v1)</label><input id="svcurl" placeholder="https://models.example.com"></div></div>
<p><button onclick="addSvc()">Add service</button></p></section>
<section><h2>Settings page login</h2><div id="loginbox"></div>
<label for="newpw">New password (at least 12 characters)</label><input id="newpw" type="password" autocomplete="new-password">
<p><button onclick="setPw()">Set password</button></p><p id="pwmsg" class="msg"></p>
<p class="note">Forgot it? On the hub's machine: <code>docker exec $(docker compose ps -q brain-hub) python -m hub.local reset-password</code></p></section>
<p id="signout"><button onclick="logout()">Sign out</button></p>
</div>
</main>
<script>
let S=null;const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const when=t=>t?new Date(t*1000).toLocaleString():"never";
async function api(p,b){const r=await fetch(p,{method:b===undefined?"GET":"POST",headers:{"Content-Type":"application/json"},body:b===undefined?undefined:JSON.stringify(b),credentials:"same-origin"});
 let j={};try{j=await r.json()}catch(e){}if(r.status===401&&p!=="/admin/login"){show(false)}return {ok:r.ok&&j.ok!==false,...j}}
function show(on){$("login").classList.toggle("hide",on);$("app").classList.toggle("hide",!on)}
async function login(){const r=await api("/admin/login",{password:$("pw").value});if(r.ok){load()}else $("loginmsg").textContent=r.why||"Wrong password"}
async function setPw(){const r=await api("/admin/api/password",{password:$("newpw").value});$("newpw").value="";$("pwmsg").textContent=r.ok?"Password changed.":r.why;load()}
async function setLogin(on){if(!on&&!confirm("Turn the settings login off? Anyone who can reach this page could approve devices and read the memory. Only do this if it can be opened from this machine alone, or you protect it yourself."))return;const r=await api("/admin/api/login",{on});if(!r.ok)alert(r.why);load()}
async function logout(){await api("/admin/logout",{});show(false)}
async function load(){const r=await api("/admin/api/state");if(!r.ok){show(false);return}S=r;show(true);render();apps()}
function render(){
 $("sub").textContent=S.hub.name+" · "+S.hub.id;
 const mems=S.memories.map(m=>m.name);
 $("pending").innerHTML=S.pending.length?"<table><tr><th>Code</th><th>Device</th><th>Memory</th><th></th></tr>"+S.pending.map(p=>
  `<tr><td class="code">${esc(p.code)}</td><td>${esc(p.name)}</td><td>${esc(p.memory||mems[0]||"")}</td><td><button class="p" onclick="approve('${esc(p.pair)}')">Approve</button> <button onclick="deny('${esc(p.pair)}')">Deny</button></td></tr>`).join("")+"</table>":'<p class="mut">No device is waiting.</p>';
 $("devices").innerHTML=S.devices.length?"<table><tr><th>Name</th><th>Memories</th><th>Last used</th><th></th></tr>"+S.devices.map(d=>
  `<tr><td>${esc(d.name)}</td><td>${mems.map(m=>`<label class="chk"><input type="checkbox" ${d.memories.includes(m)?"checked":""} onchange="setMem('${esc(d.id)}')" data-dev="${esc(d.id)}" value="${esc(m)}">${esc(m)}</label>`).join("")}</td><td class="mut">${when(d.last_seen)}</td><td><button class="d" onclick="removeDev('${esc(d.id)}')">Remove</button></td></tr>`).join("")+"</table>":'<p class="mut">No devices yet. On a device, run: <code>brethof-brain pair &lt;this hub\'s address&gt;</code></p>';
 const L=S.licence;let h="";
 if(L.state==="none")h=`<p>This hub is not connected to a Brethof account yet. Connect it to approve devices (${5} are free).</p><button class="p" onclick="activate()">Connect to my account</button>`;
 else h=`<p>${L.state==="valid"?'<span class="ok">Active</span>':L.state==="expired"?'<span class="bad">Expired — approved devices keep working; new ones wait until it renews</span>':'<span class="bad">Not valid</span>'} · ${L.in_use} of ${L.devices} devices in use${L.expires?(L.refused?" · runs until ":" · renews before ")+when(L.expires):""}</p>${L.refused?`<p class="bad">${esc(L.refused)} Approved devices keep working; when it runs out, new devices cannot be approved.</p>`:""}<button onclick="renew()">Renew now</button>`;
 if(L.state!=="none"&&L.refused)h+=` <button class="p" onclick="activate()">Connect to my account</button>`;
 $("licence").innerHTML=h;
 const off=S.login==="off";
 $("loginbox").innerHTML=off?`<p><span class="bad">Off</span>: anyone who can open this page can approve devices and change settings. Websites you visit still cannot.</p><button class="p" onclick="setLogin(true)">Turn the login on</button>`
  :`<p><span class="ok">On</span>: this page asks for the settings password.</p><button onclick="setLogin(false)">Turn the login off</button>`;
 $("signout").classList.toggle("hide",off);
 const M=S.models;for(const k of ["ai_url","ai_model","ai_reasoning","ai_extra","decision_url","decision_model"])$(k).value=M[k]||"";
 $("decision_style").value=M.decision_style||"";$("ai_key").placeholder=M.ai_key_set?"unchanged (set)":"not set";$("decision_key").placeholder=M.decision_key_set?"unchanged (set)":"not set";dec();
 $("memories").innerHTML="<table><tr><th>Name</th><th>Address</th></tr>"+S.memories.map(m=>`<tr><td>${esc(m.name)}</td><td class="mut">${esc(m.url)}</td></tr>`).join("")+"</table>";
}
function dec(){$("dec").classList.toggle("hide",$("decision_style").value!=="decision")}
$("decision_style").onchange=dec;
async function approve(p){const r=await api("/admin/api/approve",{pair:p});if(!r.ok)alert(r.why);load()}
async function deny(p){await api("/admin/api/deny",{pair:p});load()}
async function removeDev(id){const n=(S.devices.find(d=>d.id===id)||{}).name||"this device";if(!confirm("Remove "+n+"? Its key stops working at once."))return;await api("/admin/api/device/remove",{id});load()}
async function setMem(id){const ms=[...document.querySelectorAll(`input[data-dev="${id}"]:checked`)].map(i=>i.value);const r=await api("/admin/api/device/memories",{id,memories:ms});if(!r.ok)alert(r.why);load()}
async function saveModels(){const b={};for(const k of ["ai_url","ai_model","ai_key","ai_reasoning","ai_extra","decision_style","decision_url","decision_model","decision_key"])b[k]=$(k).value;
 const r=await api("/admin/api/models",b);$("modmsg").textContent=r.ok?"Saved.":r.why;$("ai_key").value="";$("decision_key").value="";load()}
async function testModels(){$("modmsg").textContent="Testing…";const r=await api("/admin/api/models/test",{});
 if(!r.ok){$("modmsg").textContent=r.why;return}let t=r.ai.ok?`AI model answered in ${r.ai.seconds}s.`:`AI model failed: ${r.ai.why}.`;
 if(r.decision)t+=r.decision.ok?` Decision model answered in ${r.decision.seconds}s.`:" Decision model failed.";$("modmsg").textContent=t}
async function apps(){const r=await api("/admin/api/apps");if(!r.ok)return;$("appsvc").innerHTML=r.services.map(x=>`<option value="${esc(x.name)}">${esc(x.name)} — ${esc(x.url)}</option>`).join("")}
async function addApp(){const r=await api("/admin/api/app/add",{name:$("appname").value,service:$("appsvc").value});
 $("appmsg").innerHTML=r.ok?`Connected. Set the app's model base address to (shown once — it carries the app's key):<br><code>${esc(location.origin+r.path)}</code>`:esc(r.why);load()}
async function addSvc(){const r=await api("/admin/api/upstream/set",{name:$("svcname").value,url:$("svcurl").value});if(!r.ok)alert(r.why);apps()}
async function addMemory(){const r=await api("/admin/api/memory/add",{name:$("mname").value,url:$("murl").value});
 $("memmsg").innerHTML=r.ok?`Added. Its key (shown once): <code>${esc(r.key)}</code><br>${esc(r.note)}`:esc(r.why);load()}
async function renew(){const r=await api("/admin/api/renew",{});$("licmsg").textContent=r.ok?"Renewed.":r.why;load()}
async function activate(){const r=await api("/admin/api/activate/start",{});if(!r.ok){$("licmsg").textContent=r.why;return}
 const go=r.verification_uri+"?code="+encodeURIComponent(r.user_code)+"&return="+encodeURIComponent(location.origin+"/settings");
 $("licence").innerHTML=`<p><a class="btn p" href="${esc(go)}" target="_blank" rel="noopener">Open brethof.ai to approve</a></p><p>Sign in there and approve this code:</p><p class="code">${esc(r.user_code)}</p><p class="mut">Waiting… this page updates by itself once you approve.</p>`;
 const until=Date.now()+r.expires_in*1000;const tick=async()=>{const p=await api("/admin/api/activate/poll",{});
  if(p.status==="ok"){$("licmsg").textContent="Connected. Next: connect your first device (below).";load();return}if(p.status==="denied"||p.status==="expired"||Date.now()>until||!p.ok){$("licmsg").textContent="Not connected ("+(p.status||p.why)+"). Try again.";load();return}setTimeout(tick,(r.interval||5)*1000)};setTimeout(tick,(r.interval||5)*1000)}
$("pw").addEventListener("keydown",e=>{if(e.key==="Enter")login()});
load();setInterval(()=>{if(S&&!$("app").classList.contains("hide"))api("/admin/api/state").then(r=>{if(r.ok&&JSON.stringify(r.pending)!==JSON.stringify(S.pending)){S=r;render()}})},5000);
</script></body></html>"""
