"""Shared, process-local switch for opt-in item recognition."""
from contextlib import nullcontext
import threading

from .database import Conflict, utcnow


class RecognitionControl:
    def __init__(self):
        self.lock = threading.RLock()
        self.enabled = False
        self.generation = 0
        self.enabled_since = utcnow()

    def snapshot(self):
        with self.lock:
            return {'enabled': self.enabled, 'generation': self.generation}

    def set_enabled(self, enabled):
        with self.lock:
            if self.enabled != enabled:
                self.enabled = enabled
                self.generation += 1
                if enabled:
                    self.enabled_since = utcnow()
            return self.snapshot()

    def check(self, generation):
        with self.lock:
            if not self.enabled or self.generation != generation:
                raise Conflict('recognition_disabled')


def recognition_lock(control):
    return control.lock if control else nullcontext()
