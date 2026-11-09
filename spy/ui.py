# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""The spy page: one HTML file, one script, one stylesheet. Data is only ever put on the page with
``textContent`` (never ``innerHTML``); the Content-Security-Policy forbids inline script and foreign sources."""

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>kiko spy</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/app.css">
</head>
<body>
<header><h1>kiko spy</h1><span id="status">connecting</span></header>
<main>
<section><h2>LLM requests</h2><div class="pane" id="llm"></div></section>
<section><h2>Server log</h2><div class="pane" id="server"></div></section>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

CSS = """body{margin:0;font:13px/1.4 ui-monospace,Consolas,monospace;background:#111;color:#ddd;display:flex;
flex-direction:column;height:100vh}
header{display:flex;gap:1em;align-items:baseline;padding:4px 10px;background:#222}
h1{font-size:15px;margin:0}h2{font-size:13px;margin:0 0 4px}#status{color:#8c8}#status.bad{color:#e66}
main{flex:1;display:flex;gap:6px;padding:6px;min-height:0}
section{flex:1;display:flex;flex-direction:column;min-width:0}
.pane{flex:1;overflow:auto;background:#000;padding:4px;white-space:pre-wrap;word-break:break-all}
.pane div.err{color:#f88}
"""

JS = """(function () {
  "use strict";
  var MAX = 1000;
  var status = document.getElementById("status");
  function add(channel, text) {
    var pane = document.getElementById(channel);
    if (!pane) { return; }
    var stick = pane.scrollTop + pane.clientHeight >= pane.scrollHeight - 8;
    var row = document.createElement("div");
    row.textContent = text;
    if (/ERROR|error|Traceback/.test(text)) { row.className = "err"; }
    pane.appendChild(row);
    while (pane.childNodes.length > MAX) { pane.removeChild(pane.firstChild); }
    if (stick) { pane.scrollTop = pane.scrollHeight; }
  }
  function setStatus(text, bad) {
    status.textContent = text;
    status.className = bad ? "bad" : "";
  }
  var es = new EventSource("/events");
  es.addEventListener("line", function (e) {
    var m;
    try { m = JSON.parse(e.data); } catch (x) { return; }
    add(String(m.ch), String(m.text));
  });
  es.onopen = function () { setStatus("live", false); };
  es.onerror = function () { setStatus("reconnecting", true); };
})();
"""
