"""Token issuing and verification.

Flow: the client signs in with a provider (Google today, Apple next) and POSTs
the provider's ID token to /auth/<provider>. The server verifies it with the
provider's public keys, finds-or-creates the athlete via auth_identities, and
issues OUR OWN JWT. Every API call after that uses Authorization: Bearer <jwt>;
provider tokens are never passed around again.
"""
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import ACCESS_TOKEN_TTL_DAYS, GOOGLE_CLIENT_IDS, JWT_SECRET
from database import get_db
from models import Athlete, AuthIdentity

JWT_ALGORITHM = "HS256"

# auto_error=False so we can return a clean 401 (instead of 403) when the
# header is missing. This also gives /docs its "Authorize" button.
_bearer = HTTPBearer(auto_error=False)


def create_access_token(athlete_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(athlete_id),
        "iat": now,
        # Long-lived for dev convenience; shorten + add refresh tokens before
        # real team rollout (see config.ACCESS_TOKEN_TTL_DAYS).
        "exp": now + timedelta(days=ACCESS_TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def get_current_athlete(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Athlete:
    """FastAPI dependency: validate our JWT and yield the signed-in athlete."""
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET,
                             algorithms=[JWT_ALGORITHM])
        athlete_id = int(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    athlete = db.get(Athlete, athlete_id)
    if athlete is None:
        raise HTTPException(status_code=401, detail="Unknown athlete",
                            headers={"WWW-Authenticate": "Bearer"})
    return athlete


def authorize_athlete_access(current: Athlete, target_athlete_id: int) -> None:
    """403 unless the target is the signed-in athlete or the caller is a coach."""
    if current.role != "coach" and current.id != target_athlete_id:
        raise HTTPException(status_code=403,
                            detail="You may only access your own data")


def verify_google_id_token(token: str) -> dict:
    """Verify a Google ID token (signature, expiry, issuer) via google-auth,
    then check the audience against our configured client IDs. Returns claims."""
    if not GOOGLE_CLIENT_IDS:
        raise HTTPException(
            status_code=503,
            detail="Google sign-in not configured: set GOOGLE_CLIENT_IDS in .env")
    try:
        # audience=None skips the lib's single-audience check; we accept any of
        # our client IDs (web + Android) and enforce membership ourselves below.
        # Allow a small clock skew (tokens live ~1h); a few seconds of drift
        # between us and Google's issuer otherwise throws "Token used too early".
        claims = google_id_token.verify_oauth2_token(
            token, google_requests.Request(), audience=None,
            clock_skew_in_seconds=10)
    except ValueError as e:
        raise HTTPException(status_code=401, detail=f"Invalid Google token: {e}")
    if claims.get("aud") not in GOOGLE_CLIENT_IDS:
        raise HTTPException(status_code=401,
                            detail="Google token audience mismatch")
    return claims


def get_or_create_athlete_for_identity(
    db: Session, provider: str, provider_user_id: str,
    email: str | None, name: str | None,
) -> Athlete:
    """Look up (provider, provider_user_id); on first sign-in create the
    athlete (defaulting name/email from the provider profile) + identity."""
    identity = db.scalar(select(AuthIdentity).where(
        AuthIdentity.provider == provider,
        AuthIdentity.provider_user_id == provider_user_id))
    if identity is not None:
        athlete = db.get(Athlete, identity.athlete_id)
        if athlete is not None:
            return athlete
        # Orphaned identity (its athlete row was deleted): recreate the athlete
        # and re-point the identity, rather than returning None -> 500 on sign-in.
        athlete = Athlete(name=name or email or "New athlete", email=email,
                          role="athlete")
        db.add(athlete)
        db.flush()
        identity.athlete_id = athlete.id
        db.commit()
        db.refresh(athlete)
        return athlete

    athlete = Athlete(name=name or email or "New athlete", email=email,
                      role="athlete")
    db.add(athlete)
    db.flush()  # populate athlete.id for the FK
    db.add(AuthIdentity(athlete_id=athlete.id, provider=provider,
                        provider_user_id=provider_user_id, email=email))
    db.commit()
    db.refresh(athlete)
    return athlete
