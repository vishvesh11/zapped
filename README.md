# Buzz Wire Shock Game: setup guide (VS Code + Raspberry Pi Pico W)

```
buzzwire/
├── backend/main.py        FastAPI server: game state, chime, score, leaderboard
├── frontend/app.py        Streamlit control panel
├── firmware/              MicroPython for the Pico W
│   ├── config.py          EDIT THIS: Wi-Fi, backend IP, pins, relay/buzzer type
│   ├── boot.py  net.py  main.py
│   ├── wiring_check.py    test the track/hooks/buzzer (never fires the relay)
│   └── relay_test.py      test the relay (5 short pulses)
├── .vscode/               recommended extensions, run tasks, debug config
└── requirements.txt
```

## SAFETY (read first)
- Never use on anyone with a pacemaker/heart condition, epilepsy, or who is pregnant; not for young children. Get consent from every player.
- Keep the toy's own cells (do not swap for stronger batteries) and keep the 0.5 s duration cap.
- Keep the kill switch in the relay's COM wire. Cells out + short the toy's contacts before soldering.
- Always bring the system up in BENCH MODE first (`BENCH_MODE = True`): the relay is never driven.

## Game rules
Click **Start** in the panel -> hang the wand on the START hook (timer 0) -> lift it: timer runs, every
track touch is a fault -> hang the wand on the END hook: game over, circuit disarmed.
**Score = 1000 − 5 × seconds − 50 × faults** (minimum 0). Higher is better.

---
## Part A: PC setup (backend + panel)

Install: **Python 3.10 to 3.12** (python.org; tick "Add to PATH"), **VS Code**.
1. VS Code -> File -> Open Folder -> the `buzzwire` folder. Accept the extension recommendations
   (Python, MicroPico).
2. Terminal -> New Terminal, then create the environment and install:

   Windows (Command Prompt or PowerShell):
   ```
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
   (PowerShell blocked? run `Set-ExecutionPolicy -Scope Process RemoteSigned` first.)

   macOS / Linux:
   ```
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Ctrl+Shift+P -> **Python: Select Interpreter** -> pick the `.venv` one.
4. Start the backend: Ctrl+Shift+P -> **Tasks: Run Task** -> **Run backend**
   (or in a terminal: `GAME_API_KEY=changeme python backend/main.py`;
   Windows cmd: `set GAME_API_KEY=changeme` then `python backend\main.py`).
   Check http://localhost:8000/docs opens.
5. Start the panel: **Tasks: Run Task** -> **Run frontend** -> http://localhost:8501.
6. Find your PC's IP address (the Pico needs it):
   Windows `ipconfig` (IPv4 Address) · macOS `ipconfig getifaddr en0` · Linux `hostname -I`.
7. Windows firewall: allow port 8000 (run once in an Administrator terminal):
   `netsh advfirewall firewall add rule name="BuzzWire" dir=in action=allow protocol=TCP localport=8000`
8. Put sound files (.wav/.ogg/.mp3) in `backend/media/`; they appear in the panel dropdown.
   A default chime is created automatically. No sound? Game still works (see Troubleshooting).

Change `changeme` to your own secret in THREE places: `.vscode/tasks.json`, `firmware/config.py`
(`API_KEY`), and wherever you start the backend/panel.

## Part B: Pico setup in VS Code (MicroPico)

1. **Install MicroPython on the Pico W** (once): hold BOOTSEL while plugging in USB, a drive
   "RPI-RP2" appears. Download the latest *Raspberry Pi Pico W* `.uf2` from
   https://micropython.org/download/RPI_PICO_W/ and drop it on the drive. It reboots by itself.
