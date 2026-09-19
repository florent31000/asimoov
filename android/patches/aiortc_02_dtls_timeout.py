"""DTLSv1_get_timeout / DTLSv1_handle_timeout are missing on old pyOpenSSL.

Neon: patches/patch_dtls_timeout.py. Without the retransmission timer the
handshake simply never completes on a lossy Wi-Fi link, so fall back to a
fixed 1 s retry rather than to no timer at all.
"""

OLD_GET = """        timeout = None
        if not self.encrypted:
            timeout = self._ssl.DTLSv1_get_timeout()"""

NEW_GET = """        timeout = None
        if not self.encrypted:
            try:
                timeout = self._ssl.DTLSv1_get_timeout()
            except AttributeError:
                timeout = 1.0  # asimoov: no DTLSv1_get_timeout on this pyOpenSSL"""

OLD_HANDLE = "                self._ssl.DTLSv1_handle_timeout()"

NEW_HANDLE = """                try:
                    self._ssl.DTLSv1_handle_timeout()
                except AttributeError:
                    pass  # asimoov: retransmission is driven by the 1 s fallback"""


def apply(source: str) -> str:
    if "asimoov: no DTLSv1_get_timeout" in source:
        return source
    if OLD_GET not in source:
        raise RuntimeError("aiortc/rtcdtlstransport.py: DTLSv1_get_timeout block not found")
    if OLD_HANDLE not in source:
        raise RuntimeError("aiortc/rtcdtlstransport.py: DTLSv1_handle_timeout call not found")
    return source.replace(OLD_GET, NEW_GET, 1).replace(OLD_HANDLE, NEW_HANDLE, 1)
