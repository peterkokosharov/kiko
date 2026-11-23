# kiko embedded - Team and rules

The team runs on the owner's home machines (up to 4 laptops, lanes A-D in [PLAN.md](PLAN.md)).

## Roles

| Role | Who | Does |
|---|---|---|
| Owner | Peter | Decides scope and priorities; reviews every step in the evening (go / redo); runs the real-router tests; holds the only real API keys. |
| Lead / architect | **Big Pickle** (Opus-class) | Owns SPEC and the shared headers; splits steps; briefs lanes each morning; merges to `main`; reviews implementers' diffs. |
| Implementers | **Nemotron Ultra** (Sonnet-class) | One step per day per lane: code + tests + review card. |
| Small tasks / tests | **Nemotron Nano** (Haiku-class) | Fixtures, mock server scenarios, test cases, fuzz corpus, doc chores, secret scans. |
| Final auditor | A strongest-class model (**Fable / GPT-6-class**) | Full audit of everything since the last tag before each tag. |
| Second auditor | One more independent model from a different vendor than the first auditor | Independent audit; gets the code and the SPEC, not the first audit's findings. |

## Rules

1. **Every step is reviewed by a different model than the one that wrote it.** Implementer (Nemotron Ultra)
   -> review by Big Pickle; lead's own code -> review by Nemotron Ultra on another lane or an auditor;
   Nano's tests -> review by Ultra. The reviewer gets the diff and the step's "done when", not the
   author's explanation.
2. **Audits before each tag**: the final auditor and the second auditor both, on everything since the
   previous tag. No tag with open critical/high findings. Findings fixed or accepted by the owner with a
   written reason in `docs/audits/`.
3. **One step, one agent, one day.** Briefs name the files the agent may touch. Agents do not change
   SPEC or shared headers; they propose changes in the review card.
4. **No real keys in agent sessions.** Agents use the mock upstream and recorded fixtures only. Real
   provider calls (fixture recording, router smoke) are done by the owner.
5. **Git:** local repository; work on `step/<id>` branches; the lead merges after the owner's go.
   Commits authored as the owner, no AI trailers. Nothing is pushed until the owner decides to publish.
6. **Verify before claiming:** a review card states only what a command showed. "Should work" is not a
   result.
7. **Readable code first:** the owner must be able to read every file and explain it. Short functions,
   one ownership comment per struct, no clever macros.

8. **Comments for a C reader (the owner reads and judges every file):**
   - every file starts with a header comment: what it does and how it fits into the whole;
   - every function has a comment: purpose, inputs, outputs, who frees memory, error cases;
   - every non-obvious line and every protocol detail is commented in plain English, with a link to the
     SPEC section;
   - simple straight-line code is preferred over clever code; no macro magic;
   - reviews reject code that a C reader cannot follow from the comments.
9. **Size of the codebase (20,000+ lines expected):** one module = one `.c` + one `.h`; the header holds
   the public API and the ownership rules; no module reaches into another's struct fields without going
   through its header; new modules are agreed with the lead (rule 3).
10. **libkiko is a library of its own** (PLAN.md, Phase 1): the proxy uses it only through
    `include/kiko/*.h`; libkiko never includes a proxy header and builds and tests alone. Its public
    names mirror kikolib (`kiko_<module>_<name>`); a step that changes the shape of a call against
    kikolib adds the difference to the name table in `libkiko/README.md`. Only the lead changes libkiko
    public headers.

## C safety checklist (every review; with evidence in L13 for libkiko and H3 for the proxy)

**Bounds**
- [ ] Every write into a buffer goes through `kiko_buf_*` (or is a fixed-size write with a checked length).
- [ ] No `strcpy`, `strcat`, `sprintf`, `gets`; `snprintf` results checked for truncation.
- [ ] All parsers (HTTP, SSE, base64, data URIs) check length before every read; no reliance on NUL
      termination of network data.
- [ ] Every configurable limit (SPEC section 6) has a test that hits it exactly and by one byte more.

**Integer overflow**
- [ ] Size arithmetic (`len + n`, `count * size`, `Content-Length` parsing, chunk sizes, base64 lengths)
      uses checked helpers (`__builtin_add_overflow` / `__builtin_mul_overflow`) and `size_t`.
- [ ] Parsed numbers from the network or config are range-checked before use (`strtoull` + errno + max).

**Ownership of buffers and objects**
- [ ] Each heap object has exactly one owner, written next to the struct definition (one owner comment
      per struct).
- [ ] Each module has a header; no `.c` is included by another file; no globals shared across modules
      except through accessor functions.
- [ ] json-c reference counts: every `json_object_get` has a matching `put`; borrowed objects from
      `json_object_object_get_ex` are never `put`.
- [ ] Callbacks from uloop/ustream/uclient never touch a freed request: connection teardown cancels
      timers and upstream requests first (one `request_free()` path).
- [ ] No leaks under valgrind/LSan for the whole test suite.

**TLS**
- [ ] Certificate verification always on for https; hostname checked; tests prove a self-signed and a
      wrong-hostname server are rejected.
- [ ] No option exists to disable verification; plain http only via explicit `insecure_http`.

**Secrets**
- [ ] API keys and client tokens never reach a log line, error message, URL or core-visible debug dump
      (grep test over all log output of the full test suite with known fake keys).
- [ ] Upstream error bodies are scrubbed before being returned.
- [ ] Key/token memory is zeroed on reload and exit; key files must be mode 0600 or the daemon refuses.

**Fuzzing (desktop)**
- [ ] libFuzzer (clang, `-fsanitize=fuzzer,address,undefined`) targets for: libkiko HTTP request
      parser, HTTP response parser, SSE parser, JSON helpers; proxy OpenAI->Gemini request translator,
      Gemini->OpenAI response translator, Gemini stream state machine, OpenAI rewrite path.
- [ ] Same targets build for AFL++ (`afl-clang-fast`) for long overnight runs.
- [ ] Every crash becomes a regression test; corpus kept in `libkiko/fuzz/corpus/<target>/` or
      `fuzz/corpus/<target>/`.
