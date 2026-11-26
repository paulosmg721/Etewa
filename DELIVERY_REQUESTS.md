# Shared delivery requests

An Etewa account can publish trips and request deliveries on other travelers’ trips. Sign in, use **Publish a Trip** to offer luggage space, or use **Send a Package** to search by departure country, destination country, and earliest travel date.

## Try it with two accounts

Start the local Flask server using the [account setup instructions](AUTHENTICATION.md). In the first browser profile, register and publish a future trip. In a second profile, register with a different email, search for that route, and choose **Request Delivery**. Enter the package category, weight, and description. Use **Other** only with a category explanation.

In the first profile, use **Refresh requests** in the dashboard and look under **Requests for My Trips**. Accept or decline the request. In the second profile, refresh **My Requests** to see the decision. Reload either page or restart Flask with the same SQLite file to check persistence. Keep the same session secret across the restart to preserve the sign-ins.

## API endpoints

| Endpoint | Behavior |
|---|---|
| `POST /api/trips/<trip_id>/requests` | Sends a request to the traveler. The signed-in account is the requester. |
| `GET /api/my/requests` | Returns the signed-in account's incoming and outgoing requests. |
| `GET /api/requests/<request_id>` | Returns a request and its history to either participant. |
| `POST /api/requests/<request_id>/accept` | Lets the traveler accept a pending request if there is enough capacity. |
| `POST /api/requests/<request_id>/decline` | Lets the traveler decline a pending request. |
| `POST /api/requests/<request_id>/cancel` | Lets the requester cancel a pending or accepted request. |

Writes use the signed-in session and the `X-CSRF-Token` header from `GET /api/me`. A creation request sends `package_category`, `package_description`, `weight_grams`, and, for Other, `category_details`. Public trip responses include `available_capacity_grams`. Request details stay private to the two participants.

## Capacity and status

A pending request does not reserve space. Accepted requests do, so the server checks the latest space again inside the acceptance transaction. Cancelling an accepted request releases that space. If simultaneous approvals compete for the last space, one succeeds and the other receives an error. A sender can have only one active request per trip. After a decline or cancellation, they may submit a new request if the trip is still eligible.

The API displays **Pending**, **Accepted**, **Declined**, and **Cancelled**. The existing database stores a decline as a cancellation by the traveler. The status history identifies who acted and turns it into **Declined** in the API. This preserves the existing schema.

## Current limits

- Fixed Price shows the listed price. Negotiable shows a suggested price. By Agreement has no initial price. Shared requests do not record an agreement or collect payment. Historical request rows show the trip's current pricing and route details, while status events retain their original timestamps.
- Shared messages and in-transit or delivered actions are outside this stage. Existing browser-only records remain labeled and stay in the browser where they were created.
- Refresh the dashboard to see changes made from another browser. The current dashboard loads the full request list for the signed-in account.
