"""Diagnostics support for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .bridge_client import XComfortBridgeClient

TO_REDACT = {
    # Credentials
    "secret",
    "username",
    "password",
    "pwd",
    "userName",
    # Network / location
    "host",
    "ipAddress",
    "ip",
    "macAddress",
    "mac",
    "location",
    "lat",
    "lon",
    # Hardware identifiers
    "bridge_id",
    "bridge_name",
    "mdnsName",
    "id",
    "connection_id",
    # HA internals
    "entry_id",
    "title",
    "currentUserId",
    "userId",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    entry_data: dict[str, Any] = async_redact_data(
        {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        TO_REDACT,
    )

    client: XComfortBridgeClient | None = getattr(entry, "runtime_data", None)

    if client is None:
        return {
            "entry_data": entry_data,
            "error": "Bridge client not available (integration may not be loaded).",
        }

    diag = async_redact_data(client.get_diagnostics_data(), TO_REDACT)

    # The bridge diagnostics log contains timestamps and login counts —
    # not useful for troubleshooting and potentially identifying.
    if "bridge_diagnostics" in diag:
        diag["bridge_diagnostics"] = "**REDACTED**"

    # home_data.name contains the bridge ID (e.g. "xComfortBridge_00001234_main").
    if isinstance(diag.get("home_data"), dict):
        diag["home_data"].pop("name", None)

    return {
        "entry_data": entry_data,
        **diag,
    }
