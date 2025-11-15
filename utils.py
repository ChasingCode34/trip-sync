# utils.py

import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

# Adjust this import if you're using a package structure, e.g.:
# from app.models import User, Rides
from models import User, Rides


# utils.py

EMORY_NAME = "Emory University"
AIRPORT_NAME = "Hartsfield-Jackson Atlanta International Airport"


def parse_route(message: str) -> tuple[str, str]:
    """
    Parse from/to locations from the free-form message.

    We only support two endpoints for now:
      - Emory University
      - Hartsfield-Jackson Atlanta International Airport

    Logic:
      - If both 'emory' and 'airport/atl/hartsfield/jackson' appear:
          whichever appears first in the text is treated as FROM,
          the other as TO.
      - If only one of them appears:
          - 'emory' only  -> Emory -> Airport
          - airport/atl only -> Airport -> Emory
      - If neither appears: default Emory -> Airport.
    """
    text = message.lower()

    # Detect mentions
    has_emory = "emory" in text
    has_airport_word = any(
        kw in text for kw in ["airport", "hartsfield", "jackson", "atl"]
    )

    # Helper to get earliest index of any airport-like keyword
    def airport_index(t: str) -> int:
        indices = [
            t.find("airport"),
            t.find("hartsfield"),
            t.find("jackson"),
            t.find("atl"),
        ]
        indices = [i for i in indices if i != -1]
        return min(indices) if indices else -1

    if has_emory and has_airport_word:
        idx_emory = text.find("emory")
        idx_airport = airport_index(text)

        if idx_emory != -1 and idx_airport != -1:
            if idx_emory < idx_airport:
                # "emory ... airport"
                return EMORY_NAME, AIRPORT_NAME
            else:
                # "airport ... emory"
                return AIRPORT_NAME, EMORY_NAME

    # Only one side mentioned → assume other side is the opposite point
    if has_emory and not has_airport_word:
        return EMORY_NAME, AIRPORT_NAME

    if has_airport_word and not has_emory:
        return AIRPORT_NAME, EMORY_NAME

    # Fallback: default Emory → Airport
    return EMORY_NAME, AIRPORT_NAME


def parse_ride_datetime(message: str) -> Optional[datetime]:
    """
    Parse a datetime from a free-form SMS message.

    Supports things like:
      - "leaving at 3 PM on 11/16 from Emory to airport"
      - "11/16 3:30pm"
      - "3 pm 11/16"

    Assumes the ride is in the current year if year not provided.
    Returns a naive datetime (no timezone) or None if parsing fails.
    """
    text = message.lower()

    # Time like "3 pm", "3:30pm", "11 am", "11:05 pm"
    time_match = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b', text)
    # Date like "11/16" or "11/16/2025"
    date_match = re.search(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b', text)

    if not time_match or not date_match:
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    ampm = time_match.group(3)

    # Convert to 24-hour
    if ampm == "pm" and hour != 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0

    month = int(date_match.group(1))
    day = int(date_match.group(2))
    year_str = date_match.group(3)

    if year_str:
        year = int(year_str)
        if year < 100:  # e.g. "/25" → 2025
            year += 2000
    else:
        year = datetime.now().year

    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        # Invalid date like 13/40/2025 etc.
        return None


def format_departure_time(dt: datetime) -> str:
    """
    Format a datetime for human-readable SMS responses.
    Example: '11/16 03:30 PM'
    """
    return dt.strftime("%m/%d %I:%M %p")


def get_active_ride_for_user(db: Session, user_id: int) -> Optional[Rides]:
    """
    Return the user's most recent 'active' ride, where status is 'pending' or 'matched'.
    If none exists, return None.
    """
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
    from datetime import timedelta

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
    # 1) One active ride per user
    active_ride = get_active_ride_for_user(db, user.id)
    if active_ride:
        return (
            "You already have a ride on file.\n\n"
            f"Departure: {format_departure_time(active_ride.departure_time)} "
            f"{active_ride.from_location} → {active_ride.to_location}.\n"
            "If you need to change it, reply 'cancel' (coming soon) and then send a new request."
        )

    # 2) Parse datetime
    ride_dt = parse_ride_datetime(body)
    if not ride_dt:
        return (
            "I couldn't understand your date/time 😅.\n\n"
            "Please send something like: "
            "'leaving at 3 PM on 11/16 from Emory to airport'."
        )

    # 3) Parse route (from/to)
    from_location, to_location = parse_route(body)

    # 4) Create new ride
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

    # 5) Try to find a match
    other = find_matching_ride(db, new_ride)

    if other:
        new_ride.status = "matched"
        new_ride.matched_with_ride_id = other.id

        other.status = "matched"
        other.matched_with_ride_id = new_ride.id

        db.commit()

        # ⬇️ this is the FIRST return block you posted
        return (
            "Good news! 🎉 We found another Emory student with a similar ride.\n\n"
            f"Your ride: {format_departure_time(ride_dt)} "
            f"{from_location} → {to_location}.\n"
            "We’ll introduce you both shortly so you can coordinate."
        )
    else:
        # ⬇️ this is the SECOND return block you posted
        return (
            "Got it ✅ Your ride request is saved.\n\n"
            f"Departure: {format_departure_time(ride_dt)} "
            f"{from_location} → {to_location}.\n"
            "We'll match you with another Emory student as soon as someone compatible joins."
        )
