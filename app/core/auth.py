"""
Testing-phase auth stopgap -- hardcoded test credentials, but real JWT
issuance/validation and real bcrypt password hashing.

This is NOT real auth. It exists so protected routes, token issuance, and
"how user identity flows into a Job" can all be built and tested now,
before real Supabase-based register/login/OTP auth lands. Built so the
credential-checking piece is the ONLY thing that needs to change when that
happens:
    - verify_credentials() is the swappable boundary -- replace its body
      with a real Supabase auth call, keep the same
      (username, password) -> bool signature, and nothing else in this
      module (or any caller) needs to change.
    - create_access_token() / get_current_user(), and how routers depend on
      get_current_user to get a username for Job.user_id, are the
      permanent pieces that survive the swap unchanged.

Test credentials (see README for the user-facing callout -- do not mistake
this for a real security posture):
    username: TEST_USERNAME env var, default "testuser"
    password: TEST_PASSWORD env var, default "testpass123" -- an obvious
    placeholder, never a real secret.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from app.core.config import settings

TEST_USERNAME = os.getenv("TEST_USERNAME", "Admin")
TEST_PASSWORD = os.getenv("TEST_PASSWORD", "Admin@123")  # placeholder, not a real secret

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Stored as a bcrypt hash, not plaintext, even for this one hardcoded test
# user -- so the comparison pattern (look up a hash, then verify) already
# matches what real auth will look like; only the lookup source changes
# when verify_credentials() is swapped out later.
_TEST_CREDENTIALS: dict[str, str] = {
    TEST_USERNAME: _pwd_context.hash(TEST_PASSWORD),
}


def verify_credentials(username: str, password: str) -> bool:
    """
    THE swappable boundary. Replace this function's body with a real
    Supabase auth call when that lands -- signature must stay
    (username: str, password: str) -> bool so nothing else (token
    issuance, route protection, callers) needs to change.
    """
    password_hash = _TEST_CREDENTIALS.get(username)
    if password_hash is None:
        return False
    return _pwd_context.verify(password, password_hash)


def create_access_token(username: str) -> str:
    """Issues a JWT with `username` as the subject claim, expiring after
    settings.jwt_expiry_hours (24h default, fine for testing)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + timedelta(hours=settings.jwt_expiry_hours),
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


# auto_error=False so a missing header falls through to our own 401 (with
# our own message) below, instead of FastAPI's default 403.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """FastAPI dependency for protected routes -- extracts and validates the
    Bearer JWT from the Authorization header, returning the username (the
    token's `sub` claim). Raises 401 if the header is missing or the token
    is invalid/expired."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated. Provide a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            credentials.credentials, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    username = payload.get("sub")
    if not username:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return username
