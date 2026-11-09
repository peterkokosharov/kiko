# kiko-lite on Linux: step by step (Linux Mint 22.3, Q4OS 6 TDE)

**Author:** Peter Kokosharov

Goal: unpack one zip, add your API keys, start kiko-lite, and use Claude Code with Gemini flash,
Gemini flash-lite and NVIDIA Nemotron Ultra behind it. Nothing is installed: no pip, no venv, no litellm.
Everything below is typed in a terminal as your normal user (never as root).

## 1. Check Python (needs 3.11 or newer)

    python3 --version

Linux Mint 22.3 has 3.12, Q4OS 6 has 3.13. Both are fine. If you see 3.10 or lower, stop and ask for help.

## 2. Unpack the zip

Copy `kiko-lite-<version>.zip` from the USB stick to your home folder, then:

    mkdir -p ~/kiko-lite
    cd ~/kiko-lite
    unzip ~/kiko-lite-<version>.zip

(type the exact file name from the stick; keep only one kiko-lite zip in your home folder)

Then make the launcher executable and test it:

    chmod +x kiko-lite
    ./kiko-lite --help

If `unzip` is missing, ask for it to be installed (`sudo apt install unzip`). You should see the list of commands
(serve, add-model, list-models, list-classes, doctor, ...). From here on `./kiko-lite` means
`python3 -m kiko_lite` run inside `~/kiko-lite` (you can type either one).

## 3. Store the API keys

Preferred, works on both systems: a `.env` file in the kiko data folder (the same folder as the database,
`~/.kiko/`). Get your keys from Google AI Studio (Gemini) and build.nvidia.com (NVIDIA).

    mkdir -p ~/.kiko
    chmod 700 ~/.kiko
    nano ~/.kiko/.env

Put exactly these two lines in it (replace the words after `=` with your real keys, no quotes, no spaces),
save with Ctrl+O, Enter, Ctrl+X:

    GEMINI_API_KEY=your-gemini-key
    NVIDIA_API_KEY=your-nvidia-key

Then lock the file so only you can read it:

    chmod 600 ~/.kiko/.env

Never paste a key into a chat, an e-mail or a document. kiko-lite never prints key values.

Order in which kiko-lite looks for a key: environment variable, then the OS keystore, then this `.env` file.

Alternatives (optional): Linux Mint (Cinnamon) can keep keys in the GNOME keyring with `secret-tool`; Q4OS TDE
can use KWallet (`kwallet` / `dcop`). These were not yet proven on real machines; the test and instructions
are in `docs/LINUX-KEYSTORE-TEST.md` (run `python3 tools/linux_keystore_check.py` first). If in doubt, use the
`.env` file.

## 4. Add the models

The alias (first word) is the name you will use later.

    cd ~/kiko-lite
    ./kiko-lite add-model gemini-flash --provider gemini/gemini-3.8-flash --api-key-env GEMINI_API_KEY --class OPUS
    ./kiko-lite add-model gemini-flash-lite --provider gemini/gemini-3.5-flash-lite --api-key-env GEMINI_API_KEY --class HAIKU
    ./kiko-lite add-model nemotron-ultra --provider nvidia/nvidia/nemotron-3-ultra-550b-a55b --api-key-env NVIDIA_API_KEY --class SONNET

Check:

    ./kiko-lite list-models
    ./kiko-lite list-classes

Notes:

* `--provider` is `<provider>/<model id at the provider>`. The ids `gemini/gemini-3.8-flash`,
  `gemini/gemini-3.5-flash-lite` and `nvidia/nvidia/nemotron-3-ultra-550b-a55b` are the ones in the owner's
  working kiko-full config (NVIDIA ids start with their organisation, hence `nvidia/nvidia/...`). If a call ever
  answers "model not found", look up the exact id at the provider and re-run `add-model` with the same alias
  (it updates the row).
