# Buzz Wire Game: setup guide (Raspberry Pi Pico W + FastAPI)

```
zapped/
├── backend/               FastAPI server: game state, chime, score, leaderboard, touch photos
│   ├── templates/         index.html (public leaderboard + photo wall), admin.html (control panel)
│   └── .env.example       copy to backend/.env for local runs
├── frontend/app.py        optional Streamlit control panel (same features as /admin)
├── firmware/              MicroPython for the Pico W
│   ├── config.py          EDIT THIS: Wi-Fi, backend URL, API key, pins, buzzer type
│   ├── boot.py  net.py  main.py
│   ├── wiring_check.py    test the track/hooks/buzzer
│   └── buzzer_test.py     test the buzzer + onboard LED
├── tests/                 pytest suite (backend, firmware simulation, browser end-to-end)
├── helm/buzzwire-app/     k3s deployment (deployed by .github/workflows/deploy.yaml)
└── dockerfile
```

## Game rules
Click **Start game** in the control panel -> hang the wand on the START hook (timer 0) -> lift it:
the timer runs and every track touch is a fault (beep on the Pico, chime in the browser, photo from
the IP camera) -> hang the wand on the END hook: game over, result goes to the leaderboard.
**Score = 1000 − 5 × seconds − 50 × faults** (minimum 0, configurable). Higher is better.

---
## Part A: run the backend locally

Python **3.11** recommended (same as the Docker image).

```
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp backend/.env.example backend/.env # set GAME_API_KEY, S3 keys, camera URL
python backend/main.py
```

- Public leaderboard + photo wall: http://localhost:8000/
- Control panel: http://localhost:8000/admin (click **🔑 API Key** and enter `GAME_API_KEY`;
  click **Enable laptop audio** so the chime plays on touches)
- API docs: http://localhost:8000/docs

Optional Streamlit panel: `pip install -r frontend/requirements.txt` then
`GAME_API_KEY=... BACKEND_URL=http://localhost:8000 streamlit run frontend/app.py`.

Put extra sound files (.wav/.ogg/.mp3) in the media folder; they appear in the chime dropdown.
A default chime is generated automatically.

## Part B: Pico setup (MicroPico in VS Code, Thonny, or mpremote)

1. Flash MicroPython **1.22 or newer** for the Pico W (needed for HTTPS): hold BOOTSEL while
   plugging in USB, drop the `.uf2` from https://micropython.org/download/RPI_PICO_W/ on the drive.
2. Edit `firmware/config.py`:
   - `WIFI_SSID`, `WIFI_PASSWORD` (**2.4 GHz only**)
   - `BACKEND_HOST`: either the deployed URL (`"https://diwaligame.vishvesh.me"`) or a local
     backend (`"http://192.168.x.y:8000"`). https uses TLS on port 443 automatically.
   - `API_KEY`: must equal the backend's `GAME_API_KEY` (the k8s secret `buzzwire-secrets`,
     key `game-api-key`, in production)
   - `BUZZER_ACTIVE` (True/False) for your buzzer type; leave `BENCH_MODE = True` at first
3. Upload: `pip install mpremote` then from `firmware/`:
   `mpremote fs cp config.py net.py boot.py main.py :` and reset the board.

## Part C: test in this order
1. `wiring_check.py`: one beep at start; touching the track beeps; hanging the wand on each hook
   prints "START HOOK / END HOOK: wand touching". Fix wiring until all three work.
2. **Bench game** (`BENCH_MODE = True`): hang the wand on START ~2 s -> "ARMED"; lift ->
   "RUN STARTED"; touch the track -> "FAULT #n" + beep; hang on END -> "FINISHED ... preview score".
3. **Live** (`BENCH_MODE = False`): the onboard LED is ON while the backend is reachable. Start a
   game from `/admin`, play, and the score appears on the leaderboard.

## Tests

```
pip install -r requirements-dev.txt
python -m playwright install chromium   # browser end-to-end tests
pytest -q
```

The suite covers the API, the SSE stream, the firmware game loop running under CPython against a
real backend (simulated wand/hooks/track), and both web pages in headless Chromium. CI runs it
before every image build.

## Deployment
Pushing to `main` with changes under `backend/`, `requirements.txt` or `dockerfile` runs the tests,
builds a multi-arch image to GHCR and bumps `helm/buzzwire-app/values.yaml`. Keep **one replica /
one uvicorn worker**: the live game state is held in memory.

## Troubleshooting
- **Pico can't reach the backend:** check the REPL for `http ... failed`. 2.4 GHz Wi-Fi? Correct
  `BACKEND_HOST`? `401` means `API_KEY` doesn't match. HTTPS needs MicroPython ≥ 1.22.
- **No chime:** click "Enable laptop audio" in `/admin` (browsers block autoplay).
- **No photos:** use "Test camera + S3" in `/admin`; it reports camera and upload errors.
- **Track always "touching":** wand wire touching a hook/track, or the track touching a hook.
- **Timer never starts:** the wand must be seen on the START hook first, then lifted.
- **Pico keeps rebooting in live mode:** that is the watchdog catching a hang; debug in bench mode.
