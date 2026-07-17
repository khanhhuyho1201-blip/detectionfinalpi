"""
Virtual (no-hardware) tests for the server-icon behaviour on the kiosk.

Covers the two rules the operator asked for:
  1. The icon only ever goes DIM or LIT — never blinks. A lone timed-out
     heartbeat must NOT dim it; only a sustained outage (>= grace) may.
  2. "Set up once": once enrolled the icon stays LIT and is not tappable; if an
     admin DELETES the device on the server, the device drops its creds and the
     icon returns to DIM + tappable so it can be paired again — and this happens
     ONLY on a confirmed delete, never on a network error.

These exercise Controller._hb_step (pure) and Controller._handle_deprovisioned
without starting any threads or touching hardware.

Run on the Pi:  ../.venv/bin/python test_server_icon_virtual.py
"""
import sys
import threading

from controller import Controller

G = Controller.HB_OFFLINE_GRACE      # 25.0s
GONE_N = Controller.HB_GONE_AFTER    # 2

_fails = []
def check(name, cond):
    print(("  ok  " if cond else " FAIL ") + name)
    if not cond:
        _fails.append(name)


def run_seq(events, start=None):
    """Feed (hb, now) events through _hb_step; return the final state and the
    list of (action) emitted. `start` is the initial state dict."""
    st = start or {"last_ok": 0.0, "gone": 0, "online": False, "locked": False}
    actions = []
    for hb, now in events:
        st, act = Controller._hb_step(st, hb, now)
        actions.append(act)
    return st, actions


print("[1] anti-flicker: single/isolated heartbeat timeouts never dim the icon")
# timings are RELATIVE to the real (possibly-tuned) grace so the test can't go stale
G = Controller.HB_OFFLINE_GRACE
# online, then an isolated blip that recovers immediately -> never dims
st, _ = run_seq([(True, 0), (True, 10), (False, 10 + G * 0.4), (True, 10 + G * 0.5)])
check("stays LIT through an isolated blip", st["online"] is True)

# a single miss WITHIN the grace window keeps it LIT
st, _ = run_seq([(True, 100), (False, 100 + G * 0.6)])   # < grace since last ok
check("a miss within grace keeps it LIT", st["online"] is True)

print("[2] real outage: sustained failure past the grace window dims the icon")
st, _ = run_seq([(True, 0), (False, G * 0.3), (False, G * 0.7), (False, G + 5)])  # > grace
check("dims after a continuous outage past grace", st["online"] is False)
# and it re-lights immediately on the next success (no dead time)
st, _ = run_seq([(True, 60)], start=st)
check("re-lights instantly on recovery", st["online"] is True)

print("[3] locked (admin lock) counts as reachable -> LIT + locked flag")
st, _ = run_seq([(True, 0), ("locked", 10)])
check("locked -> online True", st["online"] is True)
check("locked -> locked True", st["locked"] is True)
st, _ = run_seq([("locked", 20)], start=st)  # unlock path exercised elsewhere
check("locked stays reachable", st["online"] is True)

print("[4] delete detection: 'gone' must be CONFIRMED (twice) before un-enroll")
st, acts = run_seq([(True, 0), ("gone", 10)])       # first gone only
check("one 'gone' does NOT unenroll", acts[-1] is None)
st, acts = run_seq([(True, 0), ("gone", 10), ("gone", 13)])  # two in a row
check("two 'gone' in a row -> unenroll action", acts[-1] == "unenroll")
check("unenroll -> icon dims", st["online"] is False)

print("[5] a network error (False) between/after 'gone' resets the delete counter")
# 'gone' then a plain timeout must NOT accumulate toward deletion
st, acts = run_seq([(True, 0), ("gone", 10), (False, 12), ("gone", 14)])
check("False resets gone counter (no unenroll)", all(a != "unenroll" for a in acts))

print("[6] _handle_deprovisioned drops creds + returns to un-enrolled")
# build a Controller shell WITHOUT running __init__ (no threads/hardware)
c = Controller.__new__(Controller)
c._lock = threading.Lock()
c._client = object()          # pretend enrolled
c._online = True
c._server_locked = False
c._pair = {"code": "P123", "status": "pending"}
c._recording = False
import settings as _settings_mod
# Patch clear() at the CLASS level (the instance forbids attribute assignment) and
# make it a NO-OP counter so the real credentials.json is never touched by the test.
_cleared = {"n": 0}
_CredCls = type(_settings_mod.settings.credentials)
_orig_clear = _CredCls.clear
_CredCls.clear = lambda self: _cleared.__setitem__("n", _cleared["n"] + 1)
try:
    c._handle_deprovisioned()
finally:
    _CredCls.clear = _orig_clear
check("credentials.clear() called", _cleared["n"] == 1)
check("client dropped -> enrolled becomes False", c._client is None)
check("icon forced DIM", c._online is False)
check("pairing session reset (FE can re-pair)", c._pair is None)

print()
if _fails:
    print("RESULT: FAIL (%d) -> %s" % (len(_fails), ", ".join(_fails)))
    sys.exit(1)
print("RESULT: ALL PASS")
