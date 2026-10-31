# kiko-lite Project Status (2026-10-31)

## Repository
- **GitHub**: https://github.com/peterkokosharov/kiko (public, read-only for others)
- **Structure**: `kiko_lite/` (main), `kikolib/` (stdlib lib), `web-ui/` (admin UI)

## Running Services

| Service | Port | Command | Status |
|---------|------|---------|--------|
| kiko-lite proxy | 4002 | `/home/petark/kiko-lite` | ✅ Works - 6 models |
| web-ui admin | 4008 | `/home/petark/kiko_lite/start-web.sh` | ✅ Works - all endpoints |

## Configured Models (in ~/.kiko/kiko.db)
- gemini-flash-lite (Gemini, $0.30/$2.50/Mtok)
- gemini-flash (Gemini, $0.75/$3.75/Mtok) 
- nemotron-ultra (NVIDIA, no price)
- nemotron-super (NVIDIA, no price)
- groq-llama (Groq, no price)
- mistral-small (Mistral, no price)

## Keys
- Stored in `/home/petark/.kiko/.env` (8 keys, loaded via dotenv)
- Key names: GEMINI_GEMINI_FLASH_LITE_API_KEY, etc.

## web-ui (port 4008) - Complete
**Done:**
- Auth system: login/logout/change-password, cookie + Bearer token, default admin/admin
- Modular JS: `core/api.js`, `core/auth.js`, `core/utils.js`, `components/modal.js`, `components/table.js`
- Modules: `models.js`, `classes.js`, `billing.js`, `settings.js`, `aliases.js` (lazy-loaded)
- Templates: login modal, change-password modal, 5 tabs
- Tests: 16 API integration tests passing

**Fixed:**
- Static file serving (/static/*.js, /static/*.css) - router extended to support `{path:path}` syntax
- DELETE /api/classes endpoint - added `delete_class` method to Database

## Tests
- `web-ui/static/tests/api.test.js` - 16 tests passing (auth, models, classes, billing, settings, aliases, key-status)

## Quick Start
```bash
# Proxy (port 4002)
/home/petark/kiko-lite

# Web UI (port 4008) - from anywhere
/home/petark/kiko_lite/start-web.sh

# Run tests
cd /home/petark/kiko_lite/web-ui/static/tests && node --test api.test.js
```