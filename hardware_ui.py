"""All Streamlit UI for the indoor tracker hardware, kept out of app.py to avoid merge conflicts.

app.py only needs these calls:
    HW.apply_pending_camera_request()   top of script, before any widgets
    HW.sidebar()                        ESP32 port, connect, reminder time
    HW.live_panel()                     live readings + alarm banner (auto-refreshes)
    HW.latest_reading()                 dict to store with a photo, or None
    HW.on_photo_saved()                 stops the alarm on the device
"""
import datetime as dt

import streamlit as st

from device import Device


@st.cache_resource
def _device() -> Device:
    return Device()          # one serial bridge shared by every rerun and session


def latest_reading() -> dict | None:
    """{"temp", "humidity", "source", "read_at"} if the sensor is live, else None."""
    return _device().latest_reading()


def on_photo_saved():
    _device().ack()


def apply_pending_camera_request():
    """The physical button sets a flag; apply it before the camera toggle is created."""
    if st.session_state.pop("open_camera", False):
        st.session_state["use_camera"] = True


def sidebar():
    dev = _device()
    st.sidebar.title("Indoor tracker")
    ports = Device.list_ports()
    port = None
    if ports:
        names = dict(ports)
        port = st.sidebar.selectbox("ESP32 serial port", list(names),
                                    format_func=lambda p: f"{p} - {names[p]}")
    else:
        st.sidebar.caption("No serial ports found. Check the USB cable and driver.")

    c1, c2 = st.sidebar.columns(2)
    if c1.button("Connect", disabled=port is None, use_container_width=True):
        if not dev.connect(port):
            st.sidebar.error(dev.snapshot()["error"])
    if c2.button("Disconnect", use_container_width=True):
        dev.disconnect()
    if st.sidebar.button("Use simulated sensor", use_container_width=True):
        dev.start_simulation()

    remind = st.sidebar.time_input("Daily reminder", dt.time(20, 0), key="remind")
    if st.sidebar.button("Save reminder to device"):
        dev.set_reminder(remind.hour * 60 + remind.minute)
        st.sidebar.success(f"Reminder set for {remind.strftime('%H:%M')}")

    env = dev.latest_reading()
    st.sidebar.caption(
        f"Next photo will be tagged {env['temp']:.1f} °C / {env['humidity']:.0f}% humidity."
        if env else "No fresh sensor reading, so the next photo will have no room data.")


@st.fragment(run_every=2)
def live_panel():
    """Refreshes every 2 s without rerunning the whole page."""
    dev = _device()
    if dev.pop_button_event():                  # physical button pressed on the device
        st.session_state["open_camera"] = True
        st.rerun()                              # full rerun so the camera toggle can flip

    s = dev.snapshot()
    if s["alarm"]:
        st.error("Time for your daily skin photo. Use the sidebar, or press the button on the device.")
        if st.button("Stop alarm", key="stop_alarm"):
            dev.ack()
            st.rerun(scope="fragment")

    c1, c2, c3 = st.columns(3)
    c1.metric("Room temperature", f"{s['temp']:.1f} °C" if s["temp"] is not None else "--")
    c2.metric("Humidity", f"{s['hum']:.0f} %" if s["hum"] is not None else "--")
    if not s["connected"]:
        status = "Not connected"
    elif s["mode"] == "sim":
        status = "Simulated sensor"
    elif s["fresh"]:
        status = "USB connected"
    else:
        status = "Waiting for data..."
    c3.metric("Device", status)
    if s["error"]:
        st.caption(s["error"])
    if s["hum"] is not None and s["fresh"]:
        if s["hum"] < 30:
            st.info("Humidity is low. Dry air can trigger flare-ups, so consider moisturizing.")
        elif s["hum"] > 60:
            st.info("Humidity is high. Heat and sweat can irritate skin, so keep the area cool and dry.")
