"""
wifi_portal.py — captive portal cài WiFi cho Card Feeder Pi5.

Luồng: wifi_watchdog bật AP "CardFeeder-XXXX" → điện thoại quét QR kết nối AP
→ iOS/Android tự mở trang này → chọn mạng + nhập mật khẩu → Pi nối WiFi nhà → AP tắt.

7 màn hình: S0 Welcome → S1 Scanning → S2 WiFi List → S3 Password →
             S4 Connecting → S5 Sai mật khẩu → S6 Không tìm thấy mạng.

Cổng 80 (để QR http://10.42.0.1 mở thẳng). Chạy với quyền root (systemd).
"""

import json
import logging
import os
import subprocess
import threading
import time

from flask import Flask, jsonify, redirect, request, Response

# [gom folder 2026-07] file này nằm ở device_service/wifi/. Khi systemd chạy trực
#   tiếp .../wifi/wifi_portal.py thì sys.path[0] = wifi/ (không có settings.py) →
#   thêm thư mục cha device_service/ vào sys.path để 'from settings import settings' chạy.
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from settings import settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("wifi_portal")

IFACE    = settings.wifi.iface
PORT     = settings.wifi.portal_port
HERE     = os.path.dirname(os.path.abspath(__file__))
AP_SCRIPT = os.path.join(HERE, "wifi_ap.sh")
CRED_FILE = str(settings.paths.credentials)
AP_CON   = settings.wifi.ap_con
# Trong lúc file này tồn tại, wifi_watchdog.py đứng yên (không tự bật/hạ AP) —
# tránh 2 tiến trình cùng đụng vào nmcli/AP song song gây "vào mạng vài giây
# rồi tự bật AP lại".
MANUAL_LOCK_FILE = settings.wifi.manual_lock

app = Flask(__name__)

# ── connection state (set by _do_connect, read by /api/wifi/status) ──────────
_conn_lock = threading.Lock()
# [FIX HIGH 2026-07] Thêm "id" = token của PHIÊN connect đang thắng lock. Nhiều điện
#   thoại dùng CHUNG _conn toàn cục; nếu không có id, phone THUA poll /api/wifi/status
#   sẽ đọc state="ok" của phone THẮNG -> hiện "Connected!" giả với tên mạng mình chọn.
#   Client chỉ chấp nhận ok/error khi status.id === connect_id nó nhận lúc thắng lock.
_conn = {"state": "idle", "error": None, "id": None}   # state: idle|connecting|ok|error
_conn_seq = 0


def _get_ap_name() -> str:
    # [FIX HIGH 2026-07] NGUỒN SỰ THẬT = SSID thật của profile AP trong NetworkManager
    #   (khớp đúng cái wifi_ap.sh đang PHÁT). Trước đây hàm này ĐOÁN "CardFeeder-XXXX"
    #   trong khi wifi_ap.sh phát "CMD X BBSW" -> lệch: portal hiện sai tên (user tìm
    #   sai mạng để join) + scan() lọc sai (AP thật lọt vào danh sách chọn).
    try:
        r = subprocess.run(
            ["sudo", "-n", "nmcli", "-t", "-f", "802-11-wireless.ssid", "con", "show", AP_CON],
            capture_output=True, text=True, timeout=5)
        real = r.stdout.split(":", 1)[-1].strip() if ":" in r.stdout else ""
        if real:
            return real
    except Exception:
        pass
    # Fallback khi profile CHƯA tồn tại: env CARD_AP_SSID -> rồi CardFeeder-<suffix>.
    ssid = settings.wifi.ap_ssid
    if ssid:
        return ssid
    try:
        d = json.load(open(CRED_FILE))
        suffix = (d.get("device_id") or "XXXX")[-4:].upper()
    except Exception:
        suffix = "XXXX"
    return f"CardFeeder-{suffix}"


def nmcli(*args, timeout=20):
    return subprocess.run(["sudo", "-n", "nmcli", *args],
                          capture_output=True, text=True, timeout=timeout)


