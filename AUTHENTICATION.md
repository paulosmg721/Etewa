# Etewa account setup

In PowerShell, from the project directory, initialize the SQLite tables and set a random session secret before starting Flask.

```powershell
.\.venv\Scripts\python.exe -m flask --app app init-db
$env:ETEWA_SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. Sign up with a new account and a password of at least 12 characters. Reload to confirm the session is restored, edit your profile from the dashboard, then sign out. Open a private browser window and sign in with the same email and password to confirm the account is shared through SQLite.

Keep the same session secret across restarts if existing sessions should survive. The example sets it only in the current PowerShell process. Store a persistent value in your environment or a secret manager, never in the repository. For an HTTPS deployment, set `ETEWA_SESSION_COOKIE_SECURE=1` in the environment. Leave it unset for the local HTTP server.

## Account API

| Endpoint | Behavior |
|---|---|
| `GET /api/me` | Returns the current account or `user: null`, plus a CSRF token for subsequent writes. |
| `POST /api/register` | Accepts JSON with `full_name`, `email`, `password`, and optional `city`, `phone`, and `bio`. Creates the account and signs in. |
| `POST /api/login` | Accepts JSON with `email` and `password` and starts a new signed-in session. |
| `POST /api/logout` | Clears the signed-in session and returns a new anonymous CSRF token. |
| `GET /api/profile` | Returns the signed-in account's own profile, including its optional phone number. |
| `PATCH /api/profile` | Updates permitted fields on the signed-in account. Only `full_name`, `city`, `phone`, and `bio` are writable. |

POST and PATCH require the session cookie and an `X-CSRF-Token` header containing the token returned by the API. Registration, login, and logout replace that token. The browser keeps it in page memory. The session cookie is signed, HttpOnly, and SameSite=Lax. Signed-in sessions expire after eight hours.

## Transitional behavior

- Server account IDs start with `account:` so they stay separate from old browser accounts. Register a new server account even if the email was used in the old prototype.
- Legacy account and marketplace keys remain in localStorage. Server sign-in ignores old account and session keys.
- New trips and their delivery requests are shared through SQLite. Older browser-only records retain their original storage. See [Delivery requests](DELIVERY_REQUESTS.md) for the current behavior.
- The sign-in limit is per IP and per Flask process. It resets on restart and is not shared between workers. Use a shared limiter before running multiple workers. Flask's signed cookies do not provide a server-side session revocation list. Logout clears the current browser session, and deactivation blocks the account on subsequent protected requests.
- Email ownership verification, password reset, and marketplace APIs are outside this stage.
