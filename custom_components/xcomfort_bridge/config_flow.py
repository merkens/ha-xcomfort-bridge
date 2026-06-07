"""Config flow for the Eaton xComfort Bridge integration."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_USERNAME
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.data_entry_flow import FlowResult

from .bridge_client import (
    XComfortAuthenticationError,
    XComfortConnectionError,
    async_test_connection,
)
from .const import (
    AUTH_MODE_DEVICE,
    AUTH_MODE_USER,
    AUTH_MODES,
    CONF_AUTH_MODE,
    CONF_SECRET,
    DEFAULT_DEVICE_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_HOSTNAME_PATTERN = re.compile(r"^xCB([0-9A-Fa-f]+)LAN$", re.IGNORECASE)


def _parse_bridge_id_from_hostname(hostname: str) -> str | None:
    """Extract bridge ID from DHCP hostname like xCB00001234LAN."""
    if m := _HOSTNAME_PATTERN.match(hostname):
        return m.group(1).upper()
    return None


def _build_mode_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the initial host/auth-mode schema."""
    defaults = defaults or {}

    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
            vol.Required(
                CONF_AUTH_MODE,
                default=defaults.get(CONF_AUTH_MODE, AUTH_MODE_USER),
            ): vol.In(AUTH_MODES),
        }
    )


def _build_credentials_schema(
    auth_mode: str,
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the credential schema for the selected auth mode."""
    defaults = defaults or {}

    fields: dict[Any, Any] = {
        vol.Required(CONF_SECRET, default=defaults.get(CONF_SECRET, "")): str,
    }

    if auth_mode == AUTH_MODE_USER:
        fields = {
            vol.Required(
                CONF_USERNAME,
                default=defaults.get(CONF_USERNAME, ""),
            ): str,
            **fields,
        }

    return vol.Schema(fields)


class XComfortBridgeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Eaton xComfort Bridge."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow state."""
        self._host = ""
        self._auth_mode = AUTH_MODE_USER
        self._discovered_bridge_id: str | None = None

    async def _async_validate_credentials(
        self,
        host: str,
        auth_mode: str,
        username: str,
        secret: str,
    ) -> tuple[dict[str, Any] | None, dict[str, str], str]:
        """Validate credentials against the bridge."""
        errors: dict[str, str] = {}

        if not secret:
            errors["base"] = "secret_required"

        if auth_mode == AUTH_MODE_DEVICE:
            username = DEFAULT_DEVICE_USERNAME
        elif not username:
            errors["base"] = "username_required"

        if errors:
            return None, errors, username

        try:
            bridge_info = await async_test_connection(host, username, secret)
        except XComfortAuthenticationError:
            errors["base"] = "invalid_auth"
            return None, errors, username
        except XComfortConnectionError:
            errors["base"] = "cannot_connect"
            return None, errors, username
        except Exception:
            _LOGGER.exception("Unexpected error during bridge connection test")
            errors["base"] = "unknown_error"
            return None, errors, username

        return bridge_info, errors, username

    async def async_step_dhcp(self, discovery_info: DhcpServiceInfo) -> FlowResult:
        """Handle DHCP discovery of an xComfort bridge."""
        ip = discovery_info.ip
        hostname = discovery_info.hostname or ""
        bridge_id = _parse_bridge_id_from_hostname(hostname)

        if bridge_id is not None:
            await self.async_set_unique_id(bridge_id)

            # If already configured, update IP if it changed
            for entry in self._async_current_entries():
                if entry.unique_id == bridge_id:
                    if entry.data.get(CONF_HOST) != ip:
                        _LOGGER.info(
                            "Bridge %s changed IP from %s to %s, updating",
                            bridge_id,
                            entry.data.get(CONF_HOST),
                            ip,
                        )
                        self.hass.config_entries.async_update_entry(
                            entry, data={**entry.data, CONF_HOST: ip}
                        )
                        self.hass.async_create_task(
                            self.hass.config_entries.async_reload(entry.entry_id)
                        )
                    return self.async_abort(reason="already_configured")
        else:
            # MAC-only match — check if any existing entry uses this IP
            for entry in self._async_current_entries():
                if entry.data.get(CONF_HOST) == ip:
                    return self.async_abort(reason="already_configured")

        # New bridge — store IP and proceed to setup
        self._host = ip
        self._discovered_bridge_id = bridge_id

        self.context["title_placeholders"] = {"name": f"xComfort Bridge ({ip})"}

        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial host/auth-mode step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._auth_mode = user_input[CONF_AUTH_MODE]

            if not self._host:
                errors["base"] = "host_required"
            else:
                return await self.async_step_credentials()

        # Pre-fill host from DHCP discovery
        defaults = user_input or {}
        if self._host and CONF_HOST not in defaults:
            defaults = {**defaults, CONF_HOST: self._host}

        return self.async_show_form(
            step_id="user",
            data_schema=_build_mode_schema(defaults),
            errors=errors,
        )

    async def async_step_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the credential step for the selected auth mode."""
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            secret = user_input[CONF_SECRET].strip()
            bridge_info, errors, username = await self._async_validate_credentials(
                self._host,
                self._auth_mode,
                username,
                secret,
            )

            if not errors:
                assert bridge_info is not None
                bridge_id = bridge_info["bridge_id"]
                bridge_name = bridge_info["bridge_name"]

                await self.async_set_unique_id(bridge_id or self._host)
                self._abort_if_unique_id_configured()

                data = {
                    CONF_HOST: self._host,
                    CONF_AUTH_MODE: self._auth_mode,
                    CONF_USERNAME: username,
                    CONF_SECRET: secret,
                }

                return self.async_create_entry(title=bridge_name, data=data)

        return self.async_show_form(
            step_id="credentials",
            data_schema=_build_credentials_schema(self._auth_mode, user_input),
            errors=errors,
            description_placeholders={
                "auth_mode": AUTH_MODES[self._auth_mode],
            },
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the initial reconfigure step."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._auth_mode = user_input[CONF_AUTH_MODE]

            if not self._host:
                errors["base"] = "host_required"
            else:
                return await self.async_step_reconfigure_credentials()

        # Pre-fill with current config
        defaults = user_input or {
            CONF_HOST: entry.data.get(CONF_HOST, ""),
            CONF_AUTH_MODE: entry.data.get(CONF_AUTH_MODE, AUTH_MODE_USER),
        }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_mode_schema(defaults),
            errors=errors,
        )

    async def async_step_reconfigure_credentials(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> FlowResult:
        """Handle the credential step while reconfiguring an entry."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, "").strip()
            secret = user_input[CONF_SECRET].strip()
            _bridge_info, errors, username = await self._async_validate_credentials(
                self._host,
                self._auth_mode,
                username,
                secret,
            )

            if not errors:
                data = {
                    CONF_HOST: self._host,
                    CONF_AUTH_MODE: self._auth_mode,
                    CONF_USERNAME: username,
                    CONF_SECRET: secret,
                }
                return self.async_update_reload_and_abort(entry, data=data)

        defaults = user_input or {
            CONF_USERNAME: entry.data.get(CONF_USERNAME, ""),
            CONF_SECRET: entry.data.get(CONF_SECRET, ""),
        }

        return self.async_show_form(
            step_id="reconfigure_credentials",
            data_schema=_build_credentials_schema(self._auth_mode, defaults),
            errors=errors,
            description_placeholders={
                "auth_mode": AUTH_MODES[self._auth_mode],
            },
        )
