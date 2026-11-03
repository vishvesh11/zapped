"""Buzz-wire game control panel (Streamlit).

Run:  GAME_API_KEY=changeme BACKEND_URL=http://localhost:8000 streamlit run app.py
Requires streamlit >= 1.37 (st.fragment with run_every).
"""
import os
from typing import Any, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

BACKEND = os.getenv("BACKEND_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("GAME_API_KEY", "")
HEADERS = {"X-API-Key": API_KEY} if API_KEY else {}
TIMEOUT_S = 3

st.set_page_config(page_title="Buzz Wire Control", page_icon="⚡", layout="wide")


def api(method: str, path: str, **kwargs) -> Tuple[Optional[Any], Optional[str]]:
    """Call the backend. Returns (json, None) on success or (None, error_message)."""
    try:
        resp = requests.request(method, f"{BACKEND}/api/v1{path}", headers=HEADERS,
                                timeout=TIMEOUT_S, **kwargs)
    except requests.RequestException as exc:
        return None, f"Backend unreachable: {exc}"
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        return None, f"{resp.status_code}: {detail}"
    try:
        return resp.json(), None
    except ValueError:
        return None, "Backend returned invalid JSON"


PICK_TEAM = "Select a team…"
OTHER_TEAM = "Other… (type a name)"


@st.cache_data(ttl=300)
def preset_teams() -> list:
    try:
        return requests.get(f"{BACKEND}/public/teams", timeout=TIMEOUT_S).json()["teams"]
    except (requests.RequestException, ValueError, KeyError):
        return []


# --------------------------------------------------------------------------- #
st.title("⚡ Buzz Wire Control Panel")

if "flash" in st.session_state:
    kind, msg = st.session_state.pop("flash")
    getattr(st, kind)(msg)

cfg, err = api("GET", "/config")
if err:
    st.error(err)
    st.stop()

st.caption(
    "Score = %g − %g × seconds − %g × faults (minimum 0). Higher is better. "
    "The timer starts when the wand leaves the START hook and stops when it is hung on the END hook."
    % (cfg["score_base"], cfg["score_per_second"], cfg["score_per_fault"]))

left, right = st.columns([1, 1], gap="large")

# ---------------------------- Control panel -------------------------------- #
with left:
    st.subheader("Game controls")

    media, merr = api("GET", "/media")
    files = media["files"] if media else []
    if merr:
        st.warning(merr)

    with st.form("settings"):
        current = cfg["current_media_file"]
        choice = st.selectbox("Chime sound (from backend media folder)", files or [current],
                              index=files.index(current) if current in files else 0)
        if st.form_submit_button("Apply settings", type="primary"):
            _, perr = api("POST", "/config", json={
                "current_media_file": choice,
            })
            if perr:
                st.error(perr)
            else:
                st.success("Settings applied.")
                st.rerun()

    st.divider()
    st.subheader("Camera test")
    st.caption("Grab a frame from the IP camera and upload it to Garage S3 under a dummy session.")
    t1, t2 = st.columns(2)
    if t1.button("Test camera + S3"):
        res, terr = api("POST", "/camera/test", params={"use_camera": "true"})
        st.error(terr) if terr else st.json(res)
    if t2.button("Test S3 only (dummy)"):
        res, terr = api("POST", "/camera/test", params={"use_camera": "false"})
        st.error(terr) if terr else st.json(res)

    st.divider()
    st.subheader("Player")
    st.caption("Click Start, then hang the wand on the START hook. Lift it to begin. "
               "Hang it on the END hook to finish.")
    name = st.text_input("Player name", max_chars=24)
    team_choice = st.selectbox("Team / competency", [PICK_TEAM, *preset_teams(), OTHER_TEAM])
    team = team_choice if team_choice not in (PICK_TEAM, OTHER_TEAM) else ""
    if team_choice == OTHER_TEAM:
        team = st.text_input("Team name", max_chars=32).strip()
    c1, c2 = st.columns(2)
    if c1.button("Start game", type="primary"):
        if not name.strip():
            st.error("Enter a player name first.")
        elif not team:
            st.error("Select the player's team / competency.")
        else:
            _, serr = api("POST", "/session/start", json={"player_name": name, "team": team})
            st.error(serr) if serr else st.rerun()
    if c2.button("Abort game"):
        _, aerr = api("DELETE", "/session")
        st.error(aerr) if aerr else st.rerun()


# ---------------------------- Live tracker --------------------------------- #
@st.fragment(run_every=1)
def live_tracker() -> None:
    st.subheader("Live session")
    sess, serr = api("GET", "/session")
    if serr:
        st.error(serr)
        return
    if not sess["active"]:
        st.info("No active game. Enter a name and press Start.")
        return
    a, b, c, d = st.columns(4)
    a.metric("Player", sess["player_name"], help=sess.get("team") or None)
    b.metric("Faults", sess["touch_count"])
    c.metric("Timer", "%.1f s" % sess["elapsed_seconds"])
    d.metric("Score", sess["live_score"])
    if sess["phase"] == "waiting":
        st.warning("Waiting for the player: hang the wand on the START hook, then lift it to begin. "
                   "Timer stays at 0 until the wand leaves the hook.")
    else:
        st.success("Running. Hang the wand on the END hook to finish.")


with right:
    live_tracker()


# ---------------------------- Leaderboard ---------------------------------- #
@st.fragment(run_every=3)
def leaderboard() -> None:
    st.subheader("Leaderboard")
    rows, lerr = api("GET", "/leaderboard", params={"limit": 20})
    if lerr:
        st.error(lerr)
        return
    if not rows:
        st.info("No completed games yet.")
        return
    df = pd.DataFrame(rows).rename(columns={
        "rank": "Rank", "player_name": "Player", "team": "Team", "score": "Score",
        "completion_time_seconds": "Time (s)", "touch_count": "Faults"})
    if "Team" not in df:
        df["Team"] = None
    st.dataframe(df[["Rank", "Player", "Team", "Score", "Time (s)", "Faults"]], hide_index=True)


with right:
    st.divider()
    leaderboard()
    with st.expander("Reset leaderboard"):
        sure = st.checkbox("I want to delete all scores")
        if st.button("Delete all scores", disabled=not sure):
            _, derr = api("DELETE", "/leaderboard")
            st.error(derr) if derr else st.rerun()
