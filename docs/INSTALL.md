# Install kiko on a Linux desktop (Mint, Q4OS, Plasma)

`kiko install` does the whole per-user setup: copies the gateway, asks for the provider keys
(echo off), loads the verified model catalog, writes a systemd **user** unit and starts it.
No sudo is needed for the install itself; sudo is only needed to install the `.deb`.

## Option A — from a checkout (no sudo)

```bash
git clone -b fix/mint-tray-build https://github.com/peterkokosharov/kiko.git ~/kiko
cd ~/kiko
bash tray/tools/kiko install
```

The first command asks for `NVIDIA_API_KEY` and `GEMINI_API_KEY` with echo turned off. Get them from
https://build.nvidia.com/ and https://aistudio.google.com/apikey. Keys go into GNOME Keyring under
`service=/peterk`, `key=<KEY>`, `username=<your login>` and are never written to a file or to git.

## Option B — build and install the .deb

```bash
git clone -b fix/mint-tray-build https://github.com/peterkokosharov/kiko.git ~/kiko
cd ~/kiko
bash packaging/build-deb.sh
sudo apt install -y libsecret-tools curl          # package dependencies
sudo dpkg -i dist/kiko_0.3.0_all.deb
kiko install
```

The package puts the code in `/opt/kiko` and a single command in `/usr/bin/kiko`. `kiko install`
still runs as your normal user, because the keyring and the systemd user session belong to you.

## GTK3 tray for Mint / Ubuntu

Separate package, so it is clear the native GTK3 build is solved:

```bash
sudo apt install -y build-essential cmake libgtk-3-dev fakeroot
cd ~/kiko
bash packaging/build-tray-gtk3-deb.sh
sudo apt install -y ./dist/kiko-tray-gtk3_0.1.0_amd64.deb
```

The build runs `tray/tools/build-mint.sh` (`-DKT_GUI=gtk`, vendored cJSON) and CTest — 6/6 tests.
The package installs `/usr/bin/kiko-tray`, an XDG autostart entry, the four status icons and a short
README. It links only against the distribution's GTK3 libraries (no RPATH into a build folder).

## Verify

```bash
kiko doctor
curl -sS http://127.0.0.1:4000/health
# {"status":"ok"}
```

`kiko doctor` prints **key names only** — never a key value. It shows the code root, the database,
the service state and the health answer.

## Run Claude Code against it

```bash
git clone https://github.com/peterkokosharov/ai_runtime.git ~/ai_runtime
cd ~/ai_runtime
./run-claude-kiko.sh -p 'Reply with exactly: OK'   # prints OK
./run-claude-kiko.sh                               # interactive
```

Claude Code itself is installed with `curl -fsSL https://claude.ai/install.sh | bash` (no sudo, no
Node). The launcher sends Anthropic requests to `http://127.0.0.1:4000` with a dummy token; the real
provider keys stay in the keyring. `KIKO.md` in `ai_runtime` explains the model aliases.

## Useful variants

```bash
KIKO_PORT=5000 kiko install     # use another port (e.g. beside an existing gateway)
kiko models                     # re-run the verified model catalog, keeping your DB
kiko restart                    # restart the user service
kiko uninstall                  # stop service, remove code; keeps DB and keys
kiko uninstall --purge          # also removes the DB; keys still stay in the keyring
```

`kiko uninstall` never removes keyring keys — those are user data and are shared with other tools.
