import logging

from xcomfort.devices import BridgeDevice

from .const import VERBOSE

_LOGGER = logging.getLogger(__name__)

def log(msg: str):
    if VERBOSE:
        _LOGGER.info(msg)

class BinarySensor(BridgeDevice):
    def __init__(self, bridge, device_id, name, comp_id):
        super().__init__(bridge, device_id, name)
        self.comp_id = comp_id
        self._state = False
        self.curstate = None
        self._subscribers = []
        log(f"__init__: Initialized {self}")

    def handle_state(self, payload):
        log(f"handle_state: Entry for {self.name} with payload: {payload}")
        self.curstate = payload.get('curstate')
        log(f"handle_state: Processed state for {self.name}. curstate: {self.curstate}, _state: {self._state}")
        self.notify_state_change()

    def subscribe(self, callback):
        self._subscribers.append(callback)
        log(f"subscribe: Subscriber added for {self.name}. Total subscribers now: {len(self._subscribers)}")

    def notify_state_change(self):
        log(f"notify_state_change: Notifying subscribers about state change for {self.name}. curstate: {self.curstate}, _state: {self._state}")
        for subscriber in self._subscribers:
            subscriber(self.curstate)

    def __str__(self):
        return f"BinarySensor(device_id={self.device_id}, name=\"{self.name}\", comp_id={self.comp_id}, _state={self._state}, curstate={self.curstate})"
