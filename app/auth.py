import base64
import hashlib
import hmac
import logging
import re
import secrets
import struct
import threading
import time
from urllib.parse import quote

import argon2
import qrcode
import qrcode.image.svg

log = logging.getLogger("auth")

COOKIE = "procrustes_session"
ISSUER = "procrustes"
MODES = ("password", "totp", "both")
MODE_LABELS = {"password": "password only", "totp": "OTP only", "both": "MFA with both"}

TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
SECRET_BYTES = 20
TOKEN_BYTES = 32
LOCKOUT_FAILURES = 5
LOCKOUT_SECONDS = 900
PASSWORD_MIN = 8
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$")
CODE = re.compile(r"^\d{6}$")

#----- The library defaults are the parameters, recorded in CLAUDE.md section 30 and not retuned here.
HASHER = argon2.PasswordHasher()
#----- Verified against on an unknown username, so that path costs the same as a real one.
DUMMY_HASH = HASHER.hash(secrets.token_urlsafe(16))


class AuthError(Exception):
    pass


class AuthRefused(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class LockedOut(AuthError):
    def __init__(self, retry_after):
        super().__init__("too many failures, try again later")
        self.retry_after = int(retry_after)


#----- Passwords
def hash_password(password):
    return HASHER.hash(password)


def verify_password(stored, password):
    if not stored or password is None:
        return False
    try:
        return HASHER.verify(stored, password)
    except (argon2.exceptions.VerifyMismatchError, argon2.exceptions.VerificationError,
            argon2.exceptions.InvalidHashError):
        return False


def needs_rehash(stored):
    try:
        return HASHER.check_needs_rehash(stored)
    except argon2.exceptions.InvalidHashError:
        return True


#----- TOTP, RFC 6238 over RFC 4226
def new_secret():
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _key(secret):
    text = str(secret or "").strip().upper().replace(" ", "")
    return base64.b32decode(text + "=" * (-len(text) % 8))


def code_at(secret, step):
    mac = hmac.new(_key(secret), struct.pack(">Q", int(step)), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    number = int.from_bytes(mac[offset:offset + 4], "big") & 0x7FFFFFFF
    return "%0*d" % (TOTP_DIGITS, number % (10 ** TOTP_DIGITS))


def verify_code(secret, code, last_step=None, now=None):
    code = str(code or "").strip().replace(" ", "")
    if not CODE.match(code) or not secret:
        return None
    try:
        _key(secret)
    except (ValueError, TypeError):
        return None
    current = int((now if now is not None else time.time()) // TOTP_STEP)
    accepted = None
    for step in range(current - TOTP_WINDOW, current + TOTP_WINDOW + 1):
        #----- a step at or below the last accepted one is a replay, whatever the clock says.
        if last_step is not None and step <= int(last_step):
            continue
        if hmac.compare_digest(code_at(secret, step), code) and accepted is None:
            accepted = step
    return accepted


def otpauth_uri(username, secret):
    #----- the colon between issuer and account stays literal;  authenticator apps read the label by it.
    label = "%s:%s" % (quote(ISSUER, safe=""), quote(str(username), safe=""))
    return "otpauth://totp/%s?secret=%s&issuer=%s&algorithm=SHA1&digits=%d&period=%d" % (
        label, secret, quote(ISSUER, safe=""), TOTP_DIGITS, TOTP_STEP,
    )


#----- Enrolment
def qr_svg(text):
    #----- the SVG factory never imports Pillow, whatever the package pulls in.
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage)
    rendered = image.to_string()
    if isinstance(rendered, bytes):
        rendered = rendered.decode("utf-8")
    return rendered


def enrolment(username, secret=None):
    secret = secret or new_secret()
    uri = otpauth_uri(username, secret)
    return {"secret": secret, "uri": uri, "qr": qr_svg(uri)}


#----- Sessions
def new_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token):
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


#----- Modes and their invariants
def mode_needs(mode):
    return mode in ("password", "both"), mode in ("totp", "both")


def mode_allowed(row, mode):
    if mode not in MODES:
        return "mode must be one of %s" % ", ".join(MODES)
    needs_password, needs_totp = mode_needs(mode)
    if needs_password and not row.get("password_hash"):
        return "%s needs a password set first" % MODE_LABELS[mode]
    if needs_totp and not row.get("totp_secret"):
        return "%s needs an authenticator enrolled and confirmed first" % MODE_LABELS[mode]
    return None


def check_username(username):
    username = str(username or "").strip()
    if not USERNAME.match(username):
        raise AuthRefused(
            "username must be 1 to 64 characters of letters, digits, '.', '_', '@' or '-', starting with a letter or digit"
        )
    return username


def check_password(password):
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise AuthRefused("password must be at least %d characters" % PASSWORD_MIN)
    return password


#----- The lockout, in memory, keyed by username and by address
class Lockout:
    def __init__(self, failures=LOCKOUT_FAILURES, seconds=LOCKOUT_SECONDS):
        self.failures = failures
        self.seconds = seconds
        self._hits = {}
        self._lock = threading.Lock()

    def _prune(self, key, now):
        hits = [t for t in self._hits.get(key, ()) if now - t < self.seconds]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked_for(self, keys, now=None):
        now = now if now is not None else time.time()
        longest = 0
        with self._lock:
            for key in keys:
                hits = self._prune(key, now)
                if len(hits) >= self.failures:
                    longest = max(longest, self.seconds - (now - hits[0]))
        return longest

    def failure(self, keys, now=None):
        now = now if now is not None else time.time()
        with self._lock:
            for key in keys:
                self._prune(key, now)
                self._hits.setdefault(key, []).append(now)

    def clear(self, keys):
        with self._lock:
            for key in keys:
                self._hits.pop(key, None)


#----- The account rules over the store
class Auth:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.lockout = Lockout()

    @property
    def enabled(self):
        return bool(self.settings.get("auth_enabled"))

    def first_run(self):
        return self.enabled and self.store.user_count() == 0

    def session_hours(self):
        return int(self.settings.get("session_hours"))

    def _public(self, row):
        if row is None:
            return None
        return {
            "id": row["id"],
            "username": row["username"],
            "mode": row["mode"],
            "has_password": bool(row.get("password_hash")),
            "totp_enrolled": bool(row.get("totp_secret")),
            "totp_pending": bool(row.get("totp_pending")),
            "created_at": row.get("created_at"),
        }

    def _lockout_keys(self, username, address):
        return ("user:%s" % str(username or "").lower(), "addr:%s" % (address or ""))

    def _start_session(self, user_id):
        token = new_token()
        now = time.time()
        self.store.session_create(token_hash(token), user_id, now, now + self.session_hours() * 3600)
        return token

    #----- First run
    def setup(self, body, address):
        if not self.first_run():
            raise AuthRefused("the first account already exists", 409)
        body = body or {}
        if not body.get("require", True):
            self.settings.update({"auth_enabled": False}, internal=True)
            log.warning("authentication declined at first run from %s", address)
            return None
        username = check_username(body.get("username"))
        mode = str(body.get("mode") or "password").strip().lower()
        if mode not in MODES:
            raise AuthRefused("mode must be one of %s" % ", ".join(MODES))
        needs_password, needs_totp = mode_needs(mode)
        password_hash = None
        secret = None
        if needs_password:
            password_hash = hash_password(check_password(body.get("password")))
        if needs_totp:
            secret = str(body.get("secret") or "").strip()
            if not secret:
                raise AuthRefused("enrol an authenticator before creating the account")
            step = verify_code(secret, body.get("code"))
            if step is None:
                raise AuthRefused("the authenticator code did not match; check the device's clock and try the next code")
        #----- the count is checked inside the store lock, so two first-run posts cannot both land.
        user_id = self.store.user_create(username, password_hash, mode, secret, only_if_empty=True)
        if user_id is None:
            raise AuthRefused("the first account already exists", 409)
        if needs_totp:
            self.store.user_update(user_id, last_totp_step=step)
        log.info("first account %s created in mode %s from %s", username, mode, address)
        return self._start_session(user_id)

    #----- Login and sessions
    def login(self, username, password, code, address):
        username = str(username or "").strip()
        keys = self._lockout_keys(username, address)
        wait = self.lockout.blocked_for(keys)
        if wait > 0:
            log.info("locked out user=%s from %s for %ds", username, address, wait)
            raise LockedOut(wait)
        row = self.store.user_by_name(username) if username else None
        reason = None
        step = None
        if row is None:
            verify_password(DUMMY_HASH, password or "")
            reason = "unknown user"
        else:
            needs_password, needs_totp = mode_needs(row["mode"])
            if needs_password and not verify_password(row["password_hash"], password or ""):
                reason = "wrong password"
            elif needs_totp:
                step = verify_code(row["totp_secret"], code, row.get("last_totp_step"))
                if step is None:
                    reason = "wrong or reused code"
        #----- one message for every failure;  the reason is for the log alone.
        if reason:
            self.lockout.failure(keys)
            log.info("login failed user=%s from %s: %s", username, address, reason)
            raise AuthError("login failed")
        self.lockout.clear(keys)
        fields = {}
        if step is not None:
            fields["last_totp_step"] = step
        if row["password_hash"] and needs_password and needs_rehash(row["password_hash"]):
            fields["password_hash"] = hash_password(password)
        if fields:
            self.store.user_update(row["id"], **fields)
        log.info("login ok user=%s from %s", username, address)
        return self._start_session(row["id"])

    def logout(self, token):
        if token:
            self.store.session_delete(token_hash(token))

    def session_user(self, token):
        if not token:
            return None
        now = time.time()
        session = self.store.session_get(token_hash(token))
        if session is None:
            return None
        if session["expires_at"] <= now:
            self.store.session_delete(session["token_hash"])
            self.store.sessions_purge_expired(now)
            return None
        return self.store.user_get(session["user_id"])

    #----- Account changes, each confirmed by a factor the account already has
    def _confirm(self, row, password, code):
        if password:
            if verify_password(row.get("password_hash"), password):
                return None
            raise AuthRefused("the password was wrong", 403)
        if code:
            step = verify_code(row.get("totp_secret"), code, row.get("last_totp_step"))
            if step is not None:
                self.store.user_update(row["id"], last_totp_step=step)
                return step
            raise AuthRefused("the code was wrong or already used", 403)
        raise AuthRefused("the current password or a fresh authenticator code is required")

    def set_enabled(self, user, enabled, password, code, address):
        enabled = bool(enabled)
        if enabled == self.enabled:
            return False
        #----- off needs a signed-in user;  on has no caller to check, since nobody is signed in while it is off.
        if not enabled:
            if user is None or user.get("id") is None:
                raise AuthRefused("sign in to switch authentication off", 401)
            self._confirm(user, password, code)
            self.settings.update({"auth_enabled": False}, internal=True)
            log.warning("authentication switched off by %s from %s", user["username"], address)
            return True
        self.settings.update({"auth_enabled": True}, internal=True)
        log.info("authentication switched on from %s", address)
        return True

    def account_view(self, user):
        view = {"enabled": self.enabled}
        if not self.enabled or user is None or user.get("id") is None:
            view.update({"username": None, "mode": None, "totp_enrolled": None,
                         "totp_pending": None, "users": None, "modes": None})
            return view
        row = self.store.user_get(user["id"])
        view.update(self._public(row))
        view["modes"] = {mode: mode_allowed(row, mode) for mode in MODES}
        view["users"] = [self._public(r) for r in self.store.users()]
        return view

    def change_password(self, user, current, new, code=None):
        row = self.store.user_get(user["id"])
        new = check_password(new)
        if row.get("password_hash"):
            self._confirm(row, current, None)
        elif row.get("totp_secret"):
            self._confirm(row, None, code)
        self.store.user_update(row["id"], password_hash=hash_password(new))
        self.store.sessions_delete_for_user(row["id"])
        log.info("password changed for %s", row["username"])

    def enrol_totp(self, user):
        row = self.store.user_get(user["id"])
        issued = enrolment(row["username"])
        self.store.user_update(row["id"], totp_pending=issued["secret"])
        return issued

    def confirm_totp(self, user, code):
        row = self.store.user_get(user["id"])
        pending = row.get("totp_pending")
        if not pending:
            raise AuthRefused("no authenticator enrolment is pending")
        step = verify_code(pending, code)
        if step is None:
            raise AuthRefused("the authenticator code did not match; check the device's clock and try the next code")
        self.store.user_update(row["id"], totp_secret=pending, totp_pending=None, last_totp_step=step)
        log.info("totp enrolled for %s", row["username"])

    def disable_totp(self, user, password, code):
        row = self.store.user_get(user["id"])
        if not row.get("totp_secret"):
            raise AuthRefused("no authenticator is enrolled")
        if row["mode"] in ("totp", "both"):
            raise AuthRefused("the login mode %s needs the authenticator; choose password only first" % MODE_LABELS[row["mode"]])
        self._confirm(row, password, code)
        self.store.user_update(row["id"], totp_secret=None, totp_pending=None, last_totp_step=None)
        log.info("totp disabled for %s", row["username"])

    def set_mode(self, user, mode, password, code):
        row = self.store.user_get(user["id"])
        mode = str(mode or "").strip().lower()
        problem = mode_allowed(row, mode)
        if problem:
            raise AuthRefused(problem)
        if mode == row["mode"]:
            return False
        self._confirm(row, password, code)
        self.store.user_update(row["id"], mode=mode)
        log.info("mode of %s set to %s", row["username"], mode)
        return True

    #----- Users
    def add_user(self, caller, username, password):
        username = check_username(username)
        password = check_password(password)
        if self.store.user_by_name(username):
            raise AuthRefused("a user named %s already exists" % username, 409)
        user_id = self.store.user_create(username, hash_password(password), "password", None)
        if user_id is None:
            raise AuthRefused("a user named %s already exists" % username, 409)
        log.info("user %s added by %s", username, caller["username"])
        return user_id

    def remove_user(self, caller, username):
        username = str(username or "").strip()
        row = self.store.user_by_name(username)
        if row is None:
            raise AuthRefused("no user named %s" % username, 404)
        if row["id"] == caller["id"]:
            raise AuthRefused("a user cannot remove their own account")
        self.store.sessions_delete_for_user(row["id"])
        self.store.user_delete(row["id"])
        log.info("user %s removed by %s", row["username"], caller["username"])
