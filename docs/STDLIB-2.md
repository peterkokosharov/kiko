# kikolib as a second standard library — library strategy

Status: proposal, 2026-11-22. Owner's stated direction; not yet implemented.

## The idea

One small, standard-library-only Python base — **kikolib** — shared by every tool. It plays the role of a
"second stdlib": infrastructure that would otherwise be rewritten in each repository (config, key store, HTTP,
threaded server, SSE, retry, OS helpers, implementation factories). Everything above it is thin:

```
kikolib          infrastructure, stdlib only, no asyncio, threads
   └── ronilib   AI pieces: RAG, context, and similar (not published yet)
         ├── kiko    gateway / local service
         ├── rex     CLI
         ├── kircho  UI
         └── roni    application on top of ronilib
```

The point is not the package itself. The point is that after the base is stable, writing the next tool is a small
amount of code instead of a new framework. "Little by little we can write whatever we want."

## Hard constraints

These are not preferences; they are the reason the stack is small enough to maintain.

1. **Python only.** No Node.js. No Electron. A 10 MB "hello world" is a failure, not a starting point.
2. **Standard library only in the base.** A kikolib module imports the stdlib and other kikolib modules. No
   third-party package, no build step.
3. **Threads, not asyncio.** Keeps the code readable and the dependency surface empty.
4. **OS through the OS.** Native facilities via `ctypes` on system libraries or `subprocess` on system binaries,
   never a bundled runtime.
5. **Interfaces + factories.** Pluggable parts register an implementation; `factory.choose()` takes an explicit
   override, then `KIKO_<KIND>_IMPL`, then the best probe that passes by priority.
6. **Keys never leave the OS store.** Secrets are read from env or the platform keyring; values are never printed,
   logged or committed.
7. **Provenance in the file.** Every source file starts with the same three lines: SPDX identifier, copyright, and
   `# LLM: <model names>`. The `# LLM` marker names the model that wrote or last changed the file; keep it.
8. **Python >= 3.11** for kikolib (import fails earlier); kircho currently asks for >= 3.12.

## Current state — three copies, already divergent

Verified 2026-11-22. There is **no `kikolib.deb`** and no published GitHub Release in any of the repos.

| Place | Path | What is there |
|---|---|---|
| kiko | `gateway/kikolib/` | Canonical candidate. 15 modules: `config`, `dotenv`, `factory`, `http`, `keystore`, `logs`, `posix`, `retry`, `server`, `sse`, `tokens`, `tomlwrite`, `websocket`, `winhttp`, `yaml_lite`. |
| rex | `runtimelib/kikolib/` | Separate copy. 11 modules, **renamed**: `http_client` instead of `http`, `posix_utils` instead of `posix`, plus `native_utils`. Not API-compatible with kiko's copy. |
| kircho | `kircho/lib/kikolib/` | Vendored snapshot of 5 files only: `__init__`, `config`, `factory`, `posix`, `server`. Added 2026-11 to give kircho a route table without a second checkout. |
| roni | `ronilib` (per owner) | Not published, not inspected. Reported to hold the AI layer: RAG, context, and similar. Needs its own look before any packaging decision. |

Adjacent but outside this plan: `ai_runtime` is the native side (CMake, service units, `debian/` directory). It is
the launcher/service around the Python tools, not part of the stdlib layer.

Existing packaging in `kiko/packaging/`: `build-deb.sh` → `kiko_<ver>_all.deb`, and
`build-tray-gtk3-deb.sh` → `kiko-tray-gtk3_<ver>_amd64.deb`. Both build locally into `dist/`; neither is published.
`ai_runtime/debian/` has control/rules/changelog but no published artifact either.

## Why a shared deb

A single `kikolib.deb` removes the frozen-copy problem: today a change in `kiko/gateway/kikolib` does not reach
rex or kircho, and the renames in rex mean the copies cannot even be diffed cleanly. With one package:

- the library has one version and one changelog;
- the consumers declare `Depends: kikolib` instead of carrying source;
- a new tool starts from the base rather than from a fresh copy;
- the same artifact can be installed on the five target platforms that already matter (Q4OS, KDE Plasma, GTK3,
  Windows 10, macOS) where a deb applies, with the source tree usable elsewhere.

Trade-off, stated plainly: kircho currently runs from a bare clone with `python3 -m kircho`. Making the deb mandatory
loses that. The likely shape is **both** — an installed `kikolib` when present, a vendored fallback for clone-and-run.
That fallback must be version-checked so it cannot silently drift again.

## Platform matrix — the deb is not the whole answer

`.deb` covers only the Debian family. It happens to cover three of the five targets, because Q4OS, KDE Plasma and
the GTK3/Mint build are all Debian-based. macOS and Windows need different delivery for the same bytes.

