import {drawLiveCalendar} from "./live_calendar.js";
const bridge = window.AstrBotPluginPage;
const $ = id => document.getElementById(id);
const types = {video:"视频",draw:"图文",word:"文字",forward:"转发",article:"专栏",other:"其他"};
const labels = {active:"运行中",paused:"已暂停",blocked:"已禁用",error:"接口异常",initializing:"初始化中",pending:"待处理",preparing:"准备中",sending:"发送中",retry:"等待重试",uncertain:"待核对",failed:"失败",done:"已完成",cancelled:"已取消",skipped:"已跳过",sent:"已发送",running:"检查中"};
let state = null, editing = null, availableGroups = [], settingsLoaded = false, busy = false;
const stamp = n => n ? new Date(n * 1000).toLocaleString("zh-CN", {hour12:false}) : "尚未成功";
function node(tag, text, cls) { const el = document.createElement(tag); if(text !== undefined) el.textContent = text; if(cls) el.className = cls; return el; }
function notify(message, error=false) { $("notice").textContent=message; $("notice").className=error?"error":""; $("notice").hidden=false; }
function badge(status) {return node("span",labels[status] || status, "state " + status);}
function button(text, fn, danger=false) {const b=node("button",text,"small " + (danger?"danger":"secondary")); b.type="button"; b.onclick=async()=>{b.disabled=true;try {await fn();} catch(e){notify(e.message,true);} finally{b.disabled=false;}};return b;}
const post = body => bridge.apiPost("subscriptions/action",body);
function confirmAction(text){const dialog=$("confirmation");$("confirmationText").textContent=text;dialog.returnValue="cancel";dialog.showModal();return new Promise(resolve=>dialog.addEventListener("close",()=>resolve(dialog.returnValue==="ok"),{once:true}));}
async function refresh() {
  if(busy) return; busy=true;
  try {
    state=await bridge.apiGet("subscriptions/state");
    $("metricSubs").textContent=state.subscriptions.length; $("metricCreators").textContent=state.creators;
    $("metricPending").textContent=["pending","retry","preparing","sending"].reduce((n,k)=>n+(state.counts[k]||0),0); $("metricDone").textContent=state.counts.done||0;
    $("connection").className="banner " + (state.enabled?"on":"off");
    $("connection").textContent=(state.enabled?"动态检查已开启。":"动态总开关已关闭；可先添加订阅，再到设置开启。") + (state.settings.live_enabled?" 直播检查已开启。":" 直播检查已关闭。") + (state.worker_error?" "+state.worker_error:"");
    renderSubscriptions(); renderDeliveries(); if(!settingsLoaded) loadSettings();
  } catch(e) {notify("连接失败："+e.message,true);} finally {busy=false;}
}
function renderSubscriptions() {
  const query=$("search").value.trim().toLowerCase(), root=$("subscriptionRows"); root.replaceChildren();
  const rows=state.subscriptions.filter(s=>[s.name,s.uid,s.group_id,s.group_name].some(x=>String(x).toLowerCase().includes(query)));
  $("empty").hidden=rows.length>0; $("empty").textContent=query?"没有匹配的订阅。":"还没有订阅。添加一个 UP 主，或在 QQ 群发送「bili 订阅 UID」。";
  for(const s of rows){
    const tr=node("tr"), creator=node("td",s.name);creator.append(node("small","UID "+s.uid));tr.append(creator);
    const group=node("td",s.group_name);group.append(node("small",s.group_id+" · "+s.platform+" / "+s.bot_id));tr.append(group);
    const content=node("td",s.types.map(t=>types[t]).join("、"));content.append(node("small","视频文件："+(s.send_video?"开":"关")),node("small","直播通知："+(s.live_notify?"开":"关")+" · 记录："+(s.live_record?"开":"关")));tr.append(content);
    const status=node("td");status.append(badge(s.state),node("small",stamp(s.success)));if(s.error)status.append(node("div",s.error,"error-text"));if(s.gap)status.append(node("div","已达到分页上限，可能有历史缺口","error-text"));if(s.live_notify||s.live_record){status.append(node("small", "直播："+(!s.enabled?"已暂停":!s.live_allowed?"已禁用":s.live_state?.error||"检查中")+" · "+stamp(s.live_state?.checked)));}tr.append(status);
    const actions=node("td",undefined,"actions");actions.append(button("编辑",()=>openEditor(s)),button(s.live_notify?"关闭直播通知":"开启直播通知",async()=>{await post({action:"edit",id:s.id,revision:s.revision,changes:{live_notify:!s.live_notify}});await refresh();}),button("直播记录",()=>openHistory(s)),button(s.enabled?"暂停":"恢复",async()=>{await post({action:"edit",id:s.id,revision:s.revision,changes:{enabled:!s.enabled}});await refresh();}),button("检查",()=>task(s,"check")),button("测试",()=>task(s,"test")),button("取消",async()=>{if(!await confirmAction("取消本群对「"+s.name+"」的订阅？"))return;await post({action:"delete",id:s.id,revision:s.revision});await refresh();},true));tr.append(actions);root.append(tr);
  }
}
async function task(s,action){if(action==="test"&&!await confirmAction("向群「"+s.group_name+"」发送一条最新动态作为测试？"))return;const result=await post({action,id:s.id});notify("已安排"+(action==="test"?"测试推送":"检查")+"任务 #"+result.task_id);await refresh();}
function renderDeliveries(){
  const root=$("deliveryRows");root.replaceChildren();
  for(const d of state.deliveries){const tr=node("tr"),target=node("td");const link=node("a",d.dynamic_id);link.href=d.dynamic_id.startsWith("live:")?"https://space.bilibili.com/"+d.uid:"https://t.bilibili.com/"+d.dynamic_id;if(d.dynamic_id.startsWith("live:"))link.textContent=d.dynamic_id.endsWith(":end")?"下播通知":"开播通知";link.target="_blank";link.rel="noreferrer";target.append(link,node("small","群 "+d.group_id+" · UID "+d.uid+(d.test?" · 测试":"")));tr.append(target);
    const status=node("td");status.append(badge(d.status));tr.append(status);tr.append(node("td",(labels[d.card_status]||d.card_status)+" / "+(labels[d.video_status]||d.video_status)),node("td",stamp(d.created)));
    const detail=node("td");if(d.error)detail.append(node("div",d.error,"error-text"));for(const receipt of d.receipts)detail.append(node("small",(receipt.stage==="card"?"卡片":"视频")+"回执："+receipt.message_id));
    if(d.status==="failed")detail.append(button("重试失败阶段",async()=>{await post({action:"retry",id:d.id});notify("已安排重试");await refresh();}));
    if(d.status==="uncertain")for(const [outcome,label] of [["sent","确认已发"],["not_sent","确认未发并重试"]])detail.append(button(label,async()=>{if(!await confirmAction("请先核对群 "+d.group_id+" 的消息记录。确认该"+(d.stage==="card"?"卡片":"视频")+(outcome==="sent"?"已发出？确认后继续剩余阶段。":"确实未发出？确认后将重新发送该阶段。")))return;await post({action:"resolve_delivery",id:d.id,outcome});await refresh();}));tr.append(detail);root.append(tr);}
  const tasks=$("requestRows");tasks.replaceChildren();for(const r of state.requests){const p=node("div",undefined,"request");p.append(badge(r.status),document.createTextNode(" #"+r.id+" · "+(r.kind==="test"?"测试推送":"检查")+" · UID "+(r.uid||"已取消")+" · "+stamp(r.created)));if(r.error)p.append(node("div",r.error,"error-text"));tasks.append(p);}if(!state.requests.length)tasks.append(node("p","暂无手动任务。","hint"));
}
function loadSettings(){const s=state.settings;$("dynamicEnabled").checked=s.dynamic_enabled;$("interval").value=s.dynamic_interval;$("liveEnabled").checked=!!s.live_enabled;$("liveInterval").value=s.live_interval||60;$("groupMode").value=s.group_filter_mode;$("allowedGroups").value=s.allowed_group_ids;$("blockedGroups").value=s.blocked_group_ids;$("maxVideo").value=s.max_video_mb;$("cacheHours").value=s.cache_hours;$("cookieState").textContent=s.cookie_configured?"已保存 Cookie，原值不会返回页面。有效性以动态检查结果为准。":"尚未设置 Cookie，将尝试匿名访问。";$("cookie").value="";$("clearCookie").checked=false;settingsLoaded=true;}
async function openEditor(s=null){
  editing=s;$("editorTitle").textContent=s?"编辑订阅":"添加订阅";$("creatorInput").value=s?s.uid:"";$("creatorInput").readOnly=!!s;$("resolveCreator").hidden=!!s;$("creatorResult").textContent=s?s.name:"";$("groupField").hidden=!!s;$("sendVideo").checked=s?!!s.send_video:true;$("liveNotify").checked=!!s?.live_notify;$("liveRecord").checked=!!s?.live_record;
  $("typeChoices").replaceChildren();for(const [key,label] of Object.entries(types)){const l=node("label",undefined,"check"),c=document.createElement("input");c.type="checkbox";c.value=key;c.name="kind";c.checked=s?s.types.includes(key):true;l.append(c,document.createTextNode(label));$("typeChoices").append(l);}
  $("editor").showModal();if(s)return;$("groupChoices").textContent="正在加载群列表…";
  try{availableGroups=(await bridge.apiGet("subscriptions/groups")).groups;$("groupChoices").replaceChildren();availableGroups.forEach((g,i)=>{const l=node("label",undefined,"check"),c=document.createElement("input");c.type="checkbox";c.name="group";c.value=i;l.append(c,document.createTextNode(g.group_name+"（"+g.group_id+"） · "+g.bot_id));$("groupChoices").append(l);});if(!availableGroups.length)$("groupChoices").textContent="没有可用的群，请检查 NapCat 连接和群过滤设置。";}catch(e){$("groupChoices").textContent=e.message;}
}
$("subscriptionForm").onsubmit=async e=>{e.preventDefault();const save=$("saveSubscription");save.disabled=true;try{const selected=[...document.querySelectorAll('input[name="kind"]:checked')].map(x=>x.value);if(!selected.length&&!$("liveNotify").checked&&!$("liveRecord").checked)throw Error("请选择动态类型、直播通知或直播记录");if(editing){await post({action:"edit",id:editing.id,revision:editing.revision,changes:{types:selected,send_video:$("sendVideo").checked,live_notify:$("liveNotify").checked,live_record:$("liveRecord").checked}});}else{const groups=[...document.querySelectorAll('input[name="group"]:checked')].map(x=>availableGroups[Number(x.value)]);if(!groups.length)throw Error("请选择至少一个群");for(const target of groups)await post({action:"add",target,uid:$("creatorInput").value,types:selected,send_video:$("sendVideo").checked,live_notify:$("liveNotify").checked,live_record:$("liveRecord").checked});}$("editor").close();notify("订阅已保存，两端数据同步生效。");await refresh();}catch(err){notify(err.message,true);$("creatorResult").textContent=err.message;}finally{save.disabled=false;}};
$("resolveCreator").onclick=async()=>{const b=$("resolveCreator");b.disabled=true;try{const c=await post({action:"resolve",uid:$("creatorInput").value});$("creatorResult").textContent=c.name+" · UID "+c.uid;}catch(e){$("creatorResult").textContent=e.message;}finally{b.disabled=false;}};
$("settingsForm").onsubmit=async e=>{e.preventDefault();const submit=e.currentTarget.querySelector('button[type="submit"]');submit.disabled=true;try{const values={dynamic_enabled:$("dynamicEnabled").checked,dynamic_interval:Number($("interval").value),live_enabled:$("liveEnabled").checked,live_interval:Number($("liveInterval").value),group_filter_mode:$("groupMode").value,allowed_group_ids:$("allowedGroups").value,blocked_group_ids:$("blockedGroups").value,max_video_mb:Number($("maxVideo").value),cache_hours:Number($("cacheHours").value)};if($("clearCookie").checked)values.cookie="";else if($("cookie").value.trim())values.cookie=$("cookie").value.trim();await post({action:"settings",values});settingsLoaded=false;notify("设置已保存。");await refresh();}catch(err){notify(err.message,true);}finally{submit.disabled=false;}};
document.querySelectorAll(".tab").forEach(tab=>tab.onclick=()=>{document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("active",t===tab));["subscriptions","deliveries","settings"].forEach(id=>$(id).hidden=id!==tab.dataset.tab);});
$("refresh").onclick=refresh;$("search").oninput=()=>state&&renderSubscriptions();$("loadSettings").onclick=()=>state&&loadSettings();$("add").onclick=()=>openEditor();$("closeEditor").onclick=()=>$("editor").close();
if(!bridge){$("connection").textContent="请从 AstrBot 插件详情中的 Pages 打开本页面。";}else{try{await bridge.ready();await refresh();setInterval(()=>{if(!document.hidden&&!$("editor").open)refresh();},15000);}catch(e){notify(e.message,true);}}

