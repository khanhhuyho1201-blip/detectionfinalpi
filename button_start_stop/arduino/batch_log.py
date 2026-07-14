#!/usr/bin/env python3
"""
batch_log.py — Hung log 1 me feed tu Arduino Uno (production.ino) de phan tich PWM-vs-so-la.
  - CHI NGHE serial (khong tu bat motor). Anh bat cong tac A0 nhu binh thuong.
  - Luu MOI dong kem timestamp ra file, dong thoi in ra man hinh.
  - Tu nhan dien ket me (STALL/DONE), roi gui lenh 'G' lay bang DIAG theo vung 50 la.
Dung:  python3 batch_log.py            (luu vao batch_<HHMMSS>.log)
       python3 batch_log.py ten.log    (luu vao ten.log)
Ctrl-C de dung som; van gui 'G' va luu file truoc khi thoat.
"""
import serial, time, sys, datetime

PORT = '/dev/ttyACM0'
BAUD = 115200

fname = sys.argv[1] if len(sys.argv) > 1 else f"batch_{datetime.datetime.now():%H%M%S}.log"

p = serial.Serial(PORT, BAUD, timeout=1)
time.sleep(2.0)                 # Uno auto-reset sau khi mo port -> doi boot
p.reset_input_buffer()

print(f"[logger] dang nghe {PORT} @ {BAUD} -> {fname}")
print("[logger] bat cong tac may de bat dau me. Ctrl-C de dung.\n")

diag_sent = False
t_last_card = time.time()

def get(line, key):            # trich so sau 'key' trong 1 dong [CARD]/[STATUS]
    try:
        s = line.split(key, 1)[1]
        num = ''
        for c in s.lstrip():
            if c in '-0123456789': num += c
            else: break
        return int(num) if num else None
    except (IndexError, ValueError):
        return None

with open(fname, 'w') as f:
    f.write(f"# batch_log {datetime.datetime.now():%Y-%m-%d %H:%M:%S}  port={PORT}\n")
    try:
        while True:
            raw = p.readline().decode(errors='replace').rstrip()
            if not raw:
                # me ket thuc (STALL) -> Arduino in [STALL] roi im. Sau 10s im lang & da co la -> gui G 1 lan.
                if not diag_sent and (time.time() - t_last_card) > 10:
                    pass
                continue
            ts = time.time()
            f.write(f"{ts:.3f}\t{raw}\n"); f.flush()
            print(raw)

            if '[CARD]' in raw:
                t_last_card = ts
            # ket me -> lay DIAG theo vung de phan tich PWM/loadEMA/satLo tung doan
            if ('[STALL]' in raw or '[SUMMARY]' in raw or 'st=DONE' in raw) and not diag_sent:
                time.sleep(0.5)
                p.write(b'G\n')
                diag_sent = True
                print("\n[logger] da gui 'G' -> lay bang DIAG...")
    except KeyboardInterrupt:
        print("\n[logger] Ctrl-C -> gui G lay DIAG roi thoat...")
        if not diag_sent:
            p.write(b'G\n'); time.sleep(1.5)
            t0 = time.time()
            while time.time() - t0 < 3:
                raw = p.readline().decode(errors='replace').rstrip()
                if raw:
                    f.write(f"{time.time():.3f}\t{raw}\n")
                    print(raw)
    finally:
        p.close()
        print(f"\n[logger] da luu: {fname}")
