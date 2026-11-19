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
.q{display:inline-flex;align-items:center;justify-content:center;width:17px;height:17px;border-radius:50%;border:1px solid var(--mut);color:var(--mut);font:600 11px system-ui,sans-serif;cursor:help;margin-left:6px;vertical-align:1px;user-select:none}.q:hover,.q:focus,.q.open{border-color:var(--acc);color:var(--acc);outline:none}.q .tip{display:none;position:fixed;z-index:20;width:min(340px,calc(100vw - 32px));background:var(--fg);color:var(--bg);font:400 13px/1.5 system-ui,sans-serif;padding:10px 12px;border-radius:8px;box-shadow:0 6px 24px rgba(0,0,0,.25);text-align:left;cursor:auto}.q .tip code{background:none;color:inherit;font-size:12px;overflow-wrap:anywhere}.q:hover .tip,.q:focus .tip,.q.open .tip{display:block}.q .tip::before{content:"";position:absolute;left:0;right:0;top:-12px;height:12px}.q .tip.up::before{top:auto;bottom:-12px}.intro{font-size:14px;margin:0 0 12px}details.adv{margin-top:10px}details.adv summary{cursor:pointer;color:var(--mut);font-size:13px}code{font:13px ui-monospace,monospace}
</style></head><body><main>
<h1>Brain hub</h1><p class="sub" id="sub">Settings</p>

<section id="login" class="hide"><h2>Sign in</h2>
<label for="pw">Settings password<span class="q" tabindex="0">?<span class="tip">The password the installer printed once when this hub was installed, or the one you set yourself later on this page. Forgot it? See the last section of this page once you're in, or run the reset command on the hub's machine: <code>docker exec $(docker compose ps -q brain-hub) python -m hub.local reset-password</code></span></span></label>
<input id="pw" type="password" autocomplete="current-password">
<p><button class="p" onclick="login()">Sign in</button></p><p id="loginmsg" class="msg bad"></p></section>

<div id="app" class="hide">
<section><h2>Waiting for approval</h2>
<p class="intro">A computer or an app that asks to use this hub's memory appears here first. It shows a code on its own screen. Approve it only if that code is the same as the one here; if you don't recognise it, Deny.</p>
<div id="pending"></div></section>

<section><h2>Devices</h2>
<p class="intro">Every computer and app allowed to use the memory. Each one counts toward your licence's devices. Tick the memories each one may use. Remove stops it at once; you can connect it again later.</p>
<div id="devices"></div></section>

<section><h2>Licence</h2>
<p class="intro">The licence sets how many devices this hub may approve: 5 are free, shared by all the hubs on your Brethof account. The hub renews it by itself every month. For that it sends only its id, its name and how many devices it has, never anything from your memory. If the licence lapses, devices you already approved keep working; only new ones wait.</p>
<div id="licence"></div><p id="licmsg" class="msg"></p></section>

