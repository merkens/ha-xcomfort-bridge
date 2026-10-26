"""Constants for the Eaton xComfort Bridge integration."""

from homeassistant.const import Platform

DOMAIN = "xcomfort_bridge"

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SCENE,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONF_AUTH_MODE = "auth_mode"
CONF_SECRET = "secret"

AUTH_MODE_DEVICE = "device"
AUTH_MODE_USER = "user"
AUTH_MODES = {
    AUTH_MODE_USER: "User",
    AUTH_MODE_DEVICE: "Auth code",
}

DEFAULT_DEVICE_USERNAME = "default"
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_READ_TIMEOUT = 90
DEFAULT_RECONNECT_MAX = 60
DEFAULT_STARTUP_TIMEOUT = 15
# Bound on the login handshake and on the initial data that follows it, so a
# bridge that accepts the websocket and then goes quiet can't hang a reconnect.
DEFAULT_SETUP_TIMEOUT = 15
# A session that stayed loaded this long counts as healthy: the next reconnect
# starts again at the shortest backoff.
DEFAULT_STABLE_SESSION = 60
# Consecutive login rejections after startup before asking for new credentials
# (a booting bridge can briefly reject a valid login).
DEFAULT_AUTH_RETRIES = 3

MESSAGE_ACK = 1
MESSAGE_HEARTBEAT = 2
MESSAGE_CONNECT_CONFIRM = 11
MESSAGE_SC_INIT = 14
MESSAGE_SC_SECRET = 16
MESSAGE_SC_ESTABLISHED = 17
MESSAGE_AUTH_LOGIN = 30
MESSAGE_AUTH_LOGIN_DENIED = 31
MESSAGE_AUTH_LOGIN_SUCCESS = 32
MESSAGE_AUTH_APPLY_TOKEN = 33
MESSAGE_AUTH_APPLY_TOKEN_RESPONSE = 34
MESSAGE_AUTH_RENEW_TOKEN = 37
MESSAGE_AUTH_RENEW_TOKEN_RESPONSE = 38
MESSAGE_INITIAL_DATA = 240
MESSAGE_HOME_DATA = 242
MESSAGE_DIAGNOSTICS = 243
MESSAGE_ACTION_SLIDE_DEVICE = 280
MESSAGE_ACTION_SWITCH_DEVICE = 281
MESSAGE_ACTIVATE_SCENE = 285
MESSAGE_ACTION_SHADING_DEVICE = 355
MESSAGE_SET_DEVICE_STATE = 291
MESSAGE_SET_ALL_DATA = 300
MESSAGE_SET_HOME_DATA = 303
MESSAGE_SET_DIAGNOSTICS = 304
MESSAGE_SET_STATE_INFO = 310
MESSAGE_SET_HEATING_STATE = 353
MESSAGE_SET_ROOM_HEATING_STATE = 363
MESSAGE_SET_BRIDGE_STATE = 364

DEVTYPE_SWITCH = 100
DEVTYPE_DIMMER = 101
DEVTYPE_SHADING = 102
DEVTYPE_PUSHBUTTON = 201
DEVTYPE_BINARY_INPUT = 211
DEVTYPE_ROCKER = 220
DEVTYPE_TEMP_SENSOR = 410
DEVTYPE_ROOM_CONTROLLER = 450

DP_ON_OFF = "1351"

INFO_CODE_DEVICE_TEMPERATURE = "1109"
INFO_CODE_SIGNAL_STRENGTH = "1111"
INFO_CODE_TEMPERATURE = "1222"
INFO_CODE_PT1000_TEMPERATURE = "1224"
INFO_CODE_RCT_HUMIDITY = "1223"
INFO_CODE_SUM_REQUEST = "1227"

BATTERY_CODE_MAP: dict[str, int] = {
    "1113": 0,
    "1114": 25,
    "1115": 50,
    "1116": 75,
    "1117": 100,
}
# "battery-unknown": a battery device that has not yet reported a level (common
# for sleepy transmitters like pushbuttons at startup). Marks the component as
# battery-powered so a battery entity is still created; value stays unknown
# until a real level (1113-1117) arrives.
BATTERY_CODE_UNKNOWN = "1118"
# Any code that identifies a battery-powered component (level known or unknown).
BATTERY_CODES: frozenset[str] = frozenset(BATTERY_CODE_MAP) | {BATTERY_CODE_UNKNOWN}
BATTERY_CODE_MAINS = "1119"

