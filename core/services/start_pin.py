"""Doorstep arrival ("start PIN") generation and limits.

The customer sees a four-digit code after a technician is assigned and reads it
out when the technician arrives. A job may only move to ``in_progress`` after
the assigned technician submits that code, so the customer controls when the
work officially starts.

The code is deliberately short (four digits) because it is read out at the
door. Guessing is stopped by the attempt counter and the 15-minute lock stored
on the request row, not by the code's entropy.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

START_PIN_LENGTH = 4
START_PIN_ATTEMPTS = 5
START_PIN_LOCK_MINUTES = 15


def generate_start_pin() -> str:
    """A random four-digit code, including codes that start with zero."""
    return ''.join(str(secrets.randbelow(10)) for _ in range(START_PIN_LENGTH))


def start_pin_lock_until(now):
    """The lock expiry for a request whose attempts just ran out."""
    return now + timedelta(minutes=START_PIN_LOCK_MINUTES)
