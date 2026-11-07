"""
Dashboard login for BatoWatch.

LOGIN=batocera (default): user "root" with the box's Batocera root password,
read with `batocera-config getRootPassword` and kept in memory only. It's
read again when the password files change (ES's Security menu) and after a
failed login, so a new password works on the first try without a restart.

HTTP Digest (RFC 7616, SHA-256 and MD5): the browser proves it knows the
password without sending it, so it never crosses the network readable, even
over plain http. Each proof is accepted once, so a copied request can't be
sent again (the rest of a plain-http request isn't protected; HTTPS is on by
default).

The TV app on the box itself logs in with a random token from a root-only
file instead, accepted only from 127.0.0.1 / ::1.
"""
import hashlib
import hmac
import logging
import os
import re
import secrets
import shlex
import subprocess
import threading
import time

LOG = logging.getLogger("batowatch")
REALM = "BatoWatch"
NONCE_LIFE = 600          # s; an older nonce gets stale=true and the browser retries quietly
WATCH_FILES = ("/boot/batocera-boot.conf", "/userdata/system/batocera.conf")
LOCAL_ADDRS = ("127.0.0.1", "::1", "::ffff:127.0.0.1")
ALGOS = (("SHA-256", hashlib.sha256), ("MD5", hashlib.md5))


class PasswordSource:
    """The Batocera root password, in memory only."""

    def __init__(self, command):
        self.command = command
        self.lock = threading.Lock()
        self.password = None
        self.error = None
        self.read_at = 0.0
        self.stamps = None
        self.checked_at = 0.0

    def _stamps(self):
        """A hash of each password file. Not the date: /boot is FAT, which
        keeps times in 2-second steps, so two quick changes can look the same.
        The files are small (a few KB), so hashing them is cheap."""
        out = []
        for path in WATCH_FILES:
            try:
                with open(path, "rb") as fh:
                    out.append(hashlib.sha256(fh.read(1 << 20)).digest())
            except OSError:
                out.append(None)
        return out

    def _read(self):
        try:
            r = subprocess.run(shlex.split(self.command), capture_output=True, text=True,
                               timeout=5)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            return None, f"couldn't run '{self.command}': {e}"
        pw = r.stdout.rstrip("\n")
        if r.returncode != 0 or not pw:
            return None, (f"'{self.command}' failed (exit {r.returncode})"
                          if r.returncode else f"'{self.command}' printed nothing")
        return pw, None

    def refresh(self, force=False):
        """Re-read the password when one of its files changed. The files are
        checked at most every 30 s, and at once when forced (a failed login),
        so a password changed in ES works on the first try."""
        now = time.time()
        with self.lock:
            first = self.password is None and self.error is None
            if not first and not force and now - self.checked_at < 30:
                return
            stamps = self._stamps()
            # unchanged files: nothing to do, unless reading failed last
            # time (then try again once a minute; it may have been brief)
            if not first and stamps == self.stamps and \
                    not (self.error and now - self.read_at > 60):
                self.checked_at = now
                return
            self.stamps = stamps
            self.checked_at = self.read_at = now
            pw, err = self._read()
            if err and err != self.error:
                LOG.warning(f"login: {err}; the dashboard stays locked until it works "
                            f"or LOGIN is set in batowatch.conf")
            elif pw and self.error:
                LOG.info("login: the Batocera password can be read again")
            self.password, self.error = pw, err

    def get(self):
        self.refresh()
        return self.password


