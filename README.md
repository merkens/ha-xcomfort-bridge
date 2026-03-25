# Eaton xComfort Bridge

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

Home Assistant integration for the [Eaton xComfort Bridge](https://www.eaton.com/). Communicates directly with the bridge over your local network — no cloud, no external dependencies.

## Background

This project started as a fork of [javydekoning/ha-xcomfort-bridge](https://github.com/javydekoning/ha-xcomfort-bridge), which is the actively maintained version of [jankrib's original integration](https://github.com/jankrib/xcomfort-bridge-hass). The initial goal was to separate the Eaton bridge protocol logic from the Home Assistant layer. As work progressed (reverse-engineering the protocol, adding new features, rewriting the bridge client) the changes became too substantial to merge back. This repo continues independently so development won't impact others currently using the original.

If you have devices or features that don't work yet, please [open an issue](https://github.com/merkens/ha-xcomfort-bridge/issues) and attach your diagnostics export.

## Requirements

- **Eaton xComfort Bridge** (hardware)
- **Home Assistant** 2025.1.0 or newer

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
| **Light** | Switching and dimming actuators with dimming profile detection |
| **Cover** | Shading actuators with position control |
| **Climate** | Room heating and cooling with HVAC modes, presets, and setpoints |
| **Sensor** | Temperature, humidity, power (W), battery level, signal strength |
| **Binary sensor** | Door/window contact sensors |
| **Event** | Pushbutton and rocker press events |
| **Diagnostics** | Full bridge state export for troubleshooting |

## What's New Compared to the Original

- **HVAC on/off control** — turn heating and cooling on/off (reverse-engineered from the Eaton app, previously an [open issue](https://github.com/jankrib/xcomfort-bridge-hass/issues/46))
- **Dual authentication** — supports both user credentials and device auth codes
- **Battery and signal sensors** — component-level diagnostics decoded from the bridge protocol
- **Multi-bridge support** — automatically discovers devices on primary + secondary bridges
- **No external dependencies** — protocol handling is embedded, no `rx` or `pycryptodome` packages needed
- **Diagnostics export** — download redacted bridge state for troubleshooting
- **Reconfigurable** — change authentication without removing the integration

## Known Limitations

- **No long-press detection** — button events are `press_up` and `press_down` only
- **Cover platform** — functional but limited testing with real shading devices
- **No scene or timer entities** — scenes and timers are not yet exposed
- **Actuator temperature** — updates only when the actuator communicates (RF protocol limitation)
- **New devices require reload** — devices added in the Eaton app are picked up after an integration reload

## Help Wanted

This integration was developed against a specific set of xComfort devices. If you have device types that are not working or not showing up:

1. Go to **Settings** → **Devices & Services** → **xComfort Bridge**
2. Click the **⋮** menu → **Download diagnostics**
3. [Open an issue](https://github.com/merkens/ha-xcomfort-bridge/issues) and attach the diagnostics file

The diagnostics export automatically redacts sensitive information (credentials, IP addresses, bridge IDs).

## License

[Apache License 2.0](LICENSE)
