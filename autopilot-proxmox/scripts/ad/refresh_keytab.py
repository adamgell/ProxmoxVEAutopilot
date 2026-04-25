"""Refresh the gMSA keytab at /etc/krb5.keytab.

Reads ``msDS-ManagedPassword`` from AD via GSSAPI-LDAP, derives
AES256-CTS-HMAC-SHA1 and AES128-CTS-HMAC-SHA1 keys per RFC 3962,
writes an MIT-format keytab.

Caller must have:

* A valid Kerberos ticket for a principal that's in the gMSA's
  ``msDS-GroupMSAMembership`` ACL (typically a Domain Controller
  computer account running on the DC itself, OR — for one-shots —
  a Domain Admin the operator temporarily added to the list).
* Read access to ``CN=svc-apmon,CN=Managed Service Accounts,…``.

Runs idempotent: writing the same password twice produces an
identical keytab (deterministic from password + salt + kvno).
The refresher script on the DC calls this as a fresh process every
night — cron handles scheduling, this binary handles the crypto.
"""
from __future__ import annotations

import os
import struct
import sys
import time
from pathlib import Path

import ldap
import ldap.sasl
from impacket.krb5.crypto import _AES256CTS, _AES128CTS


# ---------------------------------------------------------------------------
# Defaults — operators can override via env vars.
# ---------------------------------------------------------------------------

DEFAULT_DC = os.environ.get("KEYTAB_DC", "dns.home.gell.one")
DEFAULT_REALM = os.environ.get("KEYTAB_REALM", "HOME.GELL.ONE")
DEFAULT_GMSA_SAM = os.environ.get("KEYTAB_GMSA_SAM", "svc-apmon")
DEFAULT_GMSA_DN = os.environ.get(
    "KEYTAB_GMSA_DN",
    "CN=svc-apmon,CN=Managed Service Accounts,DC=home,DC=gell,DC=one",
)
DEFAULT_OUTPUT = os.environ.get(
    "KEYTAB_PATH",
    str(Path(__file__).resolve().parents[2] / "secrets" / "krb5.keytab"),
)

# Kerberos enctype numbers (IANA / RFC 3961/3962).
ENCTYPE_AES256_CTS_HMAC_SHA1_96 = 18
ENCTYPE_AES128_CTS_HMAC_SHA1_96 = 17
KRB5_NT_PRINCIPAL = 1


# ---------------------------------------------------------------------------
# AD
# ---------------------------------------------------------------------------

def fetch_managed_password(dc: str, gmsa_dn: str) -> tuple[bytes, int]:
    """Return (raw_password_utf16le_bytes, kvno) for the gMSA.

    Caller authenticates with its own Kerberos ticket via GSSAPI.
    Reads ``msDS-ManagedPassword`` (a binary MSDS-MANAGEDPASSWORD_BLOB
    structure) and ``msDS-KeyVersionNumber``. Strips trailing UTF-16
    nulls from the password — the blob pads to 256 bytes but the
    actual password is 240 bytes of random UTF-16 characters.
    """
    l = ldap.initialize(f"ldap://{dc}")
    l.set_option(ldap.OPT_REFERRALS, 0)
    l.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
    l.sasl_interactive_bind_s("", ldap.sasl.gssapi())
    try:
        res = l.search_s(
            gmsa_dn,
            ldap.SCOPE_BASE,
            "(objectClass=msDS-GroupManagedServiceAccount)",
            ["msDS-ManagedPassword", "msDS-KeyVersionNumber",
             "sAMAccountName", "dNSHostName"],
        )
    finally:
        l.unbind_s()
    if not res:
        raise RuntimeError(f"gMSA not found: {gmsa_dn}")
    _dn, attrs = res[0]
    if b"msDS-ManagedPassword" not in {k.encode() if isinstance(k, str) else k
                                       for k in attrs.keys()}:
        # python-ldap returns keys as str on py3
        key_lookup = {k.lower(): v for k, v in attrs.items()}
    else:
        key_lookup = {k.lower(): v for k, v in attrs.items()}
    blob = key_lookup.get("msds-managedpassword", [None])[0]
    if not blob:
        raise RuntimeError(
            "msDS-ManagedPassword empty — the current Kerberos principal "
            "is not in the gMSA's PrincipalsAllowedToRetrieveManagedPassword "
            "list. Add it (Domain Controllers for production refresher, "
            "Domain Admins for one-shots) and re-run."
        )
    kvno_raw = key_lookup.get("msds-keyversionnumber", [b"1"])[0]
    kvno = int(kvno_raw)

    # MSDS-MANAGEDPASSWORD_BLOB header (MS-ADTS 2.2.19):
    #   uint16 Version, uint16 Reserved, uint32 Length,
    #   uint16 CurrentPasswordOffset, uint16 PreviousPasswordOffset,
    #   uint16 QueryPasswordIntervalOffset,
    #   uint16 UnchangedPasswordIntervalOffset,
    #   then the actual password buffers at the offsets.
    _ver, _resv, _total_len, cur_off, _prev_off, _qpi, _upi = struct.unpack(
        "<HHIHHHH", blob[:16],
    )
    # Current password is 256 bytes of UTF-16LE, nul-padded to a
    # fixed length. The AES string-to-key operates on the raw byte
    # sequence, so we only trim trailing null-pairs so the salted
    # hash doesn't include padding.
    pw_raw = blob[cur_off:cur_off + 256]
    while len(pw_raw) >= 2 and pw_raw[-2:] == b"\x00\x00":
        pw_raw = pw_raw[:-2]
    return pw_raw, kvno


