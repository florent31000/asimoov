"""
Encryption utilities for Unitree Go2 WebRTC connection.

Uses the `cryptography` library with explicit backend parameter
for compatibility with older versions (2.x) bundled by python-for-android.
"""

import base64
import uuid
import binascii

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives import serialization

_BACKEND = default_backend()


# ── AES ──

def _generate_uuid() -> str:
    return binascii.hexlify(uuid.uuid4().bytes).decode('utf-8')


def pad(data: str) -> bytes:
    block_size = 16
    p = block_size - len(data) % block_size
    return (data + chr(p) * p).encode('utf-8')


def unpad(data: bytes) -> str:
    p = data[-1]
    return data[:-p].decode('utf-8')


def aes_encrypt(data: str, key: str) -> str:
    key_bytes = key.encode('utf-8')
    padded = pad(data)
    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=_BACKEND)
    enc = cipher.encryptor()
    ct = enc.update(padded) + enc.finalize()
    return base64.b64encode(ct).decode('utf-8')


def aes_decrypt(encrypted_data: str, key: str) -> str:
    key_bytes = key.encode('utf-8')
    ct = base64.b64decode(encrypted_data)
    cipher = Cipher(algorithms.AES(key_bytes), modes.ECB(), backend=_BACKEND)
    dec = cipher.decryptor()
    pt = dec.update(ct) + dec.finalize()
    return unpad(pt)


def generate_aes_key() -> str:
    return _generate_uuid()


# ── RSA ──

class _RsaKeyWrapper:
    """Thin wrapper so existing code can call key.size_in_bytes()."""
    def __init__(self, public_key):
        self._key = public_key

    def size_in_bytes(self):
        return self._key.key_size // 8

    @property
    def key(self):
        return self._key


def rsa_load_public_key(pem_data: str):
    key_bytes = base64.b64decode(pem_data)
    pub = serialization.load_der_public_key(key_bytes, backend=_BACKEND)
    return _RsaKeyWrapper(pub)


def rsa_encrypt(data: str, public_key_wrapper) -> str:
    pub = public_key_wrapper.key
    max_chunk = public_key_wrapper.size_in_bytes() - 11
    data_bytes = data.encode('utf-8')

    encrypted = bytearray()
    for i in range(0, len(data_bytes), max_chunk):
        chunk = data_bytes[i:i + max_chunk]
        enc_chunk = pub.encrypt(chunk, asym_padding.PKCS1v15())
        encrypted.extend(enc_chunk)

    return base64.b64encode(bytes(encrypted)).decode('utf-8')
