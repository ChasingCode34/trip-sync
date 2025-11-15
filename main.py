import os

import random
from datetime import datetime, timedelta
from fastapi import FastAPI, Depends, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse
from database import Base, engine, get_db
from models import User
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage
import re
from database import SessionLocal
from models import User, Rides
from utils import create_ride_and_try_match  # 👈 import from utils



load_dotenv()  # loads variables from a .env file into os.environ

# -----------------------
# DB setup: create tables
# -----------------------
Base.metadata.create_all(bind=engine)

app = FastAPI()

# -----------------------
# Helpers
# -----------------------

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")  # e.g. "whatsapp:+1415xxxxxxx"




def generate_otp() -> str:
    """Generate a 6-digit zero-padded OTP as a string."""
    return f"{random.randint(0, 999999):06d}"


def send_verification_email(emory_email: str, code: str):
    """
    Send the verification code to the user's Emory email using SMTP.

    Expects these env vars to be set (e.g. in a .env file):
      SMTP_HOST   - e.g. "smtp.gmail.com"
      SMTP_PORT   - e.g. "587"
      SMTP_USER   - the login username (e.g. your email)
      SMTP_PASS   - the SMTP/app password
      FROM_EMAIL  - "From" address (often same as SMTP_USER)
    """
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    from_email = os.getenv("FROM_EMAIL", smtp_user)

    if not smtp_user or not smtp_pass:
        # Fail gracefully in dev if not configured
        print(
            f"[EMAIL-DEBUG] Missing SMTP_USER/SMTP_PASS; "
            f"would have sent code {code} to {emory_email}"
        )
        return

    msg = EmailMessage()
    msg["Subject"] = "Your TrypSync Verification Code"
    msg["From"] = from_email
    msg["To"] = emory_email
    msg.set_content(
        f"""Hi,

Your TrypSync verification code is: {code}

Enter this code in WhatsApp to complete verification.
If you did not request this, you can ignore this email.

Thanks,
TrypSync
"""
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.send_message(msg)

    print(f"[EMAIL] Sent verification code to {emory_email}")


# -----------------------
# Twilio SMS webhook
# -----------------------


@app.post("/sms")
async def sms_webhook(
    From: str = Form(...),   # Twilio sends "From" as the sender's phone number
    Body: str = Form(""),    # Twilio sends "Body" as the message text
    db: Session = Depends(get_db),
):
    from_number = From.strip()
    body = (Body or "").strip()
    resp = MessagingResponse()

    # 1) Get or create user by phone number.
    user = db.query(User).filter(User.phone_number == from_number).one_or_none()
    if user is None:
        user = User(
            phone_number=from_number,
            is_verified=False,
            emory_email=None,
            otp_code=None,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    # 2) Onboarding / verification flow
    if not user.is_verified:
        # STEP 1: Ask for their full name first
        if user.full_name is None:
            name = body.strip()

            # If this doesn't look like a full name yet (no space, too short),
            # just treat this as the initial ping ("hi", "hey", etc.)
            # and prompt them for their full name.
            if " " not in name or len(name) < 3:
                resp.message(
                    "Welcome to TrypSync! 🚕\n\n"
                    "To get started, please reply with your full name "
                    "(for example: 'Akhil Arularasu')."
                )
                return Response(content=str(resp), media_type="application/xml")

            # Looks like a real name → save it
            user.full_name = name
            db.commit()

            resp.message(
                f"Nice to meet you, {name}! 🎉\n\n"
                "Now please reply with your Emory email ending in @emory.edu."
            )
            return Response(content=str(resp), media_type="application/xml")

        # STEP 2: We know their name but not their email → treat message as email step
        if user.emory_email is None:
            em_raw = body.strip()
            em = em_raw.lower()

            # 1) If it doesn't even look like an email → instructions
            if "@" not in em or "." not in em.split("@")[-1]:
                resp.message(
                    "Please reply with your Emory email ending in @emory.edu.\n\n"
                    "Example: akhil.arularasu@emory.edu"
                )
                return Response(content=str(resp), media_type="application/xml")

            # 2) Allowed domain: Emory only (Gatech allowed silently for your testing)
            if not em.endswith(("@emory.edu", "@gatech.edu")):
                resp.message(
                    "The TrypSync service is currently only available to Emory students.\n\n"
                    "Please reply with a valid Emory email ending in @emory.edu."
                )
                return Response(content=str(resp), media_type="application/xml")

            # 3) Valid email → save & send OTP
            user.emory_email = em
            code = generate_otp()
            user.otp_code = code
            db.commit()

            send_verification_email(user.emory_email, code)

            resp.message(
                f"Thanks {user.full_name}! We sent a 6-digit code to {user.emory_email}. "
                "Reply with that code here to verify your account."
            )
            return Response(content=str(resp), media_type="application/xml")

        # STEP 3: We know name + email → expect OTP in this message
        if body.strip() == (user.otp_code or ""):
            user.is_verified = True
            user.otp_code = None
            db.commit()

            resp.message(
                f"You're verified ✅, {user.full_name}!\n\n"
                "From now on, just send your ride requests like:\n"
                "'8:30 am 11/17 emory to airport'.\n\n"
                "You can cancel your ride at any time by replying 'cancel'."
            )
            return Response(content=str(resp), media_type="application/xml")
        else:
            resp.message(
                "That code is incorrect. Please reply with the 6-digit code we sent "
                f"to {user.emory_email}."
            )
            return Response(content=str(resp), media_type="application/xml")


    # 3) User is verified at this point

    # If they type "cancel" -> cancel active ride instead of creating a new one
    if body.strip().lower() == "cancel":
        from utils import cancel_active_ride  # put at top of file instead
        msg = cancel_active_ride(db, user)
        resp.message(msg)
        return Response(content=str(resp), media_type="application/xml")

    # Otherwise treat message as a ride request
    response_text = create_ride_and_try_match(db, user, body)
    resp.message(response_text)
    return Response(content=str(resp), media_type="application/xml")
