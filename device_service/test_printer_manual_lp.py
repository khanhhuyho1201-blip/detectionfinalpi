#!/usr/bin/env python3
"""Test ẢO: nhập-IP-tay (manual_entry) + lp-báo-thật (_wait_job) — không cần máy in thật."""
import sys, socket, threading, time
import os as _os; sys.path.insert(0,_os.path.dirname(_os.path.abspath(__file__)))
import printer, printer_setup
PASS,FAIL="✅","❌"; res=[]
def check(n,c,d=""): res.append(bool(c)); print(f"  {PASS if c else FAIL} {n} {d}")
RID="9153f117-f279-4649-85a5-e102ca2077cf"

# máy in mạng ẢO (drain) cho :9200 (queue tốt) + :9931 (manual probe)
def drain_server(port, secs=28):
    srv=socket.socket(); srv.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
    srv.bind(("127.0.0.1",port)); srv.listen(2); srv.settimeout(1.0)
    t0=time.time()
    while time.time()-t0 < secs:
        try:
            c,_=srv.accept()
        except socket.timeout: continue
        except Exception: break
        try:
            c.settimeout(3)
            while True:
                b=c.recv(8192)
                if not b: break
        except Exception: pass
        finally:
            try: c.close()
            except: pass
    srv.close()
threading.Thread(target=drain_server,args=(9200,),daemon=True).start()
threading.Thread(target=drain_server,args=(9931,),daemon=True).start()
time.sleep(0.4)

print("### A) NHẬP IP TAY — manual_entry (dò cổng + phân loại) ###")
e=printer_setup.manual_entry("127.0.0.1:9931")
check("IP:port có máy in -> dò cổng + build URI đúng", e.get("ok") and e.get("protocol")=="socket"
      and e.get("uri")=="socket://127.0.0.1:9931" and e.get("port")==9931, f"-> {e.get('uri')} / {e.get('backend')} (cups vì localhost:631=CUPS)")
# thermal thật (IP remote, 631 ĐÓNG): fallback đúng -> escpos_net
be_thermal=printer_setup.classify_backend("Generic printer","socket","10.255.255.1",9100)
check("thermal remote (631 đóng) -> escpos_net", be_thermal=="escpos_net", f"-> {be_thermal}")
e2=printer_setup.manual_entry("socket://10.1.2.3:9100")
check("URI đầy đủ socket://", e2.get("ok") and e2.get("uri")=="socket://10.1.2.3:9100")
e3=printer_setup.manual_entry("127.0.0.1:9099")     # đóng
check("IP không có máy in -> báo lỗi (không add bừa)", not e3.get("ok"), f"-> {e3.get('error','')[:40]}")
e4=printer_setup.manual_entry("192.168.2.14")       # Brother (631 mở)
check("Brother IP tay -> cups (probe 631)", e4.get("ok") and e4.get("backend")=="cups", f"-> {e4.get('backend')}")

print("\n### B) lp-BÁO-THẬT — _wait_job phát hiện in hỏng ###")
t=time.time()
dead=printer.CupsPrinter(cups_name="_vtest_dead").print_qr(RID)   # queue chết -> phải FALSE
check("queue CHẾT (máy in không tới) -> print_qr False", dead is False, f"| {time.time()-t:.0f}s")
t=time.time()
good=printer.CupsPrinter(cups_name="_vtest_good").print_qr(RID)   # queue tốt (drain :9200) -> True
check("queue TỐT (drain socket) -> print_qr True", good is True, f"| {time.time()-t:.0f}s")

print(f"\n===== TỔNG: {sum(res)}/{len(res)} PASS =====")
sys.exit(0 if all(res) else 1)