* Prices: Gemini prices are built in. NVIDIA has none, so its cost is recorded as 0 (`doctor` shows this as a
  warning; harmless). Add `--price-in` / `--price-out` (USD per million tokens) if you want real numbers.
* The `--class` makes normal Claude names work too: a request for a model whose name contains `opus` goes to the
  best OPUS model, `sonnet` to SONNET, `haiku` to HAIKU, `fable` to FABLE.
* FABLE has no model of its own yet; step 7 sends it to `gemini-flash` through the model name.

## 5. Check the setup

    ./kiko-lite doctor

It prints, per model, `key GEMINI_API_KEY: found in dotenv` (or `env` / `store`). `NOT FOUND` means go back to
step 3. It does not use the network. In home mode these lines are expected and not a problem: `Master key: NOT
FOUND`, the `Auth mode` line, and the NVIDIA warning `price: UNKNOWN` (NVIDIA has no built-in price). The final
count of warnings is therefore not zero.

## 6. Start the server (home mode)

    ./kiko-lite serve --no-auth

* Home mode: no client key is needed (same as the environment variable `KIKO_AUTH=none`). Use it only on a
  computer or home network you trust.
* It listens on `127.0.0.1` port `4000` (this computer only). Other options: `--host 0.0.0.0` (whole network;
  trusted LAN only) and `--port 4001`.
* Browser guard: requests with an `Origin` header (any web page) are refused with 403, a POST must be
  `Content-Type: application/json` (else 415), and `Host` must be localhost / 127.x / `::1` (else 403). Claude
  Code and curl are not affected. With `--host 0.0.0.0` any IP address is accepted as Host; extra names go in
  `KIKO_ALLOWED_HOSTS=name1,name2`. At most 64 connections at once (more get 503).
* Leave this terminal open. Stop with Ctrl+C. The database is `~/.kiko/kiko.db` (change with `--db PATH` or
  `KIKO_DB_PATH`).

## 7. Point Claude Code at it

Open a second terminal and set these (put the same lines in `~/.bashrc` to keep them), then start Claude Code:

    export ANTHROPIC_BASE_URL=http://127.0.0.1:4000
    export ANTHROPIC_AUTH_TOKEN=none
    export ANTHROPIC_DEFAULT_OPUS_MODEL=gemini-flash
    export ANTHROPIC_DEFAULT_SONNET_MODEL=nemotron-ultra
    export ANTHROPIC_DEFAULT_HAIKU_MODEL=gemini-flash-lite
    export ANTHROPIC_DEFAULT_FABLE_MODEL=gemini-flash
    claude

Meaning: OPUS = Gemini flash, SONNET = NVIDIA Nemotron Ultra, HAIKU = Gemini flash-lite, FABLE = Gemini flash
(for now). The values are the model aliases from step 4; kiko-lite accepts an alias or a `provider/model-id` as
the model name. `ANTHROPIC_AUTH_TOKEN=none` is only there so Claude Code does not ask for a login; home mode
ignores it. (If your Claude Code version insists on a key, use `ANTHROPIC_API_KEY=none` instead.)

## 8. One-minute smoke test (before starting Claude Code)

    curl -s http://127.0.0.1:4000/health

Expected: a small JSON answer with `"status"` and `"ok"` (spacing may differ). Then one real message through Gemini flash:

    curl -s http://127.0.0.1:4000/v1/messages -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' -d '{"model":"gemini-flash","max_tokens":50,"messages":[{"role":"user","content":"Say hello in five words."}]}'

Expected: JSON that contains `"type"` `"message"` and a short text (spacing may differ). Repeat with `"model":"nemotron-ultra"` and
`"model":"gemini-flash-lite"`. The server terminal shows each request.

## 9. Troubleshooting

* **First step for everything:** `./kiko-lite doctor` (no network, never prints a key).
* **Port in use** (`Address already in use`): another kiko-lite is running, or use another port:
  `./kiko-lite serve --no-auth --port 4001` and set `ANTHROPIC_BASE_URL=http://127.0.0.1:4001`.
