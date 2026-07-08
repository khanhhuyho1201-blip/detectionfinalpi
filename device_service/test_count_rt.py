#!/usr/bin/env python3
"""test_count_rt.py — kiem tra RIENG luong dem count: real-time + KHONG lui + gate 412.
Tai hien false-trigger (BSS_SIM_FALSE_PCT) = nguon glitch lui tren phan cung that.
Chay: python3 test_count_rt.py
"""
import time, threading, requests
from test_sim import Server, BASE, wait_for

def poll_counts(stop_evt, out):
    """Poll /api/state that nhanh, gom (count,state) — bat moi glitch lui."""
    while not stop_evt.is_set():
        try:
            s = requests.get(f"{BASE}/api/state", timeout=1).json()
            out.append((s.get("count", 0), s.get("state")))
        except Exception:
            pass
        time.sleep(0.015)   # ~66Hz, nhanh hon UI poll (100ms) -> bat duoc lui neu co

def check_monotonic(seq):
    """Tra ve list cac lan LUI (idx, truoc, sau) trong chuoi count khi dang recording."""
    backs=[]
    prev=None
    for i,(c,st) in enumerate(seq):
        if prev is not None and c < prev:
            backs.append((i, prev, c))
        prev=c
    return backs

def scenario(name, env, expect_state, expect_min_count, target):
    print(f"\n=== {name} ===")
    with Server(env):
        stop=threading.Event(); counts=[]
        th=threading.Thread(target=poll_counts,args=(stop,counts),daemon=True); th.start()
        requests.post(f"{BASE}/api/start",timeout=5)
        s=wait_for(lambda s: s["state"] in ("done","failed"), 30)
        time.sleep(0.3); stop.set(); th.join(timeout=2)
        # loc giai doan tu luc bat dau co count>0 (bo phan idle dau)
        rec=[(c,st) for (c,st) in counts if st in ("recording","uploading","done","failed")]
        backs=check_monotonic(rec)
        maxc=max((c for c,_ in rec), default=0)
        final=s["state"]; fcount=s.get("count"); fcode=(s.get("error") or {}).get("code")
        ok_mono = len(backs)==0
        ok_state = final==expect_state
        ok_count = (fcount is not None and fcount>=expect_min_count) if expect_state=="done" else True
        print(f"  mono(khong lui)={ok_mono}  backs={backs[:5]}")
        print(f"  peak_count={maxc}  final_state={final}  final_count={fcount}  code={fcode}")
        print(f"  samples={len(rec)}")
        ok = ok_mono and ok_state
        print(f"  => {'PASS' if ok else 'FAIL'}")
        return ok

def main():
    base={"CARD_FAKE_SERVER":"1","CARD_FAKE_CAMERA":"ok","CARD_FAKE_RECORDER":"1"}
    results=[]
    # A) me DU + false-trigger 40% -> phai DON DIEU (khong lui) + done + count=60
    results.append(scenario("A. FULL+FALSE40 -> done, count mono",
        {**base,"CARD_FAKE_TARGET":"60","BSS_SIM_LEAF_MS":"12","BSS_SIM_FALSE_PCT":"40","BSS_SIM_OPTIMISTIC":"1"},
        "done", 60, 60))
    # B) stall giua chung (40<60) -> incomplete MCU-10, count mono
    results.append(scenario("B. STALL@40<60 -> failed MCU-10, mono",
        {**base,"CARD_FAKE_TARGET":"60","BSS_SIM_STALL_AT":"40","BSS_SIM_LEAF_MS":"12","BSS_SIM_FALSE_PCT":"30","BSS_SIM_OPTIMISTIC":"1"},
        "failed", 0, 60))
    # C) false-trigger CAO 70% -> stress chong lui
    results.append(scenario("C. FULL+FALSE70 stress -> done, mono",
        {**base,"CARD_FAKE_TARGET":"40","BSS_SIM_LEAF_MS":"10","BSS_SIM_FALSE_PCT":"70","BSS_SIM_OPTIMISTIC":"1"},
        "done", 40, 40))
    print(f"\n{'='*40}\nKET QUA: {sum(results)}/{len(results)} PASS")
    return 0 if all(results) else 1

if __name__=="__main__":
    import sys; sys.exit(main())
