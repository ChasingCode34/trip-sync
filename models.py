from datetime import datetime

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # Phone number from Twilio "From" field (e.g., "+14045551234")
    phone_number = Column(String(20), unique=True, nullable=False, index=True)
    # New: full name that user types once during onboarding
    full_name = Column(String(255), nullable=True)
    # Emory email typed by user via SMS, must end with @emory.edu (enforced in code)
    emory_email = Column(String(255), unique=True, nullable=True)
    # True once they successfully enter the correct code from their Emory email
    is_verified = Column(Boolean, nullable=False, default=False)
    # Temporary 6-digit code we email them; cleared (set to NULL) after success
    otp_code = Column(String(6), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    # Relationship to ride requests (optional for now, but useful later)
    rides = relationship("Rides", back_populates="user")


class Rides(Base):
    __tablename__ = "rides"

    id = Column(Integer, primary_key=True, index=True)

    # FK link to user
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="rides")

    # Original text the user sent
    original_message = Column(String(500), nullable=False)

    # Fixed for now: Emory → ATL
    from_location = Column(String(100), nullable=False, default="Emory University")
    to_location = Column(
        String(150),
        nullable=False,
        default="Hartsfield-Jackson Atlanta International Airport",
    )

    # Full datetime of departure (parsed from SMS)
    departure_time = Column(DateTime, nullable=False)

    # Always 1 person for now
    party_size = Column(Integer, nullable=False, default=1)

    # pending → matched → completed → cancelled
    status = Column(String(20), nullable=False, default="pending")

    # If matched, which other ride is it linked to?
    matched_with_ride_id = Column(Integer, ForeignKey("rides.id"), nullable=True)

    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
