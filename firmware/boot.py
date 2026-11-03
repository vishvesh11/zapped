"""Runs once at power-up: join Wi-Fi (live mode only)."""
import config

if config.BENCH_MODE:
    print("boot: BENCH MODE - no Wi-Fi")
else:
    import net
    if not net.connect_wifi():
        print("boot: wifi not connected yet; main.py will keep retrying")
