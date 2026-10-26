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
from typing import Any, Callable, Container, Iterable

import aiohttp
from cryptography.hazmat.primitives.asymmetric import padding as rsa_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_pem_public_key
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_USERNAME, MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    BATTERY_CODE_MAINS,
    BATTERY_CODE_MAP,
    BATTERY_CODES,
    COMPTYPE_DOOR_WINDOW_SENSOR,
    CONF_SECRET,
    COVER_DEVICE_TYPES,
    DOMAIN,
    DEFAULT_AUTH_RETRIES,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_READ_TIMEOUT,
    DEFAULT_RECONNECT_MAX,
    DEFAULT_SETUP_TIMEOUT,
    DEFAULT_STABLE_SESSION,
    DEFAULT_STARTUP_TIMEOUT,
    DEVTYPE_BINARY_INPUT,
    DEVTYPE_CONTACT,
    DEVTYPE_DIMMER,
    DEVTYPE_ROOM_CONTROLLER,
    DEVTYPE_TEMP_SENSOR,
    DOOR_WINDOW_DOOR_MODES,
    DP_ON_OFF,
    EVENT_DEVICE_TYPES,
    INFO_CODES_CONTACT_CLOSED,
    INFO_CODES_CONTACT_OPEN,
    INFO_CODES_SHADING_LOCKED,
    INFO_CODE_DEVICE_TEMPERATURE,
    INFO_CODE_PT1000_TEMPERATURE,
    INFO_CODE_RCT_HUMIDITY,
    INFO_CODE_TEMPERATURE,
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
    SHADING_SAFETY_STATES,
    SIGNAL_LABELS,
    USAGE_COOLING,
    USAGE_HEATING,
    USAGE_LIGHT,
)

_LOGGER = logging.getLogger(__name__)

# HA 2026.8 added DeviceInfo.via_device_id (a device registry id); the
# (domain, identifier) via_device tuple is deprecated from 2026.9 and stops
# working in 2027.8. Older supported releases only know the tuple.
_HAS_VIA_DEVICE_ID = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 8)

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
    211: "Binary Input Channel",
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


class XComfortBridgeError(HomeAssistantError):
    """Base exception for xComfort bridge errors.

    Derives from HomeAssistantError so a failed service call shows the message
    in the UI instead of an unknown error with a traceback.
    """


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


def _info_codes(obj: dict[str, Any] | None) -> set[str]:
    """Return the info codes currently reported by a device/component dict."""
    if obj is None:
        return set()
    return {
        str(item.get("text", ""))
        for item in obj.get("info", [])
        if isinstance(item, dict)
    }


def _info_capabilities(obj: dict[str, Any] | None) -> set[tuple[str, bool]]:
    """Return the (info code, has value) pairs of a device/component dict.

    Some entities only need a code to be present (battery), others need its
    value (actuator temperature, signal), so both count as capabilities.
    """
    if obj is None:
        return set()
    return {
        (str(item.get("text", "")), item.get("value") is not None)
        for item in obj.get("info", [])
        if isinstance(item, dict)
    }


def _merge_grew(existing: dict[str, Any] | None, patch: dict[str, Any]) -> bool:
    """Return True if applying the patch introduces keys the store lacked."""
    return existing is None or not set(patch) <= set(existing)


# Device / component fields that decide which entities exist and where they hang.
_DEVICE_SHAPE_KEYS = ("devType", "usage", "compId", "hasPower")
_COMP_SHAPE_KEYS = ("compType", "clientId")


def _is_dimmable(device: dict[str, Any]) -> bool:
    """Return whether a light actuator dims (dimmer hardware, not the on/off profile)."""
    return device.get("devType") == DEVTYPE_DIMMER and str(device.get("dp")) != DP_ON_OFF


