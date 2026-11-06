// Draw the same monthly data used by the QQ template; no browser date parsing.
export function drawLiveCalendar(data, month) {
  const canvas = document.createElement("canvas");
  canvas.width = 1600;
  canvas.height = 430 + month.weeks * 220 + 185;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", `${data.name} ${month.label}直播日历：${month.active_days}天，${month.count}场，约${month.total}。完整信息见下方场次详情。`);
  const ctx = canvas.getContext("2d");
  const text = (value, x, y, size = 18, color = "#302d3e", maxWidth, bold = false) => {
    ctx.font = `${bold ? "600 " : ""}${size}px "Microsoft YaHei", sans-serif`;
    ctx.fillStyle = color;
    let label = String(value);
    if (maxWidth && ctx.measureText(label).width > maxWidth) {
      while (label.length && ctx.measureText(label + "…").width > maxWidth) label = label.slice(0, -1);
      label += "…";
    }
    ctx.fillText(label, x, y);
  };
  const box = (x, y, width, height, color, radius = 14) => {
    ctx.fillStyle = color;
    ctx.beginPath();ctx.roundRect(x, y, width, height, radius);ctx.fill();
  };
  ctx.textBaseline = "top";
  box(0, 0, 1600, canvas.height, "#f5f1f4", 0);
  box(48, 48, 1504, canvas.height - 96, "#ffffff", 28);
  text("BILIBILI · LIVE CALENDAR", 88, 88, 17, "#9c6c86");
  text(data.name + " · 直播日历", 88, 121, 38, "#302d3e", 1010, true);
  text("UID " + data.uid + " · 北京时间 · 近 30 天记录", 88, 176, 18, "#88818f");
  text(month.label, 1170, 126, 34, "#a74470", 300, true);
  [["有直播记录",month.active_days+" 天"],["涉及直播场次",month.count+" 场"],["本月已记录时长（约）",month.total]].forEach(([label,value], i) => {
    const x = 88 + i * 482;
    box(x, 228, 460, 105, "#f9f4f7");
    text(label, x + 24, 246, 17, "#8f7f8b");
    text(value, x + 24, 281, 28, "#302d3e", 412, true);
  });
  const cell = (1424 - 60) / 7;
  ["周一","周二","周三","周四","周五","周六","周日"].forEach((label,i) => text(label, 88 + i * (cell + 10) + cell / 2 - 20, 363, 19, i > 4 ? "#b3537f" : "#8f7f8b"));
  const colors = ["#f8f8fa", "#fdf0f6", "#f9deeb", "#efbed5"];
  month.days.forEach((day,i) => {
    if (!day.day) return;
    const x = 88 + i % 7 * (cell + 10), y = 402 + Math.floor(i / 7) * 220;
    box(x, y, cell, 210, day.available ? colors[day.level] : "#fafafa");
    text(String(day.day).padStart(2,"0"), x+14, y+14, 23, day.available ? "#302d3e" : "#c3bfc5", undefined, true);
    if(day.today) text("今天",x+cell-44,y+20,13,"#a74470");
    if (!day.events.length) {text(day.available ? "暂无记录" : "—",x+14,y+65,15,"#b0a7b1");return;}
    text(day.duration,x+14,y+53,21,"#a74470",cell-28,true);
    day.events.slice(0,2).forEach((event,index) => {
      const top=y+88+index*47;
      text(event.time+(event.ongoing?" · 未结束":""),x+14,top,14,"#836478",cell-28);
      text(event.title,x+14,top+21,15,"#302d3e",cell-28);
    });
    if(day.events.length>2) text("另有 "+(day.events.length-2)+" 场 · 详见 Web",x+14,y+187,13,"#8f5773",cell-28);
  });
  const footer = 420 + month.weeks * 220;
  text("每日时长",88,footer,17,"#8f7f8b");
  [["< 2 小时",1],["2–4 小时",2],["≥ 4 小时",3]].forEach(([label,level],i)=>{
    box(190+i*150,footer,17,17,colors[level],4);text(label,216+i*150,footer,17,"#8f7f8b");
  });
  text("记录窗口："+month.window+"；仅统计窗口内已有记录，无记录不代表未开播。",88,footer+35,17,"#8f7f8b");
  text("跨日直播按北京时间分摊；下播时间为检测估计，未结束场次统计至最后检测。",88,footer+66,17,"#8f7f8b");
  text("生成于 "+data.generated+" · Web 可切换月份、查看完整标题与每场详情。",88,footer+97,17,"#8f7f8b");
  return canvas;
}