# ── HTML portal (S0-S6 state machine) ────────────────────────────────────────
_PAGE_TPL = r"""<!doctype html><html lang="vi"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">

<title>WiFi Setup — Card Feeder</title>
<style>
:root{
  --bg:#0A090E;--s1:#131118;--bdr:#2A273A;
  --gold:#D9B45A;--gold-hi:#F0D484;--gold-lo:#9C7430;
  --text:#E5DDD3;--dim:#7A7280;--dim2:#4A4455;
  --green:#4DD4A0;--red:#E55A5A;--blue:#7EB0F0;--orange:#F0A855;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;color:var(--text);
  background:radial-gradient(130% 90% at 50% -5%,#1A1822 0%,var(--bg) 55%);
  min-height:100vh;-webkit-tap-highlight-color:transparent}
.wrap{max-width:420px;margin:0 auto;
  padding:20px 18px max(48px,env(safe-area-inset-bottom,0px));
  height:100dvh;min-height:100svh;display:flex;flex-direction:column;overflow:hidden}

/* titles */
h2{font-weight:800;font-size:clamp(20px,6.2vw,26px);letter-spacing:-.02em;
  text-align:center;margin:0 0 6px;
  background:linear-gradient(180deg,var(--gold-hi),var(--gold) 48%,var(--gold-lo));
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--dim);font-size:clamp(12px,3.4vw,13px);text-align:center;margin:0 auto 20px;
  max-width:320px;line-height:1.55}

/* device chip */
.chip{display:inline-flex;align-items:center;gap:6px;padding:5px 14px;
  border:1px solid rgba(217,180,90,.45);border-radius:999px;
  font-size:13px;font-weight:600;color:var(--gold);margin:0 auto 24px;letter-spacing:.02em}
.chip.blue{border-color:rgba(126,176,240,.5);color:var(--blue)}
.chip.red{border-color:rgba(229,85,90,.5);color:var(--red)}
.chip.center{display:flex;justify-content:center}

/* network list */
.net-list{flex:1;overflow-y:auto;margin-bottom:12px;min-height:0;
  -webkit-overflow-scrolling:touch}
.net{display:flex;align-items:center;gap:11px;padding:13px 14px;
  background:linear-gradient(180deg,#17151C,#100E14);
  border:1px solid var(--bdr);border-radius:14px;margin-bottom:9px;cursor:pointer;
  transition:border-color .15s}
.net:active{transform:scale(.99)}.net:hover{border-color:rgba(217,180,90,.4)}
.net.selected{border-color:rgba(217,180,90,.85);box-shadow:0 0 0 1px rgba(217,180,90,.35) inset}
.bars{flex:0 0 auto;display:flex;align-items:flex-end;gap:2px;height:18px}
.bars i{width:3px;border-radius:1px;background:var(--dim2)}
.bars i.on{background:var(--gold)}
.ssid{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.sig{color:var(--dim);font-size:12px;margin-right:4px}
.lock{color:var(--gold);font-size:11px;margin-left:4px}

/* password input */
.pw-row{position:relative;margin:16px 0 6px}
input[type=password],input[type=text]{
  width:100%;padding:15px 48px 15px 15px;
  background:var(--s1);border:1px solid var(--bdr);border-radius:12px;
  color:var(--text);font-size:16px;outline:none;transition:border-color .15s}
input:focus{border-color:var(--gold)}
.eye{position:absolute;right:12px;top:50%;transform:translateY(-50%);
  width:42px;height:42px;background:none;border:0;color:var(--dim);
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;letter-spacing:.05em;cursor:pointer;border-radius:8px}
.eye:active{color:var(--gold)}

/* buttons */
.btn{display:block;width:100%;padding:15px;border:0;border-radius:999px;
  font-size:clamp(14px,4vw,15px);font-weight:800;cursor:pointer;letter-spacing:.03em;margin-top:10px;
  background:linear-gradient(180deg,var(--gold-hi),var(--gold) 55%,var(--gold-lo));
  color:#1a1407;box-shadow:0 6px 22px rgba(217,180,90,.25)}
.btn:active{transform:translateY(1px)}.btn:disabled{opacity:.5;box-shadow:none;cursor:default}
.btn.ghost{background:transparent;border:1px solid var(--bdr);
  color:var(--dim);box-shadow:none;font-weight:600}
.btn.ghost:active{color:var(--text)}
.btn.danger{background:linear-gradient(180deg,#F07070,var(--red) 55%,#A03030);
  color:#fff;box-shadow:0 6px 22px rgba(229,85,90,.25)}

/* spinner */
.spin-wrap{display:flex;flex-direction:column;align-items:center;justify-content:center;
  flex:1;gap:20px;padding:32px 0}
.spinner{width:44px;height:44px;border:3px solid var(--bdr);
  border-top-color:var(--gold);border-radius:50%;animation:rot .75s linear infinite}
.spinner.blue{border-top-color:var(--blue)}
@keyframes rot{to{transform:rotate(360deg)}}
.spin-label{color:var(--dim);font-size:14px}
@keyframes cdots{0%,100%{opacity:.2}50%{opacity:1}}
.cdots span{animation:cdots 1.2s ease-in-out infinite}
.cdots span:nth-child(2){animation-delay:.2s}
.cdots span:nth-child(3){animation-delay:.4s}

/* back link */
.back-link{display:inline-flex;align-items:center;gap:5px;color:var(--dim);
  font-size:13px;cursor:pointer;margin-bottom:16px;background:none;border:0;
  padding:0;-webkit-tap-highlight-color:rgba(217,180,90,.15)}
.back-link:active{color:var(--gold)}

/* error screens */
.err-center{display:flex;flex-direction:column;align-items:center;justify-content:center;
  flex:1;gap:16px;text-align:center;padding:24px 0}
.err-icon{font-size:44px;line-height:1}
.err-title{font-size:clamp(18px,5.2vw,21px);font-weight:700;letter-spacing:-.01em;
  background:linear-gradient(180deg,#F07070,var(--red) 60%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.err-title.orange{background:linear-gradient(180deg,#F0C855,var(--orange) 60%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.err-ssid{font-family:ui-monospace,monospace;font-size:14px;color:var(--dim);
  padding:5px 14px;background:var(--s1);border-radius:8px;border:1px solid var(--bdr)}

/* screens hide/show */
.screen{display:none;flex-direction:column;flex:1}
.screen.active{display:flex}

/* hint */
.hint{color:var(--dim2);font-size:11px;text-align:center;margin-top:auto;
  padding-top:20px;line-height:1.5}
@media(max-width:360px){
  .wrap{padding-left:14px;padding-right:14px}
  .net{padding:12px 12px;gap:9px}
  .net-list{margin-bottom:10px}
  .chip{margin-bottom:18px}
}
@media(max-height:520px){
  .wrap{padding-top:10px}
  .spin-wrap{padding:12px 0;gap:10px}
  .err-center{gap:10px;padding:10px 0}
  h2{font-size:20px;margin-bottom:3px}
  .sub{margin-bottom:10px;font-size:12px}
  .chip{margin-bottom:10px}
  .pw-row{margin:10px 0 4px}
}

/* preview-mode step nav (?preview=1 only) */
#previewBar{position:fixed;left:0;right:0;bottom:0;display:none;
  align-items:center;gap:10px;z-index:999;
  background:#131118;border-top:1px solid var(--bdr);
  padding:10px 14px max(10px,env(safe-area-inset-bottom,0px))}
body.preview-mode #previewBar{display:flex}
body.preview-mode .wrap{padding-bottom:76px}
#previewBar button{flex:1;padding:10px 0;border:1px solid var(--bdr);border-radius:12px;
  background:#131118;color:var(--dim);font-size:13px;font-weight:700;cursor:pointer;
  letter-spacing:.02em}
#previewBar button:disabled{opacity:.3;cursor:default}
#previewBar button.primary{background:linear-gradient(180deg,var(--gold-hi),var(--gold) 55%,var(--gold-lo));
  border-color:transparent;color:#1a1407}
#previewLabel{font-size:11px;font-weight:700;letter-spacing:.05em;color:var(--dim);
  min-width:26px;text-align:center;text-transform:uppercase}
</style></head><body>
<div class="wrap">
  <!-- S0: Welcome -->
  <div class="screen active" id="s0">
    <h2 style="margin-bottom:0">WiFi Setup</h2>
    <div style="flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;gap:6px">
      <div style="font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--dim)">Device WiFi Name</div>
      <div id="s0chip" style="font-size:19px;font-weight:600;color:var(--text);text-align:center;letter-spacing:-.01em">APNAME</div>
    </div>
    <button class="btn" id="btnStart">Get Started</button>
  </div>

  <!-- S1: Scanning -->
  <div class="screen" id="s1">
    <h2>WiFi Setup</h2>
    <div class="spin-wrap">
      <div class="spinner"></div>
      <span class="spin-label">Scanning networks…</span>
    </div>
  </div>

  <!-- S2: WiFi List -->
  <div class="screen" id="s2">
    <h2>Select Network</h2>
    <div class="net-list" id="netList"></div>
    <button class="btn ghost" id="btnRescan">&#8635; Rescan</button>
  </div>

  <!-- S3: Password -->
  <div class="screen" id="s3">
    <div style="position:relative">
      <button class="back-link" id="btnBack3" style="position:absolute;left:0;top:50%;transform:translateY(-50%);margin:0">&#8592; Back</button>
      <h2>Password</h2>
    </div>
    <div style="flex:1;display:flex;flex-direction:column;justify-content:center">
      <div id="s3chip" style="width:100%;margin:16px 0 0;padding:15px;background:var(--s1);border:1px solid var(--bdr);border-radius:12px;color:var(--text);font-size:16px;pointer-events:none;user-select:none;text-align:center">SSID</div>
      <div class="pw-row" id="pwRow">
        <input id="passInput" type="password" placeholder="WiFi Password" style="text-align:center;padding-left:48px"
               autocomplete="off" autocapitalize="off" spellcheck="false">
        <button class="eye" id="eyeBtn" type="button">SHOW</button>
      </div>
    </div>
    <button class="btn" id="btnConnect">Connect</button>
  </div>

  <!-- S4: Connecting -->
  <div class="screen" id="s4">
    <div class="spin-wrap">
      <div class="spinner blue"></div>
      <div class="chip blue" id="s4chip">SSID</div>
      <div class="spin-label cdots">Connecting<span>.</span><span>.</span><span>.</span></div>
    </div>
  </div>

  <!-- S5: Sai mật khẩu -->
  <div class="screen" id="s5">
    <div class="err-center">
      <div class="err-icon" style="color:var(--red)">&#10007;</div>
      <div class="err-title">Wrong Password</div>
      <div class="chip red" id="s5chip">SSID</div>
    </div>
    <button class="btn danger" id="btnRetry5">Try Again</button>
    <button class="btn ghost" id="btnOther5">Other Network</button>
  </div>

  <!-- S6: Không tìm thấy mạng -->
  <div class="screen" id="s6">
    <div class="err-center">
      <div class="err-icon" style="color:var(--orange)">&#9888;&#65038;</div>
      <div class="err-title orange">Network Not Found</div>
      <div class="err-ssid" id="s6ssid">SSID</div>
    </div>
    <button class="btn" id="btnRescan6">&#8635; Rescan</button>
    <button class="btn ghost" id="btnOther6">Other Network</button>
  </div>
  <!-- S7: Thanh cong -->
  <div class="screen" id="s7">
    <div class="err-center">
      <div class="err-icon" style="color:var(--green)">&#10003;</div>
      <div class="err-title" style="background:linear-gradient(180deg,#7DF0C0,var(--green));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent">Connected!</div>
      <div class="chip" style="border-color:rgba(77,212,160,.45);color:var(--green)" id="s7chip">WiFi</div>
      <div class="sub" id="s7msg" style="margin-top:4px;max-width:290px">Closing setup window…</div>
    </div>
  </div>

</div>
<div id="previewBar">
  <button id="prevBtnNav" type="button">&#8592; Back</button>
  <span id="previewLabel">S0</span>
  <button id="nextBtnNav" type="button" class="primary">Next &#8594;</button>
</div>
<script>
var AP_NAME="__AP_NAME__";
var screens=["s0","s1","s2","s3","s4","s5","s6","s7"];
var curSSID="", curSecured=true, pollTimer=null, pollStartedAt=0, connectWatchdogTimer=null, presumedOk=false;
var wonLock=false, myConnectId=null;   // [FIX] wonLock: chỉ phone THẮNG lock mới poll/hiện success. myConnectId: khớp status.id chống nhầm phiên.
var isLegacyAndroid=/Android\s(?:4|5|6|7|8|9)\b/i.test(navigator.userAgent||"");
var PREVIEW_MODE=/(^|[?&])preview=1(&|$)/.test(location.search);
var STORAGE_KEY="cardfeeder.portal.selection.v2";
var lastStage="list";

function loadSelection(){
  try{
    var raw=sessionStorage.getItem(STORAGE_KEY)||localStorage.getItem(STORAGE_KEY)||"";
    if(!raw){return;}
    var saved=JSON.parse(raw);
    if(saved && saved.ssid){
      curSSID=saved.ssid;
      curSecured=saved.secure!==false;
      lastStage=saved.stage==="password"?"password":"list";
    }
  }catch(e){}
}

function saveSelection(){
  if(!curSSID){return;}
  var raw=JSON.stringify({ssid:curSSID, secure:!!curSecured, stage:lastStage, ts:Date.now()});
  try{sessionStorage.setItem(STORAGE_KEY, raw);}catch(e){}
  try{localStorage.setItem(STORAGE_KEY, raw);}catch(e){}
}

function clearSelection(){
  try{sessionStorage.removeItem(STORAGE_KEY);}catch(e){}
  try{localStorage.removeItem(STORAGE_KEY);}catch(e){}
}

function setSuccessMessage(msg){
  var el=document.getElementById("s7msg");
  if(el){el.textContent=msg;}
}

function tryClosePortalWindow(){
  setSuccessMessage("Connected. Closing setup window...");
  try{ window.open("","_self"); }catch(e){}
  try{ window.close(); }catch(e){}

  setTimeout(function(){
    try{ history.back(); }catch(e){}
  }, 250);

  setTimeout(function(){
    try{ location.replace("about:blank"); }catch(e){}
  }, 700);

  setTimeout(function(){
    try{ location.replace("http://connectivitycheck.gstatic.com/generate_204"); }catch(e){}
  }, 1200);

  setTimeout(function(){
    try{ location.replace("http://captive.apple.com/hotspot-detect.html"); }catch(e){}
  }, 2000);

  setTimeout(function(){
    setSuccessMessage("Connected. You can close this window if it stays open.");
  }, 3200);
}

function go(id){
  screens.forEach(function(s){
    var el=document.getElementById(s);
    el.classList.toggle("active", s===id);
  });
}

function bars(sig){
  var n=sig>=75?4:sig>=50?3:sig>=25?2:1;
  var h=[7,10,14,18], o="";
  for(var i=0;i<4;i++) o+='<i class="'+(i<n?"on":"")+'" style="height:'+h[i]+'px"></i>';
  return o;
}

// S0 init
if(PREVIEW_MODE){clearSelection();}
loadSelection();
document.getElementById("s0chip").textContent=AP_NAME;
document.getElementById("btnStart").onclick=function(){
  go("s1"); scan();
};
if(curSSID && !PREVIEW_MODE){
  go("s1");
  setTimeout(scan, 50);
}

// S1: scan
function scan(){
  go("s1");
  fetch("/api/wifi/scan").then(function(r){return r.json();}).then(function(d){
    buildList(d.networks||[]);
    go("s2");
  }).catch(function(){
    // retry once after 2s
    setTimeout(function(){
      fetch("/api/wifi/scan").then(function(r){return r.json();}).then(function(d){
        buildList(d.networks||[]); go("s2");
      }).catch(function(){
        var l=document.getElementById("netList");
        l.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%"><p style="color:var(--dim);text-align:center">Try again</p></div>';
        go("s2");
      });
    }, 2000);
  });
}

function buildList(nets){
  var l=document.getElementById("netList");
  if(!nets.length){
    l.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%"><p style="color:var(--dim);text-align:center">No networks found.</p></div>';
    return;
  }
  l.innerHTML="";
  var selectedEl=null;
  nets.forEach(function(n){
    var d=document.createElement("div"); d.className="net";
    if(curSSID && n.ssid===curSSID){
      d.classList.add("selected");
      selectedEl=d;
    }
    // XSS-safe: cột sóng sinh từ số nguyên (an toàn) qua innerHTML; nhưng TÊN
    // MẠNG do thiết bị lạ phát sóng kiểm soát → dựng bằng textContent, trình
    // duyệt KHÔNG diễn giải thành HTML (tên "<img onerror=...>" chỉ hiện ra chữ).
    var barsEl=document.createElement("span"); barsEl.className="bars"; barsEl.innerHTML=bars(n.signal);
    var ssidEl=document.createElement("span"); ssidEl.className="ssid"; ssidEl.textContent=n.ssid;
    if(n.secure){ var lk=document.createElement("span"); lk.className="lock"; lk.innerHTML="&#128274;"; ssidEl.appendChild(lk); }
    d.appendChild(barsEl); d.appendChild(ssidEl);
    d.onclick=function(){
      curSSID=n.ssid; curSecured=!!n.secure;
      lastStage="password";
      saveSelection();
      document.getElementById("s3chip").textContent=n.ssid;
      document.getElementById("passInput").value="";
      document.getElementById("passInput").type="password";
      document.getElementById("eyeBtn").textContent="SHOW";
      document.getElementById("pwRow").style.display=curSecured?"":"none";
      go("s3");
    };
    l.appendChild(d);
  });
  if(selectedEl){
    selectedEl.scrollIntoView({block:"nearest"});
  }
}

document.getElementById("btnRescan").onclick=function(){scan();};
document.getElementById("btnBack3").onclick=function(){
  lastStage="list";
  saveSelection();
  go("s2");
};

document.getElementById("eyeBtn").onclick=function(){
  var p=document.getElementById("passInput");
  var showing=(p.type==="password");
  p.type=showing?"text":"password";
  this.textContent=showing?"HIDE":"SHOW";
};

document.getElementById("btnConnect").onclick=function(){
  var pw=document.getElementById("passInput").value;
  lastStage="password";
  saveSelection();
  document.getElementById("s4chip").textContent=curSSID;
  go("s4");
  wonLock=false; myConnectId=null;
  fetch("/api/wifi/connect",{method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify({ssid:curSSID,password:pw})
  }).then(function(r){return r.json();}).then(function(d){
    if(d && d.error==="busy"){
      // ĐIỆN THOẠI KHÁC đang cấu hình (ai bấm trước thắng) -> KHÔNG poll success,
      // vào màn CHỜ. [FIX] Trước đây phone thua vẫn startPoll -> đọc state 'ok' của
      // phone thắng -> hiện "Connected!" GIẢ với tên mạng mình chọn.
      stopPoll();
      showBusyWait();
      return;
    }
    if(d && d.ok && d.pending){
      wonLock=true; myConnectId=d.connect_id||null;   // CHỈ phone THẮNG mới poll
      startPoll();
      return;
    }
    stopPoll(); backToPassword();                       // phản hồi lạ -> nhập lại
  }).catch(function(){
    // Mất response NGAY khi POST: phone thua nhận 'busy' trong mili-giây TRƯỚC khi
    // AP hạ, nên KHÔNG rơi vào đây. Rơi vào đây gần như chắc là phone THẮNG + AP vừa hạ.
    wonLock=true;
    startPoll();
  });
  // [FIX] KHÔNG startPoll() vô điều kiện ở đây nữa.
};

// S4 polling
function startPoll(){
  stopPoll();
  pollStartedAt=Date.now();
  presumedOk=false;
  if(connectWatchdogTimer){clearTimeout(connectWatchdogTimer);}
  connectWatchdogTimer=setTimeout(function(){
    fallbackAfterTimeout();
  }, isLegacyAndroid ? 14000 : 46000);
  pollTimer=setTimeout(doPoll, 1200);
}
function stopPoll(){
  if(pollTimer){clearTimeout(pollTimer);pollTimer=null;}
  if(connectWatchdogTimer){clearTimeout(connectWatchdogTimer);connectWatchdogTimer=null;}
}
// [FIX] Màn CHỜ cho điện thoại THUA (nhận 'busy'): KHÔNG poll success, chỉ chờ máy
//   rảnh lại rồi cho thử lại. Không bao giờ hiện "Connected!" cho phone thua.
function showBusyWait(){
  stopPoll();
  var c=document.getElementById("s4chip"); if(c){c.textContent="another device configuring…";}
  go("s4");
  pollTimer=setTimeout(busyPoll, 2500);
}
function busyPoll(){
  fetch("/api/wifi/status").then(function(r){return r.json();}).then(function(d){
    if(d && d.state==="connecting"){ pollTimer=setTimeout(busyPoll, 2500); return; }
    backToPassword();   // máy rảnh (idle/error/ok) -> cho user thử lại mạng của mình
  }).catch(function(){ pollTimer=setTimeout(busyPoll, 3500); });  // mất mạng: winner có thể đã đổi mạng -> chờ tiếp
}
function backToNetworkList(){
  stopPoll();
  lastStage="list";
  saveSelection();
  scan();
}
function backToPassword(){
  stopPoll();
  lastStage="password";
  saveSelection();
  document.getElementById("s3chip").textContent=curSSID || "SSID";
  document.getElementById("pwRow").style.display=curSecured?"":"none";
  document.getElementById("passInput").focus();
  go("s3");
}
function fallbackAfterTimeout(){
  if(presumedOk){return;}
  if(isLegacyAndroid){
    if(lastStage==="password" && curSSID){
      backToPassword();
    } else {
      backToNetworkList();
    }
    return;
  }
  backToPassword();
}
function doPoll(){
  var _ctrl=new AbortController();
  var _t=setTimeout(function(){_ctrl.abort();},2200);
  fetch("/api/wifi/status",{signal:_ctrl.signal}).then(function(r){clearTimeout(_t);return r.json();}).then(function(d){
    // [FIX] Chỉ nhận kết quả của ĐÚNG phiên mình. myConnectId null = thắng qua .catch
    // (không kịp nhận id) -> bỏ qua check (vẫn là chủ phiên). id KHÁC = state của phiên
    // phone khác -> KHÔNG nhận (chống "Connected!" giả).
    var idOk = (!myConnectId) || (d.id===myConnectId);
    if(idOk && d.state==="ok"){
      stopPoll();
      document.getElementById("s7chip").textContent=curSSID;
      clearSelection();
      go("s7");
      tryClosePortalWindow();
      return;
    }
    if(idOk && d.state==="error"){
      if(d.error==="not_found"){
        backToNetworkList();
      } else {
        backToPassword();
      }
      return;
    }
    pollTimer=setTimeout(doPoll, 1200);
  }).catch(function(){
    clearTimeout(_t);
    var elapsed=Date.now()-pollStartedAt;
    if(elapsed < 4000){
      // vừa bấm Connect, AP có thể chưa kịp hạ — thử lại nhanh
      pollTimer=setTimeout(doPoll, 800);
      return;
    }
    // Mất mạng sau vài giây rất có thể là do Pi đã hạ AP để nối mạng đích —
    // đây là dấu hiệu Pi đang chuyển mạng (khả năng cao là THÀNH CÔNG), không
    // phải lỗi. Không có network để hỏi /api/wifi/status nữa nên coi như đã
    // xong, đồng thời vẫn poll nền (ít dày hơn) để bắt lỗi thật nếu AP được
    // bật lại (sai mật khẩu / không tìm thấy mạng).
    if(wonLock && !presumedOk){   // [FIX] CHỈ phone THẮNG lock mới được "presumed success" khi AP mất
      presumedOk=true;
      document.getElementById("s7chip").textContent=curSSID;
      clearSelection();
      go("s7");
      setSuccessMessage("Wi-Fi switched. If this screen doesn't close on its own, reconnect your phone's Wi-Fi manually.");
      tryClosePortalWindow();
    }
    if(elapsed < (isLegacyAndroid ? 14000 : 45000)){
      pollTimer=setTimeout(doPoll, 3000);
      return;
    }
  });
}

// S5
document.getElementById("btnRetry5").onclick=function(){go("s3");};
document.getElementById("btnOther5").onclick=function(){go("s2");};

// S6
document.getElementById("btnRescan6").onclick=function(){scan();};
document.getElementById("btnOther6").onclick=function(){go("s2");};

// Preview mode (?preview=1) — step through S0-S7 with Next/Back, mock data,
// no real network calls. Only for reviewing the design on a phone/PC.
if(PREVIEW_MODE){
  document.body.classList.add("preview-mode");
  var PREVIEW_MOCK=[
    {ssid:"BBSW_Lounge_5G", signal:92, secure:true},
    {ssid:"Viettel_2.4G_ABC", signal:74, secure:true},
    {ssid:"FPT_Telecom_Home", signal:61, secure:true},
    {ssid:"VNPT_WiFi_2F", signal:48, secure:true},
    {ssid:"CafeOpen", signal:35, secure:false}
  ];
  var pvStepMatch=/[?&]step=(\d+)/.exec(location.search);
  var pvIdx=pvStepMatch?Math.max(0,Math.min(screens.length-1,parseInt(pvStepMatch[1],10))):0;
  function pvSync(){
    var sid=screens[pvIdx];
    if(!curSSID){curSSID=PREVIEW_MOCK[0].ssid; curSecured=true;}
    if(sid==="s2"){buildList(PREVIEW_MOCK);}
    ["s3chip","s4chip","s5chip","s7chip"].forEach(function(id){
      var el=document.getElementById(id);
      if(el){el.textContent=curSSID;}
    });
    var s6el=document.getElementById("s6ssid");
    if(s6el){s6el.textContent=curSSID;}
    go(sid);
    document.getElementById("previewLabel").textContent=sid.toUpperCase()+" / "+(pvIdx+1)+"-"+screens.length;
    document.getElementById("prevBtnNav").disabled=(pvIdx===0);
    document.getElementById("nextBtnNav").disabled=(pvIdx===screens.length-1);
  }
  document.getElementById("nextBtnNav").onclick=function(){
    if(pvIdx<screens.length-1){pvIdx++; pvSync();}
  };
  document.getElementById("prevBtnNav").onclick=function(){
    if(pvIdx>0){pvIdx--; pvSync();}
  };
  pvSync();
}
</script></body></html>"""


