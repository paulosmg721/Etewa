import re
from datetime import date

from flask import Blueprint, request, session
from sqlalchemy import func, text
from sqlalchemy.orm import contains_eager, joinedload
from werkzeug.exceptions import RequestEntityTooLarge

from auth import body_too_large, json_object, protect_auth_requests, signed_in_user
from models import DeliveryRequest, Trip, User, db, utc_now


trip_api = Blueprint("trip_api", __name__, url_prefix="/api")
COUNTRY_CODES = """
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE
BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ CA CC CD
CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM
DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR GA GB GD GE GF
GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU
ID IE IL IM IN IO IQ IR IS IT JE JM JO JP KE KG KH KI KM KN
KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME
MF MG MH MK ML MM MN MO MP MQ MR MS MT MU MV MW MX MY MZ NA
NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM
PN PR PS PT PW PY QA RE RO RS RU RW SA SB SC SD SE SG SH SI
SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK
TL TM TN TO TR TT TV TW TZ UA UG UM US UY UZ VA VC VE VG VI
VN VU WF WS XK YE YT ZA ZM ZW
""".split()
SUPPORTED_COUNTRIES = frozenset(COUNTRY_CODES)
PRICING_METHODS = ("fixed", "negotiable", "agreement")
FIELDS = (
    "departure_country", "destination_country", "travel_date", "capacity_grams",
    "pricing_method", "price_minor", "currency", "notes",
)
ACTIVE_REQUEST_STATES = ("requested", "accepted", "in_transit")
CAPACITY_STATES = ("accepted", "in_transit", "delivered")
MAX_TRIP_ID = 2_147_483_647


@trip_api.before_request
def protect_private_trip_routes():
    if request.method in {"POST", "PATCH", "DELETE"} or request.endpoint == "trip_api.my_trips":
        return protect_auth_requests()


trip_api.register_error_handler(RequestEntityTooLarge, body_too_large)


