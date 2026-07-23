"""
otp_reader.py — pull the DealerCenter MFA code out of a Gmail inbox over IMAP.

DealerCenter sends the code from  do-not-reply@dealercenter.net
with subject "Your authentication code" and a body like:
    "MFA Code for DealerCenter  Your code is: 006504  This passcode will expire in 5 minutes"

We only trust codes that arrive AFTER the login click (so we never reuse a stale one),
and only from the DealerCenter sender (so an unrelated 6-digit number never leaks in).

Gmail requires an APP PASSWORD for IMAP (normal password won't work):
  1. Turn on 2-Step Verification on the Google account.
  2. Google Account -> Security -> App passwords -> generate one for "Mail".
  3. Put the 16-char value in .env.local as GMAIL_APP_PASSWORD (spaces are fine).
"""

import imaplib
import email
import re
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

DC_SENDER = "do-not-reply@dealercenter.net"
IMAP_HOST = "imap.gmail.com"

# "Your code is: 123456" is the most reliable anchor; fall back to any 6-digit run.
_CODE_ANCHORED = re.compile(r"code is:?\s*([0-9]{6})", re.IGNORECASE)
_CODE_ANY = re.compile(r"\b([0-9]{6})\b")


def _body_text(msg) -> str:
    """Flatten an email into searchable text (prefers plain, falls back to html)."""
    parts = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                try:
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(payload.decode(part.get_content_charset() or "utf-8",
                                                     errors="ignore"))
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload:
                parts.append(payload.decode(msg.get_content_charset() or "utf-8",
                                            errors="ignore"))
        except Exception:
            pass
    text = "\n".join(parts)
    # subject sometimes carries the code too
    subj = msg.get("Subject", "") or ""
    return subj + "\n" + text


def _extract_code(text: str):
    m = _CODE_ANCHORED.search(text)
    if m:
        return m.group(1)
    m = _CODE_ANY.search(text)
    return m.group(1) if m else None


def get_otp(gmail_address: str,
            gmail_app_password: str,
            after_epoch: float,
            timeout: int = 120,
            poll_seconds: int = 4,
            log=print):
    """
    Poll Gmail until a DealerCenter code that arrived after `after_epoch` shows up.
    Returns the 6-digit string, or None on timeout.
    """
    after_dt = datetime.fromtimestamp(after_epoch, tz=timezone.utc)
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL(IMAP_HOST)
            mail.login(gmail_address, gmail_app_password.replace(" ", ""))
            mail.select("INBOX")

            # newest DealerCenter messages only
            status, data = mail.search(None, 'FROM', f'"{DC_SENDER}"')
            if status == "OK" and data and data[0]:
                uids = data[0].split()
                # check the last few, newest first
                for uid in reversed(uids[-8:]):
                    st, msg_data = mail.fetch(uid, "(RFC822)")
                    if st != "OK" or not msg_data or not msg_data[0]:
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])

                    # only trust codes newer than the login click
                    try:
                        msg_dt = parsedate_to_datetime(msg.get("Date"))
                        if msg_dt.tzinfo is None:
                            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
                        if msg_dt < after_dt:
                            continue
                    except Exception:
                        pass  # if date unparseable, still try it

                    code = _extract_code(_body_text(msg))
                    if code:
                        try:
                            mail.logout()
                        except Exception:
                            pass
                        log("ok", f"Fetched MFA code from Gmail: {code}")
                        return code
            try:
                mail.logout()
            except Exception:
                pass
        except imaplib.IMAP4.error as e:
            log("error", f"Gmail login/IMAP error: {e} "
                         f"(check GMAIL_APP_PASSWORD is an app password, not your normal one)")
            return None
        except Exception as e:
            log("warn", f"OTP poll retry: {e}")

        time.sleep(poll_seconds)

    log("warn", "OTP not found in Gmail within timeout window")
    return None