2. VS Code -> **File -> Open Folder -> `buzzwire/firmware`** (open the firmware folder itself, in a
   second window, so uploads land at the Pico's root). Install the **MicroPico** extension if prompted.
3. Ctrl+Shift+P -> **MicroPico: Configure project** (adds code completion; optional).
4. Edit `config.py`:
   - `WIFI_SSID`, `WIFI_PASSWORD` (**2.4 GHz network only**: the Pico W has no 5 GHz)
   - `BACKEND_HOST` = your PC's IP from Part A step 6; `API_KEY` = same secret as the backend
   - `RELAY_ACTIVE_HIGH` (True/False) and `BUZZER_ACTIVE` (True/False) from your Phase 1 tests
   - leave `BENCH_MODE = True` for now
5. Plug the Pico in (normal USB, not BOOTSEL). Bottom bar: click **Pico Disconnected** -> it connects.
6. Ctrl+Shift+P -> **MicroPico: Upload project to Pico**. Then **MicroPico: Reset** (or replug).
7. Open the MicroPico terminal (REPL). You should see: `BENCH MODE: shock output disabled...`.

To run a test script: open `wiring_check.py` -> right-click -> **Run current file on Pico**
(or the Run button in the bottom bar). Press Ctrl+C in the REPL to stop a running program.

(No VS Code? Thonny works too: File -> Save as -> Raspberry Pi Pico for each file. CLI:
`pip install mpremote` then `mpremote fs cp config.py net.py boot.py main.py :`.)

## Part C: test in this order
1. `wiring_check.py`: one beep at start; touching the track beeps; hanging the wand on each hook
   prints "START HOOK / END HOOK: wand touching". Fix wiring until all three work.
2. `relay_test.py` with the toy's CELLS OUT: 5 clicks, 3 s apart; multimeter beeps across COM/NO
   only during a click. Relay stuck ON at rest -> see Troubleshooting.
3. Cells in: run `relay_test.py` again and test ONE pulse on your own fingertip across the toy's foil.
4. **Bench game** (still `BENCH_MODE = True`, main.py running): hang wand on START hook ~2 s ->
   "ARMED"; lift -> "RUN STARTED"; touch the track -> "FAULT #n" + beep (the LED blinks where a
   shock would fire); hang on END hook -> "FINISHED ... preview score". Touches afterwards do nothing.

## Part D: go live
1. Backend + panel running (Part A). In the panel keep **Shock OFF** for the first test game.
2. `config.py`: `BENCH_MODE = False` -> Upload project -> Reset. Console: `config loaded: shock=False`.
   The Pico's onboard LED is ON while the backend is reachable.
3. Panel: enter a name -> **Start game** -> play a full game with shock OFF. Score appears on the
   leaderboard.
4. Fit the kill switch ON, turn **Shock module ON** in the panel, tick the consent box, Apply, play.
5. Emergency: the panel's **Emergency: shock OFF** button, or flip the kill switch.

Built-in limits (Pico and backend both enforce): max 0.5 s per shock, 3 s cooldown, 3 shocks per
game, no shock if the backend is unreachable, shock only during a running game.

## Troubleshooting
- **Pico can't reach the backend:** same Wi-Fi as the PC? 2.4 GHz? Guest networks often block
  device-to-device traffic. Open `http://<PC-IP>:8000/health` from your phone on the same Wi-Fi.
  Check firewall (Part A step 7) and `BACKEND_HOST`. API key must match exactly (401 errors).
- **No chime:** `pip install pygame`; needs an audio output device. The game works without sound.
  Pygame has no wheels for very new Python versions; use Python 3.10-3.12.
- **Relay clicks ON at power-up/rest:** low-level-trigger module: add the 1k pull-up (GP14 to 3V3)
  and set `RELAY_ACTIVE_HIGH = False`; if it still stays on, power the module's logic (VCC) from 3V3 and
  the coil (JD-VCC, jumper removed) from VBUS.
- **Track always "touching":** wand wire touching a hook/track, or the track touching a hook.
- **Timer never starts:** the wand must be seen on the START hook first, then lifted.
- **Pico keeps rebooting in live mode:** that is the watchdog catching a hang; check the REPL output.
  Debug in bench mode (watchdog off).
- **Backend ports:** change with `PORT=9000`; update `BACKEND_PORT` in `config.py`.