* **Key not found** (doctor says `NOT FOUND`, or the provider answers 401/403): the key name in `.env` must
  match the `--api-key-env` name exactly (`GEMINI_API_KEY`, `NVIDIA_API_KEY`), the file must be `~/.kiko/.env`
  (next to the database; the current folder is not read), one `NAME=value` per line. A wrong or expired key
  gives the provider's own error.
* **401 from kiko-lite itself** ("invalid x-api-key"): the server was not started with `--no-auth`.
* **NVIDIA is slow or answers 429 (rate limit):** normal for the free NVIDIA endpoint. kiko-lite retries
  automatically; the first reply can take a while. If it keeps failing, wait a minute.
* **Model not found:** check the exact id (step 4 notes), fix with `add-model` using the same alias.
* **Keystore problems on Mint or Q4OS:** use the `.env` file, or see `docs/LINUX-KEYSTORE-TEST.md`.
* **"keystore did not answer (locked? unlock prompt open?)"** (doctor or serve): the wallet / keyring is locked and
  an unlock prompt may be waiting. Unlock it and run again; a slow keystore can be given more time with
  `KIKO_KEYSTORE_TIMEOUT=30` (seconds, default 15).
* **Change a model later:** `./kiko-lite disable-model ALIAS`, `enable-model ALIAS`, `delete-model ALIAS`.
  A running server notices changes within a couple of seconds.
* **Change a key in `~/.kiko/.env` later:** a running server re-reads the file when it changes (within a couple of
  seconds) and uses the new, replaced or removed key from the next request on. Keys that come from the environment
  or the OS keystore are not affected by the file, and an environment variable always wins over `.env`; a key in
  the OS keystore is looked up again after at most 5 minutes.
* Spending summary: `./kiko-lite costs` (background health checks of failed models are listed on their own
  line; they count toward the monthly cap, and no paid model is probed once the cap is reached).

## 10. Optional: start automatically (systemd user service)

The zip contains `kiko-lite.service` (a unit template) and `install-service.sh`. From the unpacked folder (it can
be anywhere):

    ./install-service.sh

It writes the real folder into `~/.config/systemd/user/kiko-lite.service`, creates `~/.kiko/.env` (key names only,
mode 600) if it is missing, then runs `daemon-reload`, `enable` and `restart`, and shows the status. Put your keys
into `~/.kiko/.env`; the unit does not read that file through systemd, kiko-lite reads it itself and picks up
changes while running. Running the script again (after unpacking a new zip, or after moving the folder) updates the
unit and restarts the service onto the new code and path. To remove the service: `./install-service.sh --uninstall`
(your data stays). The service runs `serve --no-auth --host 127.0.0.1 --rss-limit-mb 1024` (restarts by itself
after a crash or when memory gets too high).

Note: with `--no-auth`, any other local user account on the same machine can reach the loopback port 4000. On a
shared machine, do not use this; start kiko-lite with a MASTER_KEY instead.

Optional: to start at boot without logging in, run once `loginctl enable-linger "$USER"` (the script does not do it).

Fallback, if you prefer to write the unit by hand: create `~/.config/systemd/user/kiko-lite.service`
(no change needed if you unpacked to `~/kiko-lite`):

    [Unit]
    Description=kiko-lite

    [Service]
    WorkingDirectory=%h/kiko-lite
    ExecStart=/usr/bin/python3 -m kiko_lite serve --no-auth
    Restart=on-failure

    [Install]
    WantedBy=default.target

Then:

    systemctl --user daemon-reload
    systemctl --user enable --now kiko-lite
    systemctl --user status kiko-lite

Logs: `journalctl --user -u kiko-lite -f`. A user service cannot use the desktop keystore before you log in; the
`.env` file always works. systemd user services run on both Linux Mint and Q4OS 6 (TDE); if `systemctl --user`
is not available on your system, the script says so and you use step 6 instead.
