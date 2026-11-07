import sys, os, tempfile, hashlib
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, SRC)
import settings, webauth
d = tempfile.mkdtemp(); conf = os.path.join(d, "batowatch.conf")
open(conf, "w").write("# my comment\nTEMP_UNIT=F\n\n# login\nLOGIN=batocera\nOVERLAY_ACCENT=nothex\nFOO=bar\n")
s = settings.Settings(conf)
assert s["TEMP_UNIT"] == "F" and s["OVERLAY_ACCENT"] == "#6fd0ff", s.values
print("errors:", s.errors, "unknown:", s.unknown)
r, e = s.save({"TEMP_UNIT": "C", "WEB_PORT": "8687", "LOGIN": "me:pa ss"}, s.mtime)
print("restart:", r, "errors:", e)
txt = open(conf).read(); print(txt)
assert "# my comment" in txt and "FOO=bar" in txt and "LOGIN='me:pa ss'" in txt
try: s.save({"TEMP_UNIT": "F"}, "123"); print("FAIL no stale")
except settings.StaleConf: print("stale refused OK")
print("bad:", s.save({"TEMP_UNIT": "K"}, s.mtime))
# netwatch conversion
nw = os.path.join(d, "netwatch.conf")
open(nw, "w").write('NETWATCH_ARGS="--overlay-scale 1.3 --web-auth bob:pw --subnet-router 10.0.0.2 --show-gateway --weird 5"\nNETWATCH_LOG="/userdata/system/logs/netwatch.log"\nNETWATCH_LOG_KEEP=2\n')
print("convert:", settings.convert_netwatch(nw))
# bash can source the conf
print(os.popen(f"bash -c 'set -e; . {conf}; echo LOGIN=$LOGIN WEB_PORT=$WEB_PORT'").read())
# Digest round trip
a = webauth.Auth("batocera", "echo s3cret", None)
ch = a.challenges()[0]; import re
nonce = re.search(r'nonce="([^"]+)"', ch).group(1)
def resp(pw, algo="SHA-256", fn=hashlib.sha256, uri="/api/state", method="GET"):
    H=lambda x: fn(x.encode()).hexdigest()
    ha1=H(f"root:BatoWatch:{pw}"); ha2=H(f"{method}:{uri}")
    r=H(f"{ha1}:{nonce}:00000001:abc:auth:{ha2}")
    return {"Authorization": f'Digest username="root", realm="BatoWatch", nonce="{nonce}", uri="{uri}", algorithm={algo}, response="{r}", qop=auth, nc=00000001, cnonce="abc"'}
print("good sha256:", a.check("GET", "/api/state", resp("s3cret"), "1.2.3.4"))
print("good md5:", a.check("GET", "/api/state", resp("s3cret","MD5",hashlib.md5), "1.2.3.4"))
print("wrong pw:", a.check("GET", "/api/state", resp("nope"), "1.2.3.4"))
print("wrong uri:", a.check("GET", "/other", resp("s3cret"), "1.2.3.4"))
print("token remote:", a.check("GET", "/", {"X-BatoWatch-Token": a.token}, "1.2.3.4"))
print("token local:", a.check("GET", "/", {"X-BatoWatch-Token": a.token}, "127.0.0.1"))
b = webauth.Auth("batocera", "false", None); print("locked:", b.locked_reason())
c = webauth.Auth("none", "false", None); print("none:", c.check("GET", "/", {}, "1.2.3.4"))

# '#' inside a value is part of it, as in bash (colours were read back as empty)
import tempfile as _tf
_p = os.path.join(_tf.mkdtemp(), "c.conf")
open(_p, "w").write("OVERLAY_ACCENT=#123456\nLOGIN=me:pa#ss # my login\nALERT_ACCENT='#abcdef'\n")
_s = settings.Settings(_p)
assert (_s["OVERLAY_ACCENT"], _s["LOGIN"], _s["ALERT_ACCENT"]) == ("#123456", "me:pa#ss", "#abcdef"), _s.values
_s.save({"OVERLAY_FG": "#0a0b0c"}, _s.mtime)
assert settings.Settings(_p)["OVERLAY_FG"] == "#0a0b0c"
print("hash in values: ok")

# A Digest proof is accepted once; a copy is refused; out-of-order counts are fine
import re as _re
_a = webauth.Auth("me:pw", "true", None)
_ch = _a.challenges()[0]
_nonce = _re.search(r'nonce="([^"]+)"', _ch).group(1)
def _hdr(nc, cnonce, uri="/api/settings", method="POST"):
    H = lambda x: hashlib.sha256(x.encode()).hexdigest()
    ha1, ha2 = H("me:BatoWatch:pw"), H(f"{method}:{uri}")
    resp = H(f"{ha1}:{_nonce}:{nc}:{cnonce}:auth:{ha2}")
    return {"Authorization": f'Digest username="me", realm="BatoWatch", nonce="{_nonce}", uri="{uri}", '
                             f'algorithm=SHA-256, qop=auth, nc={nc}, cnonce="{cnonce}", response="{resp}"'}
_chk = lambda h: _a.check("POST", "/api/settings", h, "192.168.1.5")
assert _chk(_hdr("00000002", "c1")) == "ok"
assert _chk(_hdr("00000001", "c1")) == "ok"          # arrived out of order: fine
assert _chk(_hdr("00000002", "c1")) == "no"          # the same proof again: refused
assert _chk(_hdr("00000001", "c2")) == "ok"          # a new cnonce (curl style): fine
print("digest replay: ok")
