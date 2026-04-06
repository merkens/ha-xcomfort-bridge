"""Runtime client for the Eaton xComfort Bridge integration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import string
from base64 import b64decode, b64encode
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Callable

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    BATTERY_CODE_MAINS,
    BATTERY_CODE_MAP,
    COMPTYPE_DOOR_WINDOW_SENSOR,
    CONF_SECRET,
    COVER_DEVICE_TYPES,
    DOMAIN,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RECONNECT_MAX,
    DEFAULT_STARTUP_TIMEOUT,
    DEVTYPE_HEATER_CHANNEL,
    DEVTYPE_ROOM_CONTROLLER,
    DEVTYPE_SWITCH,
    DOOR_WINDOW_MODE_DOOR,
    EVENT_DEVICE_TYPES,
    INFO_CODE_DEVICE_TEMPERATURE,
    INFO_CODE_RCT_HUMIDITY,
    INFO_CODE_RCT_TEMPERATURE,
    INFO_CODE_SIGNAL_STRENGTH,
    INFO_CODE_SUM_REQUEST,
    LIGHT_DEVICE_TYPES,
    MESSAGE_ACK,
    MESSAGE_ACTIVATE_SCENE,
    MESSAGE_ACTION_SHADING_DEVICE,
    MESSAGE_ACTION_SLIDE_DEVICE,
    MESSAGE_ACTION_SWITCH_DEVICE,
    MESSAGE_AUTH_APPLY_TOKEN,
    MESSAGE_AUTH_APPLY_TOKEN_RESPONSE,
    MESSAGE_AUTH_LOGIN,
    MESSAGE_AUTH_LOGIN_DENIED,
    MESSAGE_AUTH_LOGIN_SUCCESS,
    MESSAGE_AUTH_RENEW_TOKEN,
    MESSAGE_AUTH_RENEW_TOKEN_RESPONSE,
    MESSAGE_CONNECT_CONFIRM,
    MESSAGE_DIAGNOSTICS,
    MESSAGE_HEARTBEAT,
    MESSAGE_HOME_DATA,
    MESSAGE_INITIAL_DATA,
    MESSAGE_SC_ESTABLISHED,
    MESSAGE_SC_INIT,
    MESSAGE_SC_SECRET,
    MESSAGE_SET_ALL_DATA,
    MESSAGE_SET_BRIDGE_STATE,
    MESSAGE_SET_DEVICE_STATE,
    MESSAGE_SET_DIAGNOSTICS,
    MESSAGE_SET_HEATING_STATE,
    MESSAGE_SET_HOME_DATA,
    MESSAGE_SET_ROOM_HEATING_STATE,
    MESSAGE_SET_STATE_INFO,
    SHADE_OP_CLOSE,
    SHADE_OP_GO_TO,
    SHADE_OP_OPEN,
    SHADE_OP_STOP,
    SIGNAL_LABELS,
    USAGE_COOLING,
    USAGE_HEATING,
)

_LOGGER = logging.getLogger(__name__)

CLIENT_TYPE = "shl-app"
CLIENT_ID = "c956e43f999f8004"
CLIENT_VERSION = "3.0.0"

COMPONENT_MODELS = {
    1: "Pushbutton 1-fold",
    2: "Pushbutton 2-fold",
    3: "Pushbutton 4-fold",
    19: "Binary Input 230V",
    20: "Binary Input Battery",
    23: "Temperature Sensor",
    29: "Motion Sensor",
    48: "Remote Control 2-fold",
    49: "Remote Control 12-fold",
    65: "Heating Valve",
    71: "Multi Heating Actuator",
    74: "Switching Actuator",
    76: "Door/Window Sensor",
    77: "Dimming Actuator",
    78: "RC Touch",
    81: "Heating Actuator",
    83: "xComfort Bridge",
    84: "LeakageStop",
    85: "Water Sensor",
    86: "Shutter Actuator",
    87: "Pushbutton Multisensor 1-fold",
    88: "Pushbutton Multisensor 2-fold",
    89: "Pushbutton Multisensor 4-fold",
    90: "Weather Station",
}

DEVICE_MODELS = {
    100: "Switching Actuator",
    101: "Dimming Actuator",
    102: "Shutter Actuator",
    201: "Built-in Pushbutton",
    220: "Rocker",
    410: "Temperature Sensor",
    440: "Heating Actuator",
    441: "Heating Valve",
    442: "Multi Heating Actuator Channel",
    450: "RC Touch",
    451: "Temperature/Humidity Sensor",
    460: "Actuator Router",
    497: "Water Guard",
    499: "Water Sensor",
    510: "Weather Station",
}


class XComfortBridgeError(Exception):
    """Base exception for xComfort bridge errors."""


class XComfortConnectionError(XComfortBridgeError):
    """Raised when the bridge connection cannot be established or maintained."""


class XComfortAuthenticationError(XComfortBridgeError):
    """Raised when bridge authentication fails."""


@dataclass(slots=True)
class ButtonEvent:
    """Raw button event emitted by the bridge runtime."""

    device_id: int
    event_type: str
    value: int | bool | None
    raw: dict[str, Any]


def _generate_salt() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12))


def _hash_secret(device_id: bytes, secret: bytes, salt: bytes) -> str:
    inner = hashlib.sha256(device_id + secret).hexdigest().encode()
    return hashlib.sha256(salt + inner).hexdigest()


def _coerce_measurement(value: str | float | int | None) -> float | None:
    """Return a float measurement unless the bridge marks it unavailable."""
    if value is None:
        return None

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None

    if numeric == -100.0:
        return None

    return numeric


def _coerce_int(value: str | float | int | None) -> int | None:
    """Return an int when possible."""
    if value is None:
        return None

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


_AES_BLOCK = 16


def _pad_to_block(data: bytes) -> bytes:
    pad_size = _AES_BLOCK - (len(data) % _AES_BLOCK)
    return data + b"\x00" * pad_size


def _aes_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _aes_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return decryptor.update(data) + decryptor.finalize()


class XComfortBridgeClient:
    """Own the xComfort transport, auth, parsing, and central state store."""

    def __init__(
        self,
        hass: HomeAssistant | None,
        entry: ConfigEntry | None,
        *,
        host: str | None = None,
        username: str | None = None,
        secret: str | None = None,
    ) -> None:
        """Initialize the runtime.

        For normal operation pass hass and entry.  For config-flow probing
        pass host/username/secret directly (hass and entry may be None).
        """
        self.hass = hass
        self.entry = entry
        self.host: str = host or (entry.data[CONF_HOST] if entry else "")
        self.username: str = username or (entry.data[CONF_USERNAME] if entry else "")
        self.secret: str = secret or (entry.data[CONF_SECRET] if entry else "")

        self.bridge_id: str | None = None
        self.bridge_name: str | None = None
        self.bridge_model: str = "xComfort Bridge"
        self.firmware_version: str | None = None

        self._available = False
        self._authenticated = False
        self._stop_event = asyncio.Event()
        self._initial_data_loaded = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None
        self._startup_future: asyncio.Future[None] | None = None

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._aes_key: bytes | None = None
        self._aes_iv: bytes | None = None
        self._message_counter = 0

        self._devices: dict[int, dict[str, Any]] = {}
        self._components: dict[int, dict[str, Any]] = {}
        self._rooms: dict[int, dict[str, Any]] = {}
        self._room_heating: dict[int, dict[str, Any]] = {}
        self._room_state: dict[int, dict[str, Any]] = {}
        self._scenes: dict[int, dict[str, Any]] = {}
        self._home_data: dict[str, Any] = {}
        self._slave_bridges: dict[int, dict[str, Any]] = {}
        self._bridge_state: dict[str, Any] = {}
        self._diagnostics: dict[str, Any] = {}

        self._device_listeners: defaultdict[int, set[Callable[[], None]]] = defaultdict(set)
        self._button_listeners: defaultdict[int, set[Callable[[ButtonEvent], None]]] = defaultdict(set)
        self._room_listeners: defaultdict[int, set[Callable[[], None]]] = defaultdict(set)
        self._bridge_listeners: set[Callable[[], None]] = set()

    @property
    def available(self) -> bool:
        """Return whether the bridge is currently connected."""
        return self._available

    @property
    def bridge_device_identifier(self) -> str:
        """Return the identifier used for the bridge device entry."""
        return self.bridge_id or self.host

    @property
    def unique_prefix(self) -> str:
        """Return a stable prefix for entity unique IDs."""
        return self.bridge_device_identifier

    @property
    def slave_bridges(self) -> dict[int, dict[str, Any]]:
        """Return discovered slave bridges keyed by clientId."""
        return self._slave_bridges

    def get_bridge_identifier_for_device(self, device_id: int) -> str:
        """Return the bridge identifier that owns a given device's component."""
        component = self._get_component_for_device(device_id)
        if component is not None:
            client_id = _coerce_int(component.get("clientId")) or 0
            if client_id != 0 and client_id in self._slave_bridges:
                slave = self._slave_bridges[client_id]
                return slave.get("mdnsName", f"slave_{client_id}")
        return self.bridge_device_identifier

    @classmethod
    def create_probe_client(
        cls,
        host: str,
        username: str,
        secret: str,
    ) -> XComfortBridgeClient:
        """Create a minimal client instance for config-flow probing."""
        return cls(None, None, host=host, username=username, secret=secret)

    async def async_start(self) -> None:
        """Start the supervised bridge runtime.

        Requires hass and entry — must not be called on a probe client.
        """
        if self.hass is None or self.entry is None:
            raise XComfortBridgeError("async_start requires hass and entry (not a probe client)")
        if self._run_task is not None:
            return

        self._stop_event.clear()
        self._startup_future = asyncio.get_running_loop().create_future()
        self._run_task = self.hass.async_create_background_task(
            self._run_forever(),
            f"xcomfort_bridge runtime {self.entry.entry_id}",
        )

        try:
            await asyncio.wait_for(
                asyncio.shield(self._startup_future),
                timeout=DEFAULT_STARTUP_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            await self.async_stop()
            raise XComfortConnectionError(
                f"Timed out waiting for bridge startup at {self.host}"
            ) from exc
        except Exception:
            await self.async_stop()
            raise

    async def async_stop(self) -> None:
        """Stop the runtime and close any active connection."""
        self._stop_event.set()

        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass
            finally:
                self._run_task = None

        await self._cleanup_connection()
        self._set_available(False)

    def get_device(self, device_id: int) -> dict[str, Any] | None:
        """Return a device entry from the central state store."""
        return self._devices.get(device_id)

    def get_component(self, comp_id: int) -> dict[str, Any] | None:
        """Return a component entry from the central state store."""
        return self._components.get(comp_id)

    def get_light_device_ids(self) -> list[int]:
        """Return all currently known light-like device IDs."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if device.get("devType") in LIGHT_DEVICE_TYPES
        )

    def get_cover_device_ids(self) -> list[int]:
        """Return all currently known shading actuator device IDs."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if device.get("devType") in COVER_DEVICE_TYPES
        )

    def get_cover_position(self, device_id: int) -> int | None:
        """Return the current shade position (0 = open, 100 = closed)."""
        device = self.get_device(device_id)
        if device is None:
            return None
        value = device.get("shPos")
        return _coerce_int(value) if value is not None else None

    def get_cover_safety(self, device_id: int) -> bool:
        """Return whether the shade safety flag is active."""
        device = self.get_device(device_id)
        if device is None:
            return False
        return _coerce_int(device.get("shSafety", 0)) != 0

    def cover_supports_position(self, device_id: int) -> bool:
        """Return whether the shade supports go-to-position control."""
        device = self.get_device(device_id)
        if device is None:
            return False
        return device.get("shRuntime") == 1

    def get_power_device_ids(self) -> list[int]:
        """Return all currently known devices with built-in power metering."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if bool(device.get("hasPower"))
        )

    def get_binary_sensor_device_ids(self) -> list[int]:
        """Return device IDs for door/window sensor components."""
        result: list[int] = []
        for device_id, device in self._devices.items():
            if device.get("devType") != DEVTYPE_SWITCH:
                continue
            comp = self._get_component_for_device(device_id)
            if comp is not None and comp.get("compType") == COMPTYPE_DOOR_WINDOW_SENSOR:
                result.append(device_id)
        return sorted(result)

    def get_binary_sensor_is_on(self, device_id: int) -> bool | None:
        """Return whether a door/window sensor reports open."""
        device = self.get_device(device_id)
        if device is None:
            return None
        return bool(device.get("switch"))

    def get_binary_sensor_is_door(self, device_id: int) -> bool:
        """Return whether the sensor is a door sensor (vs window)."""
        component = self._get_component_for_device(device_id)
        if component is None:
            return False
        return component.get("mode") == DOOR_WINDOW_MODE_DOOR

    def get_event_device_ids(self) -> list[int]:
        """Return all currently known pushbutton/rocker device IDs."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if device.get("devType") in EVENT_DEVICE_TYPES
        )

    def get_rct_device_ids(self) -> list[int]:
        """Return all currently known RC Touch device IDs."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if device.get("devType") == DEVTYPE_ROOM_CONTROLLER
        )

    def get_scene_ids(self) -> list[int]:
        """Return all scene IDs that should be exposed."""
        return sorted(self._scenes)

    def get_scene(self, scene_id: int) -> dict[str, Any] | None:
        """Return scene data by ID."""
        return self._scenes.get(scene_id)

    def get_room_ids(self) -> list[int]:
        """Return all currently known room IDs."""
        return sorted(set(self._rooms) | set(self._room_heating) | set(self._room_state))

    def get_room(self, room_id: int) -> dict[str, Any] | None:
        """Return merged room config, heating config, and live room state."""
        merged: dict[str, Any] = {}

        if room_id in self._rooms:
            merged.update(self._rooms[room_id])
        if room_id in self._room_heating:
            merged.update(self._room_heating[room_id])
        if room_id in self._room_state:
            merged.update(self._room_state[room_id])

        if not merged:
            return None

        return merged

    def get_room_heating(self, room_id: int) -> dict[str, Any] | None:
        """Return the static heating config for a room."""
        return self._room_heating.get(room_id)

    def get_room_name(self, room_id: int) -> str:
        """Return a room name suitable for entities and devices."""
        room = self.get_room(room_id)
        if room is None:
            return f"Room {room_id}"
        return str(room.get("name") or f"Room {room_id}")

    def get_room_identifier(self, room_id: int) -> str:
        """Return the stable device-registry identifier for a room."""
        return f"room_{self.unique_prefix}_{room_id}"

    def room_has_climate(self, room_id: int) -> bool:
        """Return whether a room should expose a climate entity."""
        room = self.get_room(room_id)
        return bool(room) and not room.get("temperatureOnly", True)

    def room_has_heating(self, room_id: int) -> bool:
        """Match Eaton app isRoomHeatingAllowed: explicit fields, then device usage scan."""
        room = self.get_room(room_id)
        if not room:
            return False
        if room.get("sumActuatorId") or room.get("modeSwitchHeating"):
            return True
        return self._room_has_device_usage(room_id, USAGE_HEATING)

    def room_has_cooling(self, room_id: int) -> bool:
        """Match Eaton app isRoomCoolingAllowed: explicit fields, then device usage scan."""
        room = self.get_room(room_id)
        if not room:
            return False
        if room.get("sumCoolingId") or room.get("modeSwitchCooling"):
            return True
        return self._room_has_device_usage(room_id, USAGE_COOLING)

    def _room_has_device_usage(self, room_id: int, usages: frozenset[int]) -> bool:
        """Check if any device assigned to a room has a matching usage."""
        for device in self._devices.values():
            if device.get("tempRoom") == room_id:
                usage = device.get("usage")
                if usage is not None and usage in usages:
                    return True
        return False

    def get_linked_room_id(self, device_id: int) -> int | None:
        """Resolve the room linked to an RC Touch device."""
        device = self.get_device(device_id)
        if device is None:
            return None

        room_id_int = _coerce_int(device.get("tempRoom"))
        if room_id_int is None or not self._room_exists(room_id_int):
            return None

        return room_id_int

    def get_room_sensor_device_id(self, room_id: int) -> int | None:
        """Return the linked RC Touch device ID for a room when known."""
        room = self.get_room(room_id) or {}
        sensor_id_int = _coerce_int(room.get("roomSensorId"))
        if sensor_id_int is not None and sensor_id_int in self._devices:
            return sensor_id_int

        for device_id in self.get_rct_device_ids():
            if self.get_linked_room_id(device_id) == room_id:
                return device_id

        return None

    def get_device_info_value(self, device_id: int, code: str) -> str | None:
        """Return a device info value by info code."""
        device = self.get_device(device_id) or {}

        for item in device.get("info", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("text")) == code and "value" in item:
                value = item.get("value")
                return None if value is None else str(value)

        return None

    def get_rct_temperature(self, device_id: int) -> float | None:
        """Return the RC Touch temperature or None if unavailable."""
        value = self.get_device_info_value(device_id, INFO_CODE_RCT_TEMPERATURE)
        return _coerce_measurement(value)

    def get_rct_humidity(self, device_id: int) -> float | None:
        """Return the RC Touch humidity or None if unavailable."""
        value = self.get_device_info_value(device_id, INFO_CODE_RCT_HUMIDITY)
        return _coerce_measurement(value)

    def get_actuator_temperature(self, device_id: int) -> float | None:
        """Return the built-in temperature reading from an actuator."""
        value = self.get_device_info_value(device_id, INFO_CODE_DEVICE_TEMPERATURE)
        return _coerce_measurement(value)

    def get_actuator_temperature_device_ids(self) -> list[int]:
        """Return device IDs that report a built-in temperature (info code 1109)."""
        return sorted(
            device_id
            for device_id in self._devices
            if self.get_device_info_value(device_id, INFO_CODE_DEVICE_TEMPERATURE)
            is not None
        )

    # ------------------------------------------------------------------
    # Battery & signal (component-level info codes)
    # ------------------------------------------------------------------

    def _get_primary_device_ids(self) -> list[int]:
        """Return one representative device_id per physical component."""
        seen_comps: set[int] = set()
        result: list[int] = []
        for device_id in sorted(self._devices):
            comp_id = _coerce_int(self._devices[device_id].get("compId"))
            if comp_id is None or comp_id in seen_comps:
                continue
            seen_comps.add(comp_id)
            result.append(device_id)
        return result

    def get_component_battery_percentage(self, device_id: int) -> int | None:
        """Return battery percentage for the component, or None."""
        component = self._get_component_for_device(device_id)
        if component is None:
            return None
        for item in component.get("info", []):
            code = str(item.get("text", ""))
            if code in BATTERY_CODE_MAP:
                return BATTERY_CODE_MAP[code]
        return None

    def get_component_signal_label(self, device_id: int) -> str | None:
        """Return signal strength label for the component, or None."""
        component = self._get_component_for_device(device_id)
        if component is None:
            return None
        for item in component.get("info", []):
            if str(item.get("text", "")) == INFO_CODE_SIGNAL_STRENGTH:
                value = str(item.get("value", ""))
                return SIGNAL_LABELS.get(value)
        return None

    def is_mains_powered(self, device_id: int) -> bool:
        """Return True if the component is mains-powered."""
        component = self._get_component_for_device(device_id)
        if component is None:
            return False
        return any(
            str(item.get("text", "")) == BATTERY_CODE_MAINS
            for item in component.get("info", [])
        )

    def get_battery_device_ids(self) -> list[int]:
        """Return primary device IDs for battery-powered components."""
        return sorted(
            device_id
            for device_id in self._get_primary_device_ids()
            if not self.is_mains_powered(device_id)
            and self.get_component_battery_percentage(device_id) is not None
        )

    def get_signal_device_ids(self) -> list[int]:
        """Return primary device IDs for components with signal info."""
        return sorted(
            device_id
            for device_id in self._get_primary_device_ids()
            if self.get_component_signal_label(device_id) is not None
        )

    # ------------------------------------------------------------------
    # Multi heating actuator (devType 442 channels on compType 71 hub)
    # ------------------------------------------------------------------

    def get_component_sum_request(self, device_id: int) -> int | None:
        """Return the aggregate heating sum-request value for the component."""
        component = self._get_component_for_device(device_id)
        if component is None:
            return None
        for item in component.get("info", []):
            if str(item.get("text", "")) == INFO_CODE_SUM_REQUEST:
                return _coerce_int(item.get("value"))
        return None

    def get_sum_request_device_ids(self) -> list[int]:
        """Return primary device IDs for components that report sum-request."""
        return sorted(
            device_id
            for device_id in self._get_primary_device_ids()
            if self.get_component_sum_request(device_id) is not None
        )

    def get_heater_channel_device_ids(self) -> list[int]:
        """Return all multi-heating-actuator channel device IDs."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if device.get("devType") == DEVTYPE_HEATER_CHANNEL
        )

    def get_heater_channel_valve(self, device_id: int) -> int | None:
        """Return the valve position 0-100 for a heater channel device."""
        device = self.get_device(device_id)
        if device is None:
            return None
        return _coerce_int(device.get("dimmvalue"))

    def get_component_id(self, device_id: int) -> int | None:
        """Return the comp_id for a device, or None."""
        device = self.get_device(device_id)
        if device is None:
            return None
        return _coerce_int(device.get("compId"))

    def get_device_power(self, device_id: int) -> float | None:
        """Return the current device power in watts when metering is supported."""
        device = self.get_device(device_id) or {}
        if not device.get("hasPower"):
            return None
        return _coerce_measurement(device.get("power"))

    def get_room_temperature(self, room_id: int) -> float | None:
        """Return the current room temperature or None if unavailable."""
        return self._get_room_measurement(room_id, "temp")

    def get_room_humidity(self, room_id: int) -> float | None:
        """Return the current room humidity or None if unavailable."""
        return self._get_room_measurement(room_id, "humidity")

    def get_room_setpoint(self, room_id: int) -> float | None:
        """Return the current room setpoint when available."""
        return self._get_room_measurement(room_id, "setpoint")

    def get_room_state_code(self, room_id: int) -> int | None:
        """Return the bridge climate state code for a room."""
        return self._get_room_int(room_id, "state")

    def get_room_mode_code(self, room_id: int) -> int | None:
        """Return the bridge climate mode code for a room."""
        return self._get_room_int(room_id, "mode", "currentMode")

    def get_room_valve(self, room_id: int) -> int | None:
        """Return the current valve/heating demand percentage for a room."""
        return self._get_room_int(room_id, "valve", "currentValve")

    def get_room_power(self, room_id: int) -> float | None:
        """Return the current room power reading when available."""
        return self._get_room_measurement(room_id, "power")

    def get_device_name(self, device_id: int) -> str:
        """Return the entity-facing device name."""
        device = self.get_device(device_id)
        if device is None:
            return f"Device {device_id}"
        return str(device.get("name") or f"Device {device_id}")

    def _get_component_for_device(self, device_id: int) -> dict[str, Any] | None:
        """Return the physical component dict for a device, or None."""
        device = self.get_device(device_id)
        if device is None:
            return None
        comp_id = _coerce_int(device.get("compId"))
        if comp_id is None:
            return None
        return self.get_component(comp_id)

    def get_component_name(self, device_id: int) -> str:
        """Return the physical component name for a device."""
        component = self._get_component_for_device(device_id)
        if component is not None:
            name = component.get("name")
            if name:
                return str(name)
        return self.get_device_name(device_id)

    def get_component_identifier(self, device_id: int) -> str:
        """Return the stable device-registry identifier for a physical component."""
        device = self.get_device(device_id)
        if device is None:
            return f"device_{device_id}"
        comp_id = _coerce_int(device.get("compId"))
        return f"comp_{comp_id}" if comp_id is not None else f"device_{device_id}"

    def get_component_model(self, device_id: int) -> str:
        """Return the best-known model string for a device/component."""
        component = self._get_component_for_device(device_id)
        if component is not None:
            comp_type = _coerce_int(component.get("compType"))
            if comp_type in COMPONENT_MODELS:
                return COMPONENT_MODELS[comp_type]
        device = self.get_device(device_id)
        if device is None:
            return "xComfort device"
        dev_type = _coerce_int(device.get("devType"))
        if dev_type is None:
            return "xComfort device"
        return DEVICE_MODELS.get(dev_type, "xComfort device")

    def get_component_fw_version(self, device_id: int) -> str | None:
        """Return the firmware version of the component for a device."""
        component = self._get_component_for_device(device_id)
        return component.get("versionFW") if component is not None else None

    def get_component_hw_version(self, device_id: int) -> str | None:
        """Return the hardware version of the component for a device."""
        component = self._get_component_for_device(device_id)
        return component.get("versionHW") if component is not None else None

    def get_component_device_info(self, device_id: int) -> DeviceInfo:
        """Return standardized device metadata for a physical component."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.get_component_identifier(device_id))},
            manufacturer="Eaton",
            model=self.get_component_model(device_id),
            name=self.get_component_name(device_id),
            sw_version=self.get_component_fw_version(device_id),
            hw_version=self.get_component_hw_version(device_id),
            via_device=(DOMAIN, self.get_bridge_identifier_for_device(device_id)),
        )

    def get_room_device_info(self, room_id: int) -> DeviceInfo:
        """Return standardized device metadata for a logical room."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.get_room_identifier(room_id))},
            manufacturer="Eaton",
            model="xComfort Room",
            name=self.get_room_name(room_id),
            via_device=(DOMAIN, self.bridge_device_identifier),
        )

    def get_bridge_device_info(self) -> DeviceInfo:
        """Return standardized device metadata for the primary bridge."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.bridge_device_identifier)},
            manufacturer="Eaton",
            model=self.bridge_model,
            name=self.bridge_name or (self.entry.title if self.entry else self.host),
        )

    def get_slave_bridge_device_info(self, client_id: int) -> DeviceInfo:
        """Return standardized device metadata for a slave bridge."""
        slave = self._slave_bridges.get(client_id, {})
        slave_id = slave.get("mdnsName", f"slave_{client_id}")
        return DeviceInfo(
            identifiers={(DOMAIN, slave_id)},
            manufacturer="Eaton",
            model="xComfort Bridge",
            name=slave.get("name", f"xComfort Bridge (slave {client_id})"),
            via_device=(DOMAIN, self.bridge_device_identifier),
        )

    def get_slave_bridge_ip(self, client_id: int) -> str | None:
        """Return the IP address of a slave bridge."""
        slave = self._slave_bridges.get(client_id)
        if slave is None:
            return None
        return slave.get("ipAddress")

    def get_bridge_power(self) -> float | None:
        """Return the total bridge power consumption in watts."""
        for source in (self._bridge_state, self._home_data):
            val = source.get("power")
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
        return None

    def get_diagnostics_data(self) -> dict[str, Any]:
        """Return a snapshot of bridge state for HA diagnostics."""
        device_types = Counter(
            d.get("devType") for d in self._devices.values()
        )
        component_types = Counter(
            c.get("compType") for c in self._components.values()
        )

        return {
            "bridge": {
                "bridge_id": self.bridge_id,
                "bridge_name": self.bridge_name,
                "bridge_model": self.bridge_model,
                "firmware_version": self.firmware_version,
                "available": self._available,
            },
            "summary": {
                "device_count": len(self._devices),
                "component_count": len(self._components),
                "room_count": len(
                    set(self._rooms) | set(self._room_heating) | set(self._room_state)
                ),
                "scene_count": len(self._scenes),
                "slave_bridge_count": len(self._slave_bridges),
                "device_type_counts": dict(device_types),
                "component_type_counts": dict(component_types),
            },
            "home_data": dict(self._home_data),
            "bridge_state": dict(self._bridge_state),
            "bridge_diagnostics": dict(self._diagnostics),
            "slave_bridges": list(self._slave_bridges.values()),
            "components": list(self._components.values()),
            "devices": list(self._devices.values()),
            "rooms": [
                self.get_room(rid) for rid in self.get_room_ids()
            ],
            "scenes": list(self._scenes.values()),
        }

    @callback
    def subscribe_device_updates(self, device_id: int, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to updates for a specific device."""
        return self._subscribe(self._device_listeners, device_id, listener)

    @callback
    def subscribe_button_events(
        self,
        device_id: int,
        listener: Callable[[ButtonEvent], None],
    ) -> Callable[[], None]:
        """Subscribe to raw button events for a specific device."""
        return self._subscribe(self._button_listeners, device_id, listener)

    @callback
    def subscribe_room_updates(self, room_id: int, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to updates for a specific room."""
        return self._subscribe(self._room_listeners, room_id, listener)

    @callback
    def subscribe_bridge_updates(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to bridge-level state updates."""
        self._bridge_listeners.add(listener)

        def _unsubscribe() -> None:
            self._bridge_listeners.discard(listener)

        return _unsubscribe

    async def async_switch_device(self, device_id: int, is_on: bool) -> None:
        """Send a switch command to the bridge."""
        if not self._authenticated:
            raise XComfortConnectionError("Bridge command attempted while not connected")
        _LOGGER.debug("Sending switch command: device %d -> %s", device_id, is_on)
        await self._send_message(
            MESSAGE_ACTION_SWITCH_DEVICE,
            {"deviceId": device_id, "switch": is_on},
        )

    async def async_set_dimmer_level(self, device_id: int, dimmvalue: int) -> None:
        """Send a dimmer level command to the bridge."""
        if not self._authenticated:
            raise XComfortConnectionError("Bridge command attempted while not connected")
        value = max(0, min(100, dimmvalue))
        _LOGGER.debug("Sending dimmer command: device %d -> %d%%", device_id, value)
        await self._send_message(
            MESSAGE_ACTION_SLIDE_DEVICE,
            {"deviceId": device_id, "dimmvalue": value},
        )

    async def async_activate_scene(self, scene_id: int) -> None:
        """Send a scene activation command to the bridge."""
        if not self._authenticated:
            raise XComfortConnectionError("Bridge command attempted while not connected")
        _LOGGER.debug("Activating scene %d", scene_id)
        await self._send_message(
            MESSAGE_ACTIVATE_SCENE,
            {"sceneId": scene_id},
        )

    async def _async_send_shade_command(
        self, device_id: int, state: int, **kwargs: Any
    ) -> None:
        """Send a shading command to the bridge with safety check."""
        if not self._authenticated:
            raise XComfortConnectionError("Bridge command attempted while not connected")
        if self.get_cover_safety(device_id):
            _LOGGER.warning(
                "Shade command blocked: device %d has safety enabled", device_id
            )
            return
        _LOGGER.debug(
            "Sending shade command: device %d state=%d %s", device_id, state, kwargs
        )
        await self._send_message(
            MESSAGE_ACTION_SHADING_DEVICE,
            {"deviceId": device_id, "state": state, **kwargs},
        )

    async def async_open_cover(self, device_id: int) -> None:
        """Send an open command to a shading actuator."""
        await self._async_send_shade_command(device_id, SHADE_OP_OPEN)

    async def async_close_cover(self, device_id: int) -> None:
        """Send a close command to a shading actuator."""
        await self._async_send_shade_command(device_id, SHADE_OP_CLOSE)

    async def async_stop_cover(self, device_id: int) -> None:
        """Send a stop command to a shading actuator."""
        await self._async_send_shade_command(device_id, SHADE_OP_STOP)

    async def async_set_cover_position(self, device_id: int, position: int) -> None:
        """Send a go-to-position command to a shading actuator."""
        value = max(0, min(100, position))
        await self._async_send_shade_command(device_id, SHADE_OP_GO_TO, value=value)

    async def async_set_room_climate_preset(
        self,
        room_id: int,
        mode: int,
        state: int,
        setpoint: float,
    ) -> None:
        """Send a preset/setpoint change to the bridge (confirmed=false)."""
        if not self._authenticated:
            raise XComfortConnectionError("Bridge command attempted while not connected")

        _LOGGER.debug(
            "Sending climate preset: room %d mode=%d state=%d setpoint=%.1f",
            room_id,
            mode,
            state,
            setpoint,
        )
        await self._send_message(
            MESSAGE_SET_HEATING_STATE,
            {
                "roomId": room_id,
                "mode": mode,
                "state": state,
                "setpoint": setpoint,
                "confirmed": False,
            },
        )

    async def async_set_room_hvac_state(self, room_id: int, state: int) -> None:
        """Send an HVAC state change to the bridge (confirmed=true).

        This uses the minimal payload the bridge requires for on/off toggling.
        The bridge ignores state changes sent with confirmed=false.
        """
        if not self._authenticated:
            raise XComfortConnectionError("Bridge command attempted while not connected")

        _LOGGER.debug("Sending HVAC state change: room %d state=%d", room_id, state)
        await self._send_message(
            MESSAGE_SET_HEATING_STATE,
            {
                "roomId": room_id,
                "state": state,
                "confirmed": True,
            },
        )

    async def _run_forever(self) -> None:
        """Keep the bridge connection alive with explicit reconnect backoff."""
        backoff = 1

        while not self._stop_event.is_set():
            try:
                await self._connect_and_pump()
                backoff = 1
            except XComfortAuthenticationError as exc:
                self._set_available(False)
                if self._startup_future is not None and not self._startup_future.done():
                    self._startup_future.set_exception(exc)
                _LOGGER.error("Authentication failed for bridge %s", self.host)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_available(False)
                await self._cleanup_connection()

                if self._startup_future is not None and not self._startup_future.done():
                    self._startup_future.set_exception(
                        XComfortConnectionError(f"Unable to connect to bridge {self.host}: {exc}")
                    )
                    return

                if self._stop_event.is_set():
                    break

                _LOGGER.warning(
                    "Bridge connection lost for %s, retrying in %ss: %s",
                    self.host,
                    backoff,
                    exc,
                )

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass

                backoff = min(backoff * 2, DEFAULT_RECONNECT_MAX)

    async def _connect_and_pump(self) -> None:
        """Establish the secure bridge connection and run the read loop."""
        self._message_counter = 0
        self._session = aiohttp.ClientSession()

        try:
            self._ws = await self._session.ws_connect(f"http://{self.host}/")
            await self._perform_plaintext_handshake()
            await self._perform_secure_channel_setup()
            await self._authenticate()
            await self._request_initial_data()
            await self._receive_loop()
        finally:
            self._authenticated = False
            self._set_available(False)
            await self._cleanup_connection()

    async def _perform_plaintext_handshake(self) -> None:
        """Run the plaintext websocket handshake."""
        hello = await self._receive_plaintext_message()

        if hello.get("type_int") == 0:
            raise XComfortConnectionError(f"Bridge rejected connection: {hello.get('info')}")

        payload = hello.get("payload", {})
        self.bridge_id = payload.get("device_id", self.bridge_id)
        self.firmware_version = payload.get("device_version", self.firmware_version)
        connection_id = payload.get("connection_id")
        _LOGGER.debug(
            "Bridge hello: id=%s fw=%s connection=%s",
            self.bridge_id, self.firmware_version, connection_id,
        )

        if not connection_id:
            raise XComfortConnectionError("Bridge hello did not include a connection_id")

        await self._send_plaintext_message(
            {
                "type_int": MESSAGE_CONNECT_CONFIRM,
                "mc": -1,
                "payload": {
                    "client_type": CLIENT_TYPE,
                    "client_id": CLIENT_ID,
                    "client_version": CLIENT_VERSION,
                    "connection_id": connection_id,
                },
            }
        )

        confirm = await self._receive_plaintext_message()
        if confirm.get("type_int") == 13:
            message = confirm.get("payload", {}).get("error_message", "unknown error")
            raise XComfortConnectionError(f"Bridge declined the connection: {message}")

    async def _perform_secure_channel_setup(self) -> None:
        """Negotiate the AES session over the bridge RSA key."""
        await self._send_plaintext_message({"type_int": MESSAGE_SC_INIT, "mc": -1})
        pubkey_message = await self._receive_plaintext_message()

        public_key = pubkey_message.get("payload", {}).get("public_key")
        if not public_key:
            raise XComfortConnectionError("Bridge did not return an RSA public key")

        rsa_key = load_pem_public_key(public_key.encode())
        self._aes_key = os.urandom(32)
        self._aes_iv = os.urandom(16)

        plaintext_secret = f"{self._aes_key.hex()}:::{self._aes_iv.hex()}".encode()
        encrypted_secret = b64encode(
            rsa_key.encrypt(plaintext_secret, rsa_padding.PKCS1v15())
        ).decode()

        await self._send_plaintext_message(
            {
                "type_int": MESSAGE_SC_SECRET,
                "mc": -1,
                "payload": {"secret": encrypted_secret},
            }
        )

        established = await self._receive_encrypted_message()
        if established.get("type_int") != MESSAGE_SC_ESTABLISHED:
            raise XComfortConnectionError(
                f"Secure channel setup failed, got type {established.get('type_int')}"
            )
        _LOGGER.debug("Secure channel established")

    async def _authenticate(self) -> None:
        """Authenticate with the bridge and apply the returned token."""
        if self.bridge_id is None:
            raise XComfortConnectionError("Cannot authenticate before bridge_id is known")

        salt = _generate_salt()
        password_hash = _hash_secret(
            self.bridge_id.encode(),
            self.secret.encode(),
            salt.encode(),
        )

        await self._send_message(
            MESSAGE_AUTH_LOGIN,
            {
                "username": self.username,
                "password": password_hash,
                "salt": salt,
            },
        )

        login_response = await self._receive_encrypted_message()
        if login_response.get("type_int") == MESSAGE_AUTH_LOGIN_DENIED:
            raise XComfortAuthenticationError(
                f"Bridge rejected credentials for user '{self.username}'"
            )

        if login_response.get("type_int") != MESSAGE_AUTH_LOGIN_SUCCESS:
            raise XComfortConnectionError(
                f"Unexpected login response type {login_response.get('type_int')}"
            )

        token = login_response.get("payload", {}).get("token")
        if not token:
            raise XComfortConnectionError("Bridge login response did not include a token")

        await self._send_message(MESSAGE_AUTH_APPLY_TOKEN, {"token": token})
        apply_response = await self._receive_encrypted_message()
        if apply_response.get("type_int") != MESSAGE_AUTH_APPLY_TOKEN_RESPONSE:
            raise XComfortConnectionError(
                f"Unexpected token apply response type {apply_response.get('type_int')}"
            )

        await self._send_message(MESSAGE_AUTH_RENEW_TOKEN, {"token": token})
        renew_response = await self._receive_encrypted_message()
        renew_type = renew_response.get("type_int")
        if renew_type == MESSAGE_AUTH_RENEW_TOKEN_RESPONSE:
            renewed_token = renew_response.get("payload", {}).get("token")
            if renewed_token:
                await self._send_message(MESSAGE_AUTH_APPLY_TOKEN, {"token": renewed_token})
                await self._receive_encrypted_message()
            else:
                _LOGGER.warning("Token renew response contained no token, skipping apply")
        else:
            _LOGGER.warning(
                "Unexpected token renew response type %s, skipping token renewal",
                renew_type,
            )

        self._authenticated = True
        _LOGGER.debug("Authenticated as '%s' on bridge %s", self.username, self.bridge_id)

    async def _request_initial_data(self) -> None:
        """Request the bridge data needed to populate the state store."""
        await self._send_message(MESSAGE_INITIAL_DATA, {})
        await self._send_message(MESSAGE_HOME_DATA, {})
        await self._send_message(MESSAGE_DIAGNOSTICS, {})
        await self._send_message(MESSAGE_HEARTBEAT, {})
        _LOGGER.debug("Initial data requests sent")

    async def _receive_loop(self) -> None:
        """Run the main encrypted receive loop."""
        last_message = asyncio.get_running_loop().time()
        last_heartbeat = last_message

        while not self._stop_event.is_set():
            now = asyncio.get_running_loop().time()

            if now - last_heartbeat >= DEFAULT_HEARTBEAT_INTERVAL:
                await self._send_message(MESSAGE_HEARTBEAT, {})
                last_heartbeat = now

            if now - last_message >= DEFAULT_READ_TIMEOUT:
                raise XComfortConnectionError("Bridge read timeout exceeded")

            try:
                message = await self._receive_encrypted_message(timeout=1)
            except asyncio.TimeoutError:
                continue

            last_message = asyncio.get_running_loop().time()
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """Handle an incoming bridge message without crashing the runtime."""
        try:
            if "mc" in message:
                await self._send_ack(int(message["mc"]))

            message_type = message.get("type_int")
            payload = message.get("payload", {})

            if message_type in {MESSAGE_ACK, MESSAGE_HEARTBEAT, 3, 408}:
                return

            if message_type == MESSAGE_SET_ALL_DATA:
                self._handle_set_all_data(payload)
                return

            if message_type == MESSAGE_SET_HOME_DATA:
                self._handle_home_data(payload)
                return

            if message_type == MESSAGE_SET_DIAGNOSTICS:
                self._diagnostics = payload if isinstance(payload, dict) else {}
                return

            if message_type == MESSAGE_SET_STATE_INFO:
                self._handle_state_info(payload)
                return

            if message_type == MESSAGE_SET_ROOM_HEATING_STATE:
                self._handle_room_heating_state(payload)
                return

            if message_type == MESSAGE_SET_DEVICE_STATE:
                device_id = payload.get("deviceId")
                if device_id is not None:
                    device_id_int = int(device_id)
                    previous_room_id, current_room_id = self._merge_device(device_id_int, payload)
                    self._notify_device_listeners(device_id_int)
                    for room_id in (previous_room_id, current_room_id):
                        if room_id is not None:
                            self._notify_room_listeners(room_id)
                return

            if message_type == MESSAGE_SET_BRIDGE_STATE:
                self._bridge_state = payload if isinstance(payload, dict) else {}
                self._notify_all_devices()
                self._notify_bridge_listeners()
                return

            _LOGGER.debug("Ignoring unhandled bridge message type %s", message_type)
        except Exception:
            _LOGGER.warning("Ignoring malformed bridge message: %s", message, exc_info=True)

    def _handle_set_all_data(self, payload: dict[str, Any]) -> None:
        """Merge full or incremental bridge data into the state store."""
        changed_device_ids: set[int] = set()
        changed_room_ids: set[int] = set()

        for component in payload.get("comps", []):
            comp_id = component.get("compId")
            if comp_id is None:
                continue
            self._components[int(comp_id)] = {
                **self._components.get(int(comp_id), {}),
                **component,
            }

        for device in payload.get("devices", []):
            device_id = device.get("deviceId")
            if device_id is None:
                continue
            device_id = int(device_id)
            previous_room_id, current_room_id = self._merge_device(device_id, device)
            changed_device_ids.add(device_id)
            if previous_room_id is not None:
                changed_room_ids.add(previous_room_id)
            if current_room_id is not None:
                changed_room_ids.add(current_room_id)

        for room in payload.get("rooms", []):
            room_id = room.get("roomId")
            if room_id is None:
                continue
            room_id = int(room_id)
            self._rooms[room_id] = {**self._rooms.get(room_id, {}), **room}
            changed_room_ids.add(room_id)

        for room_state in payload.get("roomHeating", []):
            room_id = room_state.get("roomId")
            if room_id is None:
                continue
            room_id = int(room_id)
            self._merge_room_store(self._room_heating, room_id, room_state)
            changed_room_ids.add(room_id)

        for scene in payload.get("scenes", []):
            scene_id = scene.get("sceneId")
            if scene_id is None:
                continue
            self._scenes[int(scene_id)] = {**self._scenes.get(int(scene_id), {}), **scene}

        for client_info in payload.get("clients", []):
            client_id = client_info.get("clientId")
            if client_id is not None and client_id != 0:
                self._slave_bridges[int(client_id)] = client_info
                _LOGGER.debug(
                    "Discovered slave bridge clientId=%s name=%s ip=%s",
                    client_id,
                    client_info.get("name"),
                    client_info.get("ipAddress"),
                )

        for device_id in changed_device_ids:
            self._notify_device_listeners(device_id)
        for room_id in changed_room_ids:
            self._notify_room_listeners(room_id)

        if payload.get("lastItem"):
            _LOGGER.debug(
                "Initial data complete: %d devices, %d components, %d rooms, %d scenes, %d slaves",
                len(self._devices), len(self._components),
                len(self._rooms), len(self._scenes), len(self._slave_bridges),
            )
            self._initial_data_loaded.set()
            self._set_available(True)
            if self._startup_future is not None and not self._startup_future.done():
                self._startup_future.set_result(None)

    def _handle_home_data(self, payload: dict[str, Any]) -> None:
        """Store bridge-level metadata."""
        if not isinstance(payload, dict):
            return

        self._home_data.update(payload)
        if "name" in payload:
            self.bridge_name = str(payload["name"])
            _LOGGER.debug("Bridge name from HOME_DATA: %s", self.bridge_name)
        if "fwBuild" in payload and self.firmware_version is None:
            self.firmware_version = str(payload["fwBuild"])
        self._notify_bridge_listeners()

    def _handle_state_info(self, payload: dict[str, Any]) -> None:
        """Merge incremental device, component, and room state updates."""
        changed_device_ids: set[int] = set()
        changed_room_ids: set[int] = set()
        notify_bridge = False

        for item in payload.get("item", []):
            if not isinstance(item, dict):
                continue

            device_id = item.get("deviceId")
            room_id = item.get("roomId")
            comp_id = item.get("compId")

            if device_id is not None:
                device_id = int(device_id)
                previous_room_id, current_room_id = self._merge_device(device_id, item)
                changed_device_ids.add(device_id)
                if previous_room_id is not None:
                    changed_room_ids.add(previous_room_id)
                if current_room_id is not None:
                    changed_room_ids.add(current_room_id)

                device = self._devices.get(device_id, {})
                if item.get("curstate") is not None and device.get("devType") in EVENT_DEVICE_TYPES:
                    value = item.get("curstate")
                    event_type = "press_up" if bool(value) else "press_down"
                    _LOGGER.debug(
                        "Button event: device %d (%s) %s",
                        device_id, self.get_device_name(device_id), event_type,
                    )
                    self._emit_button_event(
                        device_id,
                        ButtonEvent(
                            device_id=device_id,
                            event_type=event_type,
                            value=value,
                            raw=item,
                        ),
                    )

                continue

            if room_id is not None:
                room_id = int(room_id)
                self._merge_room_store(self._room_state, room_id, item)
                changed_room_ids.add(room_id)
                continue

            if comp_id is not None:
                comp_id = int(comp_id)
                self._components[comp_id] = {**self._components.get(comp_id, {}), **item}
                notify_bridge = True

        if notify_bridge:
            self._notify_bridge_listeners()

        if changed_device_ids:
            _LOGGER.debug(
                "State update for devices: %s",
                ", ".join(
                    f"{did} ({self.get_device_name(did)})"
                    for did in sorted(changed_device_ids)
                ),
            )
        for device_id in changed_device_ids:
            self._notify_device_listeners(device_id)
        for room_id in changed_room_ids:
            _LOGGER.debug("State update for room %d (%s)", room_id, self.get_room_name(room_id))
            self._notify_room_listeners(room_id)

    def _handle_room_heating_state(self, payload: dict[str, Any]) -> None:
        """Handle direct room heating state responses from the bridge."""
        _LOGGER.debug("Room heating response (363): %s", payload)

        room_id = payload.get("roomId")
        if room_id is None:
            return

        room_id = int(room_id)
        self._merge_room_store(self._room_state, room_id, payload)
        self._notify_room_listeners(room_id)

    def _merge_device(self, device_id: int, patch: dict[str, Any]) -> tuple[int | None, int | None]:
        """Merge a device patch into the central store and track room relinking."""
        previous_room_id = self.get_linked_room_id(device_id)
        self._devices[device_id] = {**self._devices.get(device_id, {}), **patch}
        current_room_id = self.get_linked_room_id(device_id)
        return previous_room_id, current_room_id

    def _emit_button_event(self, device_id: int, event: ButtonEvent) -> None:
        """Dispatch a raw button event to subscribers."""
        for listener in tuple(self._button_listeners.get(device_id, ())):
            listener(event)

    def _notify_device_listeners(self, device_id: int) -> None:
        """Notify subscribers for a specific device."""
        for listener in tuple(self._device_listeners.get(device_id, ())):
            listener()

    def _notify_room_listeners(self, room_id: int) -> None:
        """Notify subscribers for a specific room."""
        for listener in tuple(self._room_listeners.get(room_id, ())):
            listener()

    def _notify_all_devices(self) -> None:
        """Notify all currently subscribed device listeners."""
        for device_id in tuple(self._device_listeners):
            self._notify_device_listeners(device_id)

    def _notify_all_rooms(self) -> None:
        """Notify all currently subscribed room listeners."""
        for room_id in tuple(self._room_listeners):
            self._notify_room_listeners(room_id)

    def _notify_bridge_listeners(self) -> None:
        """Notify all currently subscribed bridge-level listeners."""
        for listener in tuple(self._bridge_listeners):
            listener()

    @callback
    def _subscribe(
        self,
        registry: defaultdict[Any, set[Any]],
        key: Any,
        listener: Any,
    ) -> Callable[[], None]:
        """Register a listener in one of the runtime callback registries."""
        listeners = registry[key]
        listeners.add(listener)

        def _unsubscribe() -> None:
            listeners.discard(listener)
            if not listeners:
                registry.pop(key, None)

        return _unsubscribe

    def _room_exists(self, room_id: int) -> bool:
        """Return whether a room ID exists in any room-backed store."""
        return room_id in self._rooms or room_id in self._room_heating or room_id in self._room_state

    def _get_room_value(self, room_id: int, *keys: str) -> Any:
        """Return the first present room value from the merged room state."""
        room = self.get_room(room_id)
        if room is None:
            return None

        for key in keys:
            if key in room:
                return room[key]

        return None

    def _get_room_measurement(self, room_id: int, *keys: str) -> float | None:
        """Return the first room measurement field as a float."""
        return _coerce_measurement(self._get_room_value(room_id, *keys))

    def _get_room_int(self, room_id: int, *keys: str) -> int | None:
        """Return the first room field that can be coerced to int."""
        return _coerce_int(self._get_room_value(room_id, *keys))

    def _normalize_room_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Mirror bridge valve naming so accessors do not normalize on every read."""
        normalized = dict(patch)
        if "valve" not in normalized and "currentValve" in normalized:
            normalized["valve"] = normalized["currentValve"]
        if "currentValve" not in normalized and "valve" in normalized:
            normalized["currentValve"] = normalized["valve"]
        return normalized

    def _merge_room_store(
        self,
        store: dict[int, dict[str, Any]],
        room_id: int,
        patch: dict[str, Any],
    ) -> None:
        """Merge a normalized room patch into one of the room-backed stores."""
        store[room_id] = {
            **store.get(room_id, {}),
            **self._normalize_room_patch(patch),
        }

    def _set_available(self, available: bool) -> None:
        """Update availability and notify entities when it changes."""
        if self._available == available:
            return
        self._available = available
        _LOGGER.debug("Bridge availability changed: %s", available)
        self._notify_all_devices()
        self._notify_all_rooms()

    async def _send_ack(self, message_counter: int) -> None:
        """Acknowledge an incoming bridge message."""
        await self._send_encrypted({"type_int": MESSAGE_ACK, "ref": message_counter})

    async def _send_message(self, message_type: int, payload: dict[str, Any]) -> None:
        """Send an encrypted application-level bridge message."""
        self._message_counter += 1
        await self._send_encrypted(
            {"type_int": message_type, "mc": self._message_counter, "payload": payload}
        )

    async def _send_plaintext_message(self, data: dict[str, Any]) -> None:
        """Send a plaintext websocket message."""
        if self._ws is None:
            raise XComfortConnectionError("No websocket connection is active")
        await self._ws.send_str(json.dumps(data))

    async def _send_encrypted(self, data: dict[str, Any]) -> None:
        """Encrypt and send a websocket message."""
        if self._ws is None or self._aes_key is None or self._aes_iv is None:
            raise XComfortConnectionError("Encrypted send attempted before secure channel setup")

        raw = json.dumps(data).encode()
        ciphertext = _aes_encrypt(self._aes_key, self._aes_iv, _pad_to_block(raw))
        await self._ws.send_str(b64encode(ciphertext).decode() + "\u0004")

    async def _receive_plaintext_message(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive and decode a plaintext bridge message."""
        raw = await self._receive_ws_message(timeout)
        return json.loads(raw.rstrip("\u0004"))

    async def _receive_encrypted_message(self, timeout: float | None = None) -> dict[str, Any]:
        """Receive and decrypt a bridge message."""
        raw = await self._receive_ws_message(timeout)
        if self._aes_key is None or self._aes_iv is None:
            raise XComfortConnectionError("Encrypted receive attempted before secure channel setup")

        ciphertext = b64decode(raw.rstrip("\u0004"))
        plaintext = _aes_decrypt(self._aes_key, self._aes_iv, ciphertext).rstrip(b"\x00")
        if not plaintext:
            return {}
        return json.loads(plaintext.decode())

    async def _receive_ws_message(self, timeout: float | None = None) -> str:
        """Receive a websocket frame or raise a runtime error."""
        if self._ws is None:
            raise XComfortConnectionError("No websocket connection is active")

        message = await self._ws.receive(timeout=timeout)

        if message.type == aiohttp.WSMsgType.TEXT:
            return message.data

        if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            raise XComfortConnectionError("Bridge websocket closed")

        if message.type == aiohttp.WSMsgType.ERROR:
            raise XComfortConnectionError(f"Bridge websocket error: {self._ws.exception()}")

        raise XComfortConnectionError(f"Unsupported websocket frame type: {message.type}")

    async def _cleanup_connection(self) -> None:
        """Close and clear the current websocket/session state."""
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        if self._session is not None:
            await self._session.close()
            self._session = None

        self._aes_key = None
        self._aes_iv = None
        self._authenticated = False


async def async_test_connection(
    host: str,
    username: str,
    secret: str,
) -> dict[str, str]:
    """Connect to bridge, authenticate, and return bridge info.

    Used by config flow to validate credentials and resolve the bridge name.
    Returns dict with bridge_id, bridge_name, firmware_version.
    Raises XComfortConnectionError or XComfortAuthenticationError on failure.
    """
    client = XComfortBridgeClient.create_probe_client(host, username, secret)

    try:
        _LOGGER.debug("Test connection to %s", host)
        client._session = aiohttp.ClientSession()
        client._ws = await client._session.ws_connect(f"http://{host}/", timeout=10)

        await client._perform_plaintext_handshake()
        await client._perform_secure_channel_setup()
        await client._authenticate()

        # Request HOME_DATA to get bridge name
        await client._send_message(MESSAGE_HOME_DATA, {})

        bridge_name = None
        deadline = asyncio.get_running_loop().time() + 5
        while asyncio.get_running_loop().time() < deadline:
            try:
                msg = await client._receive_encrypted_message(timeout=2)
            except asyncio.TimeoutError:
                _LOGGER.debug("Test connection: timed out waiting for HOME_DATA")
                break
            except XComfortConnectionError as exc:
                _LOGGER.warning("Test connection: connection error while waiting for HOME_DATA: %s", exc)
                break
            if "mc" in msg:
                await client._send_ack(int(msg["mc"]))
            msg_type = msg.get("type_int")
            if msg_type == MESSAGE_SET_HOME_DATA:
                bridge_name = msg.get("payload", {}).get("name")
                _LOGGER.debug("Test connection: bridge name = %s", bridge_name)
                break
            _LOGGER.debug("Test connection: skipping message type %s while waiting for HOME_DATA", msg_type)

        return {
            "bridge_id": client.bridge_id or "",
            "bridge_name": bridge_name or f"xComfort Bridge {client.bridge_id}",
            "firmware_version": client.firmware_version or "",
        }

    except (aiohttp.ClientError, OSError) as exc:
        raise XComfortConnectionError(
            f"Cannot connect to bridge at {host}: {exc}"
        ) from exc
    finally:
        await client._cleanup_connection()