<section><h2>The memory's own AI model</h2>
<p class="intro">The memory uses an AI model of its own to read your conversations and decide what to keep: it writes the records, updates them when something changes, and retires what is no longer true. This is not the model your agent uses; you choose it here, from any provider. Without one, every conversation is still saved and can still be searched, but no records are written.</p>
<div class="row"><div><label for="ai_url">Address<span class="q" tabindex="0">?<span class="tip">Where the model is reached. It must accept the OpenAI chat format, which almost every provider and local model server does. The address ends in <code>/v1</code>.<br><br>Ollama Cloud: <code>https://ollama.com/v1</code><br>OpenRouter: <code>https://openrouter.ai/api/v1</code><br>OpenAI: <code>https://api.openai.com/v1</code><br><br>A model on this same machine: not <code>localhost</code>, which is the hub's own container. Use <code>http://host.docker.internal:11434/v1</code> (Docker) or <code>http://host.containers.internal:11434/v1</code> (Podman).</span></span></label><input id="ai_url" placeholder="https://ollama.com/v1"></div>
<div><label for="ai_model">Model name<span class="q" tabindex="0">?<span class="tip">The model's exact name at that provider, spelled as in their model list, for example <code>glm-5.3-flash</code> on Ollama Cloud. Pick a model that follows instructions well: a small model writes poor records.</span></span></label><input id="ai_model" placeholder="e.g. glm-5.3-flash"></div></div>
<label for="ai_key">Key<span class="q" tabindex="0">?<span class="tip">The API key from your account at that provider. It is kept only in this hub and is never sent to Brethof. Leave the box empty to keep the key already saved. A model on your own machine usually needs no key.</span></span></label><input id="ai_key" type="password" placeholder="unchanged">
<details class="adv"><summary>Advanced: most people leave these empty</summary>
<div class="row"><div><label for="ai_reasoning">Reasoning effort<span class="q" tabindex="0">?<span class="tip">Only for models that think before they answer: how much. <code>low</code> is fast, cheap and enough for this job. Leave it empty if your model has no such setting; a model that doesn't understand it may refuse the call.</span></span></label><input id="ai_reasoning" placeholder="e.g. low"></div>
<div><label for="ai_extra">Extra request options<span class="q" tabindex="0">?<span class="tip">Anything else your provider needs in every request, written as JSON. Leave it empty unless the provider's documentation asks for it. Example, to switch thinking off for Qwen on vLLM:<br><code>{"chat_template_kwargs": {"enable_thinking": false}}</code></span></span></label><input id="ai_extra" placeholder="{}"></div></div></details>
<label for="decision_style">Decision model (optional)<span class="q" tabindex="0">?<span class="tip">A second, very cheap model for the many small yes-or-no checks, for example "does this conversation overturn something the memory already holds?". It answers with probabilities, not text, so it costs a fraction of the AI model.<br><br><b>Off</b>: the AI model decides alone. Fine to start with.<br><b>Use the AI model</b>: the same checks, asked of the AI model. Works with any model; more calls, so it costs more.<br><b>A separate decision model</b>: only a model built for this, which takes questions and returns probabilities (such as Jev on Cloudflare Workers AI). An ordinary chat model does not work here.</span></span></label>
<select id="decision_style"><option value="">Off</option><option value="ai">Use the AI model for the checks too (costs more)</option><option value="decision">A separate decision model</option></select>
<div id="dec" class="hide"><div class="row"><div><label for="decision_url">Decision model address<span class="q" tabindex="0">?<span class="tip">The address the provider gives for running the model. For Cloudflare Workers AI: <code>https://api.cloudflare.com/client/v4/accounts/&lt;your account id&gt;/ai/run</code></span></span></label><input id="decision_url"></div>
<div><label for="decision_model">Decision model name<span class="q" tabindex="0">?<span class="tip">The model's name at that provider, for example <code>typesafe/jev</code>.</span></span></label><input id="decision_model"></div></div>
<label for="decision_key">Decision model key<span class="q" tabindex="0">?<span class="tip">The API key or token from that provider. Kept only in this hub. Leave it empty to keep the saved one.</span></span></label><input id="decision_key" type="password" placeholder="unchanged"></div>
<p><button class="p" onclick="saveModels()">Save</button> <button onclick="testModels()">Test</button><span class="q" tabindex="0">?<span class="tip"><b>Save</b> keeps the settings; the memory uses them from its next decision. <b>Test</b> sends each model one short question and shows whether it answered and how long it took. Save first, then Test.</span></span></p><p id="modmsg" class="msg"></p></section>

<section><h2>Memories</h2>
<p class="intro">A memory is one separate store: its own records, conversations and projects. Most people need only the one made at install. Add another to keep something fully apart, such as a team memory or one per client, then tick it on the devices that may use it.</p>
<div id="memories"></div>
<details class="adv"><summary>Add a memory</summary>
<div class="row"><div><label for="mname">Name<span class="q" tabindex="0">?<span class="tip">A short name you'll recognise in the Devices list, for example <code>team</code>.</span></span></label><input id="mname" placeholder="team"></div>
<div><label for="murl">Its address on the hub's network<span class="q" tabindex="0">?<span class="tip">Each extra memory is its own storage container running next to the hub. Start that container first, then enter the address the hub reaches it at, for example <code>http://brain-storage-team:8610</code>. After you add it, this page shows a key once: put it in that storage container's settings as <code>BRAIN_HUB_KEY</code> and restart it.</span></span></label><input id="murl" placeholder="http://brain-storage-team:8610"></div></div>
<p><button onclick="addMemory()">Add memory</button></p></details><p id="memmsg" class="msg"></p></section>

