import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app import create_app
from models import (
    Conversation,
    ConversationParticipant,
    DeliveryRequest,
    Message,
    PricingAgreement,
    RequestStatusHistory,
    Trip,
    User,
    db,
)


class DatabaseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("database-test-fixture-only")

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(temp.cleanup)
        self.database_path = Path(temp.name, "test.sqlite3")
        self.app = create_app(
            {"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///test.sqlite3"},
            instance_path=temp.name,
        )
        context = self.app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(db.engine.dispose)
        self.addCleanup(db.session.remove)
        result = self.app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(result.exit_code, 0, result.output or repr(result.exception))

    def add_user(self, email="traveler@example.test"):
        user = User(full_name="Test user", email=email, password_hash=self.password_hash)
        db.session.add(user)
        db.session.commit()
        return user

    def make_trip(self, traveler_id, **overrides):
        fields = dict(
            traveler_id=traveler_id,
            departure_country="CA",
            destination_country="ET",
            travel_date=date.today() + timedelta(days=10),
            capacity_grams=3000,
            pricing_method="fixed",
            price_minor=2500,
        )
        return Trip(**(fields | overrides))

    def add_trip(self, traveler_id):
        trip = self.make_trip(traveler_id)
        db.session.add(trip)
        db.session.commit()
        return trip

    def make_request(self, trip_id, requester_id, **overrides):
        fields = dict(
            trip_id=trip_id,
            requester_id=requester_id,
            package_category="documents",
            package_description="Signed forms",
            weight_grams=750,
        )
        return DeliveryRequest(**(fields | overrides))

    def add_request(self, trip_id, requester_id):
        request = self.make_request(trip_id, requester_id)
        db.session.add(request)
        db.session.commit()
        return request

    def assert_insert_rejected(self, record):
        db.session.add(record)
        with self.assertRaises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_initialization_creates_all_tables_and_can_be_repeated(self):
        self.assertTrue(self.database_path.is_file())
        self.assertEqual(set(inspect(db.engine).get_table_names()), {
            "users", "trips", "delivery_requests", "conversations", "conversation_participants",
            "messages", "pricing_agreements", "request_status_history",
        })
        self.assertEqual(db.session.execute(text("PRAGMA foreign_keys")).scalar_one(), 1)
        user_id = self.add_user().id
        result = self.app.test_cli_runner().invoke(args=["init-db"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNotNone(db.session.get(User, user_id))
        self.assertEqual(db.session.execute(text("PRAGMA foreign_key_check")).all(), [])

    def test_create_read_update_and_relationships(self):
        traveler = self.add_user()
        sender = self.add_user("sender@example.test")
        trip = self.add_trip(traveler.id)
        request = self.add_request(trip.id, sender.id)
        conversation = Conversation(request=request)
        conversation.participants = [ConversationParticipant(user=traveler), ConversationParticipant(user=sender)]
        db.session.add(conversation)
        db.session.flush()
        message = Message(conversation=conversation, sender_id=sender.id, content="Can we arrange pickup?")
        offer = PricingAgreement(request=request, proposer=sender, amount_minor=1950)
        event = RequestStatusHistory(request=request, new_status="requested", changed_by=sender)
        db.session.add_all([message, offer, event])
        db.session.commit()
        trip_id, conversation_id = trip.id, conversation.id
        original_timestamp = request.updated_at
        request.status = "accepted"
        offer.status = "accepted"
        read_at = datetime(2026, 11, 26, 12, tzinfo=timezone(timedelta(hours=-5)))
        conversation.participants[0].last_read_at = read_at
        db.session.add(RequestStatusHistory(
            request=request, previous_status="requested", new_status="accepted", changed_by=traveler,
        ))
        db.session.commit()
        db.session.remove()

        saved_trip = db.session.get(Trip, trip_id)
        saved_conversation = db.session.get(Conversation, conversation_id)
        saved_request = saved_trip.requests[0]
        self.assertEqual(saved_trip.traveler.email, "traveler@example.test")
        self.assertEqual(saved_request.requester.email, "sender@example.test")
        self.assertEqual(saved_request.status, "accepted")
        self.assertGreaterEqual(saved_request.updated_at, original_timestamp)
        self.assertEqual(saved_request.updated_at.tzinfo, timezone.utc)
        self.assertIs(saved_conversation.request, saved_request)
        self.assertEqual(saved_conversation.messages[0].sender_membership.user.email, "sender@example.test")
        self.assertEqual(saved_request.offers[0].amount_minor, 1950)
        self.assertEqual(saved_request.offers[0].status, "accepted")
        self.assertEqual([row.new_status for row in saved_request.status_history], ["requested", "accepted"])
        self.assertEqual(
            [row.last_read_at for row in saved_conversation.participants if row.last_read_at],
            [read_at.astimezone(timezone.utc)],
        )

    def test_normalized_email_uniqueness(self):
        first = self.add_user("  Traveler@Example.Test  ")
        self.assertEqual(first.email, "traveler@example.test")
        self.assert_insert_rejected(User(
            full_name="Duplicate", email="TRAVELER@example.test", password_hash=self.password_hash,
        ))
        with self.assertRaises(IntegrityError):
            db.session.execute(User.__table__.insert().values(
                full_name="Unnormalized", email="UPPER@example.test", password_hash=self.password_hash,
            ))
        db.session.rollback()

    def test_foreign_keys_and_message_membership(self):
        self.assert_insert_rejected(self.make_trip(999))
        traveler = self.add_user()
        outsider = self.add_user("outsider@example.test")
        trip = self.add_trip(traveler.id)
        self.assert_insert_rejected(self.make_request(trip.id, 999))
        conversation = Conversation(trip=trip, participants=[ConversationParticipant(user=traveler)])
        db.session.add(conversation)
        db.session.commit()
        self.assert_insert_rejected(Message(conversation=conversation, sender_id=outsider.id, content="Not authorized"))
        self.assert_insert_rejected(ConversationParticipant(conversation_id=conversation.id, user_id=traveler.id))

    def test_invalid_values_are_rejected(self):
        traveler = self.add_user()
        sender = self.add_user("sender@example.test")
        for overrides in [
            {"capacity_grams": 0}, {"capacity_grams": 1.5}, {"price_minor": -1},
            {"price_minor": 12.5}, {"price_minor": None}, {"pricing_method": "agreement"},
            {"status": "unknown"}, {"destination_country": "CA"},
        ]:
            with self.subTest(trip_values=overrides):
                self.assert_insert_rejected(self.make_trip(traveler.id, **overrides))
        trip = self.add_trip(traveler.id)
        for overrides in [
            {"weight_grams": -1}, {"weight_grams": 0}, {"weight_grams": 1.5},
            {"package_description": "  "}, {"package_category": "unknown"}, {"status": "unknown"},
        ]:
            with self.subTest(request_values=overrides):
                self.assert_insert_rejected(self.make_request(trip.id, sender.id, **overrides))
        request = self.add_request(trip.id, sender.id)
        for amount in [-1, 0, 12.5]:
            with self.subTest(offer_amount=amount):
                self.assert_insert_rejected(PricingAgreement(request=request, proposer=sender, amount_minor=amount))
        self.assert_insert_rejected(Conversation())
        self.assert_insert_rejected(Conversation(trip=trip, request=request))
        self.assert_insert_rejected(RequestStatusHistory(
            request=request, changed_by=sender, previous_status="requested", new_status="requested",
        ))

    def test_archiving_preserves_delivery_history(self):
        traveler = self.add_user()
        sender = self.add_user("sender@example.test")
        trip = self.add_trip(traveler.id)
        request = self.add_request(trip.id, sender.id)
        db.session.add(RequestStatusHistory(
            request=request, changed_by=traveler, previous_status="in_transit", new_status="delivered",
        ))
        request.status = "delivered"
        trip.status = "archived"
        traveler.deactivated_at = datetime.now(timezone.utc)
        sender.deactivated_at = datetime.now(timezone.utc)
        db.session.commit()

        for record in [trip, traveler, sender, request]:
            with self.subTest(model=type(record).__name__):
                db.session.delete(record)
                with self.assertRaises(IntegrityError):
                    db.session.commit()
                db.session.rollback()
        self.assertEqual(request.status, "delivered")
        self.assertEqual(request.trip.status, "archived")
        self.assertIsNotNone(request.requester.deactivated_at)
        self.assertIsNotNone(request.trip.traveler.deactivated_at)
        self.assertEqual(request.status_history[0].new_status, "delivered")

    def test_existing_flask_routes_serve_frontend(self):
        with self.app.test_client() as client:
            page = client.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(b"/static/css/etewa.css", page.data)
            self.assertIn(b"/static/js/etewa.js", page.data)
            for asset, mimetype, content in [
                ("css/etewa.css", "text/css", b"site-header"),
                ("js/etewa.js", "text/javascript", b"localStorage"),
            ]:
                with client.get("/static/" + asset) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.mimetype, mimetype)
                    self.assertIn(content, response.data)
            health = client.get("/api/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.get_json(), {"status": "ok"})
