# kiko TDE Panel Applet

TDE (Trinity Desktop Environment) panel applet za upravlenie na kiko-lite proxy servera.

## Sastav

| Fail | Opisanie |
|------|----------|
| `src/kiko_applet.cpp` | Glaven applet kod (KPanelApplet) |
| `src/kiko_applet.h` | Hlavkata na appleta |
| `src/kiko_process.cpp` | Upravlenie na kiko-proxy servusa prez systemctl --user |
| `src/kiko_process.h` | Hlavkata na processa |
| `src/kiko_log_panel.cpp` | Log panel (billing/usage tabela) |
| `src/kiko_log_panel.h` | Hlavkata na log panela |
| `kiko-applet.desktop` | Desktop entry za kicker |
| `icons/Kiko_*.png` | Ikoni za razlichni sostoyaniya |
| `CMakeLists.txt` | Kompiliracen fail |
| `compat_headers/` | TQt3 kompatibilni zaglavia |

## Opravkite (kato chupe appleta)

1. `m_logPanel` — inicializiran s `nullptr` (bihuše garbage pointer → SIGSEGV pri "Show Logs")
2. `m_logBuffer` — dobaven kato `TQStringList` (ne bisha deklariran)
3. Dubliraщ се `#include "moc_kiko_log_panel.cpp"` — mahan (CMake generira MOC kasen izvor)
4. `TQTableItem` konstruktor — dobaven `TQTableItem::Never` EditType
5. `setHorizontalHeaderLabels`, `setAlignment`, `setTextColor` — ne sushchestvuvat v TQt3, zameneni/mahani
6. `TQCheckBox` — dobaven `#include <tqcheckbox.h>`
7. `checkServiceStatus()` — polzva `m_serviceName` namesto skrito `"kiko"`
8. `.latin1()` na vremenen TQString — opraven (dangling pointer)
9. `TDEGlobal::iconLoader()` / `TDEGlobal::dirs()` — null proverki pred izvikvane (SIGSEGV)
10. `m_iconStopped.pixmap()` — null proverka pred `setPixmap`
11. `setServiceName("kiko-proxy")` — suotvetstva na `kiko-proxy.service`

## Kompiliracene

```bash
cd /home/petark/kiko_lite/tqt3
rm -rf build
cmake -S . -B build -DCMAKE_CXX_FLAGS="-I/tmp/kiko-deps/usr/include" -DSQLITE3_LIB=/tmp/kiko-deps/usr/lib/x86_64-linux-gnu/libsqlite3.so
make -C build -j$(nproc)
```

Trebovat se:
- `libtqt3-mt-dev` (TQt3 zaglavia i biblioteki)
- `tdelibs14-trinity-dev` (TDE zaglavia)
- `tdebase-trinity-dev` (kpanelapplet.h)
- `libsqlite3-dev` (SQLite zaglavia — ako nqma, moje da se izpolzvat lokalni pati ot `/tmp/kiko-deps`)
- `tqmoc` (tqt3-dev-tools)
- `pkg-config`, `glib-2.0`

## Instalacene (trqbva sudo)

```bash
/home/petark/install.applet.sh
```

Skriptata kopira:
- `kiko_applet.so` + `kiko_applet.la` → `/opt/trinity/lib/trinity/`
- `kiko-applet.desktop` → `/opt/trinity/share/apps/kicker/applets/`
- `Kiko_*.png` → `/opt/trinity/share/icons/hicolor/22x22/apps/`

## Pokrevane

1. Restar tnite kicker panela.
2. Desno-klik v kicker → "Add applet" → izberi "kiko Proxy".
3. Appletoqt upravlyava `kiko-proxy.service` (user systemd):
   ```bash
   systemctl --user status kiko-proxy.service
   systemctl --user restart kiko-proxy.service
   ```

## Port

kiko-lite proxy raboti po default na `http://127.0.0.1:4000`. Health endpoint: `GET /health`.

## Avtor

Peter Kokosharov <peter.kokosharov@gmail.com>
