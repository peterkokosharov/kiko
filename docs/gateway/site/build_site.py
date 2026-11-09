# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Anthropic Opus
"""Build static HTML from site/<lang>/*.md into site/html/<lang>/ (standard library only).

Each page embeds its markdown (no fetch), so it opens by double-click from disk (file://).
marked and mermaid are loaded from cdn.jsdelivr.net at pinned versions, so a first view needs internet.
Languages are the ones listed in LANGS (a language is built only if its folder has .md files).
Run: python build_site.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

MARKED = "marked@12.0.2"
MERMAID = "mermaid@10.9.1"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "html"
LANGS = ("en", "bg")  # switch order; a language is built only if site/<lang>/ has .md files
ORDER = ["README", "ARCHITECTURE", "MODEL-CLASSES", "ROADMAP", "NUMBERS", "CODE-TOUR"]
TIPS = {"en": ("click to enlarge", "click to close, double-click to zoom"),
        "bg": ("кликни за уголемяване", "кликни за затваряне, двоен клик за мащаб")}
TITLES = {
    "en": {"README": "Overview", "ARCHITECTURE": "Architecture", "MODEL-CLASSES": "Model classes",
           "ROADMAP": "Roadmap", "NUMBERS": "Numbers", "CODE-TOUR": "Code tour"},
    "bg": {"README": "Обзор", "ARCHITECTURE": "Архитектура", "MODEL-CLASSES": "Класове модели",
           "ROADMAP": "План", "NUMBERS": "Числа", "CODE-TOUR": "Обиколка из кода"},
}

CSS = """
:root{--bg:#fff;--fg:#1f2328;--mut:#656d76;--nav:#f6f8fa;--bd:#d0d7de;--code:#f6f8fa;--lnk:#0969da}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--nav:#161b22;--bd:#30363d;--code:#161b22;--lnk:#58a6ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,Segoe UI,sans-serif;display:flex;min-height:100vh}
nav{width:250px;flex:none;background:var(--nav);border-right:1px solid var(--bd);padding:20px 16px;position:sticky;top:0;height:100vh;overflow:auto}
nav h2{font-size:18px;margin:0 0 12px}nav a{display:block;padding:6px 10px;border-radius:6px;color:var(--fg);text-decoration:none}
nav a:hover{background:var(--bd)}nav a.cur{background:var(--lnk);color:#fff}
.lang{margin:0 0 14px;font-size:14px}.lang a{display:inline;padding:2px 6px}.lang a.cur{background:var(--lnk);color:#fff}
main{flex:1;max-width:960px;padding:24px 40px;min-width:0}a{color:var(--lnk)}
pre{background:var(--code);border:1px solid var(--bd);border-radius:6px;padding:12px;overflow:auto;font-size:13.5px;line-height:1.45}
code{font-family:Consolas,Menlo,monospace;font-size:90%}:not(pre)>code{background:var(--code);padding:1px 5px;border-radius:4px}
table{border-collapse:collapse;display:block;overflow:auto}th,td{border:1px solid var(--bd);padding:6px 12px;text-align:left}
th{background:var(--nav)}pre.mermaid{background:transparent;border:none;text-align:center}
pre.mermaid svg{cursor:zoom-in}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:9;cursor:zoom-out;overflow:auto}
#lb.on{display:flex}#lb svg{background:#fff;width:96vw;height:94vh;max-width:96vw;max-height:94vh;padding:12px;border-radius:6px;flex:none}
#lb.big{align-items:flex-start;justify-content:flex-start}#lb.big svg{max-width:none;max-height:none;width:200%;height:auto}
h1,h2{border-bottom:1px solid var(--bd);padding-bottom:6px}
@media(max-width:700px){body{display:block}nav{width:auto;height:auto;position:static}main{padding:16px}}
"""

PAGE = """<!DOCTYPE html>
<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} - kiko-lite</title><style>{css}</style></head><body>
<nav><h2>kiko-lite</h2><div class="lang">{langs}</div>{links}</nav>
<main id="content"></main>
<div id="lb" title="{close}"></div>
<script id="md" type="application/json">{md}</script>
<script src="https://cdn.jsdelivr.net/npm/{marked}/marked.min.js"></script>
<script type="module">
import mermaid from "https://cdn.jsdelivr.net/npm/{mermaid}/dist/mermaid.esm.min.mjs";
const src = JSON.parse(document.getElementById("md").textContent);
const el = document.getElementById("content");
const r = new marked.Renderer();
r.code = (code, lang) => {{
  const t = typeof code === "object" ? code.text : code, l = typeof code === "object" ? code.lang : lang;
  const esc = t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return l === "mermaid" ? '<pre class="mermaid">' + esc + "</pre>" : '<pre><code>' + esc + "</code></pre>";
}};
el.innerHTML = marked.parse(src, {{renderer: r}});
el.querySelectorAll("a[href]").forEach(a => {{
  const h = a.getAttribute("href");
  if (/^[A-Za-z0-9_-]+[.]md(#.*)?$/.test(h)) a.setAttribute("href", h.replace(".md", ".html"));
  else if (!/^([a-z][a-z0-9+.-]*:|#|\\/)/i.test(h)) a.setAttribute("href", "../" + h);  // the html is one folder deeper than the md
}});
const dark = matchMedia("(prefers-color-scheme: dark)").matches;
mermaid.initialize({{startOnLoad: false, theme: dark ? "dark" : "default"}});
const pre = [...el.querySelectorAll("pre.mermaid")];
pre.forEach(p => {{ p.dataset.src = p.textContent; }});
try {{
  await mermaid.run({{querySelector: "pre.mermaid"}});
}} catch (e) {{
  console.error(e);
}} finally {{
  // scroll to the #anchor only now: the diagrams change the page height
  if (location.hash) {{ const t = document.getElementById(decodeURIComponent(location.hash.slice(1))); if (t) t.scrollIntoView(); }}
}}
// click to enlarge: the overlay always shows the light-theme diagram on white, so it is readable in dark mode too
const lb = document.getElementById("lb");
let n = 0;
pre.forEach(p => {{
  const sv = p.querySelector("svg");
  if (!sv) return;
  sv.setAttribute("data-tip", "1");
  p.title = "{tip}";
  sv.addEventListener("click", async () => {{
    let svg = sv.outerHTML;
    if (dark) {{
      try {{
        mermaid.initialize({{startOnLoad: false, theme: "default"}});
        svg = (await mermaid.render("lbd" + (n++), p.dataset.src)).svg;
      }} catch (e) {{}}
      mermaid.initialize({{startOnLoad: false, theme: "dark"}});
    }}
    const t = document.createElement("div");
    t.innerHTML = svg;
    const c = t.querySelector("svg");
    c.removeAttribute("width"); c.removeAttribute("height"); c.style.removeProperty("max-width");
    if (!c.getAttribute("viewBox")) {{
      const b = sv.getBBox(); c.setAttribute("viewBox", "0 0 " + b.width + " " + b.height);
    }}
    lb.querySelector("svg")?.remove();
    lb.appendChild(c);
    lb.className = "on";
  }});
}});
lb.addEventListener("click", e => {{
  if (e.detail === 1) setTimeout(() => {{ if (!lb.dataset.z) lb.className = ""; lb.dataset.z = ""; }}, 250);
}});
lb.addEventListener("dblclick", () => {{ lb.dataset.z = "1"; lb.className = lb.className === "on big" ? "on" : "on big"; }});
document.addEventListener("keydown", e => {{ if (e.key === "Escape") lb.className = ""; }});
</script></body></html>
"""

REDIRECT = '<!DOCTYPE html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url={url}"><a href="{url}">{url}</a>\n'


def pages(lang: str) -> list[Path]:
    d = ROOT / lang
    files = {p.stem: p for p in d.glob("*.md")} if d.is_dir() else {}
    names = [n for n in ORDER if n in files] + sorted(n for n in files if n not in ORDER)
    return [files[n] for n in names]


def build(lang: str) -> int:
    ps = pages(lang)
    titles = TITLES.get(lang, TITLES["en"])
    out = OUT / lang
    out.mkdir(parents=True, exist_ok=True)
    for p in ps:
        links = "".join(f'<a href="{q.stem}.html"{" class=cur" if q == p else ""}>{html.escape(titles.get(q.stem, q.stem))}</a>'
                        for q in ps)
        langs = " ".join(f'<a href="../{lg}/{p.stem}.html"{" class=cur" if lg == lang else ""}>{lg.upper()}</a>' for lg in LANGS)
        md = json.dumps(p.read_text(encoding="utf-8")).replace("</", "<" + chr(92) + "/")
        page = PAGE.format(lang=lang, title=html.escape(titles.get(p.stem, p.stem)), css=CSS, langs=langs,
                           links=links, md=md, marked=MARKED, mermaid=MERMAID,
                           tip=TIPS.get(lang, TIPS["en"])[0], close=TIPS.get(lang, TIPS["en"])[1])
        (out / f"{p.stem}.html").write_text(page, encoding="utf-8")
    return len(ps)


def main() -> None:
    avail = [lg for lg in LANGS if pages(lg)]
    OUT.mkdir(exist_ok=True)
    for lg in avail:
        print(f"{lg}: {build(lg)} pages")
    first = "bg" if "bg" in avail and (OUT / "bg" / "README.html").exists() else "en"
    (OUT / "index.html").write_text(REDIRECT.format(url=f"{first}/README.html"), encoding="utf-8")
    print(f"index.html -> {first}/README.html")


if __name__ == "__main__":
    main()
