"""get_peer_certificate(as_cryptography=True) is not accepted by old pyOpenSSL.

Neon: patches/patch_peer_cert.py. Convert the OpenSSL object to a
`cryptography` certificate by hand so the DTLS fingerprint check still runs.
"""

OLD = "        certificate = self._ssl.get_peer_certificate(as_cryptography=True)"

NEW = """        try:
            certificate = self._ssl.get_peer_certificate(as_cryptography=True)
        except TypeError:
            # asimoov: old pyOpenSSL, convert through PEM.
            from cryptography.hazmat.backends import default_backend
            from cryptography.x509 import load_pem_x509_certificate
            from OpenSSL import crypto as _asimoov_crypto

            _asimoov_cert = self._ssl.get_peer_certificate()
            certificate = (
                load_pem_x509_certificate(
                    _asimoov_crypto.dump_certificate(
                        _asimoov_crypto.FILETYPE_PEM, _asimoov_cert
                    ),
                    default_backend(),
                )
                if _asimoov_cert is not None
                else None
            )"""


def apply(source: str) -> str:
    if "asimoov: old pyOpenSSL, convert through PEM" in source:
        return source
    if OLD not in source:
        raise RuntimeError("aiortc/rtcdtlstransport.py: get_peer_certificate call not found")
    return source.replace(OLD, NEW, 1)