def _build_page() -> str:
    return _PAGE_TPL.replace("__AP_NAME__", _get_ap_name())


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route("/")
def index():
    # A phone opening the portal fresh must not see a stale result from a
    # previous session (state stays 'ok'/'error' forever otherwise). Reset
    # terminal states to idle; NEVER touch 'connecting' — first phone to hit
    # Connect keeps the lock ("first one wins"), others still get busy.
    with _conn_lock:
        if _conn["state"] in ("ok", "error"):
            _conn["state"] = "idle"
            _conn["error"] = None
            _conn["id"] = None
    return _build_page()


# captive-portal probes — 302 redirect là chuẩn captive portal:
# OS probe mong 204/200-success, nhận 302 → phát hiện captive → bắn notification tự mở portal
# Vì dnsmasq hijack ALL DNS → mọi hostname đều trỏ về 10.42.0.1, chỉ cần handle path
@app.route("/generate_204")          # Android / Chrome (tất cả hãng)
@app.route("/gen_204")               # Android cũ / Chrome < 40
@app.route("/hotspot-detect.html")   # iOS / macOS (Apple CNA)
@app.route("/library/test/success.html")  # iOS cũ (pre-iOS 9)
@app.route("/connecttest.txt")       # Windows 7–10
@app.route("/ncsi.txt")              # Windows XP/Vista/7
@app.route("/redirect")              # Windows 10+
@app.route("/fwlink/")               # Windows 10+ (MS fwlink)
@app.route("/canonical.html")        # Firefox
@app.route("/success.txt")           # Firefox cũ
@app.route("/generate204")           # Samsung (no underscore variant)
@app.route("/kindle-wifi/wifistub.html")  # Amazon Kindle
@app.route("/kindle-wifi/wifiredirect.html")
@app.route("/miui/detectportal.php") # Xiaomi MIUI
@app.route("/wpad.dat")              # Windows WPAD auto-proxy
def captive():
    return redirect("http://10.42.0.1/", 302)