@trip_api.after_request
def disable_stale_responses(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def country_code(value):
    if not isinstance(value, str) or not value.isascii() or value.strip().upper() not in SUPPORTED_COUNTRIES:
        raise ValueError("Choose a supported country code.")
    return value.strip().upper()


def parse_date(value):
    if type(value) is date:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("Enter a valid travel date (YYYY-MM-DD).")


def valid_notes(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2000:
        raise ValueError("Notes must be text up to 2,000 characters.")
    if any(0xD800 <= ord(char) <= 0xDFFF or (ord(char) < 32 and char not in "\t\r\n") for char in value):
        raise ValueError("Notes contain an invalid character.")
    return value.strip() or None


def validated_trip(data, existing=None):
    if not data or data.keys() - set(FIELDS):
        raise ValueError("Submit a JSON object containing only trip fields.")
    values = {field: getattr(existing, field) for field in FIELDS} if existing else {
        "currency": "USD", "notes": None, "price_minor": None,
    }
    values.update(data)
    for field in ("departure_country", "destination_country"):
        values[field] = country_code(values.get(field))
    if values["departure_country"] == values["destination_country"]:
        raise ValueError("Departure and destination must be different countries.")
    travel_date = parse_date(values.get("travel_date"))
    if travel_date < utc_now().date():
        raise ValueError("Travel date must be today or later (UTC).")
    values["travel_date"] = travel_date
    capacity = values.get("capacity_grams")
    if type(capacity) is not int or not 1 <= capacity <= 2_147_483_647:
        raise ValueError("Enter a positive capacity in whole grams.")
    method = values.get("pricing_method")
    if method not in PRICING_METHODS:
        raise ValueError("Choose Fixed Price, Negotiable, or By Agreement.")
    price = values["price_minor"]
    if method == "agreement":
        # Changing to agreement removes a previous price unless the request supplies a conflicting price.
        if data.get("price_minor") is not None:
            raise ValueError("By Agreement trips do not have an initial price.")
        values["price_minor"] = None
    elif type(price) is not int or not 1 <= price <= 999_999_999_999:
        raise ValueError("Enter a positive USD price in whole cents.")
    currency = values["currency"]
    if not isinstance(currency, str) or not currency.isascii() or currency.strip().upper() != "USD":
        raise ValueError("Only USD is supported for trip pricing.")
    values["currency"] = "USD"
    values["notes"] = valid_notes(values["notes"])
    return values


def begin_capacity_transaction():
    # Reserve SQLite's write lock before reading capacity or active requests.
    if db.engine.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


def trip_for_write(trip_id):
    return db.session.scalar(db.select(Trip).where(Trip.id == trip_id).with_for_update())


def capacity_reserved(trip_ids):
    return dict(db.session.execute(db.select(
        DeliveryRequest.trip_id, func.sum(DeliveryRequest.weight_grams),
    ).where(
        DeliveryRequest.trip_id.in_(trip_ids), DeliveryRequest.status.in_(CAPACITY_STATES),
    ).group_by(DeliveryRequest.trip_id)).all())


def remaining_capacity(trip):
    return max(0, trip.capacity_grams - capacity_reserved([trip.id]).get(trip.id, 0))


def serialize_trip(trip, reserved=None):
    traveler = trip.traveler
    return {
        "id": trip.id,
        "departure_country": trip.departure_country,
        "destination_country": trip.destination_country,
        "travel_date": trip.travel_date.isoformat(),
        "capacity_grams": trip.capacity_grams,
        "available_capacity_grams": remaining_capacity(trip) if reserved is None else max(0, trip.capacity_grams - reserved),
        "pricing_method": trip.pricing_method,
        "price_minor": trip.price_minor,
        "currency": trip.currency,
        "notes": trip.notes,
        "status": trip.status,
        "created_at": trip.created_at.isoformat(),
        "updated_at": trip.updated_at.isoformat(),
        "traveler": {"id": f"account:{traveler.id}", "full_name": traveler.full_name, "city": traveler.city},
    }


def public_trips():
    return db.select(Trip).join(Trip.traveler).options(contains_eager(Trip.traveler)).where(
        Trip.status == "published",
        Trip.travel_date >= utc_now().date(),
        User.deactivated_at.is_(None),
    )


def paginate(statement):
    page = request.args.get("page", "1")
    size = request.args.get("per_page", "20")
    if not re.fullmatch(r"[1-9][0-9]{0,5}", page) or not re.fullmatch(r"[1-9][0-9]?", size) or int(size) > 50:
        return {"error": "Enter a valid page and a page size from 1 to 50."}, 400
    result = db.paginate(statement, page=int(page), per_page=int(size), error_out=False)
    reserved = capacity_reserved([trip.id for trip in result.items]) if result.items else {}
    return {"trips": [serialize_trip(trip, reserved.get(trip.id, 0)) for trip in result.items],
            "page": result.page, "per_page": result.per_page, "total": result.total, "has_next": result.has_next}


@trip_api.get("/trips")
def list_trips():
    statement = public_trips()
    if request.args.get("sort", "soonest") not in ("soonest", "rating"):
        return {"error": "Choose a supported sort order."}, 400
    try:
        for field in ("departure_country", "destination_country"):
            if field in request.args:
                statement = statement.where(getattr(Trip, field) == country_code(request.args[field]))
        if "travel_date" in request.args:
            statement = statement.where(Trip.travel_date >= parse_date(request.args["travel_date"]))
    except ValueError as error:
        return {"error": str(error)}, 400
    # No rating system exists yet. The rating option uses the same stable departure order.
    return paginate(statement.order_by(Trip.travel_date, Trip.id))


@trip_api.get("/trips/<int:trip_id>")
def get_trip(trip_id):
    if not 1 <= trip_id <= MAX_TRIP_ID:
        return {"error": "Trip not found."}, 404
    trip = db.session.scalar(public_trips().where(Trip.id == trip_id))
    if trip is None:
        return {"error": "Trip not found."}, 404
    return {"trip": serialize_trip(trip)}


@trip_api.get("/my/trips")
def my_trips():
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to view your trips."}, 401
    return paginate(db.select(Trip).options(joinedload(Trip.traveler)).where(
        Trip.traveler_id == user.id,
    ).order_by(Trip.created_at.desc(), Trip.id.desc()))


@trip_api.post("/trips")
def create_trip():
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to publish a trip."}, 401
    try:
        values = validated_trip(json_object())
    except ValueError as error:
        return {"error": str(error)}, 400
    trip = Trip(traveler=user, **values)
    db.session.add(trip)
    db.session.commit()
    return {"trip": serialize_trip(trip)}, 201


@trip_api.route("/trips/<int:trip_id>", methods=["PATCH", "DELETE"])
def change_trip(trip_id):
    if session.get("user_id") is None:
        return {"error": "Sign in to manage your trips."}, 401
    begin_capacity_transaction()
    user = signed_in_user()
    if user is None:
        return {"error": "Sign in to manage your trips."}, 401
    if not 1 <= trip_id <= MAX_TRIP_ID:
        return {"error": "Trip not found."}, 404
    trip = trip_for_write(trip_id)
    if trip is None:
        return {"error": "Trip not found."}, 404
    if trip.traveler_id != user.id:
        return {"error": "Only the traveler who published this trip can change it."}, 403

    if request.method == "DELETE":
        if trip.status == "archived":
            return {"trip": serialize_trip(trip)}
        changes = {"status": "archived"}
    else:
        if trip.status != "published":
            return {"error": "Only published trips can be edited."}, 409
        try:
            values = validated_trip(json_object(), existing=trip)
        except ValueError as error:
            return {"error": str(error)}, 400
        changes = {key: value for key, value in values.items() if value != getattr(trip, key)}

    if changes.keys() - {"notes"}:
        active = db.session.scalar(db.select(DeliveryRequest.id).where(
            DeliveryRequest.trip_id == trip.id, DeliveryRequest.status.in_(ACTIVE_REQUEST_STATES),
        ).limit(1))
        if active is not None:
            return {"error": "Resolve active delivery requests before changing or archiving this trip."}, 409
        if "capacity_grams" in changes and changes["capacity_grams"] < capacity_reserved([trip.id]).get(trip.id, 0):
            return {"error": "Capacity cannot be lower than the weight already committed."}, 409
    if changes:
        result = db.session.execute(db.update(Trip).where(
            Trip.id == trip.id, Trip.status == trip.status, Trip.updated_at == trip.updated_at,
        ).values(**changes))
        if result.rowcount != 1:
            db.session.rollback()
            return {"error": "This trip changed. Refresh and try again."}, 409
        db.session.commit()
    return {"trip": serialize_trip(trip)}
