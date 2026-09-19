"""get_selected_srtp_profile() is missing on old pyOpenSSL.

Neon: patches/patch_srtp.py. Neon's version fell back to assuming
SRTP_AES128_CM_SHA1_80 when ctypes failed; a wrong profile means silently
undecryptable audio, so this version raises instead.
"""

OLD = "        openssl_profile = self._ssl.get_selected_srtp_profile()"

NEW = """        try:
            openssl_profile = self._ssl.get_selected_srtp_profile()
        except AttributeError:
            # asimoov: old pyOpenSSL, call SSL_get_selected_srtp_profile directly.
            import ctypes

            from OpenSSL._util import ffi as _asimoov_ffi

            _asimoov_libssl = ctypes.CDLL("libssl.so")
            _asimoov_libssl.SSL_get_selected_srtp_profile.restype = ctypes.c_void_p
            _asimoov_ptr = _asimoov_libssl.SSL_get_selected_srtp_profile(
                ctypes.c_void_p(int(_asimoov_ffi.cast("intptr_t", self._ssl._ssl)))
            )
            if not _asimoov_ptr:
                raise RuntimeError("DTLS peer negotiated no SRTP profile")
            openssl_profile = ctypes.cast(
                _asimoov_ptr, ctypes.POINTER(ctypes.c_char_p)
            )[0]"""


def apply(source: str) -> str:
    if "asimoov: old pyOpenSSL, call SSL_get_selected_srtp_profile" in source:
        return source
    if OLD not in source:
        raise RuntimeError("aiortc/rtcdtlstransport.py: get_selected_srtp_profile call not found")
    return source.replace(OLD, NEW, 1)
