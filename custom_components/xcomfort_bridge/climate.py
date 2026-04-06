"""Climate platform for the Eaton xComfort Bridge integration."""

from __future__ import annotations

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient
from .const import (
    CLIMATE_MODE_COMFORT,
    CLIMATE_MODE_ECO,
    CLIMATE_MODE_FROST,
    CLIMATE_MODE_LABELS,
    CLIMATE_STATE_COOLING_AUTO,
    CLIMATE_STATE_COOLING_MANUAL,
    CLIMATE_STATE_HEATING_AUTO,
    CLIMATE_STATE_HEATING_MANUAL,
    CLIMATE_STATE_OFF,
    DOMAIN,
)

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.PRESET_MODE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
)

PRESET_MODE_TO_CLIMATE_MODE: dict[str, int] = {v: k for k, v in CLIMATE_MODE_LABELS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up xComfort climate entities for a config entry."""
    client: XComfortBridgeClient = entry.runtime_data
    async_add_entities(
        [
            XComfortRoomClimateEntity(client, room_id)
            for room_id in client.get_room_ids()
            if client.room_has_climate(room_id)
        ]
    )


class XComfortRoomClimateEntity(ClimateEntity):
    """Represent a logical xComfort room climate."""

    _attr_has_entity_name = True
    _attr_name = "Climate"
    _attr_should_poll = False
    _attr_supported_features = SUPPORT_FLAGS
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, client: XComfortBridgeClient, room_id: int) -> None:
        """Initialize the room climate entity."""
        self._client = client
        self._room_id = room_id
        self._attr_unique_id = f"{DOMAIN}_{client.unique_prefix}_climate_{room_id}"
        self._last_active_state: int = CLIMATE_STATE_HEATING_AUTO

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime updates when the entity is added."""
        self._track_active_state()
        self.async_on_remove(
            self._client.subscribe_room_updates(self._room_id, self._handle_room_update)
        )

    @callback
    def _handle_room_update(self) -> None:
        """Write updated state when the runtime changes."""
        self._track_active_state()
        self.async_write_ha_state()

    def _track_active_state(self) -> None:
        """Remember the last non-off state so we can restore it."""
        state = self._client.get_room_state_code(self._room_id)
        if state is not None and state != CLIMATE_STATE_OFF:
            self._last_active_state = state

    @property
    def available(self) -> bool:
        """Return whether the room climate is currently available."""
        return self._client.available and self._client.get_room(self._room_id) is not None

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device registry metadata."""
        return self._client.get_room_device_info(self._room_id)

    @property
    def _linked_sensor_device_id(self) -> int | None:
        """Return the currently linked RCT sensor device for the room."""
        return self._client.get_room_sensor_device_id(self._room_id)

    @property
    def current_temperature(self) -> float | None:
        """Return the current room temperature."""
        temperature = self._client.get_room_temperature(self._room_id)
        if temperature is not None:
            return temperature
        if self._linked_sensor_device_id is not None:
            return self._client.get_rct_temperature(self._linked_sensor_device_id)
        return None

    @property
    def current_humidity(self) -> int | None:
        """Return the current room humidity."""
        humidity = self._client.get_room_humidity(self._room_id)
        if humidity is None and self._linked_sensor_device_id is not None:
            humidity = self._client.get_rct_humidity(self._linked_sensor_device_id)
        if humidity is None:
            return None
        return round(humidity)

    @property
    def target_temperature(self) -> float | None:
        """Return the target room temperature."""
        return self._client.get_room_setpoint(self._room_id)

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        state = self._client.get_room_state_code(self._room_id)
        if state == CLIMATE_STATE_OFF:
            return HVACMode.OFF
        if state in {CLIMATE_STATE_HEATING_AUTO, CLIMATE_STATE_HEATING_MANUAL}:
            return HVACMode.HEAT
        if state in {CLIMATE_STATE_COOLING_AUTO, CLIMATE_STATE_COOLING_MANUAL}:
            return HVACMode.COOL
        return HVACMode.OFF

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the supported HVAC modes based on room capabilities."""
        modes: list[HVACMode] = [HVACMode.OFF]
        if self._client.room_has_heating(self._room_id):
            modes.append(HVACMode.HEAT)
        if self._client.room_has_cooling(self._room_id):
            modes.append(HVACMode.COOL)
        return modes

    @property
    def hvac_action(self) -> HVACAction:
        """Return the current running HVAC action."""
        state = self._client.get_room_state_code(self._room_id)
        valve = self._client.get_room_valve(self._room_id) or 0
        power = self._client.get_room_power(self._room_id) or 0.0

        if state == CLIMATE_STATE_OFF:
            return HVACAction.IDLE

        if valve > 0 or power > 0:
            if state in {CLIMATE_STATE_COOLING_AUTO, CLIMATE_STATE_COOLING_MANUAL}:
                return HVACAction.COOLING
            if state in {CLIMATE_STATE_HEATING_AUTO, CLIMATE_STATE_HEATING_MANUAL}:
                return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        return CLIMATE_MODE_LABELS.get(self._client.get_room_mode_code(self._room_id))

    @property
    def preset_modes(self) -> list[str]:
        """Return the supported preset modes."""
        return list(CLIMATE_MODE_LABELS.values())

    @property
    def min_temp(self) -> float:
        """Return the minimum target temperature for display."""
        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum target temperature for display."""
        return 35.0

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (off/heat/cool).

        Preserves the auto/manual distinction from the last active state so that
        turning a room back on restores the previous scheduling behaviour.
        """
        current_state = self._client.get_room_state_code(self._room_id)

        if hvac_mode == HVACMode.OFF:
            target_state = CLIMATE_STATE_OFF
        elif hvac_mode == HVACMode.HEAT:
            if self._last_active_state == CLIMATE_STATE_HEATING_MANUAL:
                target_state = CLIMATE_STATE_HEATING_MANUAL
            else:
                target_state = CLIMATE_STATE_HEATING_AUTO
        elif hvac_mode == HVACMode.COOL:
            if self._last_active_state == CLIMATE_STATE_COOLING_MANUAL:
                target_state = CLIMATE_STATE_COOLING_MANUAL
            else:
                target_state = CLIMATE_STATE_COOLING_AUTO
        else:
            return

        if current_state != target_state:
            await self._client.async_set_room_hvac_state(self._room_id, target_state)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set a bridge preset mode for the room."""
        target_mode = PRESET_MODE_TO_CLIMATE_MODE.get(preset_mode)
        if target_mode is None:
            return

        current_state = self._client.get_room_state_code(self._room_id)

        # Turn the room on first if it's currently off.
        if current_state == CLIMATE_STATE_OFF:
            wake_state = self._last_active_state
            await self._client.async_set_room_hvac_state(self._room_id, wake_state)
            current_state = wake_state

        if current_state in {CLIMATE_STATE_COOLING_AUTO, CLIMATE_STATE_COOLING_MANUAL}:
            target_state = CLIMATE_STATE_COOLING_MANUAL
        else:
            target_state = CLIMATE_STATE_HEATING_MANUAL

        current_mode = self._client.get_room_mode_code(self._room_id) or CLIMATE_MODE_COMFORT
        current_setpoint = self.target_temperature or self._default_setpoint_for_mode(current_mode)

        await self._client.async_set_room_climate_preset(
            self._room_id,
            current_mode,
            target_state,
            current_setpoint,
        )

        await self._client.async_set_room_climate_preset(
            self._room_id,
            target_mode,
            target_state,
            self._default_setpoint_for_mode(target_mode),
        )

    async def async_set_temperature(self, **kwargs) -> None:
        """Set the room target temperature."""
        if ATTR_TEMPERATURE not in kwargs:
            return

        requested = float(kwargs[ATTR_TEMPERATURE])
        current_mode = self._client.get_room_mode_code(self._room_id) or CLIMATE_MODE_COMFORT
        current_state = self._client.get_room_state_code(self._room_id)

        if current_state is None or current_state == CLIMATE_STATE_OFF:
            wake_state = self._last_active_state
            await self._client.async_set_room_hvac_state(self._room_id, wake_state)
            current_state = wake_state

        await self._client.async_set_room_climate_preset(
            self._room_id,
            current_mode,
            current_state,
            max(self.min_temp, min(self.max_temp, requested)),
        )

    def _default_setpoint_for_mode(self, mode: int) -> float:
        """Return the configured default setpoint for a bridge preset."""
        room = self._client.get_room(self._room_id) or {}
        for mode_data in room.get("modes", []):
            if mode_data.get("mode") == mode:
                try:
                    return float(mode_data.get("value"))
                except (TypeError, ValueError):
                    break

        defaults = {
            CLIMATE_MODE_FROST: 12.0,
            CLIMATE_MODE_ECO: 18.0,
            CLIMATE_MODE_COMFORT: 21.0,
        }
        return defaults.get(mode, 21.0)
