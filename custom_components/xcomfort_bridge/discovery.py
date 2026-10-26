"""Runtime entity discovery for the Eaton xComfort Bridge integration.

Platforms register an ID getter and an entity factory; entities are created
for the IDs present at setup and again whenever the bridge inventory grows
(new devices paired, capabilities appearing after a cold boot, rooms gaining
climate). Discovery is additive only — entities are never removed at runtime;
stale registry entries are handled by the device-removal hook and the
setup-time disappearance warnings.
"""

from __future__ import annotations

from typing import Callable, Hashable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .bridge_client import XComfortBridgeClient


@callback
def async_setup_dynamic_entities(
    entry: ConfigEntry,
    client: XComfortBridgeClient,
    async_add_entities: AddConfigEntryEntitiesCallback,
    ids_getter: Callable[[XComfortBridgeClient], Iterable[Hashable]],
    factory: Callable[[Hashable], Entity],
) -> None:
    """Create entities for current IDs and for IDs that appear later."""
    known: set[Hashable] = set()

    @callback
    def _recheck() -> None:
        new_ids = [item for item in ids_getter(client) if item not in known]
        if not new_ids:
            return
        known.update(new_ids)
        async_add_entities([factory(item) for item in new_ids])

    entry.async_on_unload(client.subscribe_inventory_updates(_recheck))
    _recheck()
