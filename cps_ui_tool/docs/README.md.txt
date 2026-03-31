# CPS UI Utility

## Install
```bash
pip install -r requirements.txt

RUN:
python src/app.py


Features

Connect/Disconnect to IP:PORT

Stream tab: prints CPS packet details + rectified V/I

Plot tab: checkbox "Enable Plot" plots rectified voltage & current vs incoming timestamp (same plot)

Buttons: example Fetch/Set + Custom HEX send


---

# What you need to customize (your protocol-specific “fetch/set”)
In `src/app.py`, these are placeholders you should replace with your **real frames**:

- `send_fetch_status()`
- `send_fetch_power()`
- `send_set_mode()`

