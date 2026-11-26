# Shared trip listings

Trips published from the dashboard are saved in SQLite. The browser reads searches and My Trips from Flask. My Trips includes archived records and the dashboard count includes all of the current account's database trips.

## Try it locally

Start Flask using the [account setup instructions](AUTHENTICATION.md). In one browser profile, sign up and publish a trip with a future departure date. In a separate profile, sign up with another email and search for the same route. The second account can see the trip. Edit and Archive are available to the traveler who published it.

Refresh both profiles and restart Flask with the same SQLite file. The trip stays available. Keep the same session secret if you also want the sign-ins to survive the restart.

## Trip API

| Endpoint | Behavior |
|---|---|
| `GET /api/trips` | Lists published trips departing on or after today in UTC. Supports `departure_country`, `destination_country`, `travel_date` as the earliest departure, and `sort`. |
| `GET /api/trips/<id>` | Returns one public trip. |
| `POST /api/trips` | Publishes a trip for the signed-in traveler. |
| `PATCH /api/trips/<id>` | Applies supplied fields to a published trip owned by the signed-in traveler. |
| `DELETE /api/trips/<id>` | Archives the owner's trip so it no longer appears in public search. |
| `GET /api/my/trips` | Lists the signed-in traveler's trips, including history. |

The two list endpoints use `page` and `per_page`. The default page size is 20 and the maximum is 50. The available sort keys are `soonest` and `rating`. Ratings are unavailable, so both use departure date followed by trip ID.

Write requests use the session and the `X-CSRF-Token` from `GET /api/me`. Trip fields are `departure_country` and `destination_country` as supported two-letter codes, `travel_date` as YYYY-MM-DD, `capacity_grams` as a positive integer, `pricing_method` as `fixed`, `negotiable`, or `agreement`, `price_minor` in USD cents where required, `currency` as USD, and optional `notes`. For example, 1.751 kg becomes 1,751 grams and $13.37 becomes 1,337 cents. The public traveler profile includes the public account ID, full name, and city.

## Temporary limits

- Sample and legacy browser trips remain labeled and separate from SQLite listings. Browser views use a `db-trip:` prefix for database trip IDs to distinguish them from older records. This stage neither imports nor edits legacy trip records.
- Eligible shared trips accept [delivery requests](DELIVERY_REQUESTS.md) through Flask. Existing browser-local requests and messages still work on eligible legacy trips tied to server accounts in the same browser profile. Legacy listings owned by old browser-only accounts remain view-only.
- Archiving keeps the trip and delivery history. Active database delivery requests block archiving and changes to trip terms. Notes can still be edited on an otherwise editable trip.
- Pricing is USD only. Existing browser-local price discussions remain local.
