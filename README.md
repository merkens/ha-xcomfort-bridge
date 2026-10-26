# Eaton xComfort Bridge

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant integration for the [Eaton xComfort Bridge](https://www.eaton.com/). Communicates directly with the bridge over your local network — no cloud, no external dependencies.

## Background

This project started as a fork of [javydekoning/ha-xcomfort-bridge](https://github.com/javydekoning/ha-xcomfort-bridge), which is the actively maintained version of [jankrib's original integration](https://github.com/jankrib/xcomfort-bridge-hass). The initial goal was to separate the Eaton bridge protocol logic from the Home Assistant layer. As work progressed (reverse-engineering the protocol, adding new features, rewriting the bridge client) the changes became too substantial to merge back. This repo continues independently so development won't impact others currently using the original.

If you have devices or features that don't work yet, please [open an issue](https://github.com/merkens/ha-xcomfort-bridge/issues) and attach your diagnostics export.

## Requirements

- **Eaton xComfort Bridge** (hardware)
- **Home Assistant** 2025.3.0 or newer

## Installation

### HACS (recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → **⋮** (top right) → **Custom repositories**
3. Add `merkens/ha-xcomfort-bridge` with category **Integration**
4. Search for "Eaton xComfort Bridge" and install
5. Restart Home Assistant

### Manual

1. Copy `custom_components/xcomfort_bridge/` to your Home Assistant `config/custom_components/` folder
2. Restart Home Assistant

### Switching from the original integration

This integration uses the same domain (`xcomfort_bridge`) as the original jankrib / javydekoning integration, but its configuration is not compatible and existing entries are not migrated. Before installing:

1. Remove the existing xComfort Bridge integration entry under **Settings** → **Devices & Services**
2. Remove the old integration (HACS or `custom_components/xcomfort_bridge/`) and restart Home Assistant
3. Install this integration and add the bridge again

Entity IDs and history from the original integration do not carry over, so automations and dashboards that reference them need updating.

### Upgrading to 0.5.0

- Home Assistant 2025.3 or newer is required.
- Actuators whose usage in the Eaton app is not lighting (load, appliance, heating/cooling output, ...) are now `switch` entities instead of `light` entities. Update automations, scripts and dashboards that use the old `light.*` entity; the old light then appears in the repair notice under **Settings** → **Repairs** and can be deleted from its entity settings.

## Configuration

The integration is set up via the Home Assistant UI:

1. Go to **Settings** → **Devices & Services** → **Add Integration**
2. Search for "Eaton xComfort Bridge"
3. Enter the bridge IP address
4. Choose authentication mode:
   - **User credentials** — username and password (same as the Eaton app)
   - **Device auth code** — the auth key printed on the bridge
5. The integration connects, validates credentials, and creates entities

## Supported Platforms

| Platform | Description |
|----------|-------------|
| **Light** | Switching and dimming actuators used for lighting, with dimming profile detection |
| **Switch** | Switching and dimming actuators the bridge has configured for another use (load, appliance, heating/cooling output, ...) |
| **Cover** | Shading actuators — open, close, and stop; cover type (shade, shutter, awning, blind) from the app's shading type |
| **Climate** | Room heating and cooling with HVAC modes, presets, and setpoints |
| **Sensor** | Temperature (room controllers and standalone sensors), humidity, power (W), battery level, signal strength |
| **Binary sensor** | Door/window contacts (untested, see below), binary input channels, and heating hub heat-demand output |
| **Event** | Pushbutton and rocker press events with per-control naming |
| **Scene** | Activate bridge-configured scenes |
| **Diagnostics** | Full redacted bridge state export for troubleshooting |

## What's New Compared to the Original

- **HVAC on/off control** — turn heating and cooling on/off (reverse-engineered from the Eaton app, previously an [open issue](https://github.com/jankrib/xcomfort-bridge-hass/issues/46))
- **Dual authentication** — supports both user credentials and device auth codes
- **Scene entities** — activate bridge-configured scenes directly from Home Assistant
- **Battery and signal sensors** — component-level diagnostics decoded from the bridge protocol
- **Expanded device support** — standalone temperature sensors, binary inputs, and multi-channel heating actuators
- **Runtime device discovery** — devices added in the Eaton app appear in Home Assistant automatically, no reload needed
- **Multi-bridge support** — automatically discovers devices on primary + secondary bridges
- **No external dependencies** — protocol handling is embedded, no `rx` or `pycryptodome` packages needed
- **Diagnostics export** — download redacted bridge state for troubleshooting
- **Reconfigurable** — change host or authentication without removing the integration; a rejected login prompts for new credentials
- **Loads as switches** — actuators the bridge has configured for something other than lighting become switches, so they stay out of light groups
- **Self-healing connection** — reconnects with a bounded backoff, and entities follow the bridge's availability
- **Clean-up notice** — devices and entities the bridge no longer reports are listed in one Home Assistant repair notice

## Known Limitations

- **No long-press detection** — button events are `press_up` and `press_down` only.
- **No timer entities** — timer payloads are known but not yet exposed
- **Cover position** — covers support open/close/stop; go-to-position is not yet available
- **Cover safety** — commands are always sent; the actuator itself refuses them while a wind/rain safety lock is active. The `safety_function` and `safety_locked` attributes show the app setting and the lock state (lock detection not yet confirmed on hardware)
- **Door/window contacts** — implemented from the Eaton app's data model but not yet tested on a real contact; please open an issue with a diagnostics export if you have one
- **Actuator temperature** — updates only when the actuator communicates (RF protocol limitation)
- **Bridge power loss** — after a bridge reboot (e.g. power outage) the bridge reports default states (lights "off", battery unknown) until each device next transmits over RF; Home Assistant mirrors the bridge, so states recover as devices report in

## Help Wanted

This integration was developed against a specific set of xComfort devices. If you have device types that are not working or not showing up:

1. Go to **Settings** → **Devices & Services** → **xComfort Bridge**
2. Click the **⋮** menu → **Download diagnostics**
3. [Open an issue](https://github.com/merkens/ha-xcomfort-bridge/issues) and attach the diagnostics file

The diagnostics export automatically redacts sensitive information such as credentials, IP addresses, bridge IDs, location data, and MAC addresses.

## License

[Apache License 2.0](LICENSE)
