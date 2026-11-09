// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
// LLM: Anthropic Opus
// Live request log: new lines arrive as server-sent events and are added with textContent only.
// The stream is open only while the <details> block is open.
const out = document.getElementById("live");
const box = out && out.closest("details");
if (out && box && window.EventSource) {
  let source = null;
  const open = () => {
    if (source) return;
    source = new EventSource(out.dataset.src);
    source.onmessage = (event) => {
      out.textContent = JSON.parse(event.data) + "\n" + out.textContent;
      out.textContent = out.textContent.split("\n").slice(0, 200).join("\n");
    };
  };
  const close = () => {
    if (source) {
      source.close();
      source = null;
    }
  };
  box.addEventListener("toggle", () => (box.open ? open() : close()));
  if (box.open) open();
}