let historyData = null, historyMonth = 0, historyCanvas = null;
async function openHistory(s) {
  historyData = await post({action:"live_history", id:s.id});
  historyMonth = 0;
  renderHistory();
  $("liveHistory").showModal();
}
function historyLines(row) {
  return [row.title, "分区："+(row.area||"未分类"), "开播："+row.started+(row.start_estimated?"（首次检测）":""),
    "结束："+row.ended, "整场时长约 "+row.duration+" · 最后检测："+row.last_live,
    ...(row.history.length>1?["内容变化："+row.history.map(h=>h.at+" "+h.title+"（"+h.area+"）").join("；")]:[])];
}
function renderHistory() {
  const d=historyData, month=d.calendars[historyMonth];
  $("liveHistoryTitle").textContent=d.name+" · 直播日历";
  $("historyPage").textContent=month.label+" · "+month.active_days+" 天 · "+month.count+" 场";
  $("historyPrevious").disabled=historyMonth===d.calendars.length-1;
  $("historyNext").disabled=historyMonth===0;
  const root=$("historyRows");root.replaceChildren();
  historyCanvas=drawLiveCalendar(d,month);root.append(historyCanvas);
  const details=node("details"), summary=node("summary","查看本月场次详情（完整标题、分区与起止时间）");
  details.append(summary);
  const ids=new Set(month.days.flatMap(day=>(day.events||[]).map(event=>event.id)));
  for(const row of d.rows.filter(row=>ids.has(row.id))) {
    const el=node("div",undefined,"request");
    for(const line of historyLines(row))el.append(node("p",line));
    details.append(el);
  }
  if(!ids.size)details.append(node("p","本月暂无已采集的直播记录。"));
  root.append(details);
}
$("closeLiveHistory").onclick=()=>$("liveHistory").close();
$("historyPrevious").onclick=()=>{historyMonth++;renderHistory();};
$("historyNext").onclick=()=>{historyMonth--;renderHistory();};
$("exportHistory").onclick=()=>{
  historyCanvas.toBlob(blob=>{
    if(!blob){notify("日历导出失败，请重试",true);return;}
    const url=URL.createObjectURL(blob),a=document.createElement("a");
    a.href=url;a.download="直播日历_"+historyData.uid+"_"+historyData.calendars[historyMonth].month+".png";
    a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);
  },"image/png");
};
