"""Continuously preview the camera and simulate three door sensors with IO21."""

import json
import os
import time


EVENT_PREFIX = "MINDBOX_DOOR "
DEVICE_ID = "k230-key-01"
CABINET_ID = "cabinet-01"
DEBOUNCE_MS = 25
CLICK_GAP_MS = 500
HEARTBEAT_MS = 2000
CAMERA_WIDTH = 1280
CAMERA_HEIGHT = 720


def ticks_diff(now, before):
    if hasattr(time, "ticks_diff"):
        return time.ticks_diff(now, before)
    return now - before


class KeyClicks:
    def __init__(self, gap_ms=CLICK_GAP_MS, debounce_ms=DEBOUNCE_MS):
        self.gap_ms = gap_ms
        self.debounce_ms = debounce_ms
        self.raw = self.stable = 1
        self.raw_since = 0
        self.last_release = 0
        self.count = 0

    def poll(self, level, now):
        result = None
        # Complete the previous group before accepting a new, late press.
        if self.count and self.stable == 1 and self.raw == 1 and ticks_diff(now, self.last_release) >= self.gap_ms:
            result, self.count = self.count, 0
        if level != self.raw:
            self.raw, self.raw_since = level, now
        if self.raw != self.stable and ticks_diff(now, self.raw_since) >= self.debounce_ms:
            self.stable = self.raw
            if self.stable == 1:
                self.count += 1
                self.last_release = now
        return result


def new_boot_id():
    import machine
    try:
        import uhashlib as hashlib
    except ImportError:
        import hashlib
    seed = os.urandom(16) + str(time.ticks_us()).encode() + str(time.time()).encode()
    if hasattr(machine, "rng"):
        seed += str(machine.rng()).encode()
    return "".join("%02x" % value for value in hashlib.sha256(seed).digest())[:16]


def main():
    from machine import FPIOA, Pin

    os.exitpoint(os.EXITPOINT_ENABLE)
    fpioa = FPIOA()
    fpioa.set_function(21, FPIOA.GPIO21)
    key = Pin(21, Pin.IN, Pin.PULL_UP)
    clicks = KeyClicks()
    doors = [False, False, False]
    boot_id, sequence = new_boot_id(), 0
    presses, releases, last_group = 0, 0, None
    sensor = display_ready = None
    camera_frames = 0

    def publish(drawer=None):
        nonlocal sequence
        sequence += 1
        kind = "snapshot" if drawer is None else "door.opened" if doors[drawer - 1] else "door.closed"
        print(EVENT_PREFIX + json.dumps({
            "schema": 1, "device_id": DEVICE_ID, "cabinet_id": CABINET_ID,
            "boot_id": boot_id, "seq": sequence, "kind": kind,
            "source": "key", "drawer": drawer, "open": list(doors),
            "key": {"level": key.value(), "stable": clicks.stable,
                    "presses": presses, "releases": releases,
                    "pending": clicks.count, "last_group": last_group,
                    "camera_frames": camera_frames},
        }))

    try:
        # CanMV forwards the IDE framebuffer as JPEG frames on its USB bridge.
        from media.sensor import Sensor
        from media.display import Display
        sensor = Sensor(fps=30)
        sensor.reset()
        sensor.set_framesize(width=CAMERA_WIDTH, height=CAMERA_HEIGHT)
        sensor.set_pixformat(Sensor.RGB565)
        sensor.run()
        Display.init(Display.VIRT, width=CAMERA_WIDTH, height=CAMERA_HEIGHT, fps=30, to_ide=True)
        display_ready = Display
        print("Camera ready:", CAMERA_WIDTH, "x", CAMERA_HEIGHT, "RGB565 to IDE")
        print("KEY door test ready; IO21; 1/2/3 clicks select top/middle/bottom")
        publish()
        last_publish = time.ticks_ms()
        while True:
            os.exitpoint()
            now = time.ticks_ms()
            previous = clicks.stable
            count = clicks.poll(key.value(), now)
            if previous != clicks.stable:
                if clicks.stable == 0:
                    presses += 1
                else:
                    releases += 1
                print("KEY level:", clicks.stable, "presses:", presses, "releases:", releases)
            if count is not None:
                last_group = count
                if 1 <= count <= 3:
                    doors[count - 1] = not doors[count - 1]
                    publish(count)
                    last_publish = now
                else:
                    print("KEY ignored click group:", count)
            image = sensor.snapshot()
            display_ready.show_image(image)
            camera_frames += 1
            if ticks_diff(now, last_publish) >= HEARTBEAT_MS:
                publish()
                last_publish = now
            time.sleep_ms(1)
    finally:
        try:
            if sensor is not None:
                sensor.stop()
        finally:
            if display_ready is not None:
                display_ready.deinit()
            time.sleep_ms(100)


if __name__ == "__main__":
    main()
