"""Route sockets to the right Android network, for AP mode only.

When the phone joins the Go2's own access point, that network has no
internet: the robot's HTTP/WebRTC traffic must go over Wi-Fi while
everything else (the voice provider) goes over cellular. Android only
offers a process-wide binding, so the trick Neon used is to bind, open the
socket, and unbind immediately -- an established TCP connection stays on
its interface.

In STA mode (the default, plan.md section 4.8) the Go2 is on the house
Wi-Fi, one network carries everything, and nothing here is called. Outside
Android every function is a no-op.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger(__name__)

_cm = None
_cellular_network = None
_wifi_network = None


def is_android() -> bool:
    return hasattr(sys, "getandroidapilevel") or "ANDROID_ARGUMENT" in os.environ


def _ensure_cm() -> None:
    global _cm
    if _cm is not None or not is_android():
        return
    from jnius import autoclass  # type: ignore[import-not-found]

    activity = autoclass("org.kivy.android.PythonActivity").mActivity
    context = autoclass("android.content.Context")
    _cm = activity.getSystemService(context.CONNECTIVITY_SERVICE)


def _find_networks() -> None:
    global _cellular_network, _wifi_network
    _ensure_cm()
    if _cm is None:
        return
    from jnius import autoclass  # type: ignore[import-not-found]

    capabilities = autoclass("android.net.NetworkCapabilities")
    for network in _cm.getAllNetworks():
        caps = _cm.getNetworkCapabilities(network)
        if caps is None:
            continue
        if caps.hasTransport(capabilities.TRANSPORT_CELLULAR) and caps.hasCapability(
            capabilities.NET_CAPABILITY_INTERNET
        ):
            _cellular_network = network
        if caps.hasTransport(capabilities.TRANSPORT_WIFI):
            _wifi_network = network


def _bind(network: object | None, label: str) -> None:
    if network is None:
        log.warning("no %s network available", label)
        return
    try:
        _cm.bindProcessToNetwork(network)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 - binding is best effort
        log.warning("%s bind failed: %s", label, exc)


def bind_to_wifi() -> None:
    """Bind the process to Wi-Fi, to reach the robot on its AP."""
    if not is_android():
        return
    _find_networks()
    _bind(_wifi_network, "wifi")


def bind_to_cellular() -> None:
    """Bind the process to cellular, to reach the internet from the robot's AP."""
    if not is_android():
        return
    _find_networks()
    _bind(_cellular_network, "cellular")


def unbind() -> None:
    """Go back to the system's default routing. Always call this after a bind."""
    if not is_android():
        return
    _ensure_cm()
    if _cm is None:
        return
    try:
        _cm.bindProcessToNetwork(None)
    except Exception as exc:  # noqa: BLE001 - unbinding is best effort
        log.warning("unbind failed: %s", exc)
