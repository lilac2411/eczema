"""USB serial bridge to the ESP32 eczema tracker.

A background thread reads JSON lines from the ESP32 and keeps the latest state:

    {"type":"ready"}
    {"type":"env","t":23.4,"h":41.0}
    {"type":"status","event":"ALARM_ON" | "ALARM_OFF" | "BTN"}

and sends plain-text commands back: TIME <local epoch>, SCHED <minutes>, ACK, PING.

Never call Streamlit functions from here; the thread only updates plain attributes.
"""
import datetime as dt
import json
import random
import threading
import time
from collections import deque

import serial
from serial.tools import list_ports

BAUD = 115200
STALE_AFTER_S = 30          # readings older than this are not attached to photos


class Device:
    def __init__(self):
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._ser = None
        self._thread = None
        self._stop = threading.Event()
        self._synced = False
        self.log = deque(maxlen=200)
        self.reminder_minute = 20 * 60
        self._reset_state()

    def _reset_state(self):
        self.temp = None
        self.hum = None
        self.reading_ts = None
        self.alarm = False
        self.button_event = False
        self.connected = False
        self.mode = "disconnected"      # disconnected | usb | sim
        self.error = None

    # ---------- discovery ----------
    @staticmethod
    def list_ports() -> list[tuple[str, str]]:
        return [(p.device, p.description) for p in list_ports.comports()]

    # ---------- connection ----------
    def connect(self, port: str) -> bool:
        self.disconnect()
        try:
            ser = serial.Serial(port, BAUD, timeout=0.2)   # opening usually resets the ESP32
        except (serial.SerialException, OSError) as e:
            with self._lock:
                self.error = f"Could not open {port}: {e}. Close the Arduino Serial Monitor and try again."
            return False
        self._ser = ser
        self._stop.clear()
        self._synced = False
        with self._lock:
            self._reset_state()
            self.connected, self.mode = True, "usb"
        self._thread = threading.Thread(target=self._run_usb, args=(ser,), daemon=True)
        self._thread.start()
        return True

    def start_simulation(self):
        self.disconnect()
        self._stop.clear()
        with self._lock:
            self._reset_state()
            self.connected, self.mode = True, "sim"
        self._thread = threading.Thread(target=self._run_sim, daemon=True)
        self._thread.start()

    def disconnect(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None
        self._ser = None
        with self._lock:
            self._reset_state()

    # ---------- commands ----------
    def send(self, text: str):
        ser = self._ser
        if ser is None or self.mode != "usb":
            return
        try:
            with self._write_lock:
                ser.write((text + "\n").encode())
        except (serial.SerialException, OSError) as e:
            with self._lock:
                self.error = f"Write failed: {e}"

    def ack(self):
        """Stop the alarm on the device (called when the photo is saved or Stop is pressed)."""
        self.send("ACK")
        with self._lock:
            self.alarm = False

    def set_reminder(self, minutes: int):
        self.reminder_minute = minutes
        self.send(f"SCHED {minutes}")

    def _sync(self):
        """The ESP32 has no RTC, so give it local time and the reminder schedule."""
        offset = dt.datetime.now().astimezone().utcoffset().total_seconds()
        self.send(f"TIME {int(time.time() + offset)}")
        self.send(f"SCHED {self.reminder_minute}")
        self._synced = True

    # ---------- state access ----------
    def snapshot(self) -> dict:
        with self._lock:
            age = None if self.reading_ts is None else time.time() - self.reading_ts
            return {
                "temp": self.temp, "hum": self.hum, "age": age,
                "fresh": age is not None and age <= STALE_AFTER_S,
                "alarm": self.alarm, "connected": self.connected,
                "mode": self.mode, "error": self.error,
            }

    def latest_reading(self, max_age: float = STALE_AFTER_S) -> dict | None:
        """Reading to store with a photo, or None if the sensor is offline/stale."""
        with self._lock:
            if self.temp is None or self.reading_ts is None:
                return None
            if time.time() - self.reading_ts > max_age:
                return None
            return {
                "temp": round(self.temp, 1), "humidity": round(self.hum, 1),
                "source": self.mode,
                "read_at": dt.datetime.fromtimestamp(self.reading_ts).isoformat(timespec="seconds"),
            }

    def pop_button_event(self) -> bool:
        with self._lock:
            fired, self.button_event = self.button_event, False
            return fired

    # ---------- worker threads ----------
    def _handle_line(self, line: str):
        if not line:
            return
        if not line.startswith("{"):          # ESP32 boot noise
            self.log.append(line)
            return
        self.log.append(line)
        try:
            m = json.loads(line)
        except ValueError:
            return                            # partial line
        kind = m.get("type")
        if kind == "env":
            with self._lock:
                self.temp, self.hum, self.reading_ts = float(m["t"]), float(m["h"]), time.time()
        elif kind == "status":
            ev = m.get("event")
            with self._lock:
                if ev == "ALARM_ON":
                    self.alarm = True
                elif ev == "ALARM_OFF":
                    self.alarm = False
                elif ev == "BTN":
                    self.alarm = False
                    self.button_event = True
        elif kind == "ready":
            self._sync()

    def _run_usb(self, ser):
        buf = b""
        started = last_ping = time.time()
        while not self._stop.is_set():
            try:
                chunk = ser.read(256)
            except (serial.SerialException, OSError) as e:
                with self._lock:
                    self.error = f"Serial connection lost: {e}"
                break
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._handle_line(line.decode("utf-8", "ignore").strip())
            now = time.time()
            if not self._synced and now - started > 2.5:   # covers boards that don't reset on open
                self._sync()
            if now - last_ping > 5:                        # heartbeat: LCD shows "USB"
                last_ping = now
                self.send("PING")
        try:
            ser.close()
        except Exception:
            pass
        with self._lock:
            self.connected = False
            self.mode = "disconnected"

    def _run_sim(self):
        t, h = 23.0, 40.0
        started = time.time()
        fired = False
        while not self._stop.is_set():
            t = min(28, max(19, t + random.uniform(-0.3, 0.3)))
            h = min(70, max(20, h + random.uniform(-2, 2)))
            with self._lock:
                self.temp, self.hum, self.reading_ts = t, h, time.time()
                if not fired and time.time() - started > 8:
                    self.alarm, fired = True, True
            self._stop.wait(2)
