"""
Known devices: aliases.conf is the list. A device named there never sets off
an unknown-device alert; your own Tailscale machines never do either.

This module reads and edits aliases.conf for the dashboard and the TV app.
Edits change only the line they're about, so comments, examples and the
order of everything else stay as the person left them.
"""
import ipaddress
import os
import re
import tempfile

MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
NAME_MAX = 60


def clean_key(key):
    """A key is a MAC (aa:bb:cc:dd:ee:ff) or an IPv4/IPv6 address."""
    key = (key or "").strip().lower()
    if MAC_RE.match(key):
        return key
    try:
        return str(ipaddress.ip_address(key))
    except ValueError:
        raise ValueError(f"not a MAC or IP address: {key!r}") from None


def clean_name(name):
    """One line, no '#' (the comment marker), not empty."""
    name = " ".join((name or "").replace("#", " ").split())[:NAME_MAX].strip()
    if not name:
        raise ValueError("the name is empty")
    return name


def _key_of(line):
    """The key a line sets, or None for comments, blanks and anything else."""
    body = line.split("#", 1)[0].strip()
    if "=" not in body:
        return None
    k, v = (x.strip() for x in body.split("=", 1))
    if not (k and v):
        return None
    try:
        return clean_key(k)     # so "FE80::0001" and "fe80::1" are the same key
    except ValueError:
        return k.lower()


def read(path):
    """[(key, name)] in file order."""
    out = []
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                k = _key_of(line)
                if k:
                    out.append((k, line.split("#", 1)[0].split("=", 1)[1].strip()))
    except FileNotFoundError:
        pass
    return out


def _lines(path):
    try:
        with open(path, errors="replace") as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


def _write(path, lines):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".aliases.", dir=d)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_name(path, key, name):
    """Add `key = name`, or rename the line that already has the key."""
    key, name = clean_key(key), clean_name(name)
    lines, done = _lines(path), False
    for i, line in enumerate(lines):
        if _key_of(line) == key:
            lines[i] = f"{key} = {name}"
            done = True
            break
    if not done:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"{key} = {name}")
    _write(path, lines)
    return key, name


def add_many(path, entries, heading):
    """Append several (key, name) pairs under a comment heading; keys already
    in the file are left alone. Returns how many were added."""
    have = {k for k, _ in read(path)}
    new = []
    for key, name in entries:
        try:
            key, name = clean_key(key), clean_name(name)
        except ValueError:
            continue
        if key not in have:
            have.add(key)
            new.append(f"{key} = {name}")
    if new:
        lines = _lines(path)
        if lines and lines[-1].strip():
            lines.append("")
        _write(path, lines + [f"# {heading}"] + new)
    return len(new)


def forget(path, keys):
    """Remove the lines for these keys. Returns how many went."""
    keys = {clean_key(k) for k in keys if k}
    lines = _lines(path)
    kept = [line for line in lines if _key_of(line) not in keys]
    if len(kept) != len(lines):
        _write(path, kept)
    return len(lines) - len(kept)
