from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.orm import validates
from sqlalchemy.types import DateTime, TypeDecorator


db = SQLAlchemy()

REQUEST_STATUSES = "'requested', 'accepted', 'in_transit', 'delivered', 'cancelled'"


def utc_now():
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Keep timestamps in UTC, including when SQLite returns a naive datetime."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("Timestamps must include a timezone.")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class User(db.Model):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint("email = lower(trim(email)) AND length(email) > 0", name="ck_users_email_normalized"),
        CheckConstraint("length(trim(full_name)) > 0", name="ck_users_name_present"),
        CheckConstraint("length(trim(password_hash)) > 0", name="ck_users_password_hash_present"),
    )

    id = db.Column(db.Integer, primary_key=True)
    full_name = db.Column(db.String(160), nullable=False)
    email = db.Column(db.String(254), nullable=False)
    password_hash = db.Column(db.String(512), nullable=False)
    city = db.Column(db.String(120))
    bio = db.Column(db.Text)
    phone = db.Column(db.String(40))
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)
    deactivated_at = db.Column(UTCDateTime())

    @validates("email")
    def normalize_email(self, _key, email):
        return email.strip().lower()


class Trip(db.Model):
    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint(
            "length(departure_country) = 2 AND departure_country = upper(departure_country) "
            "AND length(destination_country) = 2 AND destination_country = upper(destination_country) "
            "AND departure_country <> destination_country",
            name="ck_trips_country_codes",
        ),
        CheckConstraint("capacity_grams > 0", name="ck_trips_positive_capacity"),
        CheckConstraint("typeof(capacity_grams) = 'integer'", name="ck_trips_integer_capacity").ddl_if(dialect="sqlite"),
        CheckConstraint(
            "(pricing_method = 'agreement' AND price_minor IS NULL) OR "
            "(pricing_method IN ('fixed', 'negotiable') AND price_minor IS NOT NULL AND price_minor > 0)",
            name="ck_trips_pricing",
        ),
        CheckConstraint("price_minor IS NULL OR typeof(price_minor) = 'integer'", name="ck_trips_integer_price")
        .ddl_if(dialect="sqlite"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_trips_currency_code"),
        CheckConstraint("status IN ('published', 'cancelled', 'completed', 'archived')", name="ck_trips_status"),
        Index("ix_trips_route_date", "departure_country", "destination_country", "travel_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    traveler_id = db.Column(db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    departure_country = db.Column(db.String(2), nullable=False)
    destination_country = db.Column(db.String(2), nullable=False)
    travel_date = db.Column(db.Date, nullable=False)
    capacity_grams = db.Column(db.Integer, nullable=False)
    pricing_method = db.Column(db.String(16), nullable=False)
    price_minor = db.Column(db.BigInteger)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    notes = db.Column(db.Text)
    status = db.Column(db.String(16), nullable=False, default="published")
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now)

    traveler = db.relationship("User")
    requests = db.relationship("DeliveryRequest", back_populates="trip", passive_deletes="all")


class DeliveryRequest(db.Model):
    __tablename__ = "delivery_requests"
    __table_args__ = (
        CheckConstraint(
            "package_category IN ('documents', 'electronics', 'clothing', 'small', 'fragile', 'other')",
            name="ck_requests_category",
        ),
        CheckConstraint("length(trim(package_description)) > 0", name="ck_requests_description_present"),
        CheckConstraint("weight_grams > 0", name="ck_requests_positive_weight"),
        CheckConstraint("typeof(weight_grams) = 'integer'", name="ck_requests_integer_weight").ddl_if(dialect="sqlite"),
        CheckConstraint(f"status IN ({REQUEST_STATUSES})", name="ck_requests_status"),
        Index("ix_requests_trip_status", "trip_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.ForeignKey("trips.id", ondelete="RESTRICT"), nullable=False)
    requester_id = db.Column(db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    package_category = db.Column(db.String(20), nullable=False)
    category_details = db.Column(db.Text)
    package_description = db.Column(db.Text, nullable=False)
    weight_grams = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(16), nullable=False, default="requested")
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at = db.Column(UTCDateTime(), nullable=False, default=utc_now, onupdate=utc_now)

    trip = db.relationship("Trip", back_populates="requests")
    requester = db.relationship("User")
    offers = db.relationship("PricingAgreement", back_populates="request", passive_deletes="all")
    status_history = db.relationship(
        "RequestStatusHistory", back_populates="request", order_by="RequestStatusHistory.id", passive_deletes="all"
    )


class Conversation(db.Model):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "(trip_id IS NOT NULL AND request_id IS NULL) OR (trip_id IS NULL AND request_id IS NOT NULL)",
            name="ck_conversations_context",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    trip_id = db.Column(db.ForeignKey("trips.id", ondelete="RESTRICT"), index=True)
    request_id = db.Column(db.ForeignKey("delivery_requests.id", ondelete="RESTRICT"), index=True)
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)

    trip = db.relationship("Trip")
    request = db.relationship("DeliveryRequest")
    participants = db.relationship("ConversationParticipant", back_populates="conversation", passive_deletes="all")
    messages = db.relationship("Message", back_populates="conversation", passive_deletes="all")


class ConversationParticipant(db.Model):
    __tablename__ = "conversation_participants"

    conversation_id = db.Column(db.ForeignKey("conversations.id", ondelete="RESTRICT"), primary_key=True)
    user_id = db.Column(db.ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True, index=True)
    last_read_at = db.Column(UTCDateTime())

    conversation = db.relationship("Conversation", back_populates="participants")
    user = db.relationship("User")


class Message(db.Model):
    __tablename__ = "messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["conversation_id", "sender_id"],
            ["conversation_participants.conversation_id", "conversation_participants.user_id"],
            name="fk_messages_sender_membership",
            ondelete="RESTRICT",
        ),
        CheckConstraint("length(trim(content)) > 0", name="ck_messages_content_present"),
        Index("ix_messages_conversation_sent", "conversation_id", "sent_at", "id"),
        Index("ix_messages_conversation_sender", "conversation_id", "sender_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.ForeignKey("conversations.id", ondelete="RESTRICT"), nullable=False)
    sender_id = db.Column(db.Integer, nullable=False)
    content = db.Column(db.Text, nullable=False)
    sent_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)

    conversation = db.relationship("Conversation", back_populates="messages")
    sender_membership = db.relationship("ConversationParticipant", viewonly=True)


class PricingAgreement(db.Model):
    __tablename__ = "pricing_agreements"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_offers_positive_amount"),
        CheckConstraint("typeof(amount_minor) = 'integer'", name="ck_offers_integer_amount").ddl_if(dialect="sqlite"),
        CheckConstraint("length(currency) = 3 AND currency = upper(currency)", name="ck_offers_currency_code"),
        CheckConstraint("status IN ('proposed', 'accepted', 'declined', 'withdrawn')", name="ck_offers_status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.ForeignKey("delivery_requests.id", ondelete="RESTRICT"), nullable=False, index=True)
    proposer_id = db.Column(db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount_minor = db.Column(db.BigInteger, nullable=False)
    currency = db.Column(db.String(3), nullable=False, default="USD")
    status = db.Column(db.String(16), nullable=False, default="proposed")
    created_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)

    request = db.relationship("DeliveryRequest", back_populates="offers")
    proposer = db.relationship("User")


class RequestStatusHistory(db.Model):
    __tablename__ = "request_status_history"
    __table_args__ = (
        CheckConstraint(f"previous_status IN ({REQUEST_STATUSES})", name="ck_history_previous_status"),
        CheckConstraint(f"new_status IN ({REQUEST_STATUSES})", name="ck_history_new_status"),
        CheckConstraint(
            "(previous_status IS NULL AND new_status = 'requested') OR "
            "(previous_status IS NOT NULL AND previous_status <> new_status)",
            name="ck_history_status_change",
        ),
        Index("ix_history_request_order", "request_id", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    request_id = db.Column(db.ForeignKey("delivery_requests.id", ondelete="RESTRICT"), nullable=False)
    previous_status = db.Column(db.String(16))
    new_status = db.Column(db.String(16), nullable=False)
    changed_by_id = db.Column(db.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True)
    changed_at = db.Column(UTCDateTime(), nullable=False, default=utc_now)

    request = db.relationship("DeliveryRequest", back_populates="status_history")
    changed_by = db.relationship("User")
