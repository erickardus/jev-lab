"""Signup handler for the demo shop (intentionally leaky; see examples/pii-writes)."""

import hashlib
import logging

import requests
import sentry_sdk
from flask import Blueprint, jsonify, request

from app.db import db
from app.models import User
from app.telemetry import analytics

logger = logging.getLogger(__name__)
bp = Blueprint("signup", __name__)


@bp.post("/signup")
def signup():
    data = request.json or {}
    email = data["email"].strip().lower()
    full_name = data.get("name", "")

    user = User(email=email, name=full_name)
    user.national_id = data.get("national_id")
    user.date_of_birth = data.get("date_of_birth")
    db.session.add(user)
    db.session.commit()

    logger.info("new signup user_id=%s email=%s name=%s ip=%s", user.id, email, full_name, request.remote_addr)
    analytics.track(user.id, "signup", {"email": email, "name": full_name, "plan": data.get("plan")})
    sentry_sdk.set_user({"id": user.id, "email": email})

    requests.post(
        "https://hooks.slack.com/services/T000/B000/XXXX",
        json={"text": f"New signup: {full_name} <{email}>"},
        timeout=3,
    )

    fingerprint = hashlib.sha256(email.encode()).hexdigest()[:12]
    logger.debug("signup fingerprint=%s", fingerprint)
    logger.info("signup complete user_id=%s plan=%s", user.id, data.get("plan"))

    return jsonify({"id": user.id, "email": user.email})