SIGNAL_LABELS: dict[str, str] = {
    "0": "Unknown",
    "1": "Excellent",
    "2": "Good",
    "3": "Fair",
    "4": "Poor",
}

CLIMATE_MODE_FROST = 1
CLIMATE_MODE_ECO = 2
CLIMATE_MODE_COMFORT = 3

CLIMATE_STATE_OFF = 0
CLIMATE_STATE_HEATING_AUTO = 1
CLIMATE_STATE_HEATING_MANUAL = 2
CLIMATE_STATE_COOLING_AUTO = 3
CLIMATE_STATE_COOLING_MANUAL = 4

CLIMATE_MODE_LABELS: dict[int, str] = {
    CLIMATE_MODE_FROST: "Frost protection",
    CLIMATE_MODE_ECO: "eco",
    CLIMATE_MODE_COMFORT: "comfort",
}

# Setpoint bounds per preset mode (APK `w0` table, key "m<regulation><mode>").
# Heating bounds are identical for regulation 0 (room temperature), 1 (floor
# temperature), and 3, so they collapse to per-mode. Regulation 2 is
# "effect regulation" (percent 0-100) — no hardware observed; callers fall
# back to the generic range. Cooling bounds are keyed by mode only ("m<mode>Cool").
CLIMATE_REGULATION_PERCENT = 2
CLIMATE_SETPOINT_FALLBACK: tuple[float, float] = (5.0, 35.0)
CLIMATE_SETPOINT_BOUNDS_HEAT: dict[int, tuple[float, float]] = {
    CLIMATE_MODE_FROST: (5.0, 20.0),
    CLIMATE_MODE_ECO: (10.0, 30.0),
    CLIMATE_MODE_COMFORT: (18.0, 40.0),
}
CLIMATE_SETPOINT_BOUNDS_COOL: dict[int, tuple[float, float]] = {
    CLIMATE_MODE_FROST: (20.0, 50.0),
    CLIMATE_MODE_ECO: (10.0, 40.0),
    CLIMATE_MODE_COMFORT: (0.0, 30.0),
}

# Door/window contacts (Eaton app model): a devType 202 ("SWITCH") channel on
# a compType 76 component. The component mode says door or window
# (1308/1309 window, 1310/1311 door; the second of each = "ON when opened"),
# and the open/closed state is in the component info codes.
COMPTYPE_DOOR_WINDOW_SENSOR = 76
DEVTYPE_CONTACT = 202
DOOR_WINDOW_DOOR_MODES = frozenset({"1310", "1311"})
INFO_CODES_CONTACT_OPEN = frozenset({"1121", "1123"})
INFO_CODES_CONTACT_CLOSED = frozenset({"1122", "1124"})

# Shading actuator lock state: info 1103/1127 ("Locked"), or a safety movement
# (curstate 4 = SAFETY_UP, 5 = SAFETY_DOWN). Not yet seen on real hardware.
INFO_CODES_SHADING_LOCKED = frozenset({"1103", "1127"})
SHADING_SAFETY_STATES = frozenset({4, 5})

LIGHT_DEVICE_TYPES = {DEVTYPE_SWITCH, DEVTYPE_DIMMER}
COVER_DEVICE_TYPES = {DEVTYPE_SHADING}
EVENT_DEVICE_TYPES = {DEVTYPE_PUSHBUTTON, DEVTYPE_ROCKER}

SHADE_OP_OPEN = 0
SHADE_OP_CLOSE = 1
SHADE_OP_STOP = 2
SHADE_OP_GO_TO = 5

# Device usage values that indicate heating/cooling capability (APK Dn enum).
USAGE_HEATING: frozenset[int] = frozenset({2, 22, 23, 25, 101, 102})
USAGE_COOLING: frozenset[int] = frozenset({21, 22, 24, 25, 101, 102})

# Actuators with usage LIGHT (or none reported) are lights; any other usage
# (load, appliance, heating/cooling output, ...) is exposed as a switch.
USAGE_LIGHT = 0
USAGE_LABELS: dict[int, str] = {
    0: "light",
    1: "load",
    2: "sum_heating",
    3: "shading",
    4: "water",
    5: "routing",
    6: "water_heating",
    7: "vehicle_charger",
    8: "high_load_appliance",
    21: "sum_cooling",
    22: "sum_heating_cooling",
    23: "heating",
    24: "cooling",
    25: "heating_cooling",
    26: "switch_heating_cooling",
    27: "switch_cooling",
    28: "switch_heating",
}
