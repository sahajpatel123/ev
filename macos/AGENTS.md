# macOS EV.app — agent notes

## DO NOT CHANGE: API auth key resolution

Locked files:

- `Sources/EVAuth/APIAuthKey.swift`
- `Sources/EV/AppConfig.swift` (the `APIAuthKey.resolve` call)
- `scripts/package.sh` (`sync_api_env`)

EV.app 401s as **Invalid or revoked device token** when it sends a short leftover (`EV_EARS_API_KEY`, `changeme`, `dev`) or a stale `UserDefaults` token instead of `EV_MASTER_KEY`.

Do not:

- Prefer `UserDefaults` over `~/Library/Application Support/EV/api.env`
- Write keys shorter than 16 characters into `api.env` or `com.ev.suit`
- Treat `EV_EARS_API_KEY` as the menu-bar API credential

If you must touch auth, reproduce a live 401 first and keep `APIAuthKeyTests` passing.