import os

import random
from datetime import datetime
from fastapi import FastAPI, Depends, Form
from fastapi.responses import Response
from sqlalchemy.orm import Session
from twilio.twiml.messaging_response import MessagingResponse
from database import Base, engine, get_db
from models import User
import os
from dotenv import load_dotenv
import smtplib
from email.message import EmailMessage


load_dotenv()  # loads variables from a .env file into os.environ

# -----------------------
# DB setup: create tables
# -----------------------
Base.metadata.create_all(bind=engine)

app = FastAPI()

# -----------------------
# Helpers
# -----------------------


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
        # CASE A: We don't know their Emory email yet → treat this message as the email "keyword"
        if user.emory_email is None:
            em = body.strip().lower()
            if not em.endswith("@emory.edu"):
                resp.message(
                    "TrypSync is currently only available to the Emory community. "
                    "Please reply with a valid Emory email ending in @emory.edu.\n\n"
                    "Example: akhil.arularasu@emory.edu"
                )
                return str(resp)


            # Save email & generate OTP
            user.emory_email = em
            code = generate_otp()
            user.otp_code = code
            db.commit()

            # Send the verification code to their Emory email (NOT via SMS)
            send_verification_email(user.emory_email, code)

            resp.message(
                f"Thanks! We sent a 6-digit code to {user.emory_email}. "
                "Reply with that code here to verify your account."
            )
            return str(resp)

        # CASE B (simplified): Email is known, expecting this SMS to be the OTP.
        # No separate regeneration branch; this keeps state logic tight.
        if body == (user.otp_code or ""):
            user.is_verified = True
            user.otp_code = None  # clear OTP after success
            db.commit()
            resp.message(
                "You're verified ✅ as an Emory student. From now on, just text us "
                "your ride requests from Emory to ATL airport.\n\n"
                "Example: '8:30pm, 3 people'."
            )
            return str(resp)
        else:
            resp.message(
                "That code is incorrect. Please reply with the 6-digit code we sent "
                f"to {user.emory_email}."
            )
            return str(resp)

    # 3) Already verified → treat SMS as ride request (for now: placeholder)
    #    Later, you'll parse `body` into requested_time + party_size and write a RideRequest row.
    resp.message(
        "You're already verified ✅. "
        "Send your ride request like: '8:30pm, 3 people' and we'll match you."
    )
    return str(resp)

