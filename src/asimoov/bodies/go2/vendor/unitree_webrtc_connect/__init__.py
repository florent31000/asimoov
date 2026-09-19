# Monkey-patch aioice.Connection to use a fixed username and password accross all instances.

import aioice


class Connection(aioice.Connection):
    local_username = aioice.utils.random_string(4)
    local_password = aioice.utils.random_string(22)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.local_username = Connection.local_username
        self.local_password = Connection.local_password


aioice.Connection = Connection  # type: ignore


# Monkey-patch OpenSSL.SSL to add DTLS_METHOD if missing (old pyOpenSSL on Android)
try:
    from OpenSSL import SSL as _SSL
    if not hasattr(_SSL, "DTLS_METHOD"):
        import logging as _mp_logging
        _mp_log = _mp_logging.getLogger("unitree_webrtc_connect")
        from OpenSSL._util import lib as _lib
        _dtls_func = None
        for _fname in ("DTLS_method", "DTLSv1_method", "DTLSv1_2_method"):
            if hasattr(_lib, _fname):
                _dtls_func = getattr(_lib, _fname)
                _mp_log.info(f"Found DTLS via _lib.{_fname}")
                break

        if _dtls_func is not None:
            _SSL.DTLS_METHOD = 7
            _SSL.Context._methods[7] = _dtls_func
            _mp_log.info("Patched SSL.Context._methods with DTLS support")
        else:
            _available = [x for x in dir(_lib) if "tls" in x.lower() or "dtls" in x.lower() or "ssl" in x.lower() or "method" in x.lower()]
            _mp_log.warning(f"No DTLS function found in _lib. Available: {_available[:30]}")
            _SSL.DTLS_METHOD = _SSL.SSLv23_METHOD
    else:
        pass  # DTLS_METHOD already exists
except Exception as _exc:
    import logging as _mp_logging
    _mp_logging.getLogger("unitree_webrtc_connect").warning(f"DTLS patch failed: {_exc}")
    pass


# Monkey-patch aiortc.rtcdtlstransport.X509_DIGEST_ALGORITHMS to remove extra SHA algorithms
# Extra SHA algorithms introduced in aiortc 1.10.0 causes Unity Go2 to use the new SCTP format, despite aiortc using the old SCTP syntax.
# This new format is not supported by aiortc version as of today (2025-06-02)


import aiortc
from packaging.version import Version


if Version(aiortc.__version__) == Version("1.10.0"):
    X509_DIGEST_ALGORITHMS = {
        "sha-256": "SHA256",
    }
    aiortc.rtcdtlstransport.X509_DIGEST_ALGORITHMS = X509_DIGEST_ALGORITHMS

elif Version(aiortc.__version__) >= Version("1.11.0"):
    # Syntax changed in aiortc 1.11.0, so we need to use the hashes module
    from cryptography.hazmat.primitives import hashes

    X509_DIGEST_ALGORITHMS = {
        "sha-256": hashes.SHA256(),  # type: ignore
    }
    aiortc.rtcdtlstransport.X509_DIGEST_ALGORITHMS = X509_DIGEST_ALGORITHMS


# Monkey-patch RTCCertificate._create_ssl_context to convert cryptography objects
# to pyOpenSSL objects (old pyOpenSSL doesn't accept cryptography types directly)
try:
    from OpenSSL import crypto as _crypto
    _orig_create_ssl_context = aiortc.rtcdtlstransport.RTCCertificate._create_ssl_context

    def _patched_create_ssl_context(self, srtp_profiles):
        from OpenSSL import SSL as _pSSL
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

        ctx = _pSSL.Context(_pSSL.DTLS_METHOD)
        ctx.set_verify(
            _pSSL.VERIFY_PEER | _pSSL.VERIFY_FAIL_IF_NO_PEER_CERT, lambda *args: True
        )

        # Convert cryptography cert -> pyOpenSSL X509
        cert_pem = self._cert.public_bytes(Encoding.PEM)
        ossl_cert = _crypto.load_certificate(_crypto.FILETYPE_PEM, cert_pem)
        ctx.use_certificate(ossl_cert)

        # Convert cryptography key -> pyOpenSSL PKey
        key_pem = self._key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())
        ossl_key = _crypto.load_privatekey(_crypto.FILETYPE_PEM, key_pem)
        ctx.use_privatekey(ossl_key)

        ctx.set_cipher_list(
            b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-SHA"
        )
        ctx.set_tlsext_use_srtp(b":".join(x.openssl_profile for x in srtp_profiles))
        return ctx

    aiortc.rtcdtlstransport.RTCCertificate._create_ssl_context = _patched_create_ssl_context
except Exception:
    pass


# Monkey-patch OpenSSL.SSL.Connection to add DTLS-specific methods
# required by aiortc but missing in old pyOpenSSL.
# Uses ctypes to call OpenSSL C functions directly since the CFFI binding
# doesn't define struct timeval.
try:
    from OpenSSL import SSL as _pSSL2
    import ctypes
    import ctypes.util

    _OSSLConn = _pSSL2.Connection

    _libssl_path = ctypes.util.find_library("ssl")
    if _libssl_path:
        _libssl = ctypes.CDLL(_libssl_path)
    else:
        _libssl = ctypes.CDLL("libssl.so")

    class _Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

    if not hasattr(_OSSLConn, "DTLSv1_get_timeout"):
        def _dtls_get_timeout(self):
            """Return DTLS retransmit timeout in seconds, or None."""
            try:
                from OpenSSL._util import ffi as _f, lib as _l
                ssl_ptr = _f.cast("intptr_t", self._ssl)
                ssl_addr = int(ssl_ptr)
                tv = _Timeval()
                ret = _libssl.DTLSv1_get_timeout(ctypes.c_void_p(ssl_addr), ctypes.byref(tv))
                if ret == 1:
                    return tv.tv_sec + tv.tv_usec / 1_000_000.0
            except Exception:
                pass
            return None
        _OSSLConn.DTLSv1_get_timeout = _dtls_get_timeout

    if not hasattr(_OSSLConn, "DTLSv1_handle_timeout"):
        def _dtls_handle_timeout(self):
            """Handle DTLS retransmission timeout."""
            try:
                from OpenSSL._util import ffi as _f, lib as _l
                ssl_ptr = _f.cast("intptr_t", self._ssl)
                ssl_addr = int(ssl_ptr)
                _libssl.DTLSv1_handle_timeout(ctypes.c_void_p(ssl_addr))
            except Exception:
                pass
        _OSSLConn.DTLSv1_handle_timeout = _dtls_handle_timeout

except Exception:
    pass