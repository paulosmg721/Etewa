# Etewa

Etewa is a peer-to-peer international delivery marketplace prototype. Travelers publish upcoming trips and offer spare luggage capacity. Requesters find a matching route and ask a traveler to carry a package. One account can do both.

## What works today

- Account registration, login, logout, and profile editing through Flask and SQLite. Passwords are hashed with Werkzeug scrypt.
- Publishing, searching, editing, and archiving shared trips. Country autocomplete covers departure and destination, and listings show upcoming dates, available weight, and USD pricing.
- Three pricing methods support fixed prices, suggested negotiable prices, and trips priced by agreement.
- Shared package requests with incoming and outgoing dashboards. Travelers can accept or decline, and requesters can cancel. Acceptance reserves capacity in a database transaction, and status changes are recorded.

## Stack and layout

The frontend uses HTML, CSS, and vanilla JavaScript. Flask serves the pages and API. Flask-SQLAlchemy and SQLAlchemy connect to SQLite, and Python’s built-in `unittest` runs the automated tests.

| Path | Purpose |
|---|---|
| `app.py`, `config.py` | Flask application, runtime settings, and database initialization |
| `auth.py`, `trips.py`, `delivery.py` | Account, trip, and delivery request APIs |
| `models.py` | Database tables and relationships |
| `templates/index.html`, `static/` | Site layout, styling, and browser behavior |
| `tests/` | Automated tests using isolated databases |
| `instance/` | Local database directory created during setup |

## Run locally

From the project folder in PowerShell, with Python installed:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m flask --app app init-db
$env:ETEWA_SECRET_KEY = .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
.\.venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. To exercise the shared marketplace, publish a trip from one account and request delivery from a second account in a separate browser profile. Keep the same secret between runs if existing sign-ins should survive. Keep the secret and local database out of Git.

To run the tests:

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

See the [account setup](AUTHENTICATION.md), [trip API](TRIPS.md), and [delivery request API](DELIVERY_REQUESTS.md) guides.

## Current limits and roadmap

Etewa is a local development prototype. Cross-browser requests are shared, and dashboard changes from another browser appear on refresh. Older prototype records remain browser-local. The booking flow ends at acceptance, decline, or cancellation. Prices use USD, and ratings are unavailable.

Future stages can add private messaging for shared trips, price offers and agreements, delivery progress after acceptance, ratings and verification, and payment processing. Before production use, the project will also need managed database migrations, shared rate limiting, and account recovery.