<section><h2>Connect an app without a plugin</h2>
<p class="intro">Some AI apps let you change the address they send their model calls to, such as Claude Code and Grok Build. Point that address at this hub and the app gets the memory with nothing installed. The app still signs in with its own account and uses its own plan. The hub adds what the memory knows to each of your messages, passes the call on to the app's own service, and saves the conversation. Each app connected this way counts as one device.</p>
<div class="row"><div><label for="appname">App name<span class="q" tabindex="0">?<span class="tip">Any name you'll recognise in the Devices list, for example "Claude Code on my laptop". Connect each app on each computer separately, so you can remove one without the others.</span></span></label><input id="appname" placeholder="Claude Code on my laptop"></div>
<div><label for="appsvc">The service the app normally talks to<span class="q" tabindex="0">?<span class="tip">Where the hub passes the app's calls on to:<br><b>anthropic</b>: Claude Code, on a Claude plan or an API key<br><b>grok</b>: Grok Build on a Grok subscription<br><b>xai</b>: apps on an xAI API key<br><b>openai</b>, <b>openrouter</b>, <b>ollama-cloud</b>: apps on those providers' keys<br>Not listed? Add it below.</span></span></label><select id="appsvc"></select></div></div>
<p><button class="p" onclick="addApp()">Connect app</button></p><p id="appmsg" class="msg"></p>
<details class="adv"><summary>Add a service that isn't listed</summary>
<div class="row"><div><label for="svcname">Service name<span class="q" tabindex="0">?<span class="tip">A short name for it, for example <code>mylab</code>. It then appears in the list above.</span></span></label><input id="svcname" placeholder="mylab"></div>
<div><label for="svcurl">Its address, without /v1<span class="q" tabindex="0">?<span class="tip">The service's base address with nothing after it, for example <code>https://models.example.com</code>. The app's own path (such as /v1/chat/completions) is added to it unchanged.</span></span></label><input id="svcurl" placeholder="https://models.example.com"></div></div>
<p><button onclick="addSvc()">Add service</button></p></details></section>

<section><h2>Settings page login</h2>
<p class="intro">This page can approve devices and change every setting, so by default it asks for a password. Change it here. Turn the login off only if this page can't be reached from other machines, or if you protect it yourself (your own VPN, proxy or login in front). Either way, websites you visit can't use this page.</p>
<div id="loginbox"></div>
<label for="newpw">New password<span class="q" tabindex="0">?<span class="tip">At least 12 characters. It replaces the one the installer printed; that one stops working.</span></span></label><input id="newpw" type="password" autocomplete="new-password">
<p><button onclick="setPw()">Set password</button></p><p id="pwmsg" class="msg"></p>
<p class="note">Forgot it? On the hub's machine, run this, restart the hub, then sign in with the new password it prints: <code>docker exec $(docker compose ps -q brain-hub) python -m hub.local reset-password</code></p></section>
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
  `<tr><td>${esc(d.name)}</td><td>${mems.map(m=>`<label class="chk"><input type="checkbox" ${d.memories.includes(m)?"checked":""} onchange="setMem('${esc(d.id)}')" data-dev="${esc(d.id)}" value="${esc(m)}">${esc(m)}</label>`).join("")}</td><td class="mut">${when(d.last_seen)}</td><td><button class="d" onclick="removeDev('${esc(d.id)}')">Remove</button></td></tr>`).join("")+"</table>":`<p class="mut">No devices yet. To connect a computer, run these two lines on it, then approve the code that appears under Waiting for approval:</p><p><code>pip install "git+https://github.com/BrethofAI/brethof-brain-client.git"</code><br><code>brethof-brain pair ${esc(location.origin)}</code></p><p class="note">On another computer, use the address that computer reaches this hub at instead of ${esc(location.host)}. To connect an app with nothing installed, see "Connect an app without a plugin" below.</p>`;
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
 if(!r.ok){$("appmsg").textContent=r.why;return}
 const full=location.origin+r.path,bare=full.replace(/\/v1$/,"");
 $("appmsg").innerHTML=($("appsvc").value==="anthropic"
  ?`Connected. Start Claude Code with this address (copy it now: it is shown once and carries the app's key):<br><code>ANTHROPIC_BASE_URL=${esc(bare)} claude</code><br><span class="mut">Claude Code keeps its own sign-in. Without the address it simply runs without the memory.</span>`
  :`Connected. In the app's settings, set its model address (often called "base URL" or "API base") to this. Copy it now: it is shown once and carries the app's key.<br><code>${esc(full)}</code><br><span class="mut">If the app adds /v1 by itself, leave the /v1 off: <code>${esc(bare)}</code></span>`)
  +`<br><span class="mut">If this page is open on another machine than the app, replace the address part before /model with the one the app's machine reaches the hub at.</span>`;load()}