# ---------------------------------------------------------------------------
# Kerberos string-to-key per RFC 3962 (AES-CTS-HMAC-SHA1-96)
# ---------------------------------------------------------------------------

def aes_string_to_key(password_utf16le: bytes, salt: bytes, *,
                      key_size: int, iter_count: int = 4096) -> bytes:
    """Derive an AES-CTS-HMAC-SHA1 Kerberos key from the gMSA's
    managed password.

    CRITICAL encoding detail (confirmed against gMSADumper):
    AD's string_to_key input is the UTF-8 encoding of the
    UTF-16LE-decoded password. Passing the raw UTF-16LE bytes
    produces a key that doesn't match AD's stored key
    (preauth failure on kinit -k). The DCSync-visible stored
    AES256 was ``fd3f22aa…`` while raw-UTF16 derivation gave
    ``56f69759…`` — different because AD decodes+re-encodes.
    """
    cls = _AES256CTS if key_size == 32 else _AES128CTS
    # Decode the 256-byte UTF-16LE blob to a Python str (errors='replace'
    # handles any surrogate pairs that accidentally appear in random
    # bytes), then re-encode to UTF-8. 'replace' matches gMSADumper.
    password_utf8 = password_utf16le.decode("utf-16-le", "replace").encode("utf-8")
    key = cls.string_to_key(password_utf8, salt, None)
    return key.contents


# ---------------------------------------------------------------------------
# Keytab writer (MIT format 0x0502)
# ---------------------------------------------------------------------------

def _p_counted(data: bytes) -> bytes:
    """Length-prefixed bytes (uint16 big-endian + data)."""
    return struct.pack(">H", len(data)) + data


def build_keytab_entry(principal_parts: list[str], realm: str,
                       name_type: int, timestamp: int,
                       kvno: int, enctype: int, key: bytes) -> bytes:
    components = b"".join(_p_counted(p.encode("utf-8")) for p in principal_parts)
    body = (
        struct.pack(">H", len(principal_parts))  # component count
        + _p_counted(realm.encode("utf-8"))
        + components
        + struct.pack(">I", name_type)
        + struct.pack(">I", timestamp)
        + struct.pack(">B", kvno & 0xFF)          # 8-bit kvno
        + struct.pack(">H", enctype)
        + _p_counted(key)
        + struct.pack(">I", kvno)                  # full 32-bit kvno (v5.2+)
    )
    return struct.pack(">I", len(body)) + body


def write_keytab(path: Path, *, principal: str, realm: str,
                 kvno: int, aes256_key: bytes, aes128_key: bytes) -> None:
    # Principal can be "svc-apmon$" (single component) or host-style.
    principal_parts = principal.split("/")
    ts = int(time.time())
    entries = b""
    for enctype, key in (
        (ENCTYPE_AES256_CTS_HMAC_SHA1_96, aes256_key),
        (ENCTYPE_AES128_CTS_HMAC_SHA1_96, aes128_key),
    ):
        entries += build_keytab_entry(
            principal_parts, realm, KRB5_NT_PRINCIPAL, ts, kvno, enctype, key,
        )
    # File header: version 0x0502 (MIT keytab v5.2).
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(b"\x05\x02" + entries)
    os.chmod(tmp, 0o600)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_salt(realm: str, sam_account: str) -> bytes:
    """[MS-KILE] § 3.1.1.2: for a computer-class account the Kerberos
    salt is ``<REALM>host<SAM-minus-$>.<realm-lower>``. gMSAs follow
    the computer-account rule."""
    sam = sam_account.rstrip("$")
    return f"{realm}host{sam}.{realm.lower()}".encode("utf-8")


def main() -> int:
    dc = DEFAULT_DC
    realm = DEFAULT_REALM
    sam = DEFAULT_GMSA_SAM
    gmsa_dn = DEFAULT_GMSA_DN
    out = Path(DEFAULT_OUTPUT)

    print(f"fetching msDS-ManagedPassword from {dc} for {gmsa_dn}…")
    pw, kvno = fetch_managed_password(dc, gmsa_dn)
    print(f"  password length: {len(pw)} bytes   kvno: {kvno}")

    salt = build_salt(realm, sam)
    print(f"  salt: {salt.decode('utf-8')}")

    print("deriving AES256 / AES128 keys (RFC 3962, 4096 PBKDF2 iters)…")
    aes256 = aes_string_to_key(pw, salt, key_size=32)
    aes128 = aes_string_to_key(pw, salt, key_size=16)

    principal = f"{sam}$"  # realm appended by write_keytab via separate field
    print(f"writing keytab to {out} (principal {principal}@{realm})…")
    write_keytab(
        out,
        principal=principal, realm=realm, kvno=kvno,
        aes256_key=aes256, aes128_key=aes128,
    )
    print(f"done.  Validate with:  KRB5_TRACE=/dev/stderr kinit -kt {out} {principal}@{realm}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
