import secrets
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app import create_app
from models import DeliveryRequest, Trip, User, db, utc_now
from trips import COUNTRY_CODES


class TripApiTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.instance_path = temp.name
        self.config = {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///trip-test.sqlite3",
            "SECRET_KEY": secrets.token_hex(32),
            "SESSION_COOKIE_SECURE": False,
        }
        self.app = self.make_app()
        with self.app.app_context():
            db.create_all()
        self.client = self.app.test_client()

    def make_app(self):
        app = create_app(self.config, instance_path=self.instance_path)
        with app.app_context():
            self.addCleanup(db.engine.dispose)
        return app

    def tomorrow(self, days=1):
        return (utc_now().date() + timedelta(days=days)).isoformat()

    def write(self, client, method, path, data=None):
        token = client.get("/api/me").get_json()["csrf_token"]
        return client.open(path, method=method, json=data, headers={"X-CSRF-Token": token})

    def register(self, client, email="traveler@example.test"):
        response = self.write(client, "POST", "/api/register", {
            "full_name": "Example Traveler", "email": email, "password": "test-only trip passphrase",
            "city": "Ottawa", "phone": "+1 555 0100", "bio": "Example profile",
        })
        self.assertEqual(response.status_code, 201)
        return response.get_json()["user"]

    def payload(self, **changes):
        return {
            "departure_country": "CA", "destination_country": "ET", "travel_date": self.tomorrow(),
            "capacity_grams": 1751, "pricing_method": "fixed", "price_minor": 1234,
            "currency": "USD", "notes": "Leave room for a document envelope",
        } | changes

    def publish(self, client=None, **changes):
        result = self.write(client or self.client, "POST", "/api/trips", self.payload(**changes))
        self.assertEqual(result.status_code, 201, result.get_json())
        return result.get_json()["trip"]

    def test_authenticated_publishing_uses_session_and_public_profile_is_safe(self):
        account = self.register(self.client)
        trip = self.publish(departure_country=" ca ", currency="usd")
        self.assertEqual(trip["departure_country"], "CA")
        self.assertEqual(trip["capacity_grams"], 1751)
        self.assertEqual(trip["price_minor"], 1234)
        self.assertIs(type(trip["price_minor"]), int)
        self.assertEqual(trip["currency"], "USD")
        self.assertEqual(trip["traveler"], {"id": account["id"], "full_name": "Example Traveler", "city": "Ottawa"})
        with self.app.app_context():
            saved = db.session.get(Trip, trip["id"])
            self.assertEqual(saved.traveler.email, "traveler@example.test")
        visitor = self.app.test_client()
        for response in [visitor.get("/api/trips"), visitor.get(f"/api/trips/{trip['id']}")]:
            self.assertEqual(response.status_code, 200)
            for value in ["traveler@example.test", "+1 555 0100", "password_hash", "scrypt:", "verified", "rating"]:
                self.assertNotIn(value, response.get_data(as_text=True))

    def test_unauthenticated_and_csrf_protection(self):
        self.assertEqual(self.client.get("/api/my/trips").status_code, 401)
        self.assertEqual(self.write(self.client, "POST", "/api/trips", self.payload()).status_code, 401)
        self.register(self.client)
        trip_id = self.publish()["id"]
        for method, path in [
            ("POST", "/api/trips"), ("PATCH", f"/api/trips/{trip_id}"), ("DELETE", f"/api/trips/{trip_id}"),
        ]:
            self.assertEqual(self.client.open(path, method=method, json=self.payload()).status_code, 403)
        self.assertEqual(self.client.delete(f"/api/trips/{trip_id}", headers={"X-CSRF-Token": "wrong"}).status_code, 403)

    def test_validation_rejects_unsupported_fields_types_and_values(self):
        self.register(self.client)
        bad_payloads = [
            {"departure_country": "ZZ"}, {"departure_country": "ß"}, {"departure_country": 11},
            {"destination_country": " ca "}, {"travel_date": self.tomorrow(-1)}, {"travel_date": "2030-02-30"},
            {"travel_date": "20300101"}, {"capacity_grams": -1}, {"capacity_grams": 0},
            {"capacity_grams": 1.5}, {"capacity_grams": True}, {"capacity_grams": "1000"},
            {"capacity_grams": 2_147_483_648}, {"notes": "x" * 2001}, {"notes": ["invalid"]},
            {"notes": "\ud800"}, {"pricing_method": "other"}, {"currency": "EUR"}, {"traveler_id": 2},
        ]
        for changes in bad_payloads:
            with self.subTest(changes=list(changes)):
                result = self.write(self.client, "POST", "/api/trips", self.payload(**changes))
                self.assertEqual(result.status_code, 400, result.get_json())
        for data in [[], {}, "wrong type"]:
            self.assertEqual(self.write(self.client, "POST", "/api/trips", data).status_code, 400)
        self.assertEqual(self.client.get("/api/trips").get_json()["total"], 0)

    def test_all_pricing_methods_and_exact_positive_minor_units(self):
        self.register(self.client)
        for method in ("fixed", "negotiable"):
            self.assertEqual(self.publish(pricing_method=method, price_minor=1)["price_minor"], 1)
            for invalid in (None, 0, -1, 12.5, True, "2500"):
                with self.subTest(method=method, price=invalid):
                    response = self.write(self.client, "POST", "/api/trips", self.payload(
                        pricing_method=method, price_minor=invalid,
                    ))
                    self.assertEqual(response.status_code, 400)
        payload = self.payload(pricing_method="agreement")
        self.assertEqual(self.write(self.client, "POST", "/api/trips", payload).status_code, 400)
        del payload["price_minor"]
        result = self.write(self.client, "POST", "/api/trips", payload)
        self.assertEqual(result.status_code, 201)
        self.assertIsNone(result.get_json()["trip"]["price_minor"])

    def test_search_filters_pagination_and_public_visibility(self):
        owner = self.register(self.client)
        early = self.publish(travel_date=self.tomorrow(5))
        late = self.publish(travel_date=self.tomorrow(8))
        self.publish(departure_country="FR")
        hidden = self.publish(travel_date=self.tomorrow(6))
        self.write(self.client, "DELETE", f"/api/trips/{hidden['id']}")
        sender = self.app.test_client()
        self.register(sender, "deactivated@example.test")
        deactivated = self.publish(sender, travel_date=self.tomorrow(6))
        past = self.publish(travel_date=self.tomorrow(7))
        with self.app.app_context():
            db.session.scalar(db.select(User).filter_by(email="deactivated@example.test")).deactivated_at = utc_now()
            db.session.get(Trip, past["id"]).travel_date = utc_now().date() - timedelta(days=1)
            db.session.commit()
        search = "/api/trips?departure_country=ca&destination_country=ET&per_page=1"
        first = self.client.get(search).get_json()
        second = self.client.get(search + "&page=2&sort=rating").get_json()
        self.assertEqual((first["total"], first["has_next"], first["trips"][0]["id"]), (2, True, early["id"]))
        self.assertEqual((second["total"], second["has_next"], second["trips"][0]["id"]), (2, False, late["id"]))
        later = self.client.get(search + "&travel_date=" + self.tomorrow(8)).get_json()["trips"]
        self.assertEqual([trip["id"] for trip in later], [late["id"]])
        for trip in (hidden, deactivated, past):
            self.assertEqual(self.client.get(f"/api/trips/{trip['id']}").status_code, 404)
        for query in ["page=0", "page=1e5", "per_page=51", "sort=bad", "departure_country=ZZ", "travel_date=bad"]:
            self.assertEqual(self.client.get("/api/trips?" + query).status_code, 400)
        self.assertEqual(self.client.get("/api/trips/9999999999999999999999").status_code, 404)
        mine = self.client.get("/api/my/trips?per_page=2").get_json()
        self.assertEqual((mine["total"], len(mine["trips"]), mine["has_next"]), (5, 2, True))
        self.assertTrue(all(trip["traveler"]["id"] == owner["id"] for trip in mine["trips"]))

    def test_owner_can_edit_and_archive_and_other_account_cannot(self):
        self.register(self.client)
        owner_trip = self.publish()
        path = f"/api/trips/{owner_trip['id']}"
        other = self.app.test_client()
        self.register(other, "second@example.test")
        self.assertEqual(other.get("/api/my/trips").get_json()["trips"], [])
        self.assertEqual(self.write(other, "PATCH", path, {"notes": "Unauthorised"}).status_code, 403)
        self.assertEqual(self.write(other, "DELETE", path).status_code, 403)
        self.assertEqual(self.write(self.client, "PATCH", path, {"travel_date": self.tomorrow(-1)}).status_code, 400)
        self.assertEqual(self.write(self.client, "PATCH", path, {"status": "completed"}).status_code, 400)
        updated = self.write(self.client, "PATCH", path, {
            "notes": "Updated", "pricing_method": "agreement", "capacity_grams": 2222,
        })
        self.assertEqual(updated.status_code, 200, updated.get_json())
        self.assertIsNone(updated.get_json()["trip"]["price_minor"])
        self.assertEqual(other.get(path).get_json()["trip"]["capacity_grams"], 2222)
        self.assertEqual(self.write(self.client, "DELETE", path).get_json()["trip"]["status"], "archived")
        self.assertEqual(self.write(self.client, "DELETE", path).status_code, 200)
        self.assertEqual(other.get(path).status_code, 404)
        self.assertEqual(self.write(self.client, "PATCH", path, {"notes": "Archived"}).status_code, 409)
        self.assertEqual(self.client.get("/api/my/trips").get_json()["trips"][0]["status"], "archived")

    def test_delivery_history_is_preserved(self):
        self.register(self.client)
        trip = self.publish()
        path = f"/api/trips/{trip['id']}"
        sender = self.app.test_client()
        account = self.register(sender, "sender@example.test")
        with self.app.app_context():
            request = DeliveryRequest(trip_id=trip["id"], requester_id=int(account["id"].split(":")[1]),
                                      package_category="documents", package_description="Forms", weight_grams=10)
            db.session.add(request)
            db.session.commit()
            request_id = request.id
        self.assertEqual(self.write(self.client, "PATCH", path, {"capacity_grams": 2000}).status_code, 409)
        self.assertEqual(self.write(self.client, "PATCH", path, {"notes": "New note"}).status_code, 200)
        self.assertEqual(self.write(self.client, "DELETE", path).status_code, 409)
        with self.app.app_context():
            db.session.get(DeliveryRequest, request_id).status = "delivered"
            db.session.commit()
        self.assertEqual(self.write(self.client, "DELETE", path).status_code, 200)
        with self.app.app_context():
            saved = db.session.get(DeliveryRequest, request_id)
            self.assertEqual(saved.status, "delivered")
            self.assertEqual(saved.trip.status, "archived")

    def test_persistence_across_app_instances_and_shared_country_list(self):
        self.register(self.client)
        trip = self.publish()
        restarted = self.make_app().test_client()
        self.assertEqual(restarted.get(f"/api/trips/{trip['id']}").get_json()["trip"], trip)
        markup = restarted.get("/").get_data(as_text=True)
        self.assertIn('data-country-codes="' + " ".join(COUNTRY_CODES) + '"', markup)
        self.assertEqual(len(set(COUNTRY_CODES)), 250)