@app.errorhandler(404)
def catch_all_404(e):
    # Mọi URL lạ (probe chưa biết của hãng nào đó) → redirect về portal
    # Chỉ bypass nếu là API call (trả 404 JSON bình thường)
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found"}), 404
    return redirect("http://10.42.0.1/", 302)


@app.route("/api/wifi/scan")
def scan():
    # AP mode chiếm radio wlan0 — rescan sẽ block 15s, dùng cache ngay
    try:
        active = subprocess.run(["sudo","-n","nmcli","-t","-f","NAME","con","show","--active"],
                                capture_output=True, text=True, timeout=5).stdout
        ap_on = AP_CON in active
    except Exception:
        ap_on = True
    if not ap_on:
        try:
            nmcli("dev", "wifi", "rescan", timeout=15)
        except Exception:
            pass
    else:
        # AP đang bật: driver Pi 5 (brcmfmac kernel mới) THƯỜNG vẫn cho scan khi
        # làm AP → thử rescan ngắn để thấy mạng MỚI BẬT (hotspot điện thoại).
        # Driver từ chối → nuốt lỗi, rơi về cache như cũ (không tệ hơn trước).
        try:
            nmcli("dev", "wifi", "rescan", timeout=6)
            time.sleep(1.0)   # cho NM kịp cập nhật kết quả vào cache
        except Exception:
            pass
    r = nmcli("-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "no")
    ap_ssid = _get_ap_name()   # SSID AP của chính ta — KHÔNG hiện trong list cho user chọn
    seen, nets = set(), []
    for line in r.stdout.splitlines():
        parts = line.replace("\\:", "\x00").split(":")
        parts = [p.replace("\x00", ":") for p in parts]
        if len(parts) < 3:
            continue
        ssid, signal, sec = parts[0], parts[1], parts[2]
        if not ssid or ssid in seen or ssid == ap_ssid:
            continue
        seen.add(ssid)
        nets.append({"ssid": ssid,
                     "signal": int(signal) if signal.isdigit() else 0,
                     "secure": sec not in ("", "--", "none")})
    nets.sort(key=lambda n: n["signal"], reverse=True)
    return jsonify({"networks": nets})


@app.route("/api/wifi/status")
def wifi_status():
    with _conn_lock:
        return jsonify({"state": _conn["state"], "error": _conn["error"], "id": _conn["id"]})


@app.route("/api/wifi/connect", methods=["POST"])
def connect():
    d = request.get_json(force=True, silent=True) or {}
    ssid = (d.get("ssid") or "").strip()
    password = d.get("password") or ""
    if not ssid:
        return jsonify({"ok": False, "error": "Missing network name"})
    with _conn_lock:
        # AI BẤM TRƯỚC THẮNG: đang có điện thoại khác connect dở → từ chối cái
        # sau (2 luồng nmcli đua nhau sẽ phá nhau: cùng hạ AP, cùng ghi lock).
        if _conn["state"] == "connecting":
            return jsonify({"ok": False, "error": "busy",
                            "message": "Another phone is connecting — wait a moment"})
        global _conn_seq
        _conn_seq += 1
        _conn["id"] = str(_conn_seq)          # token cho phiên THẮNG này
        _conn["state"] = "connecting"
        _conn["error"] = None
        cid = _conn["id"]
    threading.Thread(target=_do_connect, args=(ssid, password), daemon=True).start()
    return jsonify({"ok": True, "pending": True, "connect_id": cid})


def _notify_kiosk_connected():
    """Báo kiosk (cùng máy, cổng 8800) 'mạng đã vào' để nó tắt QR NGAY (≤1s)
    thay vì chờ vòng _refresh_wifi 5s (+probe máy in). Fire-and-forget — kiosk
    chưa chạy/lỗi thì thôi, vòng 5s vẫn là lưới an toàn phía sau."""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8800/api/wifi/connected",
                                     data=b"", method="POST")
        urllib.request.urlopen(req, timeout=2)
    except Exception as e:
        logger.debug("notify kiosk connected: %s", e)


