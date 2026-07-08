"""
Turn raw Arduino serial lines into a structured MachineStatus.

The parser understands BOTH formats so it works no matter which the firmware
sends (Arduino plan appendix item A5):

  * the compact machine-readable line, e.g.
        ST st=RUN n=137 tot=412 err=NONE spd=565
  * the existing human-readable logs, e.g.
        [CARD] #137 | dt=12 | PWM=565
        [CLUMP] ...        [STALL] ...        [DONE] ...
        [MACHINE] ON / OFF        [STATUS] ...

parse_line() returns an Event describing what just happened so session.py can
react (auto-finish on DONE/STALL, etc.). It never raises on a malformed line —
unknown lines just pass through with event="log".
"""

import re
from dataclasses import dataclass, replace

# state values
IDLE = "IDLE"      # ready, not running
RUN = "RUN"        # pulling leaves
WARN = "WARN"      # running but a clump was just seen
ERROR = "ERROR"    # stopped on a fault (stall / limit)
DONE = "DONE"      # batch finished normally
OFF = "OFF"        # machine powered/homed off

# error codes
ERR_NONE = "NONE"
ERR_CLUMP = "CLUMP"
ERR_STALL = "STALL"
ERR_LIMIT = "LIMIT"


@dataclass
class MachineStatus:
    state: str = IDLE
    count: int = 0          # leaves counted this batch
    total: int = 0          # batch target (0 = pull until empty)
    error: str = ERR_NONE
    speed: int = 0          # informational (PWM / leaves-per-min, firmware-defined)
    connected: bool = False
    last_line: str = ""

    def copy(self) -> "MachineStatus":
        return replace(self)


# token helpers
_INT_AFTER_HASH = re.compile(r"#\s*(\d+)")
_KV = re.compile(r"(\w+)=([^\s]+)")
_FIRST_INT = re.compile(r"(\d+)")


def _to_int(s: str, default: int = 0) -> int:
    m = _FIRST_INT.search(s or "")
    return int(m.group(1)) if m else default


def parse_line(line: str, st: MachineStatus) -> tuple[MachineStatus, str]:
    """Apply one raw line to `st`. Returns (new_status, event).

    event is one of: "st", "card", "clump", "stall", "limit", "done",
    "machine_on", "machine_off", "status", "log".
    """
    raw = (line or "").strip()
    s = st.copy()
    s.last_line = raw
    if not raw:
        return s, "log"

    upper = raw.upper()

    # ── compact machine-readable line: ST st=.. n=.. tot=.. err=.. spd=.. ──
    if upper.startswith("ST ") or upper == "ST":
        kv = {k.lower(): v for k, v in _KV.findall(raw)}
        st_val = kv.get("st", "").upper()
        if st_val in (RUN, IDLE, WARN, ERROR, DONE, OFF):
            s.state = st_val
        elif st_val:
            s.state = RUN if st_val.startswith("RUN") else s.state
        if "n" in kv:
            s.count = _to_int(kv["n"], s.count)
        if "tot" in kv:
            s.total = _to_int(kv["tot"], s.total)
        if "spd" in kv:
            s.speed = _to_int(kv["spd"], s.speed)
        if "err" in kv:
            s.error = kv["err"].upper()
            if s.error not in (ERR_NONE, ERR_CLUMP, ERR_STALL, ERR_LIMIT):
                s.error = ERR_NONE
        # firmware flags a real-time clump via err=CLUMP while still RUN —
        # surface it as a transient warning (yellow) without ending the batch.
        if s.error == ERR_CLUMP and s.state == RUN:
            s.state = WARN
        return s, "st"

    # ── legacy human-readable logs ──
    if upper.startswith("[CARD]"):
        m = _INT_AFTER_HASH.search(raw)
        if m:
            s.count = int(m.group(1))
        mk = {k.lower(): v for k, v in _KV.findall(raw)}
        if "pwm" in mk:
            s.speed = _to_int(mk["pwm"], s.speed)
        if s.state not in (RUN, WARN):
            s.state = RUN
        # a card cleared the transient clump warning
        if s.error == ERR_CLUMP:
            s.error = ERR_NONE
            s.state = RUN
        return s, "card"

    if upper.startswith("[CLUMP]"):
        s.error = ERR_CLUMP
        s.state = WARN
        return s, "clump"

    if upper.startswith("[STALL]"):
        s.error = ERR_STALL
        s.state = ERROR
        return s, "stall"

    if upper.startswith("[LIMIT]"):
        # firmware's [LIMIT] = platform hit travel ceiling, a NON-fatal warning
        # (the motor keeps running). Surface it but do NOT end the batch; the
        # next ST line clears it back to RUN.
        s.error = ERR_LIMIT
        s.state = WARN
        return s, "limit"

    if upper.startswith("[DONE]"):
        s.state = DONE
        s.error = ERR_NONE
        # real firmware: "[DONE] da dem 412 la ..." (no total=); the count is
        # already tracked from [CARD]/ST. Capture total= only if present.
        kv = {k.lower(): v for k, v in _KV.findall(raw)}
        if "total" in kv:
            s.count = _to_int(kv["total"], s.count)
        return s, "done"

    if upper.startswith("[MACHINE]"):
        if "ON" in upper:
            s.state = RUN
            s.error = ERR_NONE
            return s, "machine_on"
        if "OFF" in upper:
            # OFF after a fault keeps the fault visible; otherwise it's idle
            s.state = ERROR if s.error in (ERR_STALL, ERR_LIMIT) else OFF
            return s, "machine_off"
        return s, "log"

    if upper.startswith("[STATUS]"):
        return s, "status"

    return s, "log"
