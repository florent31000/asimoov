"""pyOpenSSL built by p4a has no SSL.DTLS_METHOD; pick the best available.

Neon: patches/patch_dtls.py.
"""

OLD = "from OpenSSL import SSL"

NEW = '''from OpenSSL import SSL

# asimoov: the pyOpenSSL python-for-android builds predates DTLS_METHOD.
if not hasattr(SSL, "DTLS_METHOD"):
    if hasattr(SSL, "DTLSv1_METHOD"):
        SSL.DTLS_METHOD = SSL.DTLSv1_METHOD
    else:
        from OpenSSL._util import lib as _asimoov_lib

        if hasattr(_asimoov_lib, "DTLS_method") or hasattr(_asimoov_lib, "DTLSv1_method"):
            SSL.DTLS_METHOD = 7
        else:
            raise ImportError(
                "this OpenSSL build exposes no DTLS method; the Go2 body cannot work"
            )'''


def apply(source: str) -> str:
    if "asimoov: the pyOpenSSL" in source:
        return source
    if OLD not in source:
        raise RuntimeError("aiortc/rtcdtlstransport.py: 'from OpenSSL import SSL' not found")
    return source.replace(OLD, NEW, 1)