def _do_connect(ssid: str, password: str):
    """Giữ MANUAL_LOCK_FILE suốt quá trình để wifi_watchdog.py đứng yên,
    tránh 2 tiến trình cùng nmcli/AP song song (nguyên nhân gây flapping)."""
    try:
        with open(MANUAL_LOCK_FILE, "w") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logger.warning("write manual lock: %s", e)
    try:
        _do_connect_locked(ssid, password)
    finally:
        try:
            os.remove(MANUAL_LOCK_FILE)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("remove manual lock: %s", e)


def _do_connect_locked(ssid: str, password: str):
    time.sleep(1.5)  # cho HTTP response kịp về điện thoại trước khi cắt AP

    def _set_error(code: str):
        with _conn_lock:
            _conn["state"] = "error"
            _conn["error"] = code

    def _delete_saved_profile(reason: str):
        # Xoá MỌI NM profile trùng tên SSID (thường nmcli đặt tên profile = SSID).
        # Dùng 2 chỗ:
        #  (a) TRƯỚC khi connect — để `nmcli dev wifi connect` LUÔN tạo profile mới
        #      sạch. Nếu để nmcli "dùng lại" profile cũ (setup lại) hay dính lỗi
        #      "802-11-wireless-security.key-mgmt: property is missing" → fail vòng 1
        #      → AP bật lại → QR hiện lần 2 (bug 2026-07-03).
        #  (b) SAU khi connect fail — dọn profile hỏng để has_saved_wifi() không bị
        #      đánh lừa + NM không autoconnect profile hỏng giành sóng với AP.
        try:
            r = nmcli("-t", "-f", "NAME", "con", "show", timeout=10)
            for line in (r.stdout or "").splitlines():
                if line.replace("\\:", ":") == ssid:
                    nmcli("con", "delete", ssid, timeout=10)
                    logger.info("xoá profile '%s' (%s)", ssid, reason)
                    break
        except Exception as e:
            logger.warning("delete profile %s: %s", ssid, e)

    def _restore_ap(code: str):
        _set_error(code)
        if code == "wrong_password":
            _delete_saved_profile("dọn profile hỏng sau connect fail")
        try:
            subprocess.run(["bash", AP_SCRIPT, "up"], capture_output=True, text=True, timeout=40)
            logger.info("AP restored after connect failure: %s", code)
        except Exception as e:
            logger.warning("restore AP after %s: %s", code, e)

    # 1. hạ AP (xóa captive DNS/nft trước để máy không mất DNS sau khi nối WiFi nhà)
    #    MỖI lệnh 1 try RIÊNG: trước đây gộp 1 try — nft delete timeout thì
    #    `nmcli con down AP` bị BỎ QUA → AP còn bật trong lúc connect (xung đột sóng).
    try:
        subprocess.run(["sudo", "-n", "nft", "delete", "table", "ip", "cardfeeder_captive"],
                       capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.warning("drop captive nft: %s", e)
    try:
        subprocess.run(["sudo", "-n", "rm", "-f",
                        "/etc/NetworkManager/dnsmasq-shared.d/card-captive.conf"],
                       capture_output=True, text=True, timeout=10)
    except Exception as e:
        logger.warning("drop captive dns: %s", e)
    try:
        nmcli("con", "down", AP_CON, timeout=15)   # QUAN TRỌNG NHẤT — luôn phải chạy
    except Exception as e:
        logger.warning("con down AP: %s", e)

    # 2. bật radio, đợi card thấy SSID
    try:
        nmcli("radio", "wifi", "on", timeout=10)
    except Exception as e:
        logger.warning("radio on: %s", e)

    # 3. kết nối WiFi — CONNECT THẲNG, không tiền-quét (tối ưu 2026-07-03):
    # bản cũ quét tìm SSID trước khi nối (3-6s, tệ nhất +12s chờ) trong khi
    # `nmcli dev wifi connect` TỰ quét nội bộ. Giờ nối ngay; nmcli báo
    # "No network with SSID" thì mới rescan + thử lại (tối đa 2 lần) → đường
    # vui nhanh hơn 4-15s, kết nối THẬT (nmcli return 0 = associate + DHCP xong).
    # XOÁ profile cũ trùng SSID trước (fix "key-mgmt property is missing" khi
    # setup lại) — chỉ xoá đúng SSID đang setup, các mạng khác vẫn được nhớ.
    _delete_saved_profile("làm mới trước khi nối")

    args = ["dev", "wifi", "connect", ssid]
    if password:
        args += ["password", password]
    args += ["ifname", IFACE]

    notfound = 0
    for attempt in range(1, 4):
        try:
            r = nmcli(*args, timeout=45)
            if r.returncode == 0:
                logger.info("connected: %s (attempt %d)", ssid, attempt)
                with _conn_lock:
                    _conn["state"] = "ok"
                    _conn["error"] = None
                _notify_kiosk_connected()   # kiosk tắt QR ngay (≤1s), khỏi chờ vòng 5s
                return
            err = (r.stderr or r.stdout or "").lower()
            logger.warning("connect %s attempt %d: %s", ssid, attempt, err.strip())
            if "no network with ssid" in err:
                # card vừa rời AP mode có thể chưa thấy mạng → rescan rồi thử lại;
                # 2 lần liên tiếp vẫn không thấy = mạng không tồn tại thật
                notfound += 1
                if notfound >= 2:
                    _restore_ap("not_found")
                    return
                try:
                    nmcli("dev", "wifi", "rescan", timeout=20)
                except Exception:
                    pass
                time.sleep(2)
                continue
            if any(kw in err for kw in ("secret", "wrong", "incorrect", "no secrets",
                                        "activation failed", "psk", "key-mgmt")):
                _restore_ap("wrong_password")
                return
        except Exception as e:
            logger.warning("connect %s attempt %d error: %s", ssid, attempt, e)
        time.sleep(3)
        try:
            nmcli("dev", "wifi", "rescan", timeout=20)
        except Exception:
            pass

    # hết 3 lần → coi như sai mật khẩu
    _restore_ap("wrong_password")
    logger.warning("không nối được %s → AP được bật lại ngay", ssid)


if __name__ == "__main__":
    logger.info("wifi portal http://0.0.0.0:%d (iface=%s)", PORT, IFACE)
    app.run(host="0.0.0.0", port=PORT, threaded=True)
