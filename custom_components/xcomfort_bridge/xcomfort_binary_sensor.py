import logging

from xcomfort.devices import BridgeDevice, DeviceState

from .const import VERBOSE

_LOGGER = logging.getLogger(__name__)

def log(msg: str):
    if VERBOSE:
        _LOGGER.info(msg)

class BinarySensorState(DeviceState):
    def __init__(self, is_top, payload):
        super().__init__(payload)
        self.isTop = is_top

    def __str__(self):
        return f"BinarySensorState(isTop={self.isTop})"

    __repr__ = __str__

class BinarySensor(BridgeDevice):
    def __init__(self, bridge, device_id, name, comp_id):
        super().__init__(bridge, device_id, name)

        self.comp_id = comp_id

    def handle_state(self, payload):
        curstate = payload.get('curstate', 0)  
        is_top = bool(curstate)
        self.state.on_next(BinarySensorState(is_top, payload))

    def __str__(self):
        return f"BinarySensor(device_id={self.device_id}, name=\"{self.name}\", comp_id={self.comp_id}, State: {self.state})"