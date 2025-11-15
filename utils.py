# utils.py

import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

# Adjust this import if you're using a package structure, e.g.:
# from app.models import User, Rides
from models import User, Rides
import json
import google.generativeai as genai
import os
from twilio.rest import Client
from dotenv import load_dotenv

# utils.py

load_dotenv()

EMORY_NAME = "Emory University"
AIRPORT_NAME = "Hartsfield-Jackson Atlanta International Airport"

ALLOWED_LOCATIONS = {EMORY_NAME, AIRPORT_NAME}

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")  # e.g. "whatsapp:+1415xxxxxxx"

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


def send_whatsapp_message(to_number: str, body: str) -> None:
    """
    Send a WhatsApp message via Twilio to the given number.
    Expects numbers like 'whatsapp:+14045551234'.
    """
    if not twilio_client or not TWILIO_WHATSAPP_NUMBER:
        print("[TWILIO] Missing config; would have sent to", to_number, ":", body)
        return

    try:
        twilio_client.messages.create(
            from_=TWILIO_WHATSAPP_NUMBER,
            to=to_number,
            body=body,
        )
    except Exception as e:
        print(f"[TWILIO] Error sending WhatsApp message to {to_number}: {e}")

def build_sms_deeplink(phone_number: str) -> str:
    """
    Convert something like 'whatsapp:+14045551234' or '+14045551234'
    into an sms: deep link that opens Messages/iMessage.
    """
    num = phone_number
    if num.startswith("whatsapp:"):
        num = num[len("whatsapp:"):]
    return f"sms:{num}"


def _get_gemini_model():
    """
    Lazily configure and return a Gemini model instance.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        # In dev, fail gracefully if key is missing
        print("[GEMINI] GEMINI_API_KEY is not set; falling back to None.")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.5-flash")
    return model

def _extract_json_from_text(text: str) -> Optional[dict]:
    """
    Gemini may wrap JSON in code fences or extra text.
    This helper extracts the first {...} block and parses it.
    """
    text = text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        # e.g. ```json\n{...}\n```
        text = text.strip("`")
        # After stripping backticks, try to find first '{'
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    json_str = text[start : end + 1]
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        return None

def _normalize_location(raw: Optional[str]) -> Optional[str]:
    """
    Map Gemini's location output or synonyms into one of the canonical names,
    or return None if we can't confidently map it.
    """
    if not raw:
        return None

    t = raw.strip().lower()

    # Direct canonical matches
    if "emory" in t:
        return EMORY_NAME

    if any(kw in t for kw in ["airport", "hartsfield", "jackson", "atl"]):
        return AIRPORT_NAME

    # If Gemini already returned exact canonical name
    if raw in ALLOWED_LOCATIONS:
        return raw

    return None


def parse_ride_with_gemini(message: str) -> Optional[dict]:
    """
    Use Gemini to parse a free-form ride request message into:
        - departure_time: datetime (Python datetime object)
        - from_location: canonical name
        - to_location: canonical name

    Returns:
        dict with keys {"departure_time", "from_location", "to_location"}
        or None on failure.
    """
    model = _get_gemini_model()
    if model is None:
        return None

    # You can make this dynamic if you want, but for now we'll just use current year info.
    now = datetime.now()
    current_date_str = now.strftime("%Y-%m-%d")
    current_year = now.year

    system_prompt = f"""
You are a strict JSON parser for a ride-sharing service between two locations:

  1. "Emory University"
  2. "Hartsfield-Jackson Atlanta International Airport"

The user will send a short text message describing a ride, for example:
- "955 AM 11/19 atl airport to emory"
- "leaving 3 pm on 11/16 from emory to airport"
- "airport to emory tomorrow 9pm"
- "11/23 8pm emory → atl airport"

You MUST extract:
  - a single departure datetime (assume the timezone is America/New_York)
  - a FROM location
  - a TO location

Rules:
  - If the message says "emory", "emory univ", or similar, map it to "Emory University".
  - If the message says "airport", "atl airport", "hartsfield", or "jackson",
    map it to "Hartsfield-Jackson Atlanta International Airport".
  - If both sides are mentioned, the first mentioned is the FROM location,
    and the second is the TO location.
  - If only one location is mentioned:
      - "from emory" or "leaving emory" => FROM: Emory University, TO: Airport
      - "to emory" or "going to emory"  => FROM: Airport, TO: Emory University
  - If the year is omitted in the date, assume the current year: {current_year}.
  - If the message uses relative words like "today" or "tomorrow", interpret them
    relative to today's date: {current_date_str}.
  - The only valid output locations are:
      - "Emory University"
      - "Hartsfield-Jackson Atlanta International Airport"

