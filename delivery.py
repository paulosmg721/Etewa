from flask import Blueprint, request, session
from sqlalchemy import or_
from sqlalchemy.orm import joinedload, selectinload
from werkzeug.exceptions import RequestEntityTooLarge

from auth import body_too_large, json_object, protect_auth_requests, signed_in_user
from models import DeliveryRequest, RequestStatusHistory, Trip, db, utc_now
from trips import (
    ACTIVE_REQUEST_STATES, MAX_TRIP_ID, begin_capacity_transaction, capacity_reserved, remaining_capacity, trip_for_write,
)


delivery_api = Blueprint("delivery_api", __name__, url_prefix="/api")
PACKAGE_CATEGORIES = ("documents", "electronics", "clothing", "small", "fragile", "other")


delivery_api.before_request(protect_auth_requests)
delivery_api.register_error_handler(RequestEntityTooLarge, body_too_large)


@delivery_api.after_request
def private_response(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def package_text(value, label, limit):
    if not isinstance(value, str) or len(value) > limit or not value.strip():
        raise ValueError(f"Enter {label} up to {limit:,} characters.")
    if any(0xD800 <= ord(char) <= 0xDFFF or (ord(char) < 32 and char not in "\t\r\n") for char in value):
        raise ValueError(f"The {label} contains an invalid character.")
    return value.strip()


def validate_package(data):
    if not data or data.keys() - {"package_category", "category_details", "package_description", "weight_grams"}:
        raise ValueError("Submit only package category, category details, description, and weight.")
    category = data.get("package_category")
    if category not in PACKAGE_CATEGORIES:
        raise ValueError("Choose a package category.")
    description = package_text(data.get("package_description"), "a package description", 2000)
    details = data.get("category_details")
    if category == "other":
        details = package_text(details, "a category explanation", 250)
    elif details not in (None, ""):
        raise ValueError("Category details are only used for the Other category.")
    weight = data.get("weight_grams")
    if type(weight) is not int or not 1 <= weight <= 2_147_483_647:
        raise ValueError("Enter a positive package weight in whole grams.")
    return {"package_category": category, "package_description": description,
            "category_details": details or None, "weight_grams": weight}


def public_account(user):
    return {"id": f"account:{user.id}", "full_name": user.full_name, "city": user.city}


def displayed_status(status, event=None, traveler_id=None):
    if status == "requested":
        return "pending"
    # Existing tables store both terminal outcomes as cancelled. The actor in
    # the status history distinguishes a traveler declining from a sender cancelling.
    if (status == "cancelled" and event and event.previous_status == "requested"
            and event.changed_by_id == traveler_id):
        return "declined"
    return status


def serialize_request(item, user_id, reserved=None):
    trip = item.trip
    history = item.status_history
    latest = history[-1] if history and history[-1].new_status == item.status else None
    status = displayed_status(item.status, latest, trip.traveler_id)
    incoming = user_id == trip.traveler_id
    accept_notice = None
    if incoming and status == "pending":
        available = remaining_capacity(trip) if reserved is None else trip.capacity_grams - reserved
        if trip.status != "published" or trip.travel_date < utc_now().date() or item.requester.deactivated_at:
            accept_notice = "This request is no longer eligible for acceptance."
        elif item.weight_grams > available:
            accept_notice = "There is not enough remaining capacity to accept this request."
        actions = ["decline"] if accept_notice else ["accept", "decline"]
    elif not incoming and status in ("pending", "accepted"):
        actions = ["cancel"]
    else:
        actions = []
    return {
        "id": item.id, "status": status, "allowed_actions": actions, "accept_notice": accept_notice,
        "package_category": item.package_category, "category_details": item.category_details,
        "package_description": item.package_description, "weight_grams": item.weight_grams,
        "created_at": item.created_at.isoformat(), "updated_at": item.updated_at.isoformat(),
        "requester": public_account(item.requester),
        "trip": {
            "id": trip.id, "departure_country": trip.departure_country, "destination_country": trip.destination_country,
            "travel_date": trip.travel_date.isoformat(), "status": trip.status, "traveler": public_account(trip.traveler),
            "pricing_method": trip.pricing_method, "price_minor": trip.price_minor, "currency": trip.currency,
        },
        "history": [{
            "previous_status": displayed_status(event.previous_status),
            "new_status": displayed_status(event.new_status, event, trip.traveler_id),
            "actor": "traveler" if event.changed_by_id == trip.traveler_id else "requester",
            "changed_at": event.changed_at.isoformat(),
        } for event in history],
    }


def account_requests(user_id):
    return db.select(DeliveryRequest).join(DeliveryRequest.trip).where(or_(
        DeliveryRequest.requester_id == user_id, Trip.traveler_id == user_id,
    )).options(
        joinedload(DeliveryRequest.requester),
        joinedload(DeliveryRequest.trip).joinedload(Trip.traveler),
        selectinload(DeliveryRequest.status_history),
    )


@delivery_api.get("/my/requests")
def list_requests():
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to view delivery requests."}, 401
    items = db.session.scalars(account_requests(user.id).order_by(
        DeliveryRequest.created_at.desc(), DeliveryRequest.id.desc(),
    )).all()
    pending_trips = {item.trip_id for item in items if item.status == "requested" and item.trip.traveler_id == user.id}
    reserved = capacity_reserved(pending_trips) if pending_trips else {}
    lists = {"incoming": [], "outgoing": []}
    for item in items:
        direction = "incoming" if item.trip.traveler_id == user.id else "outgoing"
        lists[direction].append(serialize_request(item, user.id, reserved.get(item.trip_id, 0)))
    return lists


@delivery_api.get("/requests/<int:request_id>")
def get_request(request_id):
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to view delivery requests."}, 401
    item = db.session.scalar(account_requests(user.id).where(DeliveryRequest.id == request_id)) if 1 <= request_id <= MAX_TRIP_ID else None
    if item is None:
        return {"error": "Delivery request not found."}, 404
    return {"request": serialize_request(item, user.id)}


def record_status(item, new_status, user_id):
    db.session.add(RequestStatusHistory(
        request=item, previous_status=item.status, new_status=new_status, changed_by_id=user_id,
    ))
    item.status = new_status


@delivery_api.post("/trips/<int:trip_id>/requests")
def create_request(trip_id):
    if session.get("user_id") is None:
        return {"error": "Sign in to request a delivery."}, 401
    try:
        fields = validate_package(json_object())
    except ValueError as error:
        return {"error": str(error)}, 400
    begin_capacity_transaction()
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to request a delivery."}, 401
    trip = trip_for_write(trip_id) if 1 <= trip_id <= MAX_TRIP_ID else None
    if trip is None or trip.status != "published" or trip.travel_date < utc_now().date() or trip.traveler.deactivated_at:
        return {"error": "This trip is no longer available for requests."}, 404
    if trip.traveler_id == user.id:
        return {"error": "You cannot request delivery on your own trip."}, 403
    existing = db.session.scalar(db.select(DeliveryRequest.id).where(
        DeliveryRequest.trip_id == trip.id, DeliveryRequest.requester_id == user.id,
        DeliveryRequest.status.in_(ACTIVE_REQUEST_STATES),
    ).limit(1))
    if existing is not None:
        return {"error": "You already have an active request for this trip."}, 409
    if fields["weight_grams"] > remaining_capacity(trip):
        return {"error": "That package exceeds the trip’s available capacity."}, 409
    item = DeliveryRequest(trip=trip, requester=user, **fields)
    db.session.add(item)
    record_status(item, "requested", user.id)
    db.session.commit()
    return {"request": serialize_request(item, user.id)}, 201


@delivery_api.post("/requests/<int:request_id>/<any(accept,decline,cancel):action>")
def change_request(request_id, action):
    if session.get("user_id") is None:
        return {"error": "Sign in to manage delivery requests."}, 401
    begin_capacity_transaction()
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to manage delivery requests."}, 401
    item = db.session.get(DeliveryRequest, request_id) if 1 <= request_id <= MAX_TRIP_ID else None
    if item is None:
        return {"error": "Delivery request not found."}, 404
    trip = trip_for_write(item.trip_id)
    db.session.refresh(item)
    if user.id not in (trip.traveler_id, item.requester_id):
        return {"error": "Delivery request not found."}, 404
    allowed_actor = item.requester_id if action == "cancel" else trip.traveler_id
    if user.id != allowed_actor:
        return {"error": "You cannot take this action on this request."}, 403
    allowed_statuses = ("requested", "accepted") if action == "cancel" else ("requested",)
    if item.status not in allowed_statuses:
        return {"error": "This request no longer allows that action."}, 409
    if action == "accept":
        if trip.status != "published" or trip.travel_date < utc_now().date() or item.requester.deactivated_at:
            return {"error": "This request is no longer eligible for acceptance."}, 409
        if item.weight_grams > remaining_capacity(trip):
            return {"error": "There is not enough remaining capacity to accept this request."}, 409
    record_status(item, "accepted" if action == "accept" else "cancelled", user.id)
    db.session.commit()
    return {"request": serialize_request(item, user.id)}
