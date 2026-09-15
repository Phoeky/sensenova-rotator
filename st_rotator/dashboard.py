"""控制台前端页面（单文件 HTML，零 CDN 依赖）。

为什么要塞在 Python 字符串里
---------------------------
这个工具是零依赖、可离线部署的。如果前端拆成独立的 .html/.js/.css 文件，就得处理
"打包后文件在哪"的问题；而引 CDN 又会让内网环境直接白屏。所以整页内联，由网关自己
吐出来——打开一个 URL 就有完整界面。

为什么是 Web 而不是 tkinter
--------------------------
托管 Python 不带 tkinter（`ModuleNotFoundError: No module named 'tkinter'`），
而系统自带的 3.9 又太老。用本地 Web 页面 + Edge 的 ``--app`` 模式开窗，没有地址栏和
标签页，观感和原生桌面程序一致，还能顺带把实时表格、日志、代码复制都做得很舒服。
"""

from __future__ import annotations

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多 Key 轮换控制台</title>
<style>
  :root {
    --bg: #f4f6fa;
    --card: #ffffff;
    --border: #e2e7f0;
    --border-strong: #cfd7e6;
    --text: #16202f;
    --muted: #6a7688;
    --accent: #2f6feb;
    --accent-soft: #eaf1ff;
    --green: #0f7a55;
    --green-bg: #e4f6ee;
    --amber: #8a5b00;
    --amber-bg: #fff3d4;
    --red: #b3261e;
    --red-bg: #fdeceb;
    --mono: ui-monospace, "SFMono-Regular", "Cascadia Mono", Consolas, "Liberation Mono", monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #12161d;
      --card: #1a1f28;
      --border: #2a3240;
      --border-strong: #3a4456;
      --text: #e6ebf2;
      --muted: #93a0b4;
      --accent: #5b93ff;
      --accent-soft: #1e2b45;
      --green: #4fd1a5;
      --green-bg: #14302a;
      --amber: #e8b84b;
      --amber-bg: #33290f;
      --red: #ff8078;
      --red-bg: #3a1d1c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: var(--sans); font-size: 13px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1360px; margin: 0 auto; padding: 18px 20px 40px; }

  header {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 18px; margin-bottom: 16px;
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .sub { color: var(--muted); font-size: 12px; margin-top: 2px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .dot.on { background: #16a34a; box-shadow: 0 0 0 3px rgba(22,163,74,.16); }
  .dot.paused { background: #d97706; box-shadow: 0 0 0 3px rgba(217,119,6,.16); }
  .spacer { flex: 1; }

  button {
    font-family: inherit; font-size: 12.5px; cursor: pointer;
    border: 1px solid var(--border-strong); background: var(--card); color: var(--text);
    padding: 6px 13px; border-radius: 7px; transition: .14s;
  }
  button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  button:disabled { opacity: .45; cursor: not-allowed; }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover:not(:disabled) { filter: brightness(1.08); color: #fff; }
  button.danger:hover:not(:disabled) { border-color: var(--red); color: var(--red); }
  button.tiny { padding: 3px 9px; font-size: 11.5px; border-radius: 6px; }

  .grid { display: grid; gap: 14px; }
  .kpis { grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); margin-bottom: 14px; }
  .two { grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr); }
  @media (max-width: 980px) { .two { grid-template-columns: 1fr; } }

  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 15px 17px; min-width: 0;
  }
  .card h2 {
    font-size: 12px; font-weight: 650; text-transform: uppercase; letter-spacing: .7px;
    color: var(--muted); margin: 0 0 12px; display: flex; align-items: center; gap: 8px;
  }
  .card h2 .spacer { flex: 1; }

  .kpi .label { font-size: 11.5px; color: var(--muted); letter-spacing: .3px; }
  .kpi .value { font-size: 22px; font-weight: 640; margin-top: 3px; font-variant-numeric: tabular-nums; }
  .kpi .value small { font-size: 12px; color: var(--muted); font-weight: 400; margin-left: 3px; }
  .kpi .value.green { color: var(--green); }
  .kpi .value.amber { color: var(--amber); }
  .kpi .value.red { color: var(--red); }

  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th {
    text-align: left; font-weight: 600; color: var(--muted); font-size: 11px;
    text-transform: uppercase; letter-spacing: .5px;
    padding: 0 8px 7px; border-bottom: 1px solid var(--border); white-space: nowrap;
  }
  td { padding: 8px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  td.mono { font-family: var(--mono); font-size: 11.5px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  th.num { text-align: right; }

  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 20px;
    font-size: 11px; font-weight: 600; white-space: nowrap;
  }
  .pill.healthy { background: var(--green-bg); color: var(--green); }
  .pill.cooldown { background: var(--amber-bg); color: var(--amber); }
  .pill.invalid { background: var(--red-bg); color: var(--red); }
  .pill.neutral { background: var(--accent-soft); color: var(--accent); }

  input, select {
    font-family: inherit; font-size: 12.5px; color: var(--text);
    background: var(--card); border: 1px solid var(--border-strong);
    border-radius: 7px; padding: 6px 10px; width: 100%;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  input.mono { font-family: var(--mono); font-size: 12px; }
  label.field { display: block; margin-bottom: 9px; }
  label.field > span { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 4px; }
  .row { display: flex; gap: 9px; align-items: flex-end; }
  .row > * { min-width: 0; }
  .row .grow { flex: 1; }

  .kv { display: flex; gap: 8px; align-items: center; margin-bottom: 7px; font-size: 12.5px; }
  .kv > .k { color: var(--muted); min-width: 74px; flex-shrink: 0; }
  .kv > .v { font-family: var(--mono); font-size: 11.5px; word-break: break-all; }
  .kv > .v.grow { flex: 1; }

  pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 11px 13px; margin: 0; overflow-x: auto;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.6;
  }
  .tabs { display: flex; gap: 4px; margin-bottom: 9px; flex-wrap: wrap; }
  .tabs button { padding: 4px 11px; font-size: 11.5px; }
  .tabs button.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }

  #logbox {
    height: 208px; overflow-y: auto; background: var(--bg);
    border: 1px solid var(--border); border-radius: 8px; padding: 9px 11px;
    font-family: var(--mono); font-size: 11.5px; line-height: 1.65;
  }
  #logbox div { white-space: pre-wrap; word-break: break-all; }
  #logbox .warn { color: var(--amber); }
  #logbox .err { color: var(--red); }
  #logbox .ok { color: var(--green); }
  #logbox .dim { color: var(--muted); }

  .toast {
    position: fixed; right: 22px; bottom: 22px; z-index: 50;
    display: flex; flex-direction: column; gap: 8px; align-items: flex-end;
  }
  .toast div {
    background: var(--card); border: 1px solid var(--border-strong); border-left: 3px solid var(--accent);
    border-radius: 8px; padding: 9px 14px; font-size: 12.5px; max-width: 380px;
    box-shadow: 0 6px 22px rgba(20,30,50,.14); animation: pop .18s ease-out;
  }
  .toast div.err { border-left-color: var(--red); }
  .toast div.ok { border-left-color: var(--green); }
  @keyframes pop { from { opacity: 0; transform: translateY(6px); } }

  .banner {
    display: none; align-items: center; gap: 10px; margin-bottom: 14px;
    background: var(--amber-bg); border: 1px solid var(--amber); color: var(--amber);
    border-radius: 10px; padding: 10px 15px; font-size: 12.5px;
  }
  .banner.show { display: flex; }
  .banner input { max-width: 260px; }
  .muted { color: var(--muted); }
  .empty { padding: 22px; text-align: center; color: var(--muted); }
  .spin { display: inline-block; animation: rot .9s linear infinite; }
  @keyframes rot { to { transform: rotate(360deg); } }
  .badge {
    display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10.5px;
    background: var(--accent-soft); color: var(--accent); margin-right: 3px;
  }
  svg.spark { display: block; width: 100%; height: 34px; margin-top: 6px; }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <h1>多 Key 轮换控制台</h1>
      <div class="sub" id="subline">正在连接…</div>
    </div>
    <div class="spacer"></div>
    <button id="btn-pause">暂停接入</button>
    <button id="btn-refresh">刷新</button>
  </header>

  <div class="banner" id="authbar">
    <span>该网关启用了本地鉴权，请输入访问 Token：</span>
    <input id="token-input" class="mono" type="password" placeholder="Bearer Token">
    <button class="primary tiny" id="btn-token">确认</button>
  </div>

  <div class="grid kpis" id="kpis"></div>

  <div class="grid two" style="margin-bottom:14px">
    <div class="card">
      <h2>网关接入信息 <span class="spacer"></span><button class="tiny" id="btn-copy-base">复制地址</button></h2>
      <div id="gateway"></div>
      <div class="tabs" id="snippet-tabs" style="margin-top:12px"></div>
      <pre id="snippet"></pre>
    </div>

    <div class="card">
      <h2>默认模型 <span class="spacer"></span><button class="tiny" id="btn-models">刷新清单</button></h2>
      <label class="field">
        <span>当前生效模型</span>
        <select id="model-select"></select>
      </label>
      <div id="model-meta" style="font-size:12px;margin-bottom:6px"></div>
      <div id="model-count" class="muted" style="font-size:11.5px;margin-bottom:11px"></div>
      <div class="row">
        <button class="primary grow" id="btn-apply-model">应用为默认模型</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:11px;line-height:1.6">
        网关对上层完全透明：客户端请求里带 <code>model</code> 就用它的，
        没带则回落到这里的默认值。
      </div>
    </div>
  </div>

  <div class="card" style="margin-bottom:14px">
    <h2>Key 池 <span class="spacer"></span><span class="muted" id="pool-note" style="text-transform:none;letter-spacing:0"></span></h2>
    <div id="pool"></div>

    <div style="margin-top:15px;padding-top:14px;border-top:1px solid var(--border)">
      <h2 style="margin-bottom:10px">添加 Key</h2>
      <div class="row">
        <label class="field grow" style="margin-bottom:0">
          <span>API Key（支持一次粘贴多把，逗号或换行分隔）</span>
          <input id="new-key" class="mono" placeholder="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
        </label>
        <label class="field" style="width:150px;margin-bottom:0">
          <span>归属账号</span>
          <input id="new-account" placeholder="留空自动命名">
        </label>
        <label class="field" style="width:104px;margin-bottom:0">
          <span>并发上限</span>
          <input id="new-concurrency" type="number" min="1" max="64" value="4">
        </label>
        <button class="primary" id="btn-add" style="height:33px">添加</button>
      </div>
      <div class="muted" style="font-size:11.5px;margin-top:9px">
        添加前会先逐把校验凭据（失败的直接拒收，不污染池子）；成功后立即写入配置文件并参与轮换。
      </div>
    </div>
  </div>

  <div class="grid two">
    <div class="card">
      <h2>运行参数</h2>
      <div class="row" style="margin-bottom:12px">
        <label class="field grow" style="margin-bottom:0">
          <span>调度策略</span>
          <select id="opt-strategy">
            <option value="round_robin">round_robin · 轮转（配额均摊最均匀）</option>
            <option value="least_inflight">least_inflight · 最少在途</option>
            <option value="least_recent">least_recent · 最久未用</option>
            <option value="weighted">weighted · 加权随机</option>
          </select>
        </label>
        <label class="field" style="width:132px;margin-bottom:0">
          <span>限速模式</span>
          <select id="opt-rate-mode">
            <option value="adaptive">adaptive · AIMD</option>
            <option value="fixed">fixed · 固定</option>
            <option value="off">off · 不限速</option>
          </select>
        </label>
        <label class="field" style="width:112px;margin-bottom:0">
          <span>目标 QPS</span>
          <input id="opt-qps" type="number" step="0.05" min="0.01">
        </label>
      </div>
      <div class="row" style="margin-bottom:12px">
        <label class="field grow" style="margin-bottom:0">
          <span>单请求等待预算（秒，0 = 不限）</span>
          <input id="opt-wait" type="number" step="5" min="0">
        </label>
        <label class="field" style="width:132px;margin-bottom:0">
          <span>最大重试次数</span>
          <input id="opt-attempts" type="number" min="1" max="50">
        </label>
        <button class="primary" id="btn-apply-options" style="height:33px">应用</button>
      </div>
      <div class="muted" style="font-size:11.5px">
        AIMD 模式下「目标 QPS」是**起始速率**；工具会自己往上下界之间收敛，撞 429 就降、
        长时间干净就升。改参数会重置收敛点，从起始速率重新探测。
      </div>
    </div>

    <div class="card">
      <h2>实时日志 <span class="spacer"></span>
        <button class="tiny" id="btn-autoscroll">自动滚动：开</button>
        <button class="tiny" id="btn-clearlog">清屏</button>
      </h2>
      <div id="logbox"></div>
      <div class="muted" style="font-size:11.5px;margin-top:8px" id="logfile"></div>
    </div>
  </div>
</div>
<div class="toast" id="toast"></div>

<script>
"use strict";

var S = {
  token: localStorage.getItem("sn_rotator_token") || "",
  cursor: 0,
  autoscroll: true,
  tab: "python",
  models: [],
  timerState: null,
  timerLog: null
};

/* 窗口是带 #token=xxx 打开的（fragment 不会发给服务端，也不进 Referer）。
   读出来存进 localStorage，然后立刻从地址栏抹掉，避免残留在可见 URL 里。 */
(function readTokenFromHash() {
  var match = /(?:^|[#&])token=([^&]+)/.exec(location.hash || "");
  if (!match) return;
  try {
    S.token = decodeURIComponent(match[1]);
    localStorage.setItem("sn_rotator_token", S.token);
  } catch (err) { /* 非法编码，忽略 */ }
  history.replaceState(null, "", location.pathname + location.search);
})();

/* ------------------------------------------------------------------ 工具 */

function $(id) { return document.getElementById(id); }

function esc(text) {
  return String(text == null ? "" : text)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toast(message, kind) {
  var box = $("toast");
  var item = document.createElement("div");
  item.className = kind || "";
  item.textContent = message;
  box.appendChild(item);
  setTimeout(function () { item.remove(); }, kind === "err" ? 6000 : 3200);
}

function fmtInt(n) { return Number(n || 0).toLocaleString("en-US"); }

function fmtDuration(seconds) {
  seconds = Math.max(0, Math.floor(seconds || 0));
  var d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600);
  var m = Math.floor(seconds % 3600 / 60), s = seconds % 60;
  if (d) return d + "天" + h + "小时";
  if (h) return h + "小时" + m + "分";
  if (m) return m + "分" + s + "秒";
  return s + "秒";
}

function fmtCtx(n) {
  if (!n) return "";
  if (n >= 1048576) return Math.round(n / 1048576 * 10) / 10 + "M";
  if (n >= 1024) return Math.round(n / 1024) + "K";
  return String(n);
}

/* ------------------------------------------------------------------ 请求 */

async function api(path, options) {
  options = options || {};
  var headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (S.token) headers["Authorization"] = "Bearer " + S.token;
  var response;
  try {
    response = await fetch(path, Object.assign({}, options, { headers: headers }));
  } catch (err) {
    throw new Error("网关无响应：" + err.message);
  }
  if (response.status === 401) {
    $("authbar").classList.add("show");
    throw new Error("需要 Token 才能访问控制台接口");
  }
  var data = {};
  try { data = await response.json(); } catch (err) { /* 空响应体 */ }
  if (!response.ok) {
    var detail = (data && data.error && (data.error.message || data.error)) || ("HTTP " + response.status);
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/* ------------------------------------------------------------------ 渲染 */

function renderKpis(state) {
  var s = state.summary || {};
  var rate = state.rate_control || {};
  var m = state.metrics || {};
  var rateText, rateClass = "", rateSub = "";
  if (rate.mode === "adaptive") {
    rateText = (rate.rate || 0).toFixed(2);
    rateSub = "区间 " + rate.min_rate + "~" + rate.max_rate;
    if (rate.penalties > rate.raises * 1.6 && rate.penalties > 3) rateClass = "amber";
    else if (rate.penalties === 0) rateClass = "green";
  } else if (rate.mode === "fixed") {
    rateText = (rate.rate || 0).toFixed(2); rateSub = "固定速率";
  } else {
    rateText = "关"; rateSub = "不限速";
  }
  var attempts = m.upstream_attempts || 0;
  var amp = m.client_requests ? (attempts / m.client_requests) : 0;
  var ampText = "上游尝试 " + fmtInt(attempts) + " 次";
  if (amp > 1.05) ampText += "（放大 " + amp.toFixed(2) + "×）";
  var cards = [
    ["可用 Key", fmtInt(s.healthy), "green", "共 " + fmtInt(s.total) + " 把"],
    ["冷却中", fmtInt(s.cooldown), s.cooldown ? "amber" : "", "等待恢复"],
    ["已失效", fmtInt(s.invalid), s.invalid ? "red" : "", "401/403 已隔离"],
    ["当前限速", rateText, rateClass, rateSub + " req/s"],
    ["已服务请求", fmtInt(m.client_requests), "", ampText],
    ["运行时长", fmtDuration(m.uptime_seconds), "", m.stream_requests ? "其中流式 " + fmtInt(m.stream_requests) : "进程已启动"]
  ];
  $("kpis").innerHTML = cards.map(function (c) {
    return '<div class="card kpi"><div class="label">' + esc(c[0]) + '</div>' +
      '<div class="value ' + c[2] + '">' + esc(c[1]) + '</div>' +
      '<div class="label" style="margin-top:2px">' + esc(c[3]) + '</div></div>';
  }).join("");
  if (rate.mode === "adaptive") renderSpark(rate);
}

function renderSpark(rate) {
  var history = (rate.history || []).filter(function (p) { return p && p.length === 2; });
  // 曲线要挂在"当前限速"卡片下（第 4 张），而不是第一张——它是速率的历史轨迹
  var host = $("kpis").children[3];
  if (!host || history.length < 2) return;
  var rates = history.map(function (p) { return p[1]; });
  var lo = Math.min.apply(null, rates), hi = Math.max.apply(null, rates);
  if (hi - lo < 1e-6) { hi = lo + 0.01; }
  var w = 100, h = 30;
  var points = history.map(function (p, i) {
    var x = i / (history.length - 1) * w;
    var y = h - (p[1] - lo) / (hi - lo) * (h - 4) - 2;
    return x.toFixed(2) + "," + y.toFixed(2);
  }).join(" ");
  var svg = '<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none" ' +
    'title="AIMD 速率轨迹（' + history.length + ' 次调整）">' +
    '<polyline fill="none" stroke="var(--accent)" stroke-width="1.2" ' +
    'vector-effect="non-scaling-stroke" points="' + points + '"></polyline></svg>';
  var node = host.querySelector(".kpi-spark");
  if (!node) {
    node = document.createElement("div");
    node.className = "kpi-spark";
    host.appendChild(node);
  }
  node.innerHTML = svg;
}

function renderGateway(state) {
  var g = state.gateway || {};
  var tokenText = g.token ? g.token : "未设置（本机任意进程都可调用）";
  var rows = [
    ["Base URL", g.base_url],
    ["对话端点", g.chat_endpoint],
    ["API Key", tokenText],
    ["上游", g.upstream],
    ["健康检查", g.health_endpoint],
    ["运行统计", g.stats_endpoint]
  ];
  $("gateway").innerHTML = rows.map(function (r) {
    return '<div class="kv"><span class="k">' + esc(r[0]) + '</span>' +
      '<span class="v grow">' + esc(r[1]) + '</span></div>';
  }).join("");
  renderSnippet(g);
}

function renderSnippetTabs() {
  var tabs = [["python", "Python (OpenAI SDK)"], ["curl", "cURL"], ["node", "Node.js"], ["generic", "通用 Agent 配置"]];
  $("snippet-tabs").innerHTML = tabs.map(function (t) {
    return '<button data-tab="' + t[0] + '" class="' + (S.tab === t[0] ? "active" : "") + '">' + esc(t[1]) + '</button>';
  }).join("");
  Array.prototype.forEach.call($("snippet-tabs").children, function (button) {
    button.onclick = function () { S.tab = button.dataset.tab; renderSnippetTabs(); renderSnippet(lastGateway); };
  });
}

var lastGateway = {};

function renderSnippet(g) {
  if (g) lastGateway = g;
  g = lastGateway || {};
  var base = g.base_url || "http://127.0.0.1:8080/v1";
  var token = g.token || "sk-local-any";
  var model = g.model || "deepseek-v4-flash";
  var text;
  if (S.tab === "curl") {
    text = 'curl ' + base + '/chat/completions \\\n' +
      '  -H "Content-Type: application/json" \\\n' +
      '  -H "Authorization: Bearer ' + token + '" \\\n' +
      '  -d \'{\n' +
      '    "model": "' + model + '",\n' +
      '    "messages": [{"role": "user", "content": "你好"}],\n' +
      '    "stream": false\n' +
      '  }\'';
  } else if (S.tab === "node") {
    text = 'import OpenAI from "openai";\n\n' +
      'const client = new OpenAI({\n' +
      '  baseURL: "' + base + '",\n' +
      '  apiKey: "' + token + '",   // 网关本地鉴权，非商汤 Key\n' +
      '});\n\n' +
      'const stream = await client.chat.completions.create({\n' +
      '  model: "' + model + '",\n' +
      '  messages: [{ role: "user", content: "你好" }],\n' +
      '  stream: true,\n' +
      '});\n' +
      'for await (const chunk of stream) {\n' +
      '  process.stdout.write(chunk.choices[0]?.delta?.content ?? "");\n' +
      '}';
  } else if (S.tab === "generic") {
    text = '# 通用 OpenAI 兼容配置（WorkBuddy / Dify / Cherry Studio / LobeChat …）\n' +
      'base_url : ' + base + '\n' +
      'api_key  : ' + token + '\n' +
      'model    : ' + model + '\n' +
      'stream   : 支持（含 tool_calls 透传）\n\n' +
      '# 说明\n' +
      '#   - 上层的 api_key 填上面这个本地 Token，不是商汤的 sk- Key\n' +
      '#   - 商汤的多把 Key 全部由本网关内部轮换，上层无感知\n' +
      '#   - 429 / 冷却 / 坏 Key 隔离都在网关内消化';
  } else {
    text = 'from openai import OpenAI\n\n' +
      'client = OpenAI(\n' +
      '    base_url="' + base + '",\n' +
      '    api_key="' + token + '",   # 网关本地鉴权，非商汤 Key\n' +
      ')\n\n' +
      'stream = client.chat.completions.create(\n' +
      '    model="' + model + '",\n' +
      '    messages=[{"role": "user", "content": "你好"}],\n' +
      '    stream=True,\n' +
      ')\n' +
      'for chunk in stream:\n' +
      '    delta = chunk.choices[0].delta.content\n' +
      '    if delta:\n' +
      '        print(delta, end="", flush=True)';
  }
  $("snippet").textContent = text;
}

function renderModels(state) {
  var catalog = state.models || {};
  S.models = catalog.models || [];
  var select = $("model-select");
  var current = state.default_model;
  var options = S.models.map(function (m) { return m.id; });
  if (current && options.indexOf(current) === -1) options.unshift(current);
  if (!options.length) options = [current].filter(Boolean);
  select.innerHTML = options.map(function (id) {
    return '<option value="' + esc(id) + '"' + (id === current ? " selected" : "") + '>' + esc(id) + '</option>';
  }).join("") || '<option value="">（无可用模型）</option>';
  select.dataset.current = current || "";

  // 注意：详情写在 #model-meta，条数写在 #model-count。两者必须分开——
  // 早先共用一个元素时，"共 N 个模型"会把模型详情覆盖掉，详情永远看不见。
  var count = $("model-count");
  if (catalog.error) {
    count.innerHTML = '<span style="color:var(--red)">拉取模型清单失败：' + esc(catalog.error) + '</span>';
  } else if (!S.models.length) {
    count.textContent = "暂无清单，点「刷新清单」从上游拉取。";
  } else {
    count.textContent = "共 " + S.models.length + " 个模型" + (catalog.cached ? "（缓存）" : "（刚拉取）");
  }
  updateModelMeta();
}

function updateModelMeta() {
  var id = $("model-select").value;
  var found = S.models.filter(function (m) { return m.id === id; })[0];
  var node = $("model-meta");
  if (!found) { node.innerHTML = ""; return; }
  var bits = [];
  if (found.context_length) bits.push("上下文 " + fmtCtx(found.context_length));
  if (found.max_output_length) bits.push("最大输出 " + fmtCtx(found.max_output_length));
  if ((found.input_modalities || []).length) {
    bits.push("输入 " + found.input_modalities.join("+") + " → 输出 " + (found.output_modalities || []).join("+"));
  }
  var badges = (found.features || []).map(function (f) { return '<span class="badge">' + esc(f) + '</span>'; }).join("");
  node.innerHTML = '<span class="muted">' + esc(bits.join(" · ")) + "</span>" +
    (badges ? "<div style='margin-top:5px'>" + badges + "</div>" : "");
}

function renderPool(state) {
  var keys = state.keys || [];
  if (!keys.length) {
    $("pool").innerHTML = '<div class="empty">池里还没有 Key。在下面添加至少一把才能对外提供服务。</div>';
  } else {
    var head = "<tr><th>账号</th><th>Key</th><th>状态</th><th>冷却</th><th>RPM</th>" +
      "<th class='num'>成功/失败</th><th class='num'>429</th><th class='num'>延迟</th><th></th></tr>";
    var body = keys.map(function (k) {
      var st = k.stats || {};
      var statusText = { healthy: "可用", cooldown: "冷却中", invalid: "已失效" }[k.status] || k.status;
      var cooldown = k.cooldown_remaining > 0 ? k.cooldown_remaining + "s" : "—";
      return "<tr>" +
        "<td>" + esc(k.account) + "</td>" +
        '<td class="mono">' + esc(k.key) + "</td>" +
        '<td><span class="pill ' + esc(k.status) + '">' + esc(statusText) + "</span></td>" +
        '<td class="num">' + esc(cooldown) + "</td>" +
        '<td class="mono">' + esc(k.rpm_window) + "</td>" +
        '<td class="num">' + fmtInt(st.successes) + " / " + fmtInt(st.failures) + "</td>" +
        '<td class="num">' + fmtInt(st.rate_limited) + "</td>" +
        '<td class="num">' + (st.avg_latency_ms ? Math.round(st.avg_latency_ms) + "ms" : "—") + "</td>" +
        '<td style="text-align:right;white-space:nowrap">' +
          '<button class="tiny" data-act="verify" data-id="' + esc(k.id) + '" data-label="' + esc(k.account + " / " + k.key) + '">测试</button> ' +
          '<button class="tiny danger" data-act="remove" data-id="' + esc(k.id) + '" data-label="' + esc(k.account + " / " + k.key) + '">删除</button>' +
        "</td></tr>";
    }).join("");
    $("pool").innerHTML = "<table>" + head + body + "</table>";
    Array.prototype.forEach.call($("pool").querySelectorAll("button[data-act]"), function (button) {
      button.onclick = function () {
        onPoolAction(button.dataset.act, button.dataset.id, button.dataset.label, button);
      };
    });
  }
  var s = state.summary || {};
  $("pool-note").textContent = s.total ? ("在途 " + s.inflight + " / 共 " + s.total + " 把") : "";
}

function renderOptions(state) {
  var opt = state.options || {};
  var strategy = $("opt-strategy");
  if (document.activeElement !== strategy) strategy.value = opt.strategy || "round_robin";
  var mode = $("opt-rate-mode");
  if (document.activeElement !== mode) mode.value = opt.rate_mode || "off";
  var qps = $("opt-qps");
  if (document.activeElement !== qps) qps.value = opt.qps;
  var wait = $("opt-wait");
  if (document.activeElement !== wait) wait.value = opt.max_total_wait;
  var attempts = $("opt-attempts");
  if (document.activeElement !== attempts) attempts.value = opt.max_attempts;
}

/* ------------------------------------------------------------------ 日志 */

function classifyLog(text) {
  if (/\[警告\]|WARNING|429/.test(text)) return "warn";
  if (/\[错误\]|ERROR|失败|Traceback/.test(text)) return "err";
  if (/成功|已启动|ok/i.test(text)) return "ok";
  return "";
}

function appendLogs(items) {
  var box = $("logbox");
  items.forEach(function (item) {
    var line = document.createElement("div");
    line.className = classifyLog(item.text);
    line.textContent = item.text;
    box.appendChild(line);
  });
  while (box.childElementCount > 800) box.removeChild(box.firstChild);
  if (S.autoscroll) box.scrollTop = box.scrollHeight;
}

async function pollLogs() {
  try {
    var data = await api("/api/logs?cursor=" + S.cursor);
    S.cursor = data.cursor;
    if (data.items && data.items.length) appendLogs(data.items);
  } catch (err) { /* 静默：日志轮询失败不该刷屏 */ }
}

/* ------------------------------------------------------------------ 交互 */

async function onPoolAction(action, keyId, label, button) {
  if (action === "remove") {
    if (!confirm("确定要从池中删除并写回配置文件吗？\n\n" + label)) return;
  }
  button.disabled = true;
  var original = button.textContent;
  button.innerHTML = '<span class="spin">◌</span>';
  try {
    var result = await api("/api/keys/" + action, {
      method: "POST",
      body: JSON.stringify({ id: keyId })
    });
    toast(result.message || "操作完成", result.ok === false ? "err" : "ok");
    await refreshState();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function onAddKeys() {
  var raw = $("new-key").value.trim();
  if (!raw) { toast("请先填入 API Key", "err"); return; }
  var button = $("btn-add");
  button.disabled = true;
  button.innerHTML = '<span class="spin">◌</span> 校验中…';
  try {
    var result = await api("/api/keys/add", {
      method: "POST",
      body: JSON.stringify({
        keys: raw,
        account: $("new-account").value.trim(),
        max_concurrency: parseInt($("new-concurrency").value, 10) || 4
      })
    });
    var msg = "已添加 " + result.added.length + " 把 Key";
    if (result.rejected && result.rejected.length) msg += "；" + result.rejected.length + " 把被拒收";
    toast(msg, result.added.length ? "ok" : "err");
    if (result.rejected && result.rejected.length) {
      result.rejected.forEach(function (r) { toast("拒收 " + r.key + "：" + r.reason, "err"); });
    }
    if (result.added.length) { $("new-key").value = ""; }
    await refreshState();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    button.disabled = false;
    button.textContent = "添加";
  }
}

async function onApplyModel() {
  var model = $("model-select").value;
  if (!model) return;
  try {
    var result = await api("/api/model", { method: "POST", body: JSON.stringify({ model: model }) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

async function onApplyOptions() {
  var body = {
    strategy: $("opt-strategy").value,
    rate_mode: $("opt-rate-mode").value,
    qps: parseFloat($("opt-qps").value),
    max_total_wait: parseFloat($("opt-wait").value),
    max_attempts: parseInt($("opt-attempts").value, 10)
  };
  try {
    var result = await api("/api/options", { method: "POST", body: JSON.stringify(body) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

async function onTogglePause() {
  var next = !(lastState.metrics && lastState.metrics.paused);
  try {
    var result = await api("/api/pause", { method: "POST", body: JSON.stringify({ paused: next }) });
    toast(result.message, "ok");
    await refreshState();
  } catch (err) { toast(err.message, "err"); }
}

function copyText(text, label) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function () { toast("已复制" + label, "ok"); },
      function () { toast("复制失败", "err"); });
    return;
  }
  var area = document.createElement("textarea");
  area.value = text;
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  try { document.execCommand("copy"); toast("已复制" + label, "ok"); }
  catch (err) { toast("复制失败", "err"); }
  area.remove();
}

/* ------------------------------------------------------------------ 主循环 */

var lastState = {};

async function refreshState() {
  try {
    var state = await api("/api/state");
    lastState = state;
    $("authbar").classList.remove("show");
    renderKpis(state);
    renderGateway(state);
    renderModels(state);
    renderPool(state);
    renderOptions(state);
    var g = state.gateway || {};
    $("subline").innerHTML = '<span class="dot ' + (state.metrics.paused ? "paused" : "on") + '"></span>' +
      (state.metrics.paused ? "已暂停接入（上层会收到 503）" : "运行中") +
      " · 监听 " + esc(g.listen) + " · " + fmtInt(state.summary.total) + " 把 Key · v" + esc(state.version);
    $("btn-pause").textContent = state.metrics.paused ? "恢复接入" : "暂停接入";
    if (state.log_file) $("logfile").textContent = "日志文件：" + state.log_file;
    else $("logfile").textContent = "未启用文件日志（仅内存缓冲）";
  } catch (err) {
    $("subline").innerHTML = '<span class="dot" style="background:var(--red)"></span>连接失败：' + esc(err.message);
  }
}

function bind() {
  $("btn-refresh").onclick = function () { refreshState(); toast("已刷新"); };
  $("btn-pause").onclick = onTogglePause;
  $("btn-add").onclick = onAddKeys;
  $("btn-apply-model").onclick = onApplyModel;
  $("btn-apply-options").onclick = onApplyOptions;
  $("btn-models").onclick = async function () {
    try { await api("/api/models/refresh", { method: "POST" }); await refreshState(); toast("模型清单已刷新", "ok"); }
    catch (err) { toast(err.message, "err"); }
  };
  $("btn-copy-base").onclick = function () { copyText((lastGateway.base_url || ""), "网关地址"); };
  $("model-select").onchange = updateModelMeta;
  $("new-key").addEventListener("keydown", function (event) { if (event.key === "Enter") onAddKeys(); });
  $("btn-clearlog").onclick = function () { $("logbox").innerHTML = ""; };
  $("btn-autoscroll").onclick = function () {
    S.autoscroll = !S.autoscroll;
    this.textContent = "自动滚动：" + (S.autoscroll ? "开" : "关");
  };
  $("btn-token").onclick = async function () {
    S.token = $("token-input").value.trim();
    localStorage.setItem("sn_rotator_token", S.token);
    await refreshState();
  };
  $("token-input").addEventListener("keydown", function (event) {
    if (event.key === "Enter") $("btn-token").click();
  });
  renderSnippetTabs();
}

bind();
refreshState();
pollLogs();
S.timerState = setInterval(refreshState, 2000);
S.timerLog = setInterval(pollLogs, 1200);
</script>
</body>
</html>
"""