Output format:
  - Output ONLY a single JSON object with EXACTLY these fields:
    {{
      "success": true or false,
      "reason": "<short reason if success is false>",
      "departure_time": "YYYY-MM-DDTHH:MM:SS" or null,
      "from_location": "<one of the allowed locations or null>",
      "to_location": "<one of the allowed locations or null>"
    }}

Do NOT include any additional keys.
Do NOT include any explanation outside the JSON.
"""

    user_prompt = f"User message: {message!r}"

    try:
        response = model.generate_content([system_prompt, user_prompt])
        raw_text = response.text or ""
        print("[GEMINI RAW RESPONSE]", raw_text)  # 👈 add this HERE
    except Exception as e:
        print(f"[GEMINI] Error during generate_content: {e}")
        return None

    data = _extract_json_from_text(raw_text)
    if not data:
        print("[GEMINI] Failed to extract JSON from response.")
        return None

    if not data.get("success"):
        print(f"[GEMINI] Model reported failure: {data.get('reason')}")
        return None

    # Normalize and validate locations
    raw_from = data.get("from_location")
    raw_to = data.get("to_location")

    from_location = _normalize_location(raw_from)
    to_location = _normalize_location(raw_to)

    if from_location not in ALLOWED_LOCATIONS or to_location not in ALLOWED_LOCATIONS:
        print(f"[GEMINI] Invalid locations: from={raw_from}, to={raw_to}")
        return None

    # Parse datetime
    dt_str = data.get("departure_time")
    if not dt_str:
        print("[GEMINI] Missing departure_time in JSON.")
        return None

    try:
        departure_dt = datetime.fromisoformat(dt_str)
    except Exception as e:
        print(f"[GEMINI] Failed to parse datetime '{dt_str}': {e}")
        return None

    return {
        "departure_time": departure_dt,
        "from_location": from_location,
        "to_location": to_location,
    }

def format_departure_time(dt: datetime) -> str:
    return dt.strftime("%m/%d %I:%M %p")

def get_active_ride_for_user(db: Session, user_id: int) -> Optional[Rides]:
    return (
        db.query(Rides)
        .filter(
            Rides.user_id == user_id,
            Rides.status.in_(["pending", "matched"]),
        )
        .order_by(Rides.created_at.desc())
        .first()
    )


def find_matching_ride(db: Session, new_ride: Rides) -> Optional[Rides]:
    """
    Given a newly created ride, find another 'pending' ride from a different user
    whose departure_time is within a ±30 minute window and has the same route.
    """
    window = timedelta(minutes=30)
    start = new_ride.departure_time - window
    end = new_ride.departure_time + window

    return (
        db.query(Rides)
        .filter(
            Rides.id != new_ride.id,
            Rides.user_id != new_ride.user_id,
            Rides.status == "pending",
            Rides.departure_time >= start,
            Rides.departure_time <= end,
            Rides.from_location == new_ride.from_location,
            Rides.to_location == new_ride.to_location,
        )
        .order_by(Rides.created_at)
        .first()
    )

def create_ride_and_try_match(db: Session, user: User, body: str) -> str:
    """
    Core ride creation logic using Gemini for parsing.

    - Enforces one active ride (pending/matched) per user.
    - Uses Gemini to parse datetime + from/to locations.
    - Creates a new ride.
    - Attempts to match with another pending ride within ±30 minutes,
      with the same route (from/to).
    - Sends intro DMs to both riders when a match is found.
    - Returns a string message to send back via Twilio.
    """
    # 0) First, mark any old rides in the past as completed
    complete_past_rides_for_user(db, user.id)


    # 1) Check if user already has an active ride
    active_ride = get_active_ride_for_user(db, user.id)
    if active_ride:
        return (
            "You already have a ride on file.\n\n"
            f"Departure: {format_departure_time(active_ride.departure_time)} "
            f"{active_ride.from_location} → {active_ride.to_location}.\n\n"
            "You can cancel your ride at any time by replying 'cancel', "
            "then send a new request."
        )

    # 2) Parse with Gemini
    parsed = parse_ride_with_gemini(body)
    print("[DEBUG] Gemini returned →", parsed)

    if not parsed:
        return (
            "I couldn't understand your date/time or route 😅.\n\n"
            "Try something like: '955 AM 11/19 atl airport to emory' or "
            "'leaving at 3 PM on 11/16 from Emory to airport'."
        )
    
    # TEMP: echo back what we parsed
    ride_dt = parsed["departure_time"]
    from_location = parsed["from_location"]
    to_location = parsed["to_location"]

    # 3) Create new ride
    new_ride = Rides(
        user_id=user.id,
        original_message=body,
        from_location=from_location,
        to_location=to_location,
        departure_time=ride_dt,
        party_size=1,
        status="pending",
        matched_with_ride_id=None,
    )
    db.add(new_ride)
    db.commit()
    db.refresh(new_ride)

    # 4) Try to find a matching pending ride
    other = find_matching_ride(db, new_ride)

    if other:
        # Mark both as matched and cross-link
        new_ride.status = "matched"
        new_ride.matched_with_ride_id = other.id

        other.status = "matched"
        other.matched_with_ride_id = new_ride.id

        db.commit()

        # --- Intro DMs for both riders ---
        try:
            db.refresh(new_ride)
            db.refresh(other)

            user1 = new_ride.user
            user2 = other.user

            name1 = user1.full_name or "another student"
            name2 = user2.full_name or "another student"

            phone1 = user1.phone_number  # "whatsapp:+1..."
            phone2 = user2.phone_number

            sms_link_for_2 = build_sms_deeplink(phone2)
            sms_link_for_1 = build_sms_deeplink(phone1)

            trip_str = f"{format_departure_time(ride_dt)} {from_location} → {to_location}"

            # Message to rider 1 about rider 2
            body_for_1 = (
                "Good news! 🎉 You've been matched with another student for your ride.\n\n"
                f"Match: {name2}\n"
                f"Phone: {phone2}\n"
                f"Trip: {trip_str}\n\n"
                "You can start a WhatsApp or iMessage group with them to coordinate.\n"
                f"Tap-to-text (SMS/iMessage): {sms_link_for_2}"
            )
            send_whatsapp_message(phone1, body_for_1)

            # Message to rider 2 about rider 1
            body_for_2 = (
                "Good news! 🎉 You've been matched with another student for your ride.\n\n"
                f"Match: {name1}\n"
                f"Phone: {phone1}\n"
                f"Trip: {trip_str}\n\n"
                "You can start a WhatsApp or iMessage group with them to coordinate.\n"
                f"Tap-to-text (SMS/iMessage): {sms_link_for_1}"
            )
            send_whatsapp_message(phone2, body_for_2)

        except Exception as e:
            print("[MATCH DM] Failed to send intro messages:", e)

        return (
            "Good news! 🎉 We found another student with a similar ride.\n\n"
            f"Your ride: {format_departure_time(ride_dt)} "
            f"{from_location} → {to_location}.\n"
            "We just sent you both a message with each other's contact info so you can coordinate."
        )
    else:
        return (
            "Got it ✅ Your ride request is saved.\n\n"
            f"Departure: {format_departure_time(ride_dt)} "
            f"{from_location} → {to_location}.\n"
            "We'll match you with another student as soon as someone compatible joins."
        )

def cancel_active_ride(db: Session, user: User) -> str:
    """
    If the user has an active ride (pending or matched), mark it as cancelled.
    If the ride was matched with someone else, put the other rider back to 'pending'
    and clear their matched_with_ride_id, and notify them that we're rematching them.
    """
    # First, auto-complete any past rides so we only cancel future ones
    complete_past_rides_for_user(db, user.id)

    active_ride = get_active_ride_for_user(db, user.id)
    if not active_ride:
        return "You don't have any active ride to cancel."

    other = None
    if active_ride.status == "matched" and active_ride.matched_with_ride_id:
        other = (
            db.query(Rides)
            .filter(Rides.id == active_ride.matched_with_ride_id)
            .one_or_none()
        )
        if other:
            if other.status == "matched":
                other.status = "pending"
            if other.matched_with_ride_id == active_ride.id:
                other.matched_with_ride_id = None

    # Cancel this user's ride
    active_ride.status = "cancelled"
    active_ride.matched_with_ride_id = None

    db.commit()

    # Notify the other rider (if any) that we are rematching them
    if other:
        try:
            db.refresh(other)
            other_user = other.user
            other_phone = other_user.phone_number
            sms_link = build_sms_deeplink(other_phone)

            msg = (
                "Heads up: your previous match had to cancel their ride, "
                "so we're rematching you with another rider.\n\n"
                f"Your ride is still active for "
                f"{format_departure_time(other.departure_time)} "
                f"{other.from_location} → {other.to_location}.\n\n"
                f"You'll be notified again once a new match is found."
            )
            send_whatsapp_message(other_phone, msg)
        except Exception as e:
            print("[TWILIO] Failed to notify other rider about cancel:", e)

    return (
        "Your ride has been cancelled ✅.\n\n"
        f"Original departure: {format_departure_time(active_ride.departure_time)} "
        f"{active_ride.from_location} → {active_ride.to_location}."
    )


def complete_past_rides_for_user(db: Session, user_id: int) -> None:
    """
    For this user, mark any pending/matched rides in the past as 'completed'.
    This keeps get_active_ride_for_user from treating old rides as active.
    """
    now = datetime.utcnow()

    past_rides = (
        db.query(Rides)
        .filter(
            Rides.user_id == user_id,
            Rides.status.in_(["pending", "matched"]),
            Rides.departure_time <= now,
        )
        .all()
    )

    if not past_rides:
        return

    for ride in past_rides:
        ride.status = "completed"

    db.commit()