def _describe_error(exc: BaseException) -> str:
    """Return a log-friendly reason; timeouts carry no message of their own."""
    return str(exc) or type(exc).__name__


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
        # Per-session progress, reset on every connect: drives the backoff
        # reset and the consecutive-login-rejection count in _run_forever.
        self._session_logged_in = False
        self._session_loaded_at: float | None = None

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
        # Info capabilities the platforms' discovery has already evaluated, per
        # device/component. Live updates replace the stored info list with
        # partial ones, so growth is judged against what discovery has seen,
        # not the latest list. Tracking starts once the platforms are set up.
        self._offered_device_caps: defaultdict[int, set[tuple[str, bool]]] = defaultdict(set)
        self._offered_comp_caps: defaultdict[int, set[tuple[str, bool]]] = defaultdict(set)
        self._discovery_started = False
        self._reload_scheduled = False

        self._device_listeners: defaultdict[int, set[Callable[[], None]]] = defaultdict(set)
        self._button_listeners: defaultdict[int, set[Callable[[ButtonEvent], None]]] = defaultdict(set)
        self._room_listeners: defaultdict[int, set[Callable[[], None]]] = defaultdict(set)
        self._bridge_listeners: set[Callable[[], None]] = set()
        # Ordered (dict as an ordered set): async_setup_entry subscribes first so
        # new bridge devices are registered before platforms add their children.
        self._inventory_listeners: dict[Callable[[], None], None] = {}

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

    def _device_ids_by_type(self, types: Container[int]) -> list[int]:
        """Return sorted device IDs whose devType is in the given set."""
        return sorted(
            device_id
            for device_id, device in self._devices.items()
            if device.get("devType") in types
        )

    def _is_light_usage(self, device_id: int) -> bool:
        """Return whether an actuator's bridge usage is LIGHT (or unreported)."""
        usage = _coerce_int(self._devices[device_id].get("usage"))
        return usage is None or usage == USAGE_LIGHT

    def get_light_device_ids(self) -> list[int]:
        """Return switching/dimming actuators whose usage is LIGHT."""
        return [
            device_id
            for device_id in self._device_ids_by_type(LIGHT_DEVICE_TYPES)
            if self._is_light_usage(device_id)
        ]

    def is_dimmable_light(self, device_id: int) -> bool:
        """Return whether a light actuator supports brightness."""
        return _is_dimmable(self.get_device(device_id) or {})

    def get_switch_device_ids(self) -> list[int]:
        """Return switching/dimming actuators that drive a non-light load."""
        return [
            device_id
            for device_id in self._device_ids_by_type(LIGHT_DEVICE_TYPES)
            if not self._is_light_usage(device_id)
        ]

    def get_cover_device_ids(self) -> list[int]:
        """Return all currently known shading actuator device IDs."""
        return self._device_ids_by_type(COVER_DEVICE_TYPES)

    def get_cover_position(self, device_id: int) -> int | None:
        """Return the current shade position (0 = open, 100 = closed)."""
        device = self.get_device(device_id)
        if device is None:
            return None
        value = device.get("shPos")
        return _coerce_int(value) if value is not None else None

    def get_cover_safety_function(self, device_id: int) -> bool | None:
        """Return whether the app's safety function (wind/rain) is configured.

        This is a configuration toggle, not a live lock: the actuator enforces
        an active lock itself, so commands are never blocked on it.
        """
        device = self.get_device(device_id) or {}
        if "shSafety" not in device:
            return None
        return _coerce_int(device.get("shSafety")) not in (None, 0)

    def get_cover_safety_locked(self, device_id: int) -> bool:
        """Return whether the shade reports a live safety lock or safety movement."""
        device = self.get_device(device_id) or {}
        codes = _info_codes(device) | self._component_info_codes(device_id)
        return bool(codes & INFO_CODES_SHADING_LOCKED) or (
            _coerce_int(device.get("curstate")) in SHADING_SAFETY_STATES
        )

    def get_cover_shading_type(self, device_id: int) -> int | None:
        """Return the shading type (shType) configured in the app."""
        return _coerce_int((self.get_device(device_id) or {}).get("shType"))

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
        """Return door/window contact channels (devType 202 on a compType 76 component)."""
        result: list[int] = []
        for device_id in self._device_ids_by_type({DEVTYPE_CONTACT}):
            comp = self._get_component_for_device(device_id)
            if comp is not None and _coerce_int(comp.get("compType")) == COMPTYPE_DOOR_WINDOW_SENSOR:
                result.append(device_id)
        return result

    def get_binary_sensor_is_on(self, device_id: int) -> bool | None:
        """Return whether a door/window contact reports open.

        Read from the component info codes, as the Eaton app does; None until
        the contact has reported a state.
        """
        codes = self._component_info_codes(device_id)
        if codes & INFO_CODES_CONTACT_OPEN:
            return True
        if codes & INFO_CODES_CONTACT_CLOSED:
            return False
        return None

    def get_binary_sensor_is_door(self, device_id: int) -> bool:
        """Return whether the contact is configured as a door (vs window)."""
        component = self._get_component_for_device(device_id)
        if component is None:
            return False
        return str(component.get("mode")) in DOOR_WINDOW_DOOR_MODES

    def get_binary_input_device_ids(self) -> list[int]:
        """Return device IDs for binary-input channels (devType 211)."""
        return self._device_ids_by_type({DEVTYPE_BINARY_INPUT})

    def get_binary_input_is_on(self, device_id: int) -> bool | None:
        """Return the raw contact state of a binary-input channel.

        ``curstate`` reflects the maintained open/closed state of the wired
        contact (1 = closed on the captured hardware). Polarity depends on the
        physical wiring, so the raw value is exposed without a device class.
        """
        device = self.get_device(device_id)
        if device is None:
            return None
        return bool(device.get("curstate"))

    def get_event_device_ids(self) -> list[int]:
        """Return all currently known pushbutton/rocker device IDs."""
        return self._device_ids_by_type(EVENT_DEVICE_TYPES)

    def get_rct_device_ids(self) -> list[int]:
        """Return all currently known RC Touch device IDs."""
        return self._device_ids_by_type({DEVTYPE_ROOM_CONTROLLER})

    def get_scene_ids(self) -> list[int]:
        """Return all scene IDs that should be exposed."""
        return sorted(self._scenes)

    def get_scene(self, scene_id: int) -> dict[str, Any] | None:
        """Return scene data by ID."""
        return self._scenes.get(scene_id)

    def get_room_ids(self) -> list[int]:
        """Return the rooms in the bridge's room configuration.

        Live state (310) can arrive for a room id that has no configuration
        (seen in the field as a nameless "Room 2"); such ids get no entities.
        """
        return sorted(self._rooms)

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
        value = self.get_device_info_value(device_id, INFO_CODE_TEMPERATURE)
        return _coerce_measurement(value)

    def get_temperature_sensor_device_ids(self) -> list[int]:
        """Return all standalone temperature sensor channel device IDs."""
        return self._device_ids_by_type({DEVTYPE_TEMP_SENSOR})

    def get_temperature_sensor_temperature(self, device_id: int) -> float | None:
        """Return a standalone temperature sensor channel reading or None."""
        value = self.get_device_info_value(device_id, INFO_CODE_TEMPERATURE)
        if value is None:
            value = self.get_device_info_value(device_id, INFO_CODE_PT1000_TEMPERATURE)
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

    def _iter_component_info(self, device_id: int) -> list[dict[str, Any]]:
        """Return the info items of the component a device belongs to."""
        component = self._get_component_for_device(device_id)
        return component.get("info", []) if component is not None else []

    def _component_info_codes(self, device_id: int) -> set[str]:
        """Return the set of info codes reported by a device's component."""
        return {str(item.get("text", "")) for item in self._iter_component_info(device_id)}

    def _component_info_value(self, device_id: int, code: str) -> Any:
        """Return the value of the first component info item matching a code."""
        for item in self._iter_component_info(device_id):
            if str(item.get("text", "")) == code:
                return item.get("value")
        return None

    def get_component_battery_percentage(self, device_id: int) -> int | None:
        """Return battery percentage for the component, or None if not reported.

        Returns None while the component reports only "battery-unknown" (1118),
        i.e. a battery device that has not yet sent a level.
        """
        for code in self._component_info_codes(device_id):
            if code in BATTERY_CODE_MAP:
                return BATTERY_CODE_MAP[code]
        return None

    def _component_is_battery_powered(self, device_id: int) -> bool:
        """Return True if the component reports any battery code (level or unknown)."""
        return bool(self._component_info_codes(device_id) & BATTERY_CODES)

    def get_component_signal_label(self, device_id: int) -> str | None:
        """Return signal strength label for the component, or None."""
        value = self._component_info_value(device_id, INFO_CODE_SIGNAL_STRENGTH)
        return SIGNAL_LABELS.get(str(value)) if value is not None else None

    def is_mains_powered(self, device_id: int) -> bool:
        """Return True if the component is mains-powered."""
        return BATTERY_CODE_MAINS in self._component_info_codes(device_id)

    def get_battery_device_ids(self) -> list[int]:
        """Return primary device IDs for battery-powered components.

        Gated on battery capability (any of 1113-1118), not on a known level, so
        a sleepy device reporting only "battery-unknown" (1118) at startup still
        gets a battery entity that populates once it reports a level.
        """
        return sorted(
            device_id
            for device_id in self._get_primary_device_ids()
            if not self.is_mains_powered(device_id)
            and self._component_is_battery_powered(device_id)
        )

    def get_signal_device_ids(self) -> list[int]:
        """Return primary device IDs for components with signal info."""
        return sorted(
            device_id
            for device_id in self._get_primary_device_ids()
            if self.get_component_signal_label(device_id) is not None
        )

    # ------------------------------------------------------------------
    # Heating sum-request (pump/boiler demand output on heating hubs)
    # ------------------------------------------------------------------

    def get_component_sum_request(self, device_id: int) -> int | None:
        """Return the sum-request value (info code 1227) for the component."""
        return _coerce_int(self._component_info_value(device_id, INFO_CODE_SUM_REQUEST))

    def get_sum_request_device_ids(self) -> list[int]:
        """Return primary device IDs for components that report sum-request."""
        return sorted(
            device_id
            for device_id in self._get_primary_device_ids()
            if self.get_component_sum_request(device_id) is not None
        )

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

    def _device_ids_for_component(self, comp_id: int) -> list[int]:
        """Return the channels (device IDs) that belong to a component."""
        return [
            device_id
            for device_id, device in self._devices.items()
            if _coerce_int(device.get("compId")) == comp_id
        ]

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

    def get_component_parent_identifiers(self) -> dict[str, str]:
        """Map each component's device identifier to its owning bridge's identifier."""
        return {
            self.get_component_identifier(device_id): self.get_bridge_identifier_for_device(device_id)
            for device_id in self._devices
        }

    def get_known_device_identifiers(self) -> set[str]:
        """Return all device-registry identifiers the bridge currently backs.

        Used to decide whether a stale HA device (e.g. one removed from the
        bridge) may be deleted from the registry.
        """
        known: set[str] = {self.bridge_device_identifier}
        for client_id, slave in self._slave_bridges.items():
            known.add(slave.get("mdnsName", f"slave_{client_id}"))
        for device_id in self._devices:
            known.add(self.get_component_identifier(device_id))
        for room_id in self.get_room_ids():
            known.add(self.get_room_identifier(room_id))
        return known

    def _via(self, parent_identifier: str) -> dict[str, Any]:
        """Return the DeviceInfo keys that link a device to its parent bridge.

        Bridge devices are registered by async_setup_entry before the platforms
        add entities, and again on inventory growth ahead of the platforms'
        listeners, so the registry lookup normally succeeds.
        """
        if not _HAS_VIA_DEVICE_ID:
            return {"via_device": (DOMAIN, parent_identifier)}
        if self.hass is None or self.entry is None:
            return {}
        # async_get_device_by_identifier arrived with via_device_id (2026.8);
        # the unscoped async_get_device is deprecated from 2026.10.
        parent = dr.async_get(self.hass).async_get_device_by_identifier(
            (DOMAIN, parent_identifier), self.entry.entry_id
        )
        return {"via_device_id": parent.id} if parent is not None else {}

    def get_component_device_info(self, device_id: int) -> DeviceInfo:
        """Return standardized device metadata for a physical component."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.get_component_identifier(device_id))},
            manufacturer="Eaton",
            model=self.get_component_model(device_id),
            name=self.get_component_name(device_id),
            sw_version=self.get_component_fw_version(device_id),
            hw_version=self.get_component_hw_version(device_id),
            **self._via(self.get_bridge_identifier_for_device(device_id)),
        )

    def get_room_device_info(self, room_id: int) -> DeviceInfo:
        """Return standardized device metadata for a logical room."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.get_room_identifier(room_id))},
            manufacturer="Eaton",
            model="xComfort Room",
            name=self.get_room_name(room_id),
            **self._via(self.bridge_device_identifier),
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
            **self._via(self.bridge_device_identifier),
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
            # Every room the bridge sent anything for, including live-state-only
            # ids that get no entities — useful when diagnosing ghost rooms.
            "rooms": [
                self.get_room(rid)
                for rid in sorted(
                    set(self._rooms) | set(self._room_heating) | set(self._room_state)
                )
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

    @callback
    def subscribe_inventory_updates(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to growth of the device/room/scene/component inventory.

        Fires when a previously unknown entry appears or an existing entry
        gains capabilities (new component info codes, new room fields), so
        platforms can create entities at runtime instead of only at setup.
        """
        self._inventory_listeners[listener] = None

        def _unsubscribe() -> None:
            self._inventory_listeners.pop(listener, None)

        return _unsubscribe

    async def _send_command(self, message_type: int, payload: dict[str, Any]) -> None:
        """Send a control command, failing with a clear error when not connected."""
        if not self._authenticated:
            raise XComfortConnectionError("The xComfort bridge is not connected")
        try:
            await self._send_message(message_type, payload)
        except (aiohttp.ClientError, ConnectionError) as exc:
            raise XComfortConnectionError(
                f"Lost the bridge connection while sending a command: {_describe_error(exc)}"
            ) from exc

    async def async_switch_device(self, device_id: int, is_on: bool) -> None:
        """Send a switch command to the bridge."""
        _LOGGER.debug("Sending switch command: device %d -> %s", device_id, is_on)
        await self._send_command(
            MESSAGE_ACTION_SWITCH_DEVICE,
            {"deviceId": device_id, "switch": is_on},
        )

    async def async_set_dimmer_level(self, device_id: int, dimmvalue: int) -> None:
        """Send a dimmer level command to the bridge."""
        value = max(0, min(100, dimmvalue))
        _LOGGER.debug("Sending dimmer command: device %d -> %d%%", device_id, value)
        await self._send_command(
            MESSAGE_ACTION_SLIDE_DEVICE,
            {"deviceId": device_id, "dimmvalue": value},
        )

    async def async_activate_scene(self, scene_id: int) -> None:
        """Send a scene activation command to the bridge."""
        _LOGGER.debug("Activating scene %d", scene_id)
        await self._send_command(
            MESSAGE_ACTIVATE_SCENE,
            {"sceneId": scene_id},
        )

    async def _async_send_shade_command(
        self, device_id: int, state: int, **kwargs: Any
    ) -> None:
        """Send a shading command to the bridge.

        Not gated on the safety function: shSafety is a configuration toggle,
        and the actuator itself ignores commands while a safety lock is active.
        """
        _LOGGER.debug(
            "Sending shade command: device %d state=%d %s", device_id, state, kwargs
        )
        await self._send_command(
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
        _LOGGER.debug(
            "Sending climate preset: room %d mode=%d state=%d setpoint=%.1f",
            room_id,
            mode,
            state,
            setpoint,
        )
        await self._send_command(
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
        _LOGGER.debug("Sending HVAC state change: room %d state=%d", room_id, state)
        await self._send_command(
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
        auth_failures = 0

        while not self._stop_event.is_set():
            try:
                await self._connect_and_pump()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._set_available(False)
                await self._cleanup_connection()

                if self._startup_future is not None and not self._startup_future.done():
                    self._startup_future.set_exception(
                        exc
                        if isinstance(exc, XComfortAuthenticationError)
                        else XComfortConnectionError(
                            f"Unable to connect to bridge {self.host}: {_describe_error(exc)}"
                        )
                    )
                    return

                if self._stop_event.is_set():
                    break

                if isinstance(exc, XComfortAuthenticationError):
                    auth_failures += 1
                    if auth_failures >= DEFAULT_AUTH_RETRIES:
                        _LOGGER.error(
                            "Bridge %s rejected the login %d times in a row; "
                            "reauthentication required",
                            self.host,
                            auth_failures,
                        )
                        if self.entry is not None and self.hass is not None:
                            self.entry.async_start_reauth(self.hass)
                        return
                    reason = f"login rejected ({auth_failures}/{DEFAULT_AUTH_RETRIES})"
                else:
                    if self._session_logged_in:
                        auth_failures = 0
                    reason = _describe_error(exc)

                # A session that stayed loaded long enough was healthy: start
                # again at the shortest delay instead of continuing to back off.
                loaded_at = self._session_loaded_at
                if (
                    loaded_at is not None
                    and asyncio.get_running_loop().time() - loaded_at >= DEFAULT_STABLE_SESSION
                ):
                    backoff = 1

                _LOGGER.warning(
                    "Bridge connection lost for %s, retrying in %ss: %s",
                    self.host,
                    backoff,
                    reason,
                )

                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass

                backoff = min(backoff * 2, DEFAULT_RECONNECT_MAX)

    async def _connect_and_pump(self) -> None:
        """Establish the secure bridge connection and run the read loop."""
        self._message_counter = 0
        self._session_logged_in = False
        self._session_loaded_at = None
        self._session = aiohttp.ClientSession()

        try:
            # ws_connect's own timeout kwarg is the close-handshake timeout, not
            # connection establishment — bound the connect explicitly so a dead
            # host fails within the reconnect backoff instead of aiohttp's default.
            try:
                async with asyncio.timeout(DEFAULT_CONNECT_TIMEOUT):
                    self._ws = await self._session.ws_connect(f"http://{self.host}/")
            except TimeoutError as exc:
                raise XComfortConnectionError(
                    f"Timed out connecting to {self.host}"
                ) from exc
            # The bridge can accept the websocket and then go quiet (e.g. while
            # it reboots); bound the login so a reconnect can't wait forever.
            try:
                async with asyncio.timeout(DEFAULT_SETUP_TIMEOUT):
                    await self._perform_plaintext_handshake()
                    await self._perform_secure_channel_setup()
                    await self._authenticate()
            except TimeoutError as exc:
                raise XComfortConnectionError(
                    f"Bridge did not complete the login within {DEFAULT_SETUP_TIMEOUT}s"
                ) from exc
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
        self._session_logged_in = True
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
        started = asyncio.get_running_loop().time()
        last_message = started
        last_heartbeat = started

        while not self._stop_event.is_set():
            now = asyncio.get_running_loop().time()

            if now - last_heartbeat >= DEFAULT_HEARTBEAT_INTERVAL:
                await self._send_message(MESSAGE_HEARTBEAT, {})
                last_heartbeat = now

            if now - last_message >= DEFAULT_READ_TIMEOUT:
                raise XComfortConnectionError("Bridge read timeout exceeded")

            if self._session_loaded_at is None and now - started >= DEFAULT_SETUP_TIMEOUT:
                raise XComfortConnectionError(
                    f"Bridge did not send its device data within {DEFAULT_SETUP_TIMEOUT}s"
                )

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
        comps_changed = False
        inventory_grew = False

        for component in payload.get("comps", []):
            comp_id = component.get("compId")
            if comp_id is None:
                continue
            comp_id = int(comp_id)
            existing = self._components.get(comp_id)
            if self._comp_patch_grows(comp_id, component):
                # New component, or a new capability (e.g. a battery code
                # appearing after a bridge cold boot).
                inventory_grew = True
            self._components[comp_id] = {**(existing or {}), **component}
            comps_changed = True
            changed_device_ids.update(self._device_ids_for_component(comp_id))

        for device in payload.get("devices", []):
            device_id = device.get("deviceId")
            if device_id is None:
                continue
            device_id = int(device_id)
            if self._device_patch_grows(device_id, device):
                inventory_grew = True
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
            if _merge_grew(self._rooms.get(room_id), room):
                # New room, or new room fields (gates room-sensor creation).
                inventory_grew = True
            self._rooms[room_id] = {**self._rooms.get(room_id, {}), **room}
            changed_room_ids.add(room_id)

        for room_state in payload.get("roomHeating", []):
            room_id = room_state.get("roomId")
            if room_id is None:
                continue
            room_id = int(room_id)
            existing_heating = self._room_heating.get(room_id)
            if existing_heating is None or existing_heating.get(
                "temperatureOnly"
            ) != room_state.get("temperatureOnly"):
                # Climate capability may have appeared (room sensor assigned).
                inventory_grew = True
            self._merge_room_store(self._room_heating, room_id, room_state)
            changed_room_ids.add(room_id)

        for scene in payload.get("scenes", []):
            scene_id = scene.get("sceneId")
            if scene_id is None:
                continue
            scene_id = int(scene_id)
            if scene_id not in self._scenes:
                inventory_grew = True
            self._scenes[scene_id] = {**self._scenes.get(scene_id, {}), **scene}

        for client_info in payload.get("clients", []):
            client_id = client_info.get("clientId")
            if client_id is not None and client_id != 0:
                if int(client_id) not in self._slave_bridges:
                    inventory_grew = True
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
        if comps_changed:
            # Battery/signal/sum-request and bridge-power sensors subscribe at the
            # bridge level; refresh them when a full re-sync carries component info.
            self._notify_bridge_listeners()
        if inventory_grew:
            self._notify_inventory_listeners()

        if payload.get("lastItem"):
            _LOGGER.debug(
                "Initial data complete: %d devices, %d components, %d rooms, %d scenes, %d slaves",
                len(self._devices), len(self._components),
                len(self._rooms), len(self._scenes), len(self._slave_bridges),
            )
            self._initial_data_loaded.set()
            if self._session_loaded_at is None:
                self._session_loaded_at = asyncio.get_running_loop().time()
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
        inventory_grew = False

        for item in payload.get("item", []):
            if not isinstance(item, dict):
                continue

            device_id = item.get("deviceId")
            room_id = item.get("roomId")
            comp_id = item.get("compId")

            if device_id is not None:
                device_id = int(device_id)
                if self._device_patch_grows(device_id, item):
                    inventory_grew = True
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
                if _merge_grew(self._room_state.get(room_id), item):
                    # New room fields gate room-sensor creation.
                    inventory_grew = True
                self._merge_room_store(self._room_state, room_id, item)
                changed_room_ids.add(room_id)
                continue

            if comp_id is not None:
                comp_id = int(comp_id)
                existing = self._components.get(comp_id)
                if self._comp_patch_grows(comp_id, item):
                    # New capability, e.g. a sleepy device's battery code
                    # arriving after a bridge cold boot.
                    inventory_grew = True
                self._components[comp_id] = {**(existing or {}), **item}
                notify_bridge = True
                # Device entities can read component info (contact state,
                # shading lock), so a component update refreshes its devices.
                changed_device_ids.update(self._device_ids_for_component(comp_id))

        if notify_bridge:
            self._notify_bridge_listeners()
        if inventory_grew:
            self._notify_inventory_listeners()

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

    @staticmethod
    def _actuator_kind(device: dict[str, Any]) -> str | None:
        """Return the entity shape of a switching/dimming actuator.

        Covers the platform (light vs switch) and the light's dimming support,
        both fixed when the entity is created.
        """
        if device.get("devType") not in LIGHT_DEVICE_TYPES:
            return None
        usage = _coerce_int(device.get("usage"))
        if usage is not None and usage != USAGE_LIGHT:
            return "switch"
        return "dimmable light" if _is_dimmable(device) else "light"

    def _device_patch_grows(self, device_id: int, patch: dict[str, Any]) -> bool:
        """Return True if a device patch can change which entities the device gets.

        A device first seen in a live update has no devType yet; its config,
        a usage change, or a first-seen info code (e.g. actuator temperature
        after a cold boot) must still trigger discovery. Call before merging.
        """
        existing = self._devices.get(device_id)
        new_capabilities = _info_capabilities(patch) - self._offered_device_caps[device_id]
        if existing is None:
            return True

        old_kind = self._actuator_kind(existing)
        new_kind = self._actuator_kind({**existing, **patch})
        # Entities only exist once the initial data has loaded; a kind that
        # settles while the first dump is still arriving needs no reload.
        if (
            self._initial_data_loaded.is_set()
            and old_kind is not None
            and new_kind != old_kind
        ):
            # Discovery is additive: the entity of the old kind would stay
            # active next to the new one, so rebuild the entity set.
            self._schedule_reload(
                f"device {device_id} changed from {old_kind} to {new_kind or 'other'}"
            )

        if any(key in patch and patch[key] != existing.get(key) for key in _DEVICE_SHAPE_KEYS):
            return True
        return bool(new_capabilities)

    def _comp_patch_grows(self, comp_id: int, patch: dict[str, Any]) -> bool:
        """Return True for a new component, changed type/bridge, or an unseen capability.

        A live update can create a partial component record before its config
        (compType, clientId) arrives; that config must still trigger discovery
        and the parent-bridge repair.
        """
        existing = self._components.get(comp_id)
        if existing is None:
            return True
        if any(key in patch and patch[key] != existing.get(key) for key in _COMP_SHAPE_KEYS):
            return True
        return bool(_info_capabilities(patch) - self._offered_comp_caps[comp_id])

    def _schedule_reload(self, reason: str) -> None:
        """Reload the config entry once, outside the runtime task."""
        if self._reload_scheduled or self.hass is None or self.entry is None:
            return
        self._reload_scheduled = True
        _LOGGER.info("Reloading xComfort Bridge entry: %s", reason)
        self.hass.config_entries.async_schedule_reload(self.entry.entry_id)

    def _merge_device(self, device_id: int, patch: dict[str, Any]) -> tuple[int | None, int | None]:
        """Merge a device patch into the central store and track room relinking."""
        previous_room_id = self.get_linked_room_id(device_id)
        self._devices[device_id] = {**self._devices.get(device_id, {}), **patch}
        current_room_id = self.get_linked_room_id(device_id)
        return previous_room_id, current_room_id

    @staticmethod
    def _call_listeners(listeners: Iterable[Callable[..., None]], *args: Any) -> None:
        """Call each listener; one failing entity must not starve the others.

        Listeners run inside message handling, so an escaping exception would
        also abort the rest of that message (e.g. the lastItem handling that
        restores availability).
        """
        for listener in tuple(listeners):
            try:
                listener(*args)
            except Exception:
                _LOGGER.exception("Error in xComfort update listener %s", listener)

    def _emit_button_event(self, device_id: int, event: ButtonEvent) -> None:
        """Dispatch a raw button event to subscribers."""
        self._call_listeners(self._button_listeners.get(device_id, ()), event)

    def _notify_device_listeners(self, device_id: int) -> None:
        """Notify subscribers for a specific device."""
        self._call_listeners(self._device_listeners.get(device_id, ()))

    def _notify_room_listeners(self, room_id: int) -> None:
        """Notify subscribers for a specific room."""
        self._call_listeners(self._room_listeners.get(room_id, ()))

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
        self._call_listeners(self._bridge_listeners)

    def _notify_inventory_listeners(self) -> None:
        """Notify subscribers that the inventory may have grown."""
        self._call_listeners(self._inventory_listeners)
        if self._discovery_started:
            self._mark_capabilities_offered()

    @callback
    def async_mark_discovery_started(self) -> None:
        """Record what the platforms' initial discovery saw; track growth from here.

        Called once the platforms are set up. Before that, capabilities that
        come and go (a partial info list replacing a full one) must not count
        as already offered.
        """
        self._discovery_started = True
        self._mark_capabilities_offered()

    def _mark_capabilities_offered(self) -> None:
        """Record the current capabilities as evaluated by discovery."""
        for device_id, device in self._devices.items():
            self._offered_device_caps[device_id] |= _info_capabilities(device)
        for comp_id, comp in self._components.items():
            self._offered_comp_caps[comp_id] |= _info_capabilities(comp)

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
        """Return whether a room ID is in the bridge's room configuration."""
        return room_id in self._rooms

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
        # Scenes, battery/signal and bridge sensors track availability through
        # the bridge-level subscription.
        self._notify_bridge_listeners()

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

        if message.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.CLOSING,
        ):
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
        async with asyncio.timeout(DEFAULT_CONNECT_TIMEOUT):
            client._ws = await client._session.ws_connect(f"http://{host}/")

        async with asyncio.timeout(DEFAULT_SETUP_TIMEOUT):
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

    except (aiohttp.ClientError, OSError, TimeoutError) as exc:
        raise XComfortConnectionError(
            f"Cannot connect to bridge at {host}: {_describe_error(exc)}"
        ) from exc
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        # The host accepted a websocket but spoke something other than the
        # xComfort protocol (JSON/base64/AES decode failures end up here).
        raise XComfortConnectionError(
            f"Host {host} does not appear to be an xComfort bridge: {exc}"
        ) from exc
    finally:
        await client._cleanup_connection()
