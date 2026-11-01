from __future__ import annotations


class Passes:
    def __init__(self, host) -> None:
        self.host = host
        self.stopped = False
        self._pumping = False

    def start(self) -> None:
        pass

    def stop(self):
        self.stopped = True
        return None

    def receive(self, trigger: dict) -> str:
        if self.host.closing():
            raise RuntimeError("Orchestrator is stopping")
        result = self.host.engine.receive(trigger)
        if result == "stored":
            self.request()
        return result

    def request(self) -> None:
        self.pump()

    def idle(self) -> None:
        self.host.effects.idle()

    def pump(self) -> None:
        if not self.host.configured() or self.host.closing():
            return
        self.host.effects.kick()
        if self.host.closing():
            return
        self.host.engine.evaluate_due()