async function addSvc(){const r=await api("/admin/api/upstream/set",{name:$("svcname").value,url:$("svcurl").value});if(!r.ok)alert(r.why);apps()}
async function addMemory(){const r=await api("/admin/api/memory/add",{name:$("mname").value,url:$("murl").value});
 $("memmsg").innerHTML=r.ok?`Added. Its key (shown once): <code>${esc(r.key)}</code><br>${esc(r.note)}`:esc(r.why);load()}
async function renew(){const r=await api("/admin/api/renew",{});$("licmsg").textContent=r.ok?"Renewed.":r.why;load()}
async function activate(){const r=await api("/admin/api/activate/start",{});if(!r.ok){$("licmsg").textContent=r.why;return}
 const go=r.verification_uri+"?code="+encodeURIComponent(r.user_code)+"&return="+encodeURIComponent(location.origin+"/settings");
 $("licence").innerHTML=`<p><a class="btn p" href="${esc(go)}" target="_blank" rel="noopener">Open brethof.ai to approve</a></p><p>Sign in there and approve this code:</p><p class="code">${esc(r.user_code)}</p><p class="mut">Waiting… this page updates by itself once you approve.</p>`;
 const until=Date.now()+r.expires_in*1000;const tick=async()=>{const p=await api("/admin/api/activate/poll",{});
  if(p.status==="ok"){$("licmsg").textContent="Connected. Next: connect your first device (below).";load();return}if(p.status==="denied"||p.status==="expired"||Date.now()>until||!p.ok){$("licmsg").textContent="Not connected ("+(p.status||p.why)+"). Try again.";load();return}setTimeout(tick,(r.interval||5)*1000)};setTimeout(tick,(r.interval||5)*1000)}
function tipAt(q){const t=q.querySelector(".tip");if(!t)return;t.style.display="block";const r=q.getBoundingClientRect(),w=t.offsetWidth,h=t.offsetHeight;
 let x=Math.min(Math.max(16,r.left+r.width/2-w/2),innerWidth-w-16),y=r.bottom+8;let up=false;if(y+h>innerHeight-8){y=Math.max(8,r.top-h-8);up=true}t.classList.toggle("up",up);
 t.style.left=x+"px";t.style.top=y+"px";t.style.display=""}
document.addEventListener("mouseover",e=>{const q=e.target.closest&&e.target.closest(".q");if(q)tipAt(q)});
document.addEventListener("focusin",e=>{const q=e.target.closest&&e.target.closest(".q");if(q)tipAt(q)});
document.addEventListener("click",e=>{const q=e.target.closest&&e.target.closest(".q");if(q&&e.target.closest(".tip"))return;
 document.querySelectorAll(".q.open").forEach(o=>{if(o!==q)o.classList.remove("open")});if(q){e.preventDefault();q.classList.toggle("open");tipAt(q)}});
addEventListener("scroll",()=>document.querySelectorAll(".q.open,.q:hover,.q:focus").forEach(tipAt),{passive:true});
$("pw").addEventListener("keydown",e=>{if(e.key==="Enter")login()});
load();setInterval(()=>{if(S&&!$("app").classList.contains("hide"))api("/admin/api/state").then(r=>{if(r.ok&&JSON.stringify(r.pending)!==JSON.stringify(S.pending)){S=r;render()}})},5000);
</script></body></html>"""