class Auth:
    """Checks requests. mode: 'batocera', 'none', or 'user:password'."""

    def __init__(self, mode, password_cmd, token_path):
        self.secret = secrets.token_bytes(32)
        self.source = PasswordSource(password_cmd)
        self.set_mode(mode)
        self.token = secrets.token_hex(24)
        self.token_path = token_path
        self.used = {}          # (nonce, nc, cnonce) -> time: each proof is accepted once
        self.used_lock = threading.Lock()
        self._write_token()

    def set_mode(self, mode):
        self.mode = mode
        if mode == "batocera":
            self.user = "root"
            self.source.refresh(force=True)
        elif mode == "none":
            self.user = None
        else:
            self.user, _, self.own_pw = mode.partition(":")

    def _write_token(self):
        if not self.token_path:
            return
        try:
            fd = os.open(self.token_path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(self.token + "\n")
            os.replace(self.token_path + ".tmp", self.token_path)
        except OSError as e:
            LOG.warning(f"couldn't write the TV app's token: {e}")

    @property
    def required(self):
        return self.mode != "none"

    def password(self):
        if self.mode == "batocera":
            return self.source.get()
        return getattr(self, "own_pw", None)

    def locked_reason(self):
        """Why nobody can log in right now, or None."""
        if self.mode == "batocera" and self.source.get() is None:
            return self.source.error or "the Batocera password couldn't be read"
        return None

    # -- nonces: a signed timestamp, so nothing has to be remembered ---------
    def _nonce(self):
        ts = f"{int(time.time()):x}"
        sig = hmac.new(self.secret, ts.encode(), hashlib.sha256).hexdigest()[:24]
        return f"{ts}.{sig}"

    def _nonce_state(self, nonce):
        ts, _, sig = nonce.partition(".")
        want = hmac.new(self.secret, ts.encode(), hashlib.sha256).hexdigest()[:24]
        if not sig or not hmac.compare_digest(sig, want):
            return "bad"
        try:
            age = time.time() - int(ts, 16)
        except ValueError:
            return "bad"
        return "ok" if 0 <= age <= NONCE_LIFE else "stale"

    def challenges(self, stale=False):
        nonce, opaque = self._nonce(), hashlib.sha256(self.secret).hexdigest()[:16]
        extra = ", stale=true" if stale else ""
        return [f'Digest realm="{REALM}", qop="auth", algorithm={name}, '
                f'nonce="{nonce}", opaque="{opaque}"{extra}' for name, _ in ALGOS]

    def check(self, method, path, headers, client_ip):
        """'ok', 'stale' (ask again quietly) or 'no'."""
        if not self.required:
            return "ok"
        tok = headers.get("X-BatoWatch-Token")
        if tok and client_ip in LOCAL_ADDRS:
            return "ok" if hmac.compare_digest(tok, self.token) else "no"
        h = headers.get("Authorization", "")
        if not h.startswith("Digest "):
            return "no"
        f = dict((k.lower(), v1 or v2) for k, v1, v2 in
                 re.findall(r'(\w+)=(?:"([^"]*)"|([^\s,]+))', h[7:]))
        algo = f.get("algorithm", "MD5").upper()
        fn = dict(ALGOS).get(algo)
        need = ("username", "realm", "nonce", "uri", "response")
        if fn is None or any(k not in f for k in need) or f["realm"] != REALM:
            return "no"
        if f["uri"] != path:
            return "no"
        state = self._nonce_state(f["nonce"])
        if state == "bad":
            return "no"
        if f["username"] != self.user:
            return "no"

        def H(s):
            return fn(s.encode()).hexdigest()

        def expected(pw):
            ha1 = H(f"{self.user}:{REALM}:{pw}")
            ha2 = H(f"{method}:{f['uri']}")
            if f.get("qop") == "auth":
                return H(f"{ha1}:{f['nonce']}:{f.get('nc', '')}:{f.get('cnonce', '')}:auth:{ha2}")
            return H(f"{ha1}:{f['nonce']}:{ha2}")

        pw = self.password()
        ok = pw is not None and hmac.compare_digest(expected(pw), f["response"])
        if not ok and self.mode == "batocera":
            # The password may have just been changed in ES: read it again once
            self.source.refresh(force=True)
            pw = self.source.password
            ok = pw is not None and hmac.compare_digest(expected(pw), f["response"])
        if not ok:
            return "no"
        if state == "stale":
            return "stale"
        # A proof seen before is a copy: refuse it. Browsers count up (nc) with
        # each request on the same nonce; several requests can arrive out of
        # order, so remember each one rather than the highest
        key = (f["nonce"], f.get("nc", ""), f.get("cnonce", ""))
        now = time.time()
        with self.used_lock:
            if key in self.used:
                return "no"
            self.used[key] = now
            if len(self.used) > 2000:
                self.used = {k: t for k, t in self.used.items() if now - t <= NONCE_LIFE}
        return "ok"
