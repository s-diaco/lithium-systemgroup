"""SystemGroup Login.aspx RSA password hashing (Dave Shapiro / TYF PKCS#1 v1.5).

Mirrors Authentication/Login.js ``beforeLogin`` + RSA.js ``encryptedString``:
hashedPassword = encryptedString(RSAKeyPair(rsa_e, "", rsa_m), sessionid + "--" + password)
"""

from __future__ import annotations

import random
import secrets
from collections.abc import Callable


# Login.js unicodeCorrectInvalidCharacters: Arabic yeh/keh → Farsi yeh/keh
_ARABIC_YEH = "\u064a"
_ARABIC_KEH = "\u0643"
_FARSI_YEH = "\u06cc"
_FARSI_KEH = "\u06a9"


def unicode_correct_invalid_characters(text: str | None) -> str | None:
    """Match Login.js ``unicodeCorrectInvalidCharacters``."""
    if text is None or not isinstance(text, str) or len(text) == 0:
        return text
    return text.replace(_ARABIC_YEH, _FARSI_YEH).replace(_ARABIC_KEH, _FARSI_KEH)


def string_to_utf8_byte_array(s: str) -> bytes:
    """Match RSA.js ``stringToUtf8ByteArray`` (UTF-8)."""
    return s.encode("utf-8")


def bi_to_hex(value: int) -> str:
    """Match BigInt.js ``biToHex``: lowercase hex, 4-char digit groups, no 0x."""
    if value == 0:
        return "0000"
    h = format(value, "x")
    rem = len(h) % 4
    if rem:
        h = ("0" * (4 - rem)) + h
    return h


def digit_size_for_modulus(n: int) -> int:
    """Match RSA.js ``digitSize = 2 * biHighIndex(m) + 2`` (bytes in the padded block)."""
    # biHighIndex for n: number of 16-bit digits minus trailing zeros.
    if n <= 0:
        raise ValueError("modulus must be positive")
    bit_len = n.bit_length()
    # digits used = ceil(bit_len / 16); high index = digits - 1
    num_digits = (bit_len + 15) // 16
    high_index = max(num_digits - 1, 0)
    return 2 * high_index + 2


def encrypted_string(
    plaintext: str,
    *,
    exponent_hex: str,
    modulus_hex: str,
    randbelow: Callable[[int], int] | None = None,
) -> str:
    """PKCS#1 v1.5 type-2 RSA encrypt matching RSA.js ``encryptedString`` (TYF).

    Returns space-separated lowercase hex ciphertext chunk(s).
    ``randbelow(254)`` should return 0..253 inclusive (mapped to pad byte 1..254);
    defaults to ``secrets.randbelow``.
    """
    e = int(exponent_hex, 16)
    n = int(modulus_hex, 16)
    if e <= 0 or n <= 0:
        raise ValueError("Invalid RSA public key.")

    digit_size = digit_size_for_modulus(n)
    chunk_size = digit_size - 11
    if chunk_size <= 0:
        return "Error"

    data = string_to_utf8_byte_array(plaintext)
    if not data:
        return ""
    _randbelow = randbelow or secrets.randbelow

    chunks: list[str] = []
    for i in range(0, len(data), chunk_size):
        msg = data[i : i + chunk_size]
        msg_length = len(msg)
        # EM big-endian: 0x00 || 0x02 || PS || 0x00 || M  (PKCS#1 v1.5 type 2)
        # TYF builds the little-endian digit view of this same layout.
        need = digit_size - 3 - msg_length
        if need < 8:
            raise ValueError("Message too long for RSA modulus.")
        ps = bytes(_randbelow(254) + 1 for _ in range(need))
        em = b"\x00\x02" + ps + b"\x00" + msg
        m_int = int.from_bytes(em, "big")
        if m_int >= n:
            raise ValueError("PKCS#1 encoding >= modulus; check digit_size.")
        c_int = pow(m_int, e, n)
        chunks.append(bi_to_hex(c_int))

    return " ".join(chunks)


def build_hashed_password(
    password: str,
    *,
    session_id: str,
    exponent_hex: str,
    modulus_hex: str,
    randbelow: Callable[[int], int] | None = None,
) -> str:
    """``encryptedString(key, sessionid + "--" + password)`` after unicode correction."""
    corrected = unicode_correct_invalid_characters(password) or ""
    sid = session_id or ""
    return encrypted_string(
        f"{sid}--{corrected}",
        exponent_hex=exponent_hex,
        modulus_hex=modulus_hex,
        randbelow=randbelow,
    )


def deterministic_randbelow(seed: int) -> Callable[[int], int]:
    """Test helper: stable PKCS pad stream."""
    rng = random.Random(seed)

    def _rb(n: int) -> int:
        return rng.randrange(n)

    return _rb