**The format is secondary.** Build one payload tree once, then wrap it in whatever the target wants — deb, rpm, tgz,
pkg or zip. On Linux the payload root can simply be `/usr/local`; `kiko/packaging/build-deb.sh` already works this
way, staging into `$D` and then running `fakeroot dpkg-deb --build "$D"`. Adding rpm or tgz means another wrapper
around the same staged tree, not another copy of the library.

One Debian-specific trap: the default import path under `/usr/local` is
`/usr/local/lib/python3.X/dist-packages` (confirmed on Mint 22.3 / Python 3.12.3), while upstream CPython uses
`.../site-packages`. Ask `sysconfig.get_path("purelib")` in the installer instead of hard-coding either name.
Windows and macOS have no `/usr/local` convention for Python packages; there the same payload goes into
`site-packages` via pip, or into a per-app folder via build-time vendoring.

| Platform | Native installer | What to use for kikolib | Note |
|---|---|---|---|
| Q4OS, KDE Plasma, GTK3/Mint | `.deb` | `kikolib.deb` → `/usr/lib/python3/dist-packages/kikolib` | One artifact for all three. |
| macOS | `.pkg`, Homebrew formula | pure-Python wheel via `pip install kikolib`; `.pkg` only as a later wrapper | The system Python is too old; a python.org or Homebrew Python is needed anyway. |
| Windows 10 / 11 | MSI, NSIS, Inno Setup exist but are heavy | the **same pure-Python wheel** via `pip install kikolib` | No global Python path convention; pip puts it in `site-packages`, so no `PYTHONPATH` in a `.cmd`. |

The genuinely cross-platform artifact is therefore **not** the deb — it is a versioned source snapshot / pure-Python
wheel (`kikolib-<version>-py3-none-any.whl`, tens of KB, zero dependencies). The deb, the `.pkg` and pip are only
delivery wrappers that put the same files on disk. Keeping one wheel and three thin wrappers avoids maintaining three
different libraries.

For Windows specifically, avoid: compiled installers, a bundled Python runtime, or PATH hacks in launcher scripts.
All three contradict the size constraint. A wheel needs only the Python that rex/kircho already require on PATH.
If clone-and-run must stay on Windows, the fallback is build-time vendoring from one git tag into the app's folder,
with a version stamp written at the same time — still one source, many copies made deliberately.

Two rules make this safe:

1. **One install location per platform.** If the kiko deb puts kikolib in `/opt/kiko` and a pip install puts another
   copy in `site-packages`, the split is back. Decide the path once and make every installer use it.
2. **A version stamp.** Add `kikolib/__version__.py` (or a `__version__` in `__init__.py`). Each app checks at
   startup: use the installed kikolib when it is present and new enough; otherwise use its vendored snapshot and
   print a visible warning naming both versions. Silent drift is the failure mode to prevent.

## What has to happen

1. **Choose one source.** Suggested: `kiko/gateway/kikolib` — the most complete copy.
2. **Reconcile the consumers.** Map rex's renamed modules back to the canonical names (or define stable aliases);
   reduce kircho's 5-file snapshot to the same version with a clear fallback rule. Inspect `ronilib` once roni is
   published.
3. **Define the package.** Install location, suggested `/usr/lib/python3/dist-packages/kikolib`, so no consumer
   needs `PYTHONPATH`. Declare `Depends: python3 (>= 3.11)`.
4. **Choose where it is published.** Two candidates: GitHub Release with an install script, or a real apt
   repository with `Packages.gz`. Nothing is published today, so this is the missing piece behind "once it is
   uploaded, we can upload whatever we want".
5. **Then package the consumers** — kiko, rex, kircho, roni — each declaring the dependency instead of copying.
6. **Keep the provenance.** Carry the SPDX/copyright/`# LLM` headers into the deb and record the source commit in
   the package changelog, so a deployed copy can be traced back.

## Open questions

- Is `kikolib.deb` meant for all five platforms, or only the Debian-family ones? The other platforms may need a
  source-level distribution instead.
- Who owns the release: kiko repo, or a separate small `kikolib` repo? A separate repo makes the dependency honest
  but adds one more place to push.
- Does `ronilib` sit inside the same deb as a second component, or is it a separate package with its own version?
- Do the Windows and macOS targets actually have Python 3.12 with `pip` on PATH? rex/kircho need that Python anyway;
  if they do, the wheel is free. If not, build-time vendoring is the only small option.
- Is a `.pkg` needed on macOS at all, or is `pip install kikolib` acceptable for the intended users?
- Should rex's renamed modules be kept as compatibility aliases? Renaming them back may break rex.
- Is the vendored fallback in kircho acceptable long term, or is a clean "install the deb first" requirement preferred?

## Sources

`kiko/gateway/kikolib/README.md`, `kiko/packaging/*.sh`, `docs/INSTALL.md`, repository trees of rex and kircho
(2026-11-22), and the owner's description of roni/ronilib on 2026-11-22 (`ronilib` not yet inspected).
