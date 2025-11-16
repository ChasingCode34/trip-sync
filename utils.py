# utils.py

import re
from datetime import datetime, timedelta
from typing import Optional
import requests
from elevenlabs.client import ElevenLabs
from sqlalchemy.orm import Session
from models import User, Rides
import json
import google.generativeai as genai
import os
from twilio.rest import Client
from dotenv import load_dotenv
from io import BytesIO

load_dotenv()

EMORY_NAME = "Emory University"
AIRPORT_NAME = "Hartsfield-Jackson Atlanta International Airport"

ALLOWED_LOCATIONS = {EMORY_NAME, AIRPORT_NAME}

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")  # e.g. "whatsapp:+1415xxxxxxx"
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")


twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

elevenlabs_client = None
if ELEVENLABS_API_KEY:
    elevenlabs_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)

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


    def transcribe_audio_with_elevenlabs(audio_url: str) -> Optional[str]:
        if not elevenlabs_client:
            print("[ELEVENLABS] API client not configured. Skipping transcription.")
            return None

        try:
            print(f"[ELEVENLABS] Downloading audio from: {audio_url}")
            auth = (TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            resp = requests.get(audio_url, auth=auth)
            resp.raise_for_status()
            audio_data = resp.content

            audio_file = BytesIO(audio_data)

            print("[ELEVENLABS] Transcribing audio with Scribe...")
            # **Use model_id, not model**
            result = elevenlabs_client.speech_to_text.convert(
                file=audio_file,
                model_id="scribe_v1"
            )

            # The API returns a JSON-like object with a "text" field. :contentReference[oaicite:2]{index=2}  
            transcript = None
            if isinstance(result, dict):
                transcript = result.get("text", "")
            else:
                # In case result isn't a dict (but likely it is)
                try:
                    transcript = getattr(result, "text", "")
                except Exception:
                    transcript = str(result)

            transcript = transcript or ""
            print(f"[ELEVENLABS] Transcription result: '{transcript}'")
            return transcript

        except requests.exceptions.RequestException as e:
            print(f"[ELEVENLABS] Failed to download audio file: {e}")
            return None
        except Exception as e:
            print(f"[ELEVENLABS] Error during transcription: {e}")
            return None

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

   # print("[GEMINI] GEMINI_API_KEY found, configuring client.")
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
You are a strict, deterministic JSON parser for a ride-sharing service between two locations:

  1. "Emory University"
  2. "Hartsfield-Jackson Atlanta International Airport"

The user will send a short text message describing a ride, for example:
- "955 AM 11/19 atl airport to emory"
- "leaving 3 pm on 11/16 from emory to airport"
- "airport to emory tomorrow 9pm"
- "11/23 8pm emory → atl airport"
- "emory to hartsfield jackson, a quarter of an hour before noon next friday"
- "next saturday 5pm from airport to emory"

You MUST extract:
  - a single departure datetime (assume the timezone is America/New_York)
  - a FROM location
  - a TO location

Current context:
  - Today's date is {current_date_str} (YYYY-MM-DD).
  - The current year is {current_year}.
  - Interpret all dates/times in America/New_York.

Location mapping rules (very strict):
  - If the message says "emory", "emory univ", "emory university", or similar,
    map it to "Emory University".
  - If the message says "airport", "atl airport", "atl", "hartsfield",
    "hartsfield-jackson", or "jackson",
    map it to "Hartsfield-Jackson Atlanta International Airport".
  - If both locations are mentioned, the FIRST mentioned is the FROM location
    and the SECOND mentioned is the TO location.
  - If only one location is clearly mentioned:
      - "from emory" or "leaving emory" => FROM: Emory University, TO: Airport
      - "to emory" or "going to emory"  => FROM: Airport, TO: Emory University
  - The only valid output locations are:
      - "Emory University"
      - "Hartsfield-Jackson Atlanta International Airport"

Date and time interpretation rules (VERY IMPORTANT):

  - If the year is omitted in the date, assume the current year: {current_year}.
  - If the message uses relative words:
      - "today"    => {current_date_str}
      - "tomorrow" => the day after {current_date_str}
  - If the message mentions an explicit weekday name
      (monday, tuesday, wednesday, thursday, friday, saturday, sunday):

      • "this <weekday>":
          - Means the first occurrence of that weekday ON OR AFTER today.
          - For example, if today is Friday and the user says "this Friday",
            use TODAY.

      • "next <weekday>":
          - Means the first occurrence of that weekday STRICTLY AFTER
            "this <weekday>" (i.e., 7 days after the "this" date).
          - For example, if today is Sunday 2025-11-16 and the user says
            "next Saturday", that is Saturday 2025-11-22 (7 days after
            "this Saturday").

      • The departure_time you return MUST fall on the correct weekday
        if a weekday word is present. If your initially inferred date
        does not match that weekday, adjust it forward in time until it does.

  - For phrases like "a quarter of an hour before noon":
      - Interpret as 11:45 AM.
  - If a time of day is requested (e.g., "5pm", "in the morning",
    "at noon"), convert it to a specific HH:MM:SS in 24-hour time.

  - The departure_time MUST NOT be more than 60 days in the future
    relative to {current_date_str} unless the user explicitly specifies
    a later month and day.

  - The departure_time MUST NOT be in the past relative to
    {current_date_str} when the user uses words like "today", "tomorrow",
    "this <weekday>", or "next <weekday>".

If you cannot confidently determine a valid departure datetime AND both locations,
set "success" to false and give a short explanation in "reason".

Output format (must be EXACT):

  - Output ONLY a single JSON object with EXACTLY these fields:
    {{
      "success": true or false,
      "reason": "<short reason if success is false, otherwise null>",
      "departure_time": "YYYY-MM-DDTHH:MM:SS" or null,
      "from_location": "Emory University" or
                       "Hartsfield-Jackson Atlanta International Airport" or null,
      "to_location":   "Emory University" or
                       "Hartsfield-Jackson Atlanta International Airport" or null
    }}

Do NOT include any additional keys.
Do NOT include any comments.
Do NOT include any text before or after the JSON.
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

def perform_match_and_notify(db: Session, ride1: Rides, ride2: Rides) -> None:
    """
    Mark two rides as matched, cross-link them, commit,
    and send the intro WhatsApp messages to both riders.
    """
    # We'll treat ride1 as the "primary" for trip summary
    ride_dt = ride1.departure_time
    from_location = ride1.from_location
    to_location = ride1.to_location

    # Mark both as matched and cross-link
    ride1.status = "matched"
    ride1.matched_with_ride_id = ride2.id

    ride2.status = "matched"
    ride2.matched_with_ride_id = ride1.id

    db.commit()
    db.refresh(ride1)
    db.refresh(ride2)

    # Fetch users
    user1 = ride1.user
    user2 = ride2.user

    name1 = user1.full_name or "another student"
    name2 = user2.full_name or "another student"

    phone1 = user1.phone_number
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
        try:
            perform_match_and_notify(db, new_ride, other)
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

    # If there was a partner, try to instantly rematch them with someone else
    if other:
        # Make sure we use the latest DB state
        db.refresh(other)

        new_match = find_matching_ride(db, other)
        if new_match:
            try:
                perform_match_and_notify(db, other, new_match)
            except Exception as e:
                print("[MATCH DM] Failed to send intro messages on rematch:", e)
        else:
            # Optional: notify them their ride is still active but waiting
            try:
                other_user = other.user
                other_phone = other_user.phone_number

                msg = (
                    "Heads up: your previous match had to cancel their ride, "
                    "so we're rematching you with another rider.\n\n"
                    f"Your ride is still active for "
                    f"{format_departure_time(other.departure_time)} "
                    f"{other.from_location} → {other.to_location}.\n\n"
                    "You'll be notified again once a new match is found."
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