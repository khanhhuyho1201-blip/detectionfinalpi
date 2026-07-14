/*
========================================================
  SMART CARD FEEDER — AI CONTROL (encoder + sensor)   v5.51
========================================================
  Kéo từng lá bài từ đáy chồng, đếm, cho camera đọc -> tốc PHẢI đều (camera nét).

  TRIẾT LÝ "AI VERSION": chỉ 1 giá trị LÝ TƯỞNG cố định (steadySpeed = 570 c/s);
  mọi actuator còn lại TỰ THÍCH NGHI real-time theo tín hiệu sensor + encoder.

  3 BƯỚC mỗi vòng loop (theo thứ tự):
    1) pollSensor()         — đo lá bằng ENCODER (độ dài che sensor) -> đếm + phát hiện cụm.
    2) outerControlUpdate() — targetSpeed = steadySpeed + speedTrim (BOUNDED, tự về ideal sau 1-2 lá):
         . trim ≈ 0           -> BÁM ideal (đa số thời gian)                      [md=CRZ]
         . lá kéo CHẬM/stuck   -> trim DƯƠNG (tăng tốc kéo ra)                     [md=ESC]
         . CỤM / lá QUÁ NHANH  -> trim ÂM    (chậm tách, chống vồ nhiều lá ở tail) [md=CLMP]
         . kẹt > 2s            -> đề-ba (quay NGƯỢC re-grip) rồi xuôi lại          [md=REV]
    3) velocityLoopUpdate() — PI (encoder -> PWM) giữ targetSpeed; tải nặng -> tự tăng PWM (torque).
         . floor PWM TỰ HỌC (freeSpinFloor): coast giữa 2 lá hội tụ ≈ ideal (hết "nhanh vô lý").

  TỰ THÍCH NGHI (real-time, KHÔNG hardcode): PWM (PI), freeSpinFloor, speedTrim,
    normLen (dài lá), dtFilt (nhịp), pickupPWM (lực bắt lá), loadEMA (trọng lượng chồng).

  MOTOR DC chạy LIÊN TỤC. Serial 115200. Lệnh: S=status | G=DIAG | N<n>=so la | R=test-reverse.
  Hết lá > ~8s -> tự DỪNG (stall).
========================================================
*/

#include <avr/wdt.h>
#include <EEPROM.h>

// ======================================================
// >>>>>>>>>>>>>>  KHU VỰC TUNING  <<<<<<<<<<<<<<
// ======================================================

// --- Nhịp lá = TELEMETRY (chỉ để LOG "la/giay", KHÔNG điều khiển — điều khiển bám steadySpeed) ---
const float    TARGET_RATE_CPS  = 2.0;     // nhịp "trung tính" seed filtRate lúc khởi động / vừa thoát stall
const float    RATE_ALPHA       = 0.25;    // EMA lọc nhịp lá/giây hiển thị trong log
const float    SPEED_MIN        = 120.0;  // v7.0: 300->180 — gate hoc FF (targetSpeed>SPEED_MIN); toc cruise moi ~250-300   // SÀN tốc — dùng trong điều kiện lấy mẫu loadEMA

// --- Vòng tốc độ PI (tầng trong / encoder) ---
const uint16_t VELOCITY_SAMPLE_MS = 50;    // chu kỳ lấy mẫu tốc độ (~20Hz)
const float    KP = 0.06;                  // hệ số tỉ lệ
const float    KI = 0.30;                  // hệ số tích phân
const uint8_t  PWM_HARD_MIN = 55;          // v6.5: HẠ 80->55. Ở 450, sàn 80 ép tốc tối thiểu ~800 c/s (free-spin) -> PI bão hòa sàn, KHÔNG hãm về 450 được. Hạ sàn -> PI có quyền hãm về đúng 450 (bớt giật/đề-ba). Nếu cog/giật ở PWM thấp thì nâng lại.
const uint8_t  PWM_HARD_MAX = 255;
const uint8_t  PWM_START    = 130;         // PWM soft-start — khởi động NHẸ (bớt spike đầu)

// ===== v6.0: ĐIỀU KHIỂN ỔN ĐỊNH — setpoint CỐ ĐỊNH + FEED-FORWARD theo số lá (TỰ HỌC, lưu EEPROM) =====
//   Tải = trọng lượng chồng đè THẲNG lên con lăn -> GIẢM ĐỀU theo số lá đã kéo (nhiễu BIẾT TRƯỚC + mượt).
//   => Khử THẲNG bằng feed-forward: PWM nền = ffTable[vùng 50 lá]; PI chỉ vi chỉnh phần dư rất nhỏ.
//   ffTable TỰ HỌC trong lúc chạy (EMA về PWM cruise thực) + LƯU EEPROM -> mẻ sau bù đúng ngay, hội tụ dần.
const float    STEADY_SPEED   = 143.0;  // [SLOW 2026-07-06] ha toc theo yeu cau user  // v7.0: seed gan can bang dt700 (~270c/s); 450 cu = 45+ la dau phong 150ms/la  // v6.2: 200 ghim san PWM 80 -> ban + cum nhieu. CLEAN tach la can PWM cao -> 450 (PWM~150, tach sach), de camera/AI phan
// v6.6 GENTLE CADENCE: giu NHIP la DEU ca me. Chong voi -> slip giam -> la ra nhanh dan;
//   governor tu HA steadySpeed bu lai -> dt (nhip) ~ CAD_DT_TARGET deu tu dau toi cuoi.
//   Dung dtFilt (da EMA) + gain NHO + slew + bound -> bam XU HUONG cham, KHONG dua nhieu nhu v5.54.
const uint16_t CAD_DT_TARGET  = 333;   // [v19 FLAT-PACE] gop = FLATPACE_DT_MS (nhip DONG NHAT ca me, governor ko da servo) // truoc 800
// v7.3 PHASE-PACE: nhip theo TRONG LUONG chong (data me 600ms: pha nang chay 378ms van SACH len=37,
//   truot chi bat dau ~la150 khi chong nhe). Nang = grip khoe = chay nhanh an toan; nhe = 700 giu sach.
const uint16_t CAD_DT_HEAVY   = 333;   // [v19 FLAT-PACE] gop = 520 (dong nhat) // truoc 680
const uint16_t CAD_DT_MID     = 333;   // [v19 FLAT-PACE] gop = 520 (dong nhat) // truoc 740
const uint16_t CAD_PH1_END    = 140;   // het pha nang
const uint16_t CAD_PH2_END    = 280;   // het pha giua (tu day ve cuoi = 700ms)
const float    CAD_HI_HEAVY   = 180.0f;  // [MEDIUM 2026-07-07] 215->360 tran toc pha nang // [SLOW 2026-07-06] // v15: trần tốc pha nặng 500->360
const float    CAD_HI_MID     = 350.0f;  // [MEDIUM 2026-07-07] 205->330 tran toc pha giua // [SLOW 2026-07-06] // v15: 460->340
const float    CAD_GAIN       = 0.06f;  // c/s chinh tren moi ms lech dt (nho -> muot)
const float    CAD_STEP_MAX   = 3.0f;  // v7.0: 4->6, hoi tu nhanh hon van muot   // slew: chinh toi da moi la (chong giat)
const float    CAD_SPD_LO     = 143.0f;  // [MEDIUM 2026-07-07] 180->270 san toc (= mocdt700 ~270c/s bam sach) // [SLOW 2026-07-06] // v7.0 DATA: can bang dt700ms ~270c/s
const float    CAD_SPD_HI     = 180.0f;  // [MEDIUM 2026-07-07] 205->310 // giu lam MOC clamp startSpeed (heavy start)
// v18 2026-07-07: PHA NHE co band toc RIENG (cham han) de la tach sach -> HET TRUNG 2 LA (double-feed).
//   v17: pha nhe bi ghim o san 270 c/s (governor muon cham ma khong xuong duoc) -> 32 clump o la 279-408.
const float    CAD_LO_LIGHT   = 143.0f;  // [v18] SAN toc pha nhe — cham h...n de la nhe tach het truoc khi keo la ke
const float    CAD_HI_LIGHT   = 340.0f;  // [v18] TRAN toc pha nhe (thay CAD_SPD_HI trong cadSpdHiNow) -> pha nhe chay 175..220
// v6.8: TU HOC toc khoi dong (heavy-start). Trong cua so HEAVY (sau ramp), EMA startSpeed theo
//   steadySpeed governor dung -> me sau khoi dong DUNG toc -> 50 la dau deu luon. Luu EEPROM.
const uint16_t START_LEARN_END   = 70;    // hoc trong khoang la STARTUP_CARDS..END (vung chong nang)
const float    START_LEARN_ALPHA = 0.03f; // toc hoc startSpeed moi la trong cua so (hoi tu 1-2 me)
const float    PWM_FF_HEAVY   = 120.0;  // v7.0: seed cho ~300c/s (cu 150 cho 450)  // feed-forward MẶC ĐỊNH lúc chồng ĐẦY (vùng 0) — seed cho ~450 c/s
const float    PWM_FF_LIGHT   = 85.0;   // v7.0: seed cho ~300c/s  // feed-forward MẶC ĐỊNH lúc chồng VƠI (vùng cuối) — seed cho ~450 c/s
const float    FF_LEARN_ALPHA = 0.02f;  // tốc độ học ffTable mỗi tick cruise (nhỏ -> mượt, hội tụ vài chục giây)
const float    INTEG_TERM_MAX = 90.0f;  // |KI*velIntegral| tối đa (PWM) — PI chỉ vi chỉnh quanh feed-forward
const uint8_t  FF_EE_MAGIC    = 0xCD;   // v7.0: bump -> hoc lai FF/startSpeed cho toc moi   // v6.8: BUMP (them startSpeed vao EEPROM) -> doi format, hoc lai
const int      FF_EE_ADDR     = 0;      // địa chỉ EEPROM lưu ffTable (magic + NBUCKETS byte + checksum)

// ===== CÔNG TẮC TỔNG STEPPER (tạm tắt khi cần test feed/đếm riêng) =====
//   false = KHÔNG dùng stepper: KHÔNG hạ platform, KHÔNG home khi tắt máy. Feed/đếm/escape GIỮ NGUYÊN.
//   Muốn dùng lại: đổi thành true rồi nạp. (Code stepper vẫn còn nguyên, chỉ bị bỏ qua.)
const bool     STEPPER_ENABLED    = true;   // v28.1: BAT lai (da cap nguon stepper 2026-07-13). Luu y: moi 10 la ha platform ~72ms blocking (ly do v5.32 tung tat). Test truc tiep: P<n>=ha, H=home.

// --- Stepper hạ platform ---
const uint8_t  CARDS_PER_LOWER    = 10;    // cứ 10 lá -> hạ 1 lần
// ===== HÀNH TRÌNH PLATFORM — TỰ TÍNH TỪ THÔNG SỐ VẬT LÝ (v5.4) =====
//   Hạ tổng = TRAVEL_MM trên cả mẻ DECK_CARDS lá => mặt chồng lá luôn ~ngang cửa ra.
//   Đổi vít / bộ bài: sửa LEAD_MM/TRAVEL_MM/DECK_CARDS -> trần MAX_LOWER_STEPS tự ra. Bước hạ = STEPS_PER_LOWER_DEF (live: L<n>).
const float    LEAD_MM            = 8.0;   // lead vít me (mm/vòng) — T8 BƯỚC 8 (anh xác nhận)
// v28.1 (2026-07-13): VÍT ME 20cm (200mm), CÔNG TẮC HÀNH TRÌNH ở TOP (= home, vị trí nạp bài).
//   Mẻ 412 lá ~0.32mm/lá => hạ tổng ~132mm. TRAVEL trần = 150mm: đủ mẻ + headroom,
//   vít 200mm trừ hao 2 đầu vẫn dư ~40-50mm KHÔNG đụng đáy. Home = LÊN tới chạm công tắc top.
const float    TRAVEL_MM          = 150.0; // TRẦN hành trình dùng được (vít 200mm trừ hao)
const uint16_t DECK_CARDS         = 412;   // tổng lá 1 mẻ (để chia hạ/lá)
const uint16_t STEPS_PER_REV      = 200;   // NEMA17 1.8°/bước (full-step)
//   Nhịp hạ = 80 full-step/10 lá = 3.20mm (~0.32mm/lá) — GIỮ NGUYÊN nhịp đã chốt (không đụng logic tốc DC).
//   Cả mẻ 412 lá = 41.2 lần hạ × 80 = 3296 step = 131.8mm < trần 3750 (150mm) ✓.
const uint16_t STEPS_PER_LOWER_DEF = 80;   // bước hạ/10 lá MẶC ĐỊNH (~3.20mm ~0.32mm/lá) — CHỐT L80.
//   CHỐT TRẦN hành trình = TRAVEL_MM (150mm => 3750 step). KHÔNG hạ quá -> chống đụng đáy + home sai.
const uint16_t MAX_LOWER_STEPS    = (uint16_t)(TRAVEL_MM * STEPS_PER_REV / LEAD_MM + 0.5f);
const uint16_t STEP_PULSE_HIGH_US = 5;
const uint16_t STEP_DELAY_RUN_US  = 900;    // tốc độ HẠ (cruise)
const uint16_t STEP_DELAY_HOME_US = 8000;   // tốc HOME (us/full-step-eq) — v5.3: CHỐT 8000 (anh test: nhanh + êm + về đủ đỉnh)
                                            //   (microstep nên KHÔNG kẹt cộng hưởng dù đổi tốc). v28.2: đổi tốc = sửa hằng số này
                                            //   rồi nạp lại (lệnh H giờ = CHẠY home, không còn là chỉnh tốc).

// --- CHỐNG RUNG / GIẢM ỒN: gia tốc & GIẢM tốc (ramp 2 đầu) ---
const uint16_t STEP_START_RUN_US     = 2500;  // ramp-start khi HẠ
const uint16_t STEP_START_HOME_US    = 16000; // ramp-start khi HOME — TRỞ VỀ cũ (v5.0: 22000->16000)
const uint8_t  STEP_RAMP_STEPS       = 6;     // số bước ramp khi HẠ
const uint8_t  STEP_RAMP_STEPS_HOME  = 40;    // số bước ramp khi HOME — TRỞ VỀ cũ (v5.0: 100->40)
uint16_t       homeDelayUs           = STEP_DELAY_HOME_US;  // tốc HOME hiện hành (us/bước) — v28.2: cố định theo hằng số trên

// --- Sensor ---
const uint8_t  CARD_PRESENT_LEVEL = LOW;   // mức digitalRead khi CÓ lá che (TEST đã xác nhận = LOW)
const uint16_t SENSOR_CLEAR_MS    = 40;    // DEBOUNCE sườn: mức mới phải ỔN ĐỊNH >= ms này mới nhận
                                           // -> nhấp nháy < ms này bị bỏ qua (mép lá, khe in...)
// [v2] Đã BỎ công tắc máy A0 — máy chạy/dừng 100% bằng serial B1/B0 từ Pi.

// --- ĐO LÁ BẰNG ENCODER (bất biến theo tốc độ) + PHÁT HIỆN CỤM ---
//   "Độ dài" 1 lần che sensor = số count encoder từ sườn-XUỐNG tới sườn-LÊN.
//   Lá đơn ~ normLen (tự hiệu chuẩn). len >= CLUMP_RATIO*normLen => NHIỀU lá dính.
const float    LEN_INIT      = 160.0;  // normLen khởi tạo (count). HẠ 200->160: threshold=272<pair~284, bắt cụm sớm
const float    NORMLEN_FLOOR = 60.0;   // v5.52c: SÀN normLen — chống "sập chuẩn" (đo thực normLen tụt 25-32
                                       //   do lá mồi/đo ngắn -> lá đơn ~80 bị ratio>CLUMP_RATIO -> tưởng CỤM -> đếm phình).
                                       //   Giữ >=60 thì lá đơn (<=~100) luôn ratio<1.70 = đếm 1; cụm 2 lá (~140+) vẫn bắt đúng.
const float    LEN_ALPHA     = 0.20;   // EMA hiệu chuẩn normLen (CHỈ cập nhật khi là lá ĐƠN)
const float    CLUMP_RATIO   = 1.70;   // v5.29: REVERT ve 1.70 (muc run 412/0-clump muot nhat). Ha xuong 1.3-1.5 lam CLUMP-LIVE tut toc 550 giua feed -> GIAT/bat on. Dem dinh khit -> tach co khi.
const float    SLIP_CLUMP_GUARD = 3.0; // v6.2: gap-slip > mức này = roller free-spin/grip kém -> len encoder KHÔNG tin -> đếm 1 (chống cụm-ảo lúc đầu mẻ nặng trượt mạnh)
const uint8_t  CLUMP_CAUTION_CARDS = 6;   // v6.9: sau 1 cụm -> giữ CHẬM bao nhiêu lá (bò xuyên mảng bài dính, chống dính chùm)
const float    CLUMP_CAUTION_TRIM  = 80;  // v6.9: trim tối thiểu giữ trong lúc caution (tgt = steadySpeed - 80 = chậm hơn)
const float    CLUMP_STEP    = 1.5;    // mỗi lá THÊM trong cụm cộng ~1.5*normLen (gồm khe hở giữa 2 lá)
                                       //   n = round((ratio-1)/CLUMP_STEP)+1. Đo THỰC: cặp 2 lá ~2.5x normLen.
const uint8_t  CLUMP_MAX     = 6;      // chặn trên số lá/cụm (chống nhiễu/kẹt thổi phồng)
const float    LEN_MIN_FRAC  = 0.40;   // len < 0.40*normLen => quá ngắn = nhiễu, BỎ (không đếm)
const uint16_t LEN_ABS_MIN   = 20;     // len < count này (tuyệt đối) => nhiễu, BỎ + không seed

// --- TỐC ĐỘ KHI BẤT THƯỜNG (KHÔNG còn đẩy lên MAX) ---
const uint16_t BLOCK_MAX_MS     = 1000;  // CÓ lá che > ms này = cụm/kẹt TẠI sensor -> GIẢM tốc
const uint16_t GAP_STALL_MS     = 8000;  // KHÔNG thấy lá > ms này -> HẾT LÁ -> DỪNG motor (kiên nhẫn)
const uint16_t GAP_STALL_START  = 13000; // LÁ ĐẦU (cardCount==0): kiên nhẫn HƠN (priming lá đầu cần thời gian)
// v5.52e/53: sensor BỊ CHE LIÊN TỤC quá lâu = lá KẸT dưới sensor (không trượt qua được).
//   Trước đây chỉ bắt stall khi sensor TRỐNG (GAP_STALL) -> case kẹt-che-sensor KHÔNG được xử lý
//   -> máy ĐỨNG IM mãi. Lá đơn bình thường che sensor < ~1s.
//   v5.53 JAM-RECOVERY: che > JAM_RECOVER_MS -> tự "đề ba" (lùi rồi đẩy xuôi dứt khoát) để TỐNG lá ra,
//   thử tối đa JAM_RECOVER_MAX lần; vẫn kẹt -> DỪNG báo lỗi (Operation error). Reverse vẫn là BACKUP.
// v6.4: MOTOR-NOMOVE — bấm START mà CHƯA cấp điện motor: PWM ra nhưng encoder KHÔNG quay.
//   Báo lỗi NGAY (~1.5s) "Motor không chạy" thay vì đợi GAP_STALL 13s rồi báo mơ hồ "hết lá".
const uint16_t MOTOR_NOMOVE_MS     = 1500;  // sau ms này từ lúc ON, nếu encoder gần như đứng -> motor chưa cấp điện
const uint16_t MOTOR_NOMOVE_COUNTS = 50;    // |encoder| < mức này sau MOTOR_NOMOVE_MS = coi như KHÔNG quay
const uint16_t JAM_RECOVER_MS   = 3000;  // che liên tục > ms này -> thử gỡ
const uint8_t  JAM_RECOVER_MAX  = 3;     // số lần thử gỡ trước khi bỏ cuộc
const uint16_t STALL_DT_MS      = 1800;  // dt > mức này = vừa thoát stall -> KHÔNG kéo filtRate (chỉ log) về 0
const float    LOAD_PWM_HEAVY   = 115;   // init loadEMA (telemetry trọng lượng chồng)
const float    LOAD_ALPHA       = 0.04;  // EMA tải (telemetry) — chậm, mượt

// --- MASTER = steadySpeed = TOC LA DI QUA CAMERA (HANG SO, cham, net, on dinh ca me) ---
//   Camera phai DOC duoc tung la -> toc PHAI cham + KHONG DOI (nhanh = blur + dem sai).
//   GIU toc hang so, de PI tu dieu PWM (torque) theo trong luong chong -> nang day PWM cao, nhe giam,
//   MA toc la van KHONG DOI -> camera luon net. Bug (blur/nhanh/cham/yeu) -> chi chinh 1 so steadySpeed.
const float    STARTUP_FRAC    = 0.70f; // v6.7: 0.50->0.70 (bot lam cham dau me -> nhip DEU ngay tu dau). Neu cum dau me tang lai thi ha xuong.

// --- v5.27: DAU MAY chay CHAM (chong day-nang nhat -> manh la vo 2-3 la). Ramp len full dan ---
const uint16_t STARTUP_CARDS   = 25;    // so la DAU chay cham roi ramp len steadySpeed (toc dau = STARTUP_FRAC × steadySpeed)

// --- v5.50: IDEAL-LOCKED SPEED + BOUNDED AUTO-DECAY TRIM (AI vision + tay) ---
//   targetSpeed = steadySpeed(ideal) + speedTrim. Binh thuong trim≈0 -> BAM ideal (camera net, on dinh ca me).
//   CHI lech khi CO LY DO (vision tu sensor/encoder), va LECH co GIOI HAN + TU VE ideal sau 1-2 la:
//     la ra CHAM / stuck       -> trim DUONG (tang toc keo ra)         [md=ESC]
//     CUM / la ra QUA NHANH     -> trim AM   (cham lai tach, chong vo nhieu la o tail)  [md=CLMP]
//   Ap dung TOAN BO me (la 1 -> het), KHONG hardcode theo so la.
const uint16_t IDEAL_DT_MS     = 333;   // seed nhip dt (ms); dtFilt se HOC nhip THUONG that su khi chay
const float    DT_ALPHA        = 0.30f; // EMA loc nhip dt (CHI cap nhat tu la DON -> "ideal cadence" sach)
const float    TRIM_UP_MAX     = 60;    // v15 (chống nhanh/sót sensor): 200->60 — giảm biên sprint, lá đi đều không vọt
const float    TRIM_DOWN_MAX   = 130;   // tran trim (-): toi da ideal-130 (vd 570->440) khi cum/overfeed
const float    TRIM_BUMP_SLOW  = 90;    // la CHAM/de-ba -> cong trim (tang toc keo la ke)
const float    TRIM_BUMP_FAST  = 90;    // la NHANH/CUM  -> tru trim (cham lai tach)
const float    TRIM_DECAY_CARD = 0.40f; // moi la THUONG: trim *= so nay -> sau 1 la con 40%, 2 la 16% ~ VE ideal
const float    DT_SLOW_FRAC    = 1.45f; // dt > dtFilt*1.45 = la ra CHAM (le me) -> speed up
const float    DT_FAST_FRAC    = 0.62f; // dt < dtFilt*0.62 = la ra QUA NHANH (multi-feed) -> slow down
const uint16_t GAP_SPEEDUP_MS  = 250;   // v15: 80->250 — hoãn sprint, không vọt tốc ngay khi lá thoát sensor
const float    GAP_TRIM_RATE   = 0.40f; // v15: 1.20->0.40 — sprint tăng chậm, tránh lá phóng qua sensor quá nhanh

// --- RE-GRIP LÙI ("đề ba"): mãi không thấy lá qua sensor -> quay NGƯỢC nhẹ 1 xíu cho con lăn
//     bám lại mặt bài, rồi quay XUÔI bình thường. (Dùng lại bộ máy trạng thái nudge, nhưng pha
//     "dip" giờ là QUAY NGƯỢC thay vì chạy chậm xuôi.)
const bool     NUDGE_ENABLED     = true;
const uint16_t NUDGE_GAP_MS      = 2000;  // không thấy lá > ms này (2s = kẹt QUÁ LÂU) -> mới đề-ba-lùi. (1.2s là quá sớm)
const float    REGRIP_BACK_FRAC  = 0.25f; // lùi NET = 0.25 × normLen (~1/4 lá) — chỉ ĐỀ BA NHẸ cho con lăn bám lại mặt lá, không kéo lùi nhiều
const uint16_t REGRIP_MAX_MS     = 700;   // trần thời gian lùi (v5.49: 500->700, đủ thời gian hãm free-spin rồi lùi NET)
const uint8_t  REGRIP_PWM        = 120;   // PWM quay NGƯỢC — CỐ ĐỊNH (v5.44: 100->120 nâng nhẹ). Chỉ để bám lại.
const uint16_t NUDGE_INTERVAL_MS = 800;   // thời gian quay XUÔI giữa 2 lần đề-ba (v5.44: 600->800, cho lực kéo có thời gian ăn)
const uint8_t  NUDGE_MAX         = 6;     // tối đa số lần/lá; sau đó nhường STALL xử lý
// v5.44: LỰC QUAY XUÔI sau đề-ba BIẾN THIÊN MẠNH DẦN mỗi retry (reverse cố định, chỉ forward tăng).
//   floor xuôi = REGRIP_FWD_BASE + (lần-1)×STEP -> retry1=150, 2=180, 3=210... (ĐÈ cả free-spin cap để CỐ kéo lá cuối)
const uint8_t  REGRIP_FWD_BASE   = 140;   // lực kéo xuôi lần đề-ba ĐẦU (v5.47: 150->140)
const uint8_t  REGRIP_FWD_STEP   = 12;    // mỗi retry sau mạnh hơn (v5.47: 30->12, êm hơn)
const uint8_t  REGRIP_FWD_MAX    = 180;   // v5.47: CAP lực xuôi -> kéo firm nhưng KHÔNG yank cả xấp lá (trước lên 255)
const uint8_t  PWM_FREESPIN_FLOOR= 70;    // floor free-spin KHỞI TẠO (sau đó TỰ HỌC = freeSpinFloor, xem v5.51)
// v5.51 (AI): ADAPTIVE free-spin floor — tự học PWM để coast giữa 2 lá ≈ ideal (chống "hơi nhanh vô lý" coast 640)
const uint8_t  FS_FLOOR_MIN = 56;         // floor free-spin THẤP NHẤT (dưới nữa motor dễ đứng/giật)
const uint8_t  FS_FLOOR_MAX = 96;         // v7.5: 86->96 — coast khoe hon 1 chut, ben la som hon
const float    FS_MARGIN    = 25;         // deadband c/s quanh ideal: trong dải này KHÔNG chỉnh floor (chống sàng)
const float    FS_ADAPT     = 0.15f;      // PWM chỉnh mỗi tick 50ms (~3 PWM/giây) -> hội tụ từ từ, ổn định
const float    MEAS_ALPHA        = 0.25f; // lọc EMA tốc đo (mượt PI, bớt giật PWM do nhiễu lượng tử)

// --- v5.18: DYNAMIC PWM FLOOR — khoanh vùng PWM bắt lá, tăng dần khi lá stuck ---
//   Ý tưởng: đo PWM THỰC TẾ lúc lá vào sensor (= torque cần để bắt lá) -> lưu EMA pickupPWM.
//   Khi gap dài (lá stuck), motor free-spin ở PWM thấp (ví dụ 88) dù cần ~pickupPWM (98) để grip.
//   => Tăng dần PWM tối thiểu (floor) lên tới pickupPWM + boost, tạo đủ torque liên tục.
//   Ramp chậm để không phá pattern hiện tại; đạt trần sau ~3s.
const float    PICKUP_ALPHA      = 0.15f; // EMA cập nhật pickupPWM mỗi lần bắt lá thành công
const uint16_t PWM_RAMP_GAP_MS  = 120;   // v7.7: 200->120 — floor-assist som hon nua, cong huong voi sprint
const float    PWM_RAMP_RATE    = 0.35f; // v7.7: 0.20->0.35 — toi luc bat la trong ~0.25s
const float    PWM_GRAB_BOOST   = 90;    // v5.21: tran floor = pickupPWM + 90 ≈ 200 (TORQUE MANH bóc lá ket, KHONG tang speed -> ko clump). Ban cu escape dap PWM 255 qua speed=1500 -> clump; day dap luc ma giu toc 600.
// v17 GRIP LIEN TUC (STEADY-GRIND): giu LUC keo la LIEN TUC theo trong luong chong (nang->nhe),
//   de la luon duoc keo muot+deu, KHONG doi ket 2s roi moi nhap (het giat). S50-56/v16 truoc chi co
//   PWM_HARD_MIN=55 + nudge phan ung => la nang ket 10s/6 nhap. Day = san PWM toi thieu giam dan.
const uint8_t  GRIP_FLOOR_HEAVY = 145;   // luc grip lien tuc luc chong NANG (412 la) — du boc la ngay, khong ket
const uint8_t  GRIP_FLOOR_LIGHT = 60;    // [v18] 95->60: pha nhe grip NHE tay -> khong ep toc len, la tach sach (het trung 2 la)
// v19 FLAT-PACE dt-SERVO: giu NHIP dt gan NHU NHAU tuyet doi ca me (user 2026-07-07). Moi la DON:
//   la ra CHAM (dt>dich) -> tang grip (keo manh hon -> nhanh len); la NHANH (dt<dich) -> giam grip.
//   gripServo tu thich nghi trong luong chong (nang can grip cao, nhe can thap) MA van giu dt = dich.
//   Thay duong grip co dinh v17/v18 (GRIP_FLOOR_HEAVY/LIGHT gio chi la BIEN [MIN,MAX] cua servo).
const uint16_t FLATPACE_DT_MS  = 333;    // [v23 SLOW20] 520->650: nhip cham 20% deu (user: nhanh qua cam bien khong bat kip). SERVO_START_CARD=25 giu grip het pha nang -> 650 khong truot (khac v20 ha grip som). // DIAL toc DUY NHAT
const uint8_t  SERVO_START_CARD = 25;    // [v21] 6->25: servo KHONG duoc ha grip trong luc startup NANG (v20 ha xuong 100 -> truot). Giu grip manh het pha nang, servo vao khi chong da voi
const float    GSERVO_MIN      = 60.0f;  // grip nho nhat (pha nhe) — = GRIP_FLOOR_LIGHT
const float    GSERVO_MAX      = 135.0f; // grip lon nhat (pha nang) — du boc la, khong giat
const float    GSERVO_K        = 0.02f;  // do nhay servo (PWM / ms lech dt)
const float    GSERVO_UP       = 2.5f;   // tang grip toi da moi la (cham -> muot, khong vot)
const float    GSERVO_DN       = 3.5f;   // giam grip toi da moi la (nhanh hon chut -> nhay chong trung 2 la)
float          gripServo       = 130.0f; // luc grip HIEN TAI (servo dieu chinh) — init moi me o doMachineOn

// ======================================================
// >>>>>>>>>>>>  HẾT KHU VỰC TUNING  <<<<<<<<<<<<<
// ======================================================

const uint16_t totalCards = DECK_CARDS; // SỐ LÁ TRONG KHO — v28.2 (review): gộp về DECK_CARDS, đổi bộ bài chỉ sửa MỘT chỗ

// SỐ LÁ CẦN ĐẾM cho mẻ này — chỉnh LIVE bằng lệnh N<n> (vd N50, N100). 0 = chạy không giới hạn.
// v6.8 TARGET APPROACH — fix vot target (412 -> 414): con <= APPROACH_CARDS la
// cuoi me thi ep toc cham (la roi DON, khong dinh cum) -> cardCount khong nhay
// vot qua batchTarget (cardCount += n voi cum n>1 la nguyen nhan dem lo).
const uint8_t  APPROACH_CARDS = 12;     // con <= n la cuoi -> ve dich cham
const float    APPROACH_SPEED = 180.0;  // v7.0: = san governor moi  // toc ve dich (~SPEED_MIN; startup cu chay ~350 van bat la tot)
uint16_t batchTarget = 0;     // v5.26: 0 = keo HET sach hoc (khong dung o con so co dinh) -> khong bo sot la cuoi. Bao tong dem khi STALL.
bool     batchDone   = false;           // đã đếm đủ mẻ -> dừng, chờ tắt/bật lại

bool DEBUG_MODE = true;
#define DBG(msg)            // v5.28: bo log debug (giai phong flash)
#define DBG_VAL(k, v)       // v5.28: bo log debug (giai phong flash)

// ======================================================
// PIN CONFIG  — KHỚP SCHEMATIC PCB v2 (sơ đồ mạch xếp bài, 2026-07)
//   Đã đổi so với board cũ: MOTOR_IN1/IN2 (đảo), DIR_PIN (8->10), MS1/MS3 (đảo), bỏ A0, thêm D7.
//   Cần bản OLD-board: xem git history (git log --follow file này) — commit trước v2. ĐỪNG nạp file này cho board cũ.
// ======================================================
// [v2] MACHINE_SW (A0) ĐÃ GỠ HẲN — không còn công tắc vật lý. Máy điều khiển bằng serial B1/B0 từ Pi.
#define SENSOR_PIN   4    // v2: D4  -> TCRT5000 (J3)          [không đổi]
#define ENC_A        2    // v2: D2 -> J7 pin4 = C1 (Channel A)  INT0  [khớp schematic]
#define ENC_B        3    // v2: D3 -> J7 pin3 = C2 (Channel B)  INT1  [khớp schematic]
// Encoder ĐẾM NGƯỢC chiều? (chạy TỚI mà số đếm GIẢM, hoặc lỡ đảo dây A↔B) -> đổi 0<->1.
//   Chỉ đảo CHIỀU đếm, AN TOÀN, khỏi rút dây. (Đảo A↔B phần cứng cũng cho kết quả y hệt.)
//   Đo thật trên board v2 (2026-07-13): B1 xuôi -> ENC GIẢM (0→-6401), R lùi -> ENC TĂNG (+290)
//   = NGƯỢC quy ước -> REGRIP đo quãng lùi dính trớn xuôi, thoát sớm 40/40 ảo. Bật =1 cho khớp.
#define ENC_REVERSED 1
#define MOTOR_IN1    5    // v2: D5  -> DRV8871 IN1  (PWM)     [ĐỔI 6->5 theo schematic]
#define MOTOR_IN2    6    // v2: D6  -> DRV8871 IN2            [ĐỔI 5->6 theo schematic]
#define STEP_PIN     9    // v2: D9  -> A4988 STEP             [không đổi]
#define DIR_PIN     10    // v2: D10 -> A4988 DIR              [ĐỔI 8->10 theo schematic]
// MS1/MS2/MS3 (chọn microstep A4988). Cả 3 LUÔN cùng mức nên thứ tự chân vô hại,
//   nhưng đặt đúng theo schematic v2: MS1<-A3, MS2<-A2, MS3<-A1.
//   LOW = full-step (feed/hạ platform); HIGH = 1/16 (chỉ lúc HOME cho ÊM).
#define MS_PIN1      A3   // v2: A3 -> A4988 MS1  [ĐỔI A1->A3]
#define MS_PIN2      A2   // v2: A2 -> A4988 MS2  [không đổi]
#define MS_PIN3      A1   // v2: A1 -> A4988 MS3  [ĐỔI A3->A1]
#define LIMIT_SW     7    // v2: D7  -> CÔNG TẮC HÀNH TRÌNH (J4). Chạm = stepper DỪNG NGAY.  [MỚI]
const uint8_t HOME_USTEP = 16;   // HOME chạy 1/16 microstep cho ÊM (A4988 MS=H/H/H). Feed vẫn full-step (MS=L/L/L).

// >>> CHỐNG RUNG KHI NẠP CODE (PHẦN CỨNG) <<<  10k: STEP(D9)->GND, DIR(D10)->GND  (v2: DIR đổi D8->D10)

// v28.1 (2026-07-13): CHỐT chiều theo NGHIỆM THU THẬT trên máy — từng thử đảo (UP=HIGH) thì
//   các vòng Home đẩy platform VỀ BOTTOM (suýt đâm đáy, phải rút điện motor) => mapping GỐC đúng:
//   DOWN=HIGH là HẠ thật (top->bottom khi ăn bài), UP=LOW là HOME đi LÊN tìm công tắc top.
#define STEPPER_UP     LOW
#define STEPPER_DOWN   HIGH

// ---- CÔNG TẮC HÀNH TRÌNH (endstop D7) ----
// LIMIT_ACTIVE_LOW=1: công tắc nối GND, chạm = mức LOW (dùng INPUT_PULLUP). D7 để HỞ = luôn HIGH = KHÔNG chạm (an toàn).
//   Nếu công tắc của anh chạm = HIGH thì đổi thành 0.
#define LIMIT_ACTIVE_LOW  1
// Hướng bị CHẶN = phía có công tắc. Mặc định STEPPER_UP (công tắc ở ĐỈNH/HOME — stepper lên chạm là dừng).
//   Nếu công tắc gắn ở ĐÁY thì đổi thành STEPPER_DOWN.
#define LIMIT_STOP_DIR    STEPPER_UP

// ======================================================
// STATE
// ======================================================
enum MachineState { IDLE, RUNNING };
MachineState machineState = IDLE;

// --- Trạng thái BÁO CÁO cho Pi (dòng ST / lệnh S) — KHÔNG ảnh hưởng logic cơ khí ---
//   st  = runStatus  : RUN | IDLE | OFF | DONE | ERROR
//   err = lastErr    : NONE | CLUMP | STALL | LIMIT  (CLUMP suy ra real-time từ sClumpLive khi RUN)
enum RunStatus { RS_IDLE, RS_RUN, RS_OFF, RS_DONE, RS_ERROR };
RunStatus runStatus = RS_IDLE;
enum ErrFlag   { EF_NONE, EF_CLUMP, EF_STALL, EF_LIMIT, EF_NOMOTOR, EF_NOHOME, EF_LINK };  // v28.2 NOHOME=B1 chua home | v28.3 LINK=mat lien lac Pi khi dang chay
ErrFlag   lastErr   = EF_NONE;     // lý do dừng gần nhất (latch cho st=ERROR/DONE/OFF)
uint32_t  lastStMs  = 0;           // mốc phát dòng ST định kỳ (~250ms khi RUN)
// v28.3 DEADMAN: mốc NHẬN byte cuối từ Pi. Đang CHẠY mà Pi im lặng quá LINK_DEADMAN_MS
//   (Pi treo / rớt USB / MẤT ĐIỆN Pi trong khi rail 12V vẫn nuôi ATmega) -> DỪNG MOTOR NGAY.
//   Pi gửi heartbeat ~400ms lúc recording -> ngưỡng 1500ms chịu được 3 nhịp trễ, không báo nhầm.
uint32_t  lastRxMs  = 0;
const uint16_t LINK_DEADMAN_MS = 1500;

// ======================================================
// GLOBALS
// ======================================================
volatile long encoderCount = 0;     // CW+ / CCW-

uint32_t cardCount       = 0;
// v7.4 EXT-SPEED (model tren Pi): lenh V<c/s> dat steadySpeed truc tiep.
//   Con hieu luc EXT_SPEED_TTL_MS ke tu lenh cuoi; HET HAN -> tu ve governor
//   noi bo (an toan khi Pi treo/mat serial). Bound [CAD_SPD_LO, 520].
float    extSpeed      = 0;
bool     extSpeedOn    = false;  // lệnh V đang hiệu lực — v28.1: flag + mốc set (an toàn wrap millis)
uint32_t extSpeedSetMs = 0;      // mốc nhận lệnh V (TTL so bằng HIỆU thời gian)
const uint32_t EXT_SPEED_TTL_MS = 10000;
const float    EXT_SPD_LO       = 70.0f;   // [v25] 140->70: user muon CHAM MOT NUA -> model gui V~75-90
                                          //   (RIENG voi CAD_SPD_LO=san governor noi bo). Model
                                          //   v23 dieu toc V140-195 (cham+deu) < CAD_SPD_LO(216)
                                          //   -> phai co san rieng thap hon, ko fw se tu choi/kep.
// v7.3: nhip + tran toc theo pha trong luong chong
static inline uint16_t cadTargetNow() { return cardCount < CAD_PH1_END ? CAD_DT_HEAVY : (cardCount < CAD_PH2_END ? CAD_DT_MID : CAD_DT_TARGET); }
static inline float    cadSpdHiNow()  { return cardCount < CAD_PH1_END ? CAD_HI_HEAVY : (cardCount < CAD_PH2_END ? CAD_HI_MID : CAD_HI_LIGHT); }
// v18: SÀN tốc theo pha — nặng/giữa giữ CAD_SPD_LO(270), NHẸ hạ xuống CAD_LO_LIGHT(175) để governor
//   được phép chậm lại -> lá nhẹ tách sạch (hết double-feed). startSpeed vẫn clamp theo CAD_SPD_LO.
static inline float    cadSpdLoNow()  { return cardCount < CAD_PH2_END ? CAD_SPD_LO : CAD_LO_LIGHT; }
uint16_t cardsSinceLower = 0;      // số lá kể từ lần hạ platform gần nhất (xử lý cụm nhảy nhiều lá)
uint16_t clumpEvents     = 0;      // SỐ LẦN phát hiện cụm (>=2 lá dính) trong mẻ — để ĐO & giảm dần
uint32_t lastStatusPrint = 0;

bool motorRunning    = false;

long stepperCurrentSteps = 0;
bool platformMaxWarned   = false;      // đã cảnh báo chạm trần hành trình chưa (warn 1 lần/mẻ)
uint16_t stepsPerLower   = STEPS_PER_LOWER_DEF;  // bước hạ mỗi lần (full-step) — chỉnh LIVE bằng L<n>

uint8_t  motorPWM = PWM_START;

// --- Cascade control state ---
float    targetSpeed   = 0;            // setpoint tốc (counts/giây) — outerControlUpdate đặt mỗi vòng (= steadySpeed + speedTrim)
float    measuredSpeed = 0;            // tốc độ đo từ encoder (counts/giây) — RAW
float    measFilt      = 0;            // tốc đo đã LỌC EMA (PI dùng cái này -> mượt, bớt giật do nhiễu)
float    dtFilt        = 520;          // nhịp dt THƯỜNG đã LỌC EMA (ms) — tham chiếu "ideal cadence" để phát hiện lá chậm/nhanh
float    speedTrim     = 0;            // v5.50: ĐỘ LỆCH tốc quanh ideal (steadySpeed). Bounded + auto-decay về 0 sau 1-2 lá
float    freeSpinFloor = (float)PWM_FREESPIN_FLOOR;  // v5.51: floor free-spin TỰ HỌC (coast giữa 2 lá ≈ ideal). Giữ qua các mẻ (calib motor/pin)
float    velIntegral   = 0;            // tích phân PI (tầng trong)
uint32_t lastVelMs     = 0;
long     lastEncSnap   = 0;

uint32_t lastCardMs    = 0;            // mốc thời gian lá gần nhất
uint16_t lastDt        = 500;          // dt lá gần nhất (ms) — khởi tạo = chu kỳ mục tiêu
float    filtRate      = 2.0;          // nhịp lá/giây đã LỌC EMA — TELEMETRY (hiển thị "avg" trong log)
// v5.54: CADENCE GOVERNOR — giữ NHỊP cố định (khoảng cách 2 lá ĐỀU cả mẻ), bám NHỊP không bám tốc.
//   Lý do (đo thực v5.53): điều khiển theo TỐC không cho spacing đều — cuối mẻ con lăn bám tốt hơn
//   -> feed nhanh hơn dù tốc thấp -> lá sát nhau. Governor đo dt mỗi lá, lệch DT_TARGET thì chỉnh
//   steadySpeed (CÓ slew-limit + chặn [MIN,MAX]) kéo dt về target. Tự bù độ bám: đầu mẻ trượt -> tăng;
//   cuối mẻ bám tốt -> giảm -> spacing ĐỀU full quá trình. Chặn cứng -> KHÔNG tăng quá mạnh/giảm quá yếu.
const uint16_t DT_TARGET_MS    = 640;   // v5.55: 700->640 (~9% nhanh hơn theo yêu cầu); slew/bounds giữ nguyên
const float    CADENCE_GAIN    = 0.10f; // c/s chỉnh trên mỗi 1ms lệch dt (nhẹ -> mượt, không giật)
const float    CADENCE_STEP_MAX= 12.0f; // SLEW LIMIT: chỉnh tối đa mỗi lá (chống nhảy tốc mạnh/nhiễu)
const float    CAD_SPD_MIN     = 440.0; // SÀN tốc cadence: dưới nữa con lăn TRƯỢT (đo: 400 slip). KHÔNG yếu hơn.
const float    CAD_SPD_MAX     = 560.0; // TRẦN tốc cadence: trên nữa dễ văng/blur. KHÔNG mạnh hơn.
float    steadySpeed   = 500.0;         // ★ tốc HIỆN TẠI — cadence governor tự chỉnh trong [SPEED_MIN, SPEED_MAX]. targetSpeed = steadySpeed + speedTrim.
// --- Sensor state (1 nguồn sự thật, có debounce) ---
bool     sPresent      = false;        // CÓ lá che (đã debounce)
uint32_t sPresentSince = 0;            // mốc bắt đầu trạng thái sensor hiện tại
uint8_t  jamRecoverCount = 0;          // v5.53: số lần đã thử gỡ lá kẹt-che-sensor (reset khi đếm được lá)
bool     nomoveChecked   = false;      // v6.4: đã kiểm tra "motor có quay" cho mẻ này chưa (one-shot ~1.5s sau ON)
bool     sRawPrev      = false;        // mức thô lần trước (phục vụ debounce)
uint32_t sRawSince     = 0;            // mốc mức thô vừa đổi
bool     sLowActive    = false;        // đang đo 1 xung LOW (lá đang che)
long     lowStartEnc   = 0;            // encoder lúc bắt đầu che (đo độ dài lá)
float    normLen       = LEN_INIT;     // độ dài lá ĐƠN đã hiệu chuẩn (count)
bool     lenCalibrated = false;        // đã seed normLen từ lá đầu chưa
uint16_t lastLen       = 0;            // len lá/cụm vừa hoàn tất (để log)
bool     escapeWarned  = false;        // chống spam log [SLOW] (1 lần / lần kẹt tại sensor)

// --- v5.16 NUDGE state ---
uint8_t  nudgeCount    = 0;      // nudge đã fire cho slot hiện tại
uint32_t nudgeDipStartMs = 0;    // mốc BẮT ĐẦU dip — v28.1: so HIỆU thời gian (an toàn wrap millis 49.7 ngày)
bool     nudgeActive   = false;  // đang trong dip phase
bool     nudgeHold     = false;  // đang nghỉ giữa 2 nudge — v28.1: thay sentinel nudgeNextMs (hết so tuyệt đối)
uint32_t nudgeHoldMs   = 0;      // mốc BẮT ĐẦU khoảng nghỉ
uint16_t nudgeTotal    = 0;      // tổng nudge toàn mẻ (DIAG)
uint8_t  lastNudgeCount = 0;     // nudge đã dùng cho lá vừa bắt được (log CARD line)
long     encAtRegrip   = 0;      // encoder lúc bắt đầu quay ngược (để verify motor lùi THẬT)

// --- v5.18 PWM FLOOR state ---
float    pickupPWM   = 105.0f;   // EMA PWM lúc lá vào sensor (proxy torque bắt lá) — init NẶNG
float    pwmFloor    = 80.0f;    // dynamic floor hiện tại (cập nhật mỗi velocity tick)
float    lastFloor   = 80.0f;    // floor lúc lá BẮT ĐẦU VÀO sensor (để log đúng, tránh reset sớm)
float    lastPickupPWM = 105.0f; // pickupPWM truoc khi cap nhat EMA (log CARD line + DIAG)

// --- v5.19 chi tiet log: gap state ---
uint32_t lastSlotMs      = 0;    // moc in [SLOT] gan nhat trong gap hien tai
uint8_t  stuckBits       = 0;    // bitmask: stuck threshold nao da in (bit0=500ms 1=1000 2=1500 3=2000 4=3000)
bool     flrRampLogged   = false;// [RAMP] da in cho gap nay chua
uint8_t  flrMilestoneMask = 0;   // bitmask: floor milestone da in (bit0=@90 1=@95 2=@100 3=@PUP)

// --- v5.20 BO TIN HIEU TOAN DIEN (research: observability + slip + loop-health) ---
//   Trong "doi bong" tin hieu: log KHONG duoc lam cham control loop. Serial.print BLOCK >10ms
//   -> [HEALTH] do chinh tac dong cua log len loop. logLevel cho phep ha log de bao ve control.
uint8_t  logLevel        = 2;    // 0=QUIET(CARD+su kien) 1=+HEALTH/STAT 2=+SLOT/STUCK/FLOOR/RAMP. Lenh Q<n>.

// Loop-health / observer-effect (do chinh log co pha control loop khong)
uint32_t loopIters       = 0;    // dem vong loop trong 1s -> loopHz
uint32_t loopHzMark      = 0;
uint16_t loopHz          = 0;    // tan so loop thuc (Hz) — thap = dang bi block
uint32_t lastLoopUs      = 0;
uint16_t loopMaxUs       = 0;    // vong loop LAU nhat (us) — bat spike do Serial block
uint16_t velTickMiss     = 0;    // so lan tick toc do (50ms) bi TRE >75ms = control deadline miss
int      minFreeRAM      = 9999; // RAM trong THAP nhat tung thay (an toan stack/heap)
uint32_t runStartMs      = 0;    // moc bat dau me -> tong thoi gian chay
uint32_t lastHealthMs    = 0;    // moc in [HEALTH] gan nhat

// PID saturation / windup (research: theo doi CO khi actuator bao hoa)
uint32_t satLowMs        = 0;    // tong ms PWM ghim o FLOOR (controller muon cham hon nhung san giu)
uint32_t satHighMs       = 0;    // tong ms PWM ghim o MAX (torque-limited, muon manh hon nhung het)
uint16_t windupEvents    = 0;    // so lan velIntegral cham tran clamp (windup risk)

// Slip / grip quantification (TIN HIEU FIX CUOI: con lan quay "khong" bao xa truoc khi tom duoc la)
long     lastClearEnc    = 0;    // encoder luc la VUA ROI (rising) -> do gapDist toi la ke
uint16_t gapDist         = 0;    // counts con lan da quay TRONG gap (truoc khi tom la) = quang duong "truot/mo"
float    slipRatio       = 0;    // gapDist / normLen = so DO DAI LA con lan quay khong truoc khi tom (>1 = truot)
uint8_t  lastCatchPWM    = 0;    // motorPWM NGAY luc la vao sensor (raw, khac pickupPWM la EMA)

// Velocity quality (research: vi phan encoder khuech dai nhieu luong tu o toc THAP)
long     lastDCnt        = 0;    // counts/tick gan nhat (raw) — thay nhieu luong tu khi toc thap
float    prevMeasured    = 0;    // meas tick truoc -> tinh gia toc
int      lastAccel       = 0;    // gia toc bang tai (counts/s^2) — thay xung nudge co THUC su tang toc khong
//   (per-zone arrays gapMaxBucket/slipSumBucket/nudgedBucket khai bao trong khoi DIAG, sau #define NBUCKETS)

// --- DIAG / instrumentation: ĐO để CHỐT nguyên nhân mất ổn định cuối mẻ (chia vùng 50 lá) ---
uint32_t lastClearMs   = 0;            // mốc sensor vừa HẾT che (rising) — đo gap tới lá kế
uint16_t pickupGap     = 0;            // ms gap TRƯỚC khi lá này che (gap dài/biến động = khó grab)
uint32_t lowStartMs    = 0;            // mốc bắt đầu che (ms)
uint16_t lowDurMs      = 0;            // ms lá che sensor (falling->rising)
#define  NBUCKETS 10                   // 10 vùng x 50 lá = 0..499
uint16_t cardBucket[NBUCKETS];         // số lá theo vùng
uint16_t clumpBucket[NBUCKETS];        // số LẦN cụm theo vùng
uint16_t evtBucket[NBUCKETS];          // số sự kiện đếm theo vùng (mẫu số avg)
uint32_t gapSumBucket[NBUCKETS];       // tổng pickupGap theo vùng (-> avg)
uint32_t dtSumBucket[NBUCKETS];        // tổng dt theo vùng (-> avg, đo mượt)
uint32_t sLenSumBucket[NBUCKETS];      // tổng len LÁ ĐƠN theo vùng (-> avg)
uint16_t sLenCntBucket[NBUCKETS];      // số lá ĐƠN theo vùng
uint16_t sLenMaxBucket[NBUCKETS];      // len LÁ ĐƠN LỚN NHẤT theo vùng (đo "đuôi" phân bố)
uint16_t nudgeBucket[NBUCKETS];        // tổng nudge theo vùng (DIAG: thấy vùng nào cần nudge nhiều)
uint32_t pupSumBucket[NBUCKETS];       // tổng pickupPWM (trước update) theo vùng (DIAG: sức bám thực tế mỗi vùng)
uint16_t gapMaxBucket[NBUCKETS];       // gap LON nhat moi vung (duoi phan bo, khac avg) — v5.20
uint32_t slipSumBucket[NBUCKETS];      // tong gapDist -> avg slip distance moi vung — v5.20
uint16_t nudgedBucket[NBUCKETS];       // so LA can >=1 nudge moi vung (ti le grip kho) — v5.20
// --- v6.0: FEED-FORWARD tự học (PWM cruise theo vùng 50 lá) — lưu EEPROM, dùng làm base PWM ---
float    ffTable[NBUCKETS];             // PWM feed-forward theo vùng chồng (học dần + lưu EEPROM)
bool     ffLoaded      = false;        // đã nạp ffTable từ EEPROM chưa (false = đang dùng default)
float    startSpeed    = 200.0f;       // v6.8: tốc KHỞI ĐỘNG tự học (heavy-start) — default 480 (v6.7 chạy tốt), lưu EEPROM, init steadySpeed mỗi mẻ
bool     sClumpLive    = false;        // 2 lá đang TRÙNG QUA sensor ngay lúc này (real-time, chưa thoát)
// (v28.2 review: finalDrain/finalDrainUntil/FINAL_DRAIN_MS đã XÓA — drain bỏ từ v5.52, code chết)

// --- TELEMETRY tải + chế độ điều khiển ---
float    loadEMA       = LOAD_PWM_HEAVY;     // ước lượng TẢI = PWM cruise (EMA, telemetry) — proxy trọng lượng chồng
uint8_t  ctrlMode      = 0;                  // 0=CRZ(ideal) 1=CLMP(chậm tách) 2=ESC(tăng kéo) 4=REV(đề-ba)
uint8_t  clumpCaution  = 0;                  // v6.9: số lá còn phải chạy CHẬM sau 1 cụm (bò xuyên mảng bài dính)

// ======================================================
// HELPER
// ======================================================
// "Máy có ĐANG được phép chạy không" — NGUỒN SỰ THẬT = machineState
//   (đặt bởi CÔNG TẮC A0 [theo sườn] HOẶC lệnh serial B1/B0). Mọi abort-check chạy dài
//   (softStart / lowerPlatform / home) dùng hàm này -> serial B1 bật được máy DÙ công tắc vật lý đang HỞ.
inline bool machineStillOn()
{
    return (machineState == RUNNING);
}

// RAM trong con lai (AVR): khoang cach giua dinh heap va dinh stack. Thap dan = nguy co tran stack.
extern int __heap_start, *__brkval;
int freeRAM()
{
    int v;
    return (int)&v - (__brkval == 0 ? (int)&__heap_start : (int)__brkval);
}

long readEncoderAtomic()
{
    long c;
    noInterrupts();
    c = encoderCount;
    interrupts();
    return c;
}

// ENCODER ISR — quadrature direction
void encoderISR()
{
    bool cw = (digitalRead(ENC_B) == LOW);
    if (ENC_REVERSED) cw = !cw;                       // đảo chiều bằng cờ (khỏi rút dây A↔B)
    if (cw) encoderCount++;                            // CW
    else    encoderCount--;                            // CCW
}

// ======================================================
// MOTOR LOW-LEVEL
// ======================================================
void applyMotorPWM()                    // quay XUOI (keo la ra)
{
    analogWrite(MOTOR_IN1, motorPWM);
    digitalWrite(MOTOR_IN2, LOW);
}

void motorReverse(uint8_t pwm)          // quay NGUOC nhe ("de ba" re-grip) — dao chieu H-bridge
{
    digitalWrite(MOTOR_IN1, LOW);
    analogWrite (MOTOR_IN2, pwm);
}

void motorStop()
{
    analogWrite(MOTOR_IN1, 0);
    digitalWrite(MOTOR_IN2, LOW);
    motorRunning = false;
    motorPWM = 0; measuredSpeed = 0; measFilt = 0; targetSpeed = 0;   // v5.21: telemetry khong bao stale (truoc day STAT in meas=700/PWM=121 luc da dung)
}

// v5.53 JAM-RECOVERY: lá kẹt che sensor + con lăn TRƯỢT (free-spin, mất grip) -> lùi nhẹ "đề ba"
//   để con lăn bám lại mặt lá, rồi đẩy XUÔI dứt khoát (lực firm) để TỐNG lá ra. Blocking ngắn (~700ms)
//   — chỉ chạy khi ĐÃ kẹt (hiếm), không ảnh hưởng vòng điều khiển lúc chạy bình thường.
void jamClearAttempt()
{
    // [review v28.3] ~700ms blocking này KHÔNG về loop nên deadman không bắt được -> chia
    //   khúc 50ms + kiểm deadman: mất liên lạc Pi giữa chừng -> DỪNG NGAY (giữ đảm bảo <1.5s).
    motorReverse(REGRIP_PWM);
    for (uint8_t i = 0; i < 6; i++) {                      // lùi ~300ms: bám lại mặt lá
        delay(50); wdt_reset();
        if ((uint32_t)(millis() - lastRxMs) > LINK_DEADMAN_MS) { motorStop(); return; }
    }
    motorPWM = REGRIP_FWD_MAX; applyMotorPWM();
    for (uint8_t i = 0; i < 8; i++) {                      // đẩy xuôi FIRM ~400ms: tống lá ra
        delay(50); wdt_reset();
        if ((uint32_t)(millis() - lastRxMs) > LINK_DEADMAN_MS) { motorStop(); return; }
    }
    // trả về điều khiển PI bình thường ở vòng kế (velocityLoopUpdate sẽ tiếp quản)
}

void softStartMotor(uint8_t targetPWM)
{
    DBG("SoftStart begin");
    motorRunning = true;
    for (uint8_t p = 80; p <= targetPWM; p += 4)
    {
        if (!machineStillOn()) { motorStop(); return; }
        motorPWM = p;
        applyMotorPWM();
        delay(15);
        wdt_reset();
    }
    motorPWM = targetPWM;
    applyMotorPWM();
    DBG_VAL("SoftStart done, PWM=", motorPWM);
}

// ======================================================
// v6.0 FEED-FORWARD — base PWM theo độ vơi của chồng (số lá đã kéo)
//   Tải = trọng lượng chồng đè con lăn -> giảm ĐỀU theo cardCount. ff(cardCount) nội suy tuyến tính
//   giữa các vùng đã học -> base PWM mượt, khử THẲNG nhiễu BIẾT TRƯỚC; PI chỉ còn vi chỉnh phần dư.
// ======================================================
float feedForwardPWM()
{
    float span = (totalCards > 1) ? (float)(totalCards - 1) : 1.0f;
    float pos  = (float)cardCount / span * (float)(NBUCKETS - 1);   // vị trí trên thang vùng [0..NBUCKETS-1]
    if (pos < 0) pos = 0;
    int z = (int)pos;
    if (z >= NBUCKETS - 1) return ffTable[NBUCKETS - 1];
    float frac = pos - (float)z;
    return ffTable[z] + (ffTable[z + 1] - ffTable[z]) * frac;       // nội suy trong vùng -> không nhảy bậc
}

void ffInitDefault()    // đường cong MẶC ĐỊNH (affine HEAVY->LIGHT) khi chưa có dữ liệu học
{
    for (uint8_t z = 0; z < NBUCKETS; z++) {
        float f = (NBUCKETS > 1) ? (float)z / (float)(NBUCKETS - 1) : 0.0f;
        ffTable[z] = PWM_FF_HEAVY + (PWM_FF_LIGHT - PWM_FF_HEAVY) * f;
    }
}

void ffLoadEEPROM()     // nạp ffTable đã học từ EEPROM; magic/checksum hỏng -> dùng default
{
    if (EEPROM.read(FF_EE_ADDR) != FF_EE_MAGIC) { ffInitDefault(); startSpeed = 200.0f; ffLoaded = false; return; }
    uint8_t sum = 0;
    float tmp[NBUCKETS];
    for (uint8_t z = 0; z < NBUCKETS; z++) {
        uint8_t v = EEPROM.read(FF_EE_ADDR + 1 + z);
        tmp[z] = (float)v; sum += v;
    }
    uint8_t ssB = EEPROM.read(FF_EE_ADDR + 1 + NBUCKETS); sum += ssB;          // v6.8: startSpeed byte (×4)
    if (EEPROM.read(FF_EE_ADDR + 2 + NBUCKETS) != sum) { ffInitDefault(); startSpeed = 200.0f; ffLoaded = false; return; }
    for (uint8_t z = 0; z < NBUCKETS; z++)
        ffTable[z] = constrain(tmp[z], (float)PWM_HARD_MIN, (float)PWM_HARD_MAX);
    startSpeed = constrain((float)ssB * 4.0f, CAD_SPD_LO, CAD_SPD_HI);
    ffLoaded = true;
}

void ffSaveEEPROM()     // lưu ffTable hiện tại (đã học trong mẻ) vào EEPROM — EEPROM.update chỉ ghi byte đổi
{
    uint8_t sum = 0;
    EEPROM.update(FF_EE_ADDR, FF_EE_MAGIC);
    for (uint8_t z = 0; z < NBUCKETS; z++) {
        uint8_t v = (uint8_t)constrain(ffTable[z] + 0.5f, (float)PWM_HARD_MIN, (float)PWM_HARD_MAX);
        EEPROM.update(FF_EE_ADDR + 1 + z, v); sum += v;
    }
    uint8_t ssB = (uint8_t)constrain(startSpeed / 4.0f + 0.5f, CAD_SPD_LO / 4.0f, CAD_SPD_HI / 4.0f);  // v6.8
    EEPROM.update(FF_EE_ADDR + 1 + NBUCKETS, ssB); sum += ssB;
    EEPROM.update(FF_EE_ADDR + 2 + NBUCKETS, sum);
}

// ======================================================
// TẦNG TRONG — VÒNG TỐC ĐỘ PI (encoder) + FEED-FORWARD (v6.0)
// ======================================================
void velocityLoopUpdate()
{
    uint32_t now = millis();
    uint32_t elapsedMs = now - lastVelMs;
    if (elapsedMs < VELOCITY_SAMPLE_MS) return;

    // RE-GRIP LÙI: trong pha "dip" của nudge -> QUAY NGƯỢC nhẹ ("đề ba") cho con lăn bám lại mặt bài.
    //   Bỏ qua PI xuôi; reset tích phân để khi quay xuôi lại PI ramp sạch (không spike).
    if (nudgeActive && motorRunning) {
        motorReverse(REGRIP_PWM);
        motorPWM    = REGRIP_PWM;          // telemetry phản ánh
        velIntegral = 0;
        lastEncSnap = readEncoderAtomic(); // tránh spike measuredSpeed khi quay xuôi lại
        lastVelMs   = now;
        return;
    }

    // Control deadline miss: tick le ra >75ms (dang ky 50ms) = Serial block / loop ket -> control TRE
    if (elapsedMs > (uint32_t)VELOCITY_SAMPLE_MS + 25 && motorRunning) velTickMiss++;

    long c = readEncoderAtomic();
    long d = c - lastEncSnap;
    if (d < 0) d = -d;
    float elapsedSec = elapsedMs / 1000.0;
    prevMeasured  = measuredSpeed;
    measuredSpeed = d / elapsedSec;               // counts / giây (RAW)
    measFilt     += MEAS_ALPHA * (measuredSpeed - measFilt);   // v5.44: LỌC EMA -> PI mượt, bớt giật PWM do nhiễu
    lastDCnt      = d;                             // raw counts/tick (thay nhieu luong tu o toc thap)
    float acc     = (measuredSpeed - prevMeasured) / elapsedSec;          // gia toc counts/s^2
    lastAccel     = (int)constrain(acc, -30000.0f, 30000.0f);             // clamp: AVR int 16-bit (tranh tran)
    lastEncSnap = c;
    lastVelMs   = now;

    // ── v6.0 FEED-FORWARD: base PWM theo tải BIẾT TRƯỚC (trọng lượng chồng giảm đều theo số lá đã kéo) ──
    float ff = feedForwardPWM();               // PWM nền cho mức chồng hiện tại (đã học, theo cardCount)

    // v19 FLAT-PACE: sàn grip = gripServo (dt-servo tự giữ nhịp ĐỀU cả mẻ; cập nhật mỗi lá đơn ở
    //   khối đếm lá). Tự tăng khi lá chậm/kẹt (chống giật), tự giảm khi nhanh (chống trùng 2 lá).
    float minFloor = (float)PWM_HARD_MIN;
    if (motorRunning && gripServo > minFloor) minFloor = gripServo;
    // RIÊNG lúc ĐỀ-BA đang kéo lá kẹt (chưa tới sensor) -> kéo firm hơn nữa, tăng dần mỗi retry.
    if (nudgeCount > 0 && !sPresent && motorRunning) {
        float fwd = (float)REGRIP_FWD_BASE + (float)(nudgeCount - 1) * (float)REGRIP_FWD_STEP;
        fwd = constrain(fwd, (float)PWM_HARD_MIN, (float)REGRIP_FWD_MAX);   // CAP: kéo firm, không yank cả xấp
        if (fwd > minFloor) minFloor = fwd;
    }
    pwmFloor = minFloor;                        // telemetry

    float error = targetSpeed - measFilt;       // dùng measFilt (đã lọc) -> mượt
    float out   = ff + KP * error + KI * velIntegral;   // feed-forward + PI vi chỉnh

    // Anti-windup: ngừng tích phân khi đã bão hòa NGƯỢC hướng error (clamp 2 đầu vì ff đã gánh phần lớn tải).
    bool satHi = (out >= (float)PWM_HARD_MAX) && (error > 0);
    bool satLo = (out <= minFloor)            && (error < 0);
    if (!satHi && !satLo) velIntegral += error * elapsedSec;
    velIntegral = constrain(velIntegral, -INTEG_TERM_MAX / KI, INTEG_TERM_MAX / KI);

    out = ff + KP * error + KI * velIntegral;
    out = constrain(out, minFloor, (float)PWM_HARD_MAX);

    motorPWM = (uint8_t)(out + 0.5f);
    if (motorRunning) applyMotorPWM();

    // --- saturation / windup telemetry ---
    if (motorRunning) {
        if (out <= minFloor + 0.5f)                 satLowMs  += elapsedMs;  // ghim FLOOR
        else if (out >= (float)PWM_HARD_MAX - 0.5f) satHighMs += elapsedMs;  // ghim MAX (torque-limited)
        if (fabs(velIntegral) >= 0.90f * (INTEG_TERM_MAX / KI)) windupEvents++;
    }

    // --- HỌC feed-forward + đo tải: CHỈ lúc CRUISE ổn định (ctrlMode=CRZ, bám tốc) ---
    //   ffTable[vùng] EMA về PWM cruise THỰC -> mẻ sau (và phần còn lại của mẻ này) bù trước ĐÚNG mức tải
    //   -> PI gần như không phải làm gì -> hết vọt/giật. loadEMA giữ nguyên cho telemetry/DIAG.
    if (ctrlMode == 0 && motorRunning && cardCount >= STARTUP_CARDS && targetSpeed > SPEED_MIN
        && measuredSpeed > 0.80f * targetSpeed
        && measuredSpeed < 1.20f * targetSpeed)
    {
        uint8_t z = (uint8_t)(cardCount / 50); if (z >= NBUCKETS) z = NBUCKETS - 1;
        ffTable[z] += FF_LEARN_ALPHA * ((float)motorPWM - ffTable[z]);   // tự học đường cong tải
        ffTable[z]  = constrain(ffTable[z], (float)PWM_HARD_MIN, (float)PWM_HARD_MAX);
        loadEMA    += LOAD_ALPHA * ((float)motorPWM - loadEMA);          // telemetry
    }
}

// ======================================================
// TẦNG NGOÀI — đặt targetSpeed = steadySpeed(ideal) + speedTrim (chạy MỖI vòng loop)
//   . kẹt >2s          -> đề-ba (quay NGƯỢC re-grip) rồi xuôi lại
//   . cụm/kẹt tại sensor-> trim ÂM (chậm tách)   . lá stuck (gap dài) -> trim DƯƠNG (tăng tốc kéo)
//   . còn lại          -> trim tự decay về 0 (BÁM ideal). Bounded [ideal-130, ideal+150].
//   (Hết lá quá lâu -> DỪNG: xử lý trong loop theo GAP_STALL_MS.)
//   * Dùng sPresent/sPresentSince do pollSensor cập nhật (nguồn sự thật, đã debounce).
// ======================================================
void outerControlUpdate()
{
    uint32_t now = millis();
    uint32_t dur = now - sPresentSince;

    // --- REAL-TIME CLUMP: kiểm tra encoder ngay trong khi lá đang che sensor ---
    //   Nếu encoder đã đi > CLUMP_RATIO * normLen mà lá CHƯA thoát = đang có 2+ lá trùng nhau
    if (sLowActive && lenCalibrated)
    {
        long cLen = readEncoderAtomic() - lowStartEnc;
        if (cLen < 0) cLen = -cLen;
        if ((float)cLen > CLUMP_RATIO * normLen)
            sClumpLive = true;
    }

    // === RE-GRIP LÙI (đề ba) — chạy BẤT KỂ sPresent (fix: trước nằm trong !sPresent -> kẹt reverse khi lá vào sensor) ===
    //   Lùi khi kẹt QUÁ LÂU (>NUDGE_GAP_MS=2s) — kể cả MỒI lá đầu. Lúc chạy đều lá ra <2s nên ko lùi.
    //   Đang lùi -> dừng ngay khi: đủ 1/4 lá | hết giờ an toàn | CÓ LÁ xuất hiện.
    if (nudgeActive)
    {
        long backNow    = encAtRegrip - readEncoderAtomic();
        long backTarget = (long)(normLen * REGRIP_BACK_FRAC);
        if (backNow >= backTarget || (uint32_t)(now - nudgeDipStartMs) >= REGRIP_MAX_MS || sPresent) {
            nudgeActive = false; nudgeHold = true; nudgeHoldMs = now;
            if (motorRunning) applyMotorPWM();          // quay XUÔI lại NGAY (không lùi tiếp khi đã có lá)
            Serial.print(F("[REGRIP] #")); Serial.print(nudgeCount);
            Serial.print(F(" xong: lui ")); Serial.print(backNow); Serial.print(F("/")); Serial.print(backTarget);
            Serial.print(sPresent ? F(" (co la->dung) -> xuoi luc ") : F(" -> xuoi luc "));
            Serial.println(REGRIP_FWD_BASE + (nudgeCount - 1) * REGRIP_FWD_STEP);   // lực xuôi retry này (tăng dần)
        } else {
            ctrlMode = 4; return;                       // đang lùi -> velocityLoop drive reverse
        }
    }
    else if (NUDGE_ENABLED && !sPresent && dur > NUDGE_GAP_MS
             && nudgeCount < NUDGE_MAX
             && (!nudgeHold || (uint32_t)(now - nudgeHoldMs) >= NUDGE_INTERVAL_MS))
             // chỉ cần kẹt >2s là đề-ba (kể cả mồi lá ĐẦU). Lúc chạy đều lá ra <2s nên KHÔNG lùi -> vẫn "ban đầu ko lùi".
    {
        nudgeCount++; nudgeTotal++; nudgeActive = true;
        nudgeDipStartMs = now;
        encAtRegrip = readEncoderAtomic();
        Serial.print(F("[REGRIP] #")); Serial.print(nudgeCount);
        Serial.print(F(" QUAY NGUOC (ket ")); Serial.print(dur / 1000.0, 1);
        Serial.print(F("s) lui ")); Serial.print((int)(normLen * REGRIP_BACK_FRAC)); Serial.println(F(" counts"));
        ctrlMode = 4; return;                           // bắt đầu lùi
    }

    // ===== v5.50: TỐC ĐỘ = ideal (steadySpeed) + speedTrim — BOUNDED + AUTO-DECAY =====
    //   Bình thường trim≈0 -> BÁM ideal (camera nét, ổn định CẢ MẺ — la 1 tới hết, ko hardcode so la).
    //   CHỈ lệch khi VISION (sensor/encoder) thấy lý do, và lệch có GIỚI HẠN + tự về ideal sau 1-2 lá:
    if ((sClumpLive && sPresent) || (sPresent && dur > BLOCK_MAX_MS))  // VISION: 2+ lá trùng / lá kẹt tại sensor
    {
        speedTrim = -TRIM_DOWN_MAX;                    // -> CHẬM lại tách (chống multi-feed). Bounded.
        if (!escapeWarned) {
            escapeWarned = true;
            Serial.print(F("[SLOW] cum/ket tai sensor -> giam toc tach (trim="));
            Serial.print((int)speedTrim); Serial.println(F(")"));
        }
    }
    else if (!sPresent && motorRunning && dur > GAP_SPEEDUP_MS)        // VISION: lá stuck (gap dài) chưa ra
    {
        float add = (float)(dur - GAP_SPEEDUP_MS) * GAP_TRIM_RATE;     // -> ramp trim DƯƠNG real-time kéo lá ra
        if (add > speedTrim) speedTrim = add;                          // chỉ NÂNG ở đây (hạ là việc của decay/cụm)
    }
    speedTrim = constrain(speedTrim, -TRIM_DOWN_MAX, TRIM_UP_MAX);

    // v5.54: steadySpeed do CADENCE GOVERNOR đặt (cập nhật mỗi lá trong pollSensor, giữ nhịp DT_TARGET).
    //   Ở đây chỉ cộng speedTrim (xử lý cụm/đề-ba tức thời, tự về 0 khi nhịp ổn).
    float effSpd = steadySpeed + speedTrim;
    // DAU MAY tu ton (gentle intro STARTUP_CARDS la dau, chong day-nang vo 2-3 la) — GIU vi user thich start cham
    if (cardCount < STARTUP_CARDS) {
        float spStart  = steadySpeed * STARTUP_FRAC;
        float startSpd = spStart + (steadySpeed - spStart) * (float)cardCount / (float)STARTUP_CARDS;
        if (startSpd < effSpd) effSpd = startSpd;
    }
    targetSpeed = constrain(effSpd, steadySpeed - TRIM_DOWN_MAX, steadySpeed + TRIM_UP_MAX);
    // v6.8 TARGET APPROACH: sap du me -> ve dich cham de dung DUNG batchTarget
    if (batchTarget > 0 && motorRunning && cardCount + (uint32_t)APPROACH_CARDS >= (uint32_t)batchTarget
        && targetSpeed > APPROACH_SPEED)
        targetSpeed = APPROACH_SPEED;

    ctrlMode    = (speedTrim > 20.0f) ? 2 : (speedTrim < -20.0f ? 1 : 0);  // 2=ESC(tang) 1=CLMP(cham) 0=CRZ(ideal)
}

// ======================================================
// SENSOR — đo lá bằng ENCODER + đếm cụm (máy trạng thái có debounce)
//   - Debounce sườn bằng SENSOR_CLEAR_MS: mức mới phải ổn định mới chốt.
//   - Sườn XUỐNG (lá che): ghi encoder mốc.
//   - Sườn LÊN (lá rời): len = |Δencoder| = ĐỘ DÀI đã đi qua (bất biến tốc độ).
//       len ~ normLen  -> 1 lá  (cập nhật hiệu chuẩn normLen)
//       len >= 1.5x    -> nhiều lá dính -> đếm bù round(len/normLen)
//   Trả về SỐ lá vừa hoàn tất (0 nếu chưa). Cập nhật sPresent/sPresentSince.
// ======================================================
uint8_t pollSensor()
{
    uint32_t now = millis();
    bool raw = (digitalRead(SENSOR_PIN) == CARD_PRESENT_LEVEL);

    if (raw != sRawPrev) { sRawPrev = raw; sRawSince = now; }   // mức thô vừa đổi -> đếm lại debounce
    if (raw == sPresent) return 0;                              // không có thay đổi so với mức đã chốt
    if (now - sRawSince < SENSOR_CLEAR_MS) return 0;            // chưa ổn định đủ -> chờ (debounce)

    // ---- CHỐT 1 sườn (đã debounce) ----
    sPresent      = raw;
    sPresentSince = now;
    escapeWarned  = false;

    if (sPresent)                       // SƯỜN XUỐNG: lá bắt đầu che -> mốc encoder
    {
        sLowActive  = true;
        lowStartEnc = readEncoderAtomic();
        lowStartMs  = now;
        uint32_t g  = now - lastClearMs; if (g > 60000UL) g = 60000UL;
        pickupGap   = (uint16_t)g;       // gap trước lá này = thời gian con lăn "mò" lá kế
        // SLIP: quang duong con lan quay TRONG gap (khong tom duoc la) = chi so truot/grip kem
        long gd = lowStartEnc - lastClearEnc; if (gd < 0) gd = -gd; if (gd > 60000L) gd = 60000L;
        gapDist     = (uint16_t)gd;
        slipRatio   = (normLen > 1.0f) ? (gd / normLen) : 0.0f;   // so do-dai-la con lan quay khong
        lastCatchPWM = motorPWM;          // PWM NGAY luc tom duoc la (raw, khac pickupPWM EMA)
        // v7.6 GAP-SPRINT: la VUA cham sensor -> cat sprint NGAY (trim duong ve 0)
        //   de la di qua camera o toc cruise chuan (khong blur). Trim am (declump) giu nguyen.
        if (speedTrim > 0) speedTrim = 0;
        lastNudgeCount = nudgeCount;     // lưu số nudge đã dùng cho slot vừa xong
        lastFloor   = pwmFloor;          // lưu floor đang hoạt động lúc lá bắt đầu vào (để log)
        lastPickupPWM = pickupPWM;       // lưu pickupPWM TRƯỚC khi EMA update (log CARD + DIAG)
        nudgeCount  = 0;                 // reset cho slot mới
        nudgeActive = false;
        nudgeHold   = false;             // bắt được lá -> nudge kế được phép ngay (hết khoảng nghỉ)
        // reset v5.19 gap-log state (gap vua ket thuc)
        stuckBits        = 0;
        flrRampLogged    = false;
        flrMilestoneMask = 0;
        if (motorRunning)                // cập nhật pickupPWM: EMA của PWM lúc lá vào = torque bắt lá
            pickupPWM += PICKUP_ALPHA * ((float)motorPWM - pickupPWM);
        // v6.0: KHÔNG cap PWM về PWM_START khi bắt lá nữa (cap đó tạo dip mỗi lá khi ff>PWM_START).
        //   Feed-forward+PI giữ tốc HẰNG SỐ -> không fling; vòng tốc kế (50ms) tự đặt đúng PWM.
        pwmFloor = (float)PWM_HARD_MIN;
        lastStMs = now;                  // v6.2: reset nhịp ST
        emitStatus();                    // v6.2 REAL-TIME: lá vừa TỚI (che sensor) -> đẩy count NGAY (ST hiện cardCount+1)
        return 0;
    }

    // SƯỜN LÊN: lá vừa rời -> đo độ dài xung LOW (bằng encoder)
    if (!sLowActive) return 0;
    sLowActive  = false;
    sClumpLive  = false;    // reset: lá đã thoát, xóa cờ trùng real-time
    lastSlotMs  = 0;        // gap moi bat dau -> [SLOT] in luon tu dau
    lastClearMs = now;
    lastClearEnc = readEncoderAtomic();   // moc encoder luc la roi -> do slip-distance toi la ke
    { uint32_t ld = now - lowStartMs; if (ld > 60000UL) ld = 60000UL; lowDurMs = (uint16_t)ld; }
    long d = readEncoderAtomic() - lowStartEnc;
    if (d < 0) d = -d;
    float len = (float)d;
    lastLen = (uint16_t)len;

    if (len < LEN_ABS_MIN) return 0;                            // nhiễu tuyệt đối -> bỏ
    if (!lenCalibrated) {                                                    // seed từ lá ĐẦU HỢP LỆ
        // v5.52 FIX: CHỈ seed khi lá đủ dài hợp lý. Lá mồi/đầu mẻ hay đo NGẮN bất thường
        //   (đo thực: lá #1 len=27 trong khi lá đơn ~88) -> nếu seed normLen=27 thì lá đơn
        //   sau đó bị ratio>CLUMP_RATIO -> TƯỞNG LÀ CỤM -> đếm phình (412 thật -> 1387 đếm).
        if (len >= LEN_INIT * 0.40f) {                // ngưỡng ~64: "đủ dài để là 1 lá thật"
            normLen = (len < LEN_INIT) ? len : LEN_INIT;  // cap trên: lá đầu dài bất thường không kéo chuẩn lên
            lenCalibrated = true;
        }
        return 1;                                     // lá đầu vẫn TÍNH 1, nhưng CHƯA chốt normLen nếu còn ngắn
    }
    if (len < LEN_MIN_FRAC * normLen) return 0;                 // ngắn bất thường -> bỏ

    float   ratio = len / normLen;
    uint8_t n;
    // v6.2 SLIP GUARD: gap TRƯỚC lá này roller TRƯỢT mạnh (free-spin/grip kém) -> len đo bằng encoder
    //   KHÔNG đáng tin -> đếm 1, KHÔNG tách cụm, KHÔNG học normLen (chống cụm-ảo + trôi chuẩn lúc
    //   đầu mẻ nặng). Cụm THẬT lúc grip tốt (slip thấp) vẫn bắt bình thường.
    if (slipRatio > SLIP_CLUMP_GUARD)
    {
        n = 1;
    }
    else if (ratio < CLUMP_RATIO)
    {
        n = 1;
        // học normLen từ lá đơn (guard 1.50: chuẩn tự hồi phục, tránh partial-overlap kéo lên quá)
        if (len < normLen * 1.50f)
            normLen += LEN_ALPHA * (len - normLen);
        if (normLen < NORMLEN_FLOOR) normLen = NORMLEN_FLOOR;   // chặn sập chuẩn
    }
    else
    {
        // Cụm: mỗi lá thêm cộng ~CLUMP_STEP*normLen (có khe hở)
        n = (uint8_t)((ratio - 1.0f) / CLUMP_STEP + 0.5f) + 1; // = round((ratio-1)/step)+1
        if (n < 2) n = 2;
        if (n > CLUMP_MAX) n = CLUMP_MAX;
    }
    return n;
}

// ======================================================
// STEPPER
// ======================================================
// Công tắc hành trình D7 có ĐANG chạm không? Lọc nhiễu nhẹ: đọc 2 lần cách ~0.3ms, cả 2 phải "chạm".
//   Khi KHÔNG chạm -> chỉ 1 lần digitalRead (nhanh, không delay) -> không ảnh hưởng nhịp bước bình thường.
inline bool limitHit()
{
    bool active = (digitalRead(LIMIT_SW) == (LIMIT_ACTIVE_LOW ? LOW : HIGH));
    if (!active) return false;
    delayMicroseconds(300);
    return (digitalRead(LIMIT_SW) == (LIMIT_ACTIVE_LOW ? LOW : HIGH));
}

void stepperPulseOnce(bool direction)
{
    // ENDSTOP: đang đi VỀ PHÍA công tắc hành trình mà công tắc chạm -> KHÔNG bước nữa (DỪNG NGAY, mọi nơi gọi).
    //   Đi hướng NGƯỢC lại (rời công tắc) thì KHÔNG chặn -> không bị kẹt, vẫn thoát ra được.
    if (direction == LIMIT_STOP_DIR && limitHit()) return;
    digitalWrite(DIR_PIN, direction);
    delayMicroseconds(5);
    digitalWrite(STEP_PIN, HIGH);
    delayMicroseconds(STEP_PULSE_HIGH_US);
    digitalWrite(STEP_PIN, LOW);
}

// Delay CHÍNH XÁC cho bước stepper khi > 16383us (delayMicroseconds chỉ đúng <=16383). Tách thành khúc <=16000.
void stepDelayUs(uint32_t us)
{
    while (us > 16000UL) { delayMicroseconds(16000); us -= 16000UL; }
    if (us) delayMicroseconds((uint16_t)us);
}

// Chọn chế độ bước: true = 1/16 microstep (MS=HIGH), false = full-step (MS=LOW). GỌI KHI STEPPER ĐỨNG YÊN.
void setStepMode(bool microstep)
{
    uint8_t lv = microstep ? HIGH : LOW;
    digitalWrite(MS_PIN1, lv);
    digitalWrite(MS_PIN2, lv);
    digitalWrite(MS_PIN3, lv);
}

uint16_t rampDelay(long i, long n, uint16_t cruiseUs, uint16_t startUs, uint16_t rampSteps)
{
    if (cruiseUs >= startUs) return cruiseUs;
    long fromEnd = n - 1 - i;
    long r = (i < fromEnd) ? i : fromEnd;
    if (r >= rampSteps) return cruiseUs;
    return startUs - (uint16_t)((uint32_t)(startUs - cruiseUs) * r / rampSteps);
}

// Hạ platform — v5.2: chạy 1/16 MICROSTEP cho ÊM (giống home). Đếm stepperCurrentSteps theo FULL-step.
void lowerPlatform(uint16_t fullSteps)
{
    if (!STEPPER_ENABLED) return;          // stepper tạm tắt -> KHÔNG hạ platform
    DBG(">> lower START");
    long     micro   = (long)fullSteps * HOME_USTEP;            // số microstep (16x full-step)
    uint16_t cruise  = STEP_DELAY_RUN_US / HOME_USTEP;          // 900/16 ~56us/ustep (xấp xỉ tốc cũ)
    uint16_t startUs = STEP_START_RUN_US / HOME_USTEP;          // 2500/16 ~156us
    uint16_t rampN   = (uint16_t)STEP_RAMP_STEPS * HOME_USTEP;  // 6*16 = 96 microstep ramp
    for (long i = 0; i < micro; i++)
    {
        if (!machineStillOn()) { DBG("!! lower ABORT"); motorStop(); return; }
        // ENDSTOP: nếu công tắc gắn ở ĐÁY (LIMIT_STOP_DIR=DOWN) thì chạm khi HẠ -> DỪNG NGAY.
        if (LIMIT_STOP_DIR == STEPPER_DOWN && limitHit()) { Serial.println(F("[LOWER] cham cong tac hanh trinh -> dung")); break; }
        stepperPulseOnce(STEPPER_DOWN);
        if ((i % HOME_USTEP) == (HOME_USTEP - 1)) stepperCurrentSteps++;  // mỗi 16 microstep = 1 full-step
        stepDelayUs(rampDelay(i, micro, cruise, startUs, rampN));
    }
    DBG_VAL("<< lower DONE, STEP_POS=", stepperCurrentSteps);
}

// Homing — v28.1: LUÔN đi LÊN tới CHẠM công tắc hành trình top (yêu cầu: home = vừa chạm top limit).
//   Số bước đã hạ chỉ còn là DỰ PHÒNG (+HOME_OVERSHOOT) phòng công tắc hỏng -> không xay vô hạn vào cữ.
//   Fix 2 lỗ hổng cũ: (1) mất bước -> đi hết số đếm mà CHƯA chạm = home hụt; (2) mất điện giữa mẻ
//   -> stepperCurrentSteps=0 -> home KHÔNG đi bước nào dù platform đang giữa vít me.
const uint16_t HOME_OVERSHOOT_FS = 625;   // dự phòng 625 full-step = 25mm quá số đếm rồi mới chịu dừng
void returnStepperHomeBlocking()
{
    if (!STEPPER_ENABLED) return;                          // stepper tạm tắt -> KHÔNG home
    // Số full-step tối đa: đã biết vị trí -> đếm + dự phòng; KHÔNG biết (0, vd mất điện) -> quét cả hành trình.
    long fullSteps = (stepperCurrentSteps > 0 ? (long)stepperCurrentSteps : (long)MAX_LOWER_STEPS)
                     + (long)HOME_OVERSHOOT_FS;
    if (fullSteps > (long)MAX_LOWER_STEPS + (long)HOME_OVERSHOOT_FS)
        fullSteps = (long)MAX_LOWER_STEPS + (long)HOME_OVERSHOOT_FS;
    Serial.print(F("[HOME] Tim cong tac top... 1/16 microstep (")); Serial.print(homeDelayUs / HOME_USTEP);
    Serial.print(F("us/ustep, max ")); Serial.print(fullSteps); Serial.println(F(" full-step)"));
    long     total   = fullSteps * (long)HOME_USTEP;       // tổng MICROSTEP (16x) — MS luôn 1/16 (v5.2)
    uint16_t cruise  = homeDelayUs / HOME_USTEP;           // GIỮ cùng tốc vật lý (12000/16 = 750us/ustep)
    uint16_t startC  = (homeDelayUs < STEP_START_HOME_US) ? STEP_START_HOME_US : homeDelayUs;
    uint16_t startUs = startC / HOME_USTEP;
    uint16_t rampN   = (uint16_t)STEP_RAMP_STEPS_HOME * HOME_USTEP;   // ramp tính theo microstep
    bool     hitStop = false;                              // v28.2: dừng VÌ chạm công tắc (phân biệt với hết bước dự phòng)
    for (long i = 0; i < total; i++)
    {
        // ENDSTOP: chạm công tắc hành trình khi đang LÊN -> DỪNG NGAY (đây chính là HOME thật, chính xác hơn đếm bước).
        if (LIMIT_STOP_DIR == STEPPER_UP && limitHit()) { hitStop = true; Serial.println(F("[HOME] cham cong tac hanh trinh -> dung")); break; }
        stepperPulseOnce(STEPPER_UP);
        stepDelayUs(rampDelay(i, total, cruise, startUs, rampN));
        wdt_reset();
    }
    stepperCurrentSteps = 0;
    // v28.1 KIỂM CHỨNG: home CHUẨN = DỪNG VÌ CHẠM công tắc (hitStop) hoặc đang chạm sau khi
    //   cơ khí lắng 50ms (bước cuối còn rung -> đọc ngay dễ false-negative). Không chạm thật
    //   = hết dự phòng vẫn chưa tới -> công tắc hỏng / dây đứt / kẹt -> vị trí 0 KHÔNG tin được.
    delay(50); wdt_reset();
    if (hitStop || limitHit()) Serial.println(F("[HOME] Done — DANG CHAM cong tac top (home CHUAN)"));
    else                       Serial.println(F("[HOME] !! Done NHUNG KHONG cham cong tac top — KIEM TRA cong tac/day D7 / ket co khi. Vi tri co the SAI."));
}

// ======================================================
// DIAG — nhãn chế độ + TỔNG KẾT MẺ theo vùng 50 lá
//   In sau MỖI mẻ (DONE/STALL/OFF) hoặc lệnh 'G'. Đọc để CHỐT nguyên nhân:
//   - cum% TĂNG VỌT ở vùng cuối  -> mất ổn định cuối mẻ (đúng triệu chứng)
//   - avgSL / maxSL (len lá ĐƠN) TĂNG ở cuối -> lá ĐƠN bị TRƯỢT/kéo dài -> phân bố đơn ĐÈ LÊN phân bố đôi
//   - avgGap TĂNG ở cuối -> con lăn khó "mò" lá (lực đè chồng yếu) -> gốc CƠ KHÍ
// ======================================================
const __FlashStringHelper* modeStr(uint8_t m)
{
    switch (m) { case 0: return F("CRZ"); case 1: return F("CLMP"); case 2: return F("ESC"); case 4: return F("REV"); }
    return F("?");
}

void printRunSummary()
{
    Serial.println(F("[DIAG] ===== TONG KET ME (moi vung = 50 la) ====="));
    Serial.println(F("[DIAG]  vung  |  la | cum | cum%  | avgGap | gapMax | avgDt | avgSL | maxSL | nudge | nudged% | avgPUP | avgSlip"));
    for (uint8_t b = 0; b < NBUCKETS; b++)
    {
        if (evtBucket[b] == 0) continue;
        uint16_t ag  = (uint16_t)(gapSumBucket[b] / evtBucket[b]);
        uint16_t ad  = (uint16_t)(dtSumBucket[b]  / evtBucket[b]);
        uint16_t asl = sLenCntBucket[b] ? (uint16_t)(sLenSumBucket[b] / sLenCntBucket[b]) : 0;
        uint16_t apu = (uint16_t)(pupSumBucket[b] / evtBucket[b]);
        uint16_t asp = (uint16_t)(slipSumBucket[b] / evtBucket[b]);
        float    cp  = 100.0f * clumpBucket[b]  / evtBucket[b];
        float    np  = 100.0f * nudgedBucket[b] / evtBucket[b];   // % la can >=1 nudge
        Serial.print(F("[DIAG] ")); Serial.print(b * 50); Serial.print(F("-")); Serial.print(b * 50 + 49);
        Serial.print(F(" | ")); Serial.print(cardBucket[b]);
        Serial.print(F(" | ")); Serial.print(clumpBucket[b]);
        Serial.print(F(" | ")); Serial.print(cp, 1); Serial.print(F("%"));
        Serial.print(F(" | ")); Serial.print(ag);  Serial.print(F("ms"));
        Serial.print(F(" | ")); Serial.print(gapMaxBucket[b]); Serial.print(F("ms"));
        Serial.print(F(" | ")); Serial.print(ad);  Serial.print(F("ms"));
        Serial.print(F(" | ")); Serial.print(asl);
        Serial.print(F(" | ")); Serial.print(sLenMaxBucket[b]);
        Serial.print(F(" | ")); Serial.print(nudgeBucket[b]);
        Serial.print(F(" | ")); Serial.print(np, 0); Serial.print(F("%"));
        Serial.print(F(" | ")); Serial.print(apu);
        Serial.print(F(" | ")); Serial.println(asp);
    }
    Serial.print(F("[DIAG] normLen cuoi=")); Serial.print((int)normLen);
    Serial.print(F(" loadEMA="));            Serial.print((int)loadEMA);
    Serial.print(F(" pickupPWM="));          Serial.print((int)pickupPWM);
    Serial.print(F(" clumpTong="));          Serial.print(clumpEvents);
    Serial.print(F(" nudgeTong="));          Serial.print(nudgeTotal);
    Serial.print(F(" | nguong cum hien="));  Serial.print((int)(CLUMP_RATIO * normLen));
    Serial.println(F(" cnt (len>=nguong => coi la >=2 la)"));
    Serial.print(F("[DIAG] FF curve PWM/vung(50 la): "));
    for (uint8_t z = 0; z < NBUCKETS; z++) { Serial.print((int)ffTable[z]); if (z < NBUCKETS - 1) Serial.print(','); }
    Serial.println(ffLoaded ? F("  (da hoc/EEPROM)") : F("  (default)"));
    // Health footer: tac dong observer + tham quyen controller toan me
    uint32_t upS = (runStartMs == 0) ? 0 : (millis() - runStartMs) / 1000;
    Serial.print(F("[DIAG] HEALTH: up="));   Serial.print(upS); Serial.print(F("s"));
    Serial.print(F(" loopHz="));             Serial.print(loopHz);
    Serial.print(F(" loopMax="));            Serial.print(loopMaxUs); Serial.print(F("us"));
    Serial.print(F(" tickMiss="));           Serial.print(velTickMiss);
    Serial.print(F(" freeRAM="));            Serial.print(minFreeRAM); Serial.print(F("B"));
    Serial.print(F(" satLo="));              Serial.print(satLowMs); Serial.print(F("ms"));
    Serial.print(F(" satHi="));              Serial.print(satHighMs); Serial.print(F("ms"));
    Serial.print(F(" windup="));             Serial.println(windupEvents);
}

// ======================================================
// STATUS PRINT
// ======================================================
void printStatus()
{
    uint32_t rem = (batchTarget == 0 || cardCount >= batchTarget) ? 0 : (batchTarget - cardCount);
    Serial.print(F("tgt="));      Serial.print((int)targetSpeed);
    Serial.print(F(" meas="));    Serial.print((int)measuredSpeed); Serial.print(F(" c/s"));
    Serial.print(F(" | PWM="));   Serial.print(motorPWM);
    Serial.print(F(" | CARD="));  Serial.print(cardCount);
    Serial.print(F(" REM="));     Serial.print(rem);
    Serial.print(F(" CL="));      Serial.print(clumpEvents);
    Serial.print(F(" | LD="));    Serial.print((int)loadEMA);
    Serial.print(F(" | FLR="));   Serial.print((int)pwmFloor);
    Serial.print(F("/fsf="));     Serial.print((int)freeSpinFloor);
    Serial.print(F("/")); Serial.print((int)pickupPWM);
    Serial.print(F(" | STEP="));  Serial.print(stepperCurrentSteps);
    Serial.print(F(" | ENC="));   Serial.print(readEncoderAtomic());
    Serial.print(F(" | LIM="));   Serial.print(limitHit() ? "HIT" : "-");
    Serial.print(F(" | SEN="));   Serial.println(digitalRead(SENSOR_PIN) ? "HIGH" : "LOW");
}

// ======================================================
// HEALTH — suc khoe he thong + tac dong cua log len control (observer effect)
//   loopHz THAP / loopMax CAO / tickMiss TANG = log dang pha control loop -> ha logLevel (Q1/Q0).
//   slip = quang duong con lan quay "khong" truoc khi tom la (cao = grip kem / chong vo).
// ======================================================
void printHealth()
{
    uint32_t upS = (runStartMs == 0) ? 0 : (millis() - runStartMs) / 1000;
    Serial.print(F("[HEALTH] loopHz=")); Serial.print(loopHz);
    Serial.print(F(" loopMax="));        Serial.print(loopMaxUs); Serial.print(F("us"));
    Serial.print(F(" tickMiss="));       Serial.print(velTickMiss);
    Serial.print(F(" freeRAM="));        Serial.print(minFreeRAM); Serial.print(F("B"));
    Serial.print(F(" | satLo="));        Serial.print(satLowMs);  Serial.print(F("ms"));
    Serial.print(F(" satHi="));          Serial.print(satHighMs); Serial.print(F("ms"));
    Serial.print(F(" windup="));         Serial.print(windupEvents);
    Serial.print(F(" | up="));           Serial.print(upS); Serial.println(F("s"));
    loopMaxUs = 0;   // reset dinh moi cua so -> moi lan in la peak cua 2s vua qua
}

// ======================================================
// ST — dòng trạng thái GỌN cho Pi (giao thức): "ST st=.. n=.. tot=.. err=.. spd=.."
//   st  = RUN|IDLE|OFF|DONE|ERROR   n=số lá đã đếm   tot=mục tiêu(0=không giới hạn)
//   err = NONE|CLUMP|STALL|LIMIT    spd=PWM hiện tại
// ======================================================
void emitStatus()
{
    const __FlashStringHelper* st;
    switch (runStatus) {
        case RS_RUN:   st = F("RUN");   break;
        case RS_OFF:   st = F("OFF");   break;
        case RS_DONE:  st = F("DONE");  break;
        case RS_ERROR: st = F("ERROR"); break;
        default:       st = F("IDLE");  break;
    }
    const __FlashStringHelper* err;
    if (runStatus == RS_RUN) {
        err = sClumpLive ? F("CLUMP") : F("NONE");   // CLUMP = cảnh báo real-time (2+ lá đang trùng tại sensor)
    } else {
        switch (lastErr) {
            case EF_CLUMP: err = F("CLUMP"); break;
            case EF_STALL: err = F("STALL"); break;
            case EF_NOMOTOR: err = F("NOMOVE"); break;
            case EF_LIMIT: err = F("LIMIT"); break;
            case EF_NOHOME: err = F("NOHOME"); break;   // v28.2: tu choi start vi chua home
            case EF_LINK:   err = F("LINK");   break;   // v28.3: mat lien lac Pi khi dang chay
            default:       err = F("NONE");  break;
        }
    }
    // [v28 COUNT-RT] REAL-TIME: hien cardCount+1 NGAY khi la che sensor (suon XUONG) ->
    //   count nhay tuc thi luc la toi (khong doi ~150ms toi suon LEN). Pi lo phan chong LUI
    //   (display max-guard) + cong gui 412 dung COMMITTED (tu [CARD]) nen +1 lac quan cua
    //   false-trigger KHONG lam sai ket qua. Real-time toi da, UI khong lui, gui van chinh xac.
    uint32_t shownN = cardCount + ((sLowActive && machineState == RUNNING) ? 1 : 0);
    Serial.print(F("ST st=")); Serial.print(st);
    Serial.print(F(" n="));    Serial.print(shownN);
    Serial.print(F(" tot="));  Serial.print(batchTarget);
    Serial.print(F(" err="));  Serial.print(err);
    Serial.print(F(" spd="));  Serial.print(motorPWM);
    // v28.2 HOME-GATING: Pi doc lim de MO/KHOA nut START (1 = dang cham cong tac top = home chuan).
    Serial.print(F(" lim="));  Serial.println(limitHit() ? 1 : 0);
}

// ======================================================
// BẬT / TẮT MÁY — hàm DÙNG CHUNG cho CÔNG TẮC vật lý A0 (sườn) VÀ lệnh serial B1/B0.
//   GIỮ NGUYÊN 100% logic cơ khí (chỉ gói lại để 2 nguồn điều khiển gọi chung, không nhân đôi code).
// ======================================================
void doMachineOn()
{
    // v28.2 HOME-GATING: chi cho START khi platform DANG CHAM cong tac top (home chuan).
    //   Mat dien giua me / loi giua chung -> vi tri khong tin duoc -> Pi phai gui B0 (home) truoc.
    //   Backstop lop firmware — lop UI (nut START mo) da chan tu truoc theo co lim= trong ST.
    if (STEPPER_ENABLED && !limitHit()) {
        runStatus = RS_ERROR; lastErr = EF_NOHOME;
        Serial.println(F("\n[NOHOME] B1 TU CHOI: platform chua cham cong tac top. Gui B0 de home roi start lai."));
        emitStatus();
        return;
    }
    // ===== MACHINE ON =====
    machineState        = RUNNING;
    cardCount           = 0;
    cardsSinceLower     = 0;
    clumpEvents         = 0;
    batchDone           = false;
    stepperCurrentSteps = 0;
    platformMaxWarned   = false;
    noInterrupts(); encoderCount = 0; interrupts();   // v28.2 (review): long 4-byte + ISR encoder đang sống -> xóa phải atomic

    runStatus = RS_RUN; lastErr = EF_NONE;       // báo Pi: đang chạy, chưa lỗi
    lastRxMs  = millis();                          // v28.3: mở cửa sổ deadman sạch từ lúc bắt đầu chạy

    Serial.println(F("\n[MACHINE] ON"));
    softStartMotor(PWM_START);

    // v6.0: bàn giao sang vòng tốc — setpoint CỐ ĐỊNH cả mẻ; feed-forward gánh tải, PI bắt đầu ở 0.
    steadySpeed   = startSpeed;   // v6.8: khoi dong o toc TU HOC (heavy-start) -> dau me deu ngay; governor tu HA dan khi chong voi
    targetSpeed   = steadySpeed;
    velIntegral   = 0.0f;           // ff() cấp PWM nền -> PI khởi từ 0, không cần seed PWM_START/KI
    gripServo     = 145.0f;         // [v21] 100->145: chong NANG nguoi can grip >=~140 moi CAN la; 100 -> roller TRUOT (meas 1200c/s, slip 16x, la#1 ket 7s). Seed manh -> bat la ngay
    measuredSpeed = 0; measFilt = 0; dtFilt = (float)IDEAL_DT_MS; speedTrim = 0;  // v5.50: me moi bat dau o ideal, ko mang trim cu
    lastEncSnap   = readEncoderAtomic();
    lastVelMs     = millis();
    lastCardMs    = millis();
    lastDt        = (uint16_t)(1000.0 / TARGET_RATE_CPS);  // ~500ms: bắt đầu trung tính
    filtRate      = TARGET_RATE_CPS;                        // nhịp lọc bắt đầu = mục tiêu
    sPresent      = (digitalRead(SENSOR_PIN) == CARD_PRESENT_LEVEL);
    sPresentSince = millis();
    sRawPrev      = sPresent;
    sRawSince     = millis();
    sLowActive    = false;
    sClumpLive    = false;
    lenCalibrated = false;                                  // hiệu chuẩn lại độ dài lá mẻ mới
    normLen       = LEN_INIT;
    escapeWarned  = false;
    loadEMA       = LOAD_PWM_HEAVY;    // reset TẢI = NẶNG (telemetry) cho chồng ĐẦY đầu mẻ
    ctrlMode      = 0;
    // nudge + DIAG reset cho mẻ mới
    nudgeCount = 0; nudgeActive = false; nudgeHold = false; nudgeTotal = 0; lastNudgeCount = 0;
    pickupPWM = 105.0f; pwmFloor = (float)PWM_HARD_MIN; lastFloor = (float)PWM_HARD_MIN;
    lastClearMs    = millis(); lowStartMs = millis(); pickupGap = 0; lowDurMs = 0;
    stuckBits=0; flrRampLogged=false; flrMilestoneMask=0; lastSlotMs=0;
    lastPickupPWM=105.0f;
    // v5.20 reset: loop-health + saturation + slip + velocity-quality
    loopIters=0; loopHzMark=millis(); loopHz=0; lastLoopUs=micros(); loopMaxUs=0;
    velTickMiss=0; minFreeRAM=9999; runStartMs=millis(); lastHealthMs=millis();
    nomoveChecked=false;   // v6.4: mẻ mới -> kiểm tra lại motor có quay không
    clumpCaution=0;        // v6.9
    satLowMs=0; satHighMs=0; windupEvents=0;
    lastClearEnc=0; gapDist=0; slipRatio=0; lastCatchPWM=0;
    lastDCnt=0; prevMeasured=0; lastAccel=0;
    for (uint8_t b = 0; b < NBUCKETS; b++)
    { cardBucket[b]=0; clumpBucket[b]=0; evtBucket[b]=0; gapSumBucket[b]=0;
      dtSumBucket[b]=0; sLenSumBucket[b]=0; sLenCntBucket[b]=0; sLenMaxBucket[b]=0;
      nudgeBucket[b]=0; pupSumBucket[b]=0;
      gapMaxBucket[b]=0; slipSumBucket[b]=0; nudgedBucket[b]=0; }

    lastStMs = millis();   // bắt đầu nhịp phát ST
    emitStatus();          // báo Pi NGAY: ST st=RUN
}

void doMachineOff()
{
    bool wasRunning = (machineState == RUNNING);
    // ===== MACHINE OFF =====
    motorStop();
    machineState = IDLE;
    runStatus    = RS_OFF;
    lastErr      = EF_NONE;
    Serial.println(F("\n[MACHINE] OFF"));
    if (wasRunning && cardCount > 0) printRunSummary();    // tổng kết mẻ vừa chạy (kể cả khi TAT giữa chừng)
    if (wasRunning) { ffSaveEEPROM(); Serial.println(F("[FF] da luu duong cong feed-forward vao EEPROM")); }  // v6.0: giữ học qua các mẻ
    // v28.3: KHÔNG auto-home nữa. Platform giữ nguyên vị trí (thường ở dưới sau khi hạ theo mẻ).
    //   User BẮT BUỘC bấm nút HOME (lệnh 'H') -> stepper leo về chạm công tắc top -> lim=1 -> START mới mở.
    //   (Yêu cầu anh: không được tự động home; để user BIẾT + phát hiện stepper lỗi thay vì âm thầm.)
    cardCount = 0;         // v28.3 FIX: xoá đếm NGAY khi dừng -> idle/HOME/handshake luôn báo n=0,
                           //   không để Pi đọc số mẻ cũ (vd 100) làm UI nháy khi START mẻ mới.
    Serial.println(F("[MACHINE] READY (chua home — bam HOME de dua ban ve dinh truoc khi START)"));
    emitStatus();          // báo Pi: ST st=OFF n=0 lim=0 -> UI hiện nút HOME
}

// ======================================================
// SERIAL COMMANDS
// ======================================================
// v28.1 REAL-TIME + BỀN VĨNH VIỄN: parser buffer TĨNH, non-blocking.
//   Bỏ String (cấp phát heap mỗi lệnh -> phân mảnh khi chạy 24/7) + bỏ readStringUntil
//   (BLOCK tới 1s khi lệnh thiếu '\n' -> đơ control loop motor). Gom ký tự không chặn,
//   đủ dòng mới xử lý; dòng dài bất thường = rác nhiễu -> bỏ nguyên dòng.
void handleSerialCommand()
{
    static char    cbuf[24];
    static uint8_t clen  = 0;
    static bool    cjunk = false;                 // đang trong dòng quá dài (rác) -> bỏ tới '\n'
    while (Serial.available()) {
        char ch = (char)Serial.read();
        lastRxMs = millis();                       // v28.3 DEADMAN: có byte từ Pi = còn liên lạc
        if (ch == '\n' || ch == '\r') {
            if (!cjunk && clen > 0) { cbuf[clen] = '\0'; processCommand(cbuf); }
            clen = 0; cjunk = false;
        }
        else if (clen < sizeof(cbuf) - 1) cbuf[clen++] = ch;
        else cjunk = true;
    }
}

void processCommand(const char* cmd)
{
    while (*cmd == ' ') cmd++;                    // trim đầu (thay cmd.trim() cũ)
    char c0 = cmd[0];
    if (c0 == '\0') return;

    if (c0 == 'S' || c0 == 's')             // STATUS: dong ST (Pi doc) + dong [STATUS] (nguoi doc)
    {
        emitStatus();                       // <-- Pi parse dong nay
        Serial.print(F("[STATUS] ")); printStatus();
    }
    else if (c0 == 'B' || c0 == 'b')        // B1 = CHAY (ON) | B0 = DUNG + home (OFF) — dieu khien tu Pi
    {
        long v = atol(cmd + 1);
        if (v == 1) {                       // B1: bat may neu CHUA chay
            if (machineState != RUNNING) doMachineOn();
            else emitStatus();              // dang chay roi -> chi xac nhan trang thai
        } else {                            // B0 (hoac so khac): dung + home
            doMachineOff();
        }
    }
    else if (c0 == 'G' || c0 == 'g')        // DIAG: tong ket me theo vung 50 la
    {
        printRunSummary();
    }
    else if (c0 == 'N' || c0 == 'n')        // batchTarget: 0=keo het hoc, >0=dung o N la
    {
        long v = atol(cmd + 1);
        if (v >= 0 && v <= 9999) { batchTarget = (uint16_t)v; Serial.print(F("[CMD] batchTarget=")); Serial.print(batchTarget); Serial.println(F(" la (0=keo het hoc)")); }
        else Serial.println(F("[CMD] N: 0..9999"));
    }
    else if (c0 == 'V' || c0 == 'v')        // v7.4: MODEL Pi dat toc: V<c/s> (TTL 10s), V0 = off
    {
        long v = atol(cmd + 1);
        if (v >= (long)EXT_SPD_LO && v <= 520) {
            extSpeed = (float)v; extSpeedOn = true; extSpeedSetMs = millis();
            Serial.print(F("[CMD] extSpeed=")); Serial.print((int)extSpeed); Serial.println(F(" c/s (TTL 10s)"));
        } else if (v == 0) {
            extSpeedOn = false; Serial.println(F("[CMD] extSpeed OFF -> governor noi bo"));
        } else Serial.println(F("[CMD] V: 70..520 hoac 0=off"));
    }
    else if (c0 == 'R' || c0 == 'r')        // TEST: quay NGUOC 0.6s ngay -> kiem tra dao chieu motor
    {
        if (machineState == RUNNING) { Serial.println(F("[R] dang chay me -> khong test")); return; }  // [review v28.3] guard như P/H
        long e0 = readEncoderAtomic();
        Serial.print(F("[TEST-REV] QUAY NGUOC 600ms PWM=")); Serial.print(REGRIP_PWM);
        Serial.print(F(" enc=")); Serial.println(e0);
        bool wasRun = motorRunning; motorRunning = true;
        motorReverse(REGRIP_PWM);
        for (uint8_t i = 0; i < 12; i++) { delay(50); wdt_reset(); }   // 600ms, van reset wdt
        motorStop(); motorRunning = wasRun;
        long e1 = readEncoderAtomic();
        Serial.print(F("[TEST-REV] xong: lui ")); Serial.print(e0 - e1);
        Serial.println(F(" counts  (>0 = DAO CHIEU OK; <=0 = motor KHONG lui / sai chieu day)"));
    }
    else if (c0 == 'F' || c0 == 'f')        // F = xem duong cong feed-forward da hoc; F0 = reset ve default + luu
    {
        if (cmd[1] == '0') {   // v28.2 (review): YÊU CẦU số 0 tường minh — "F " (space rác) không được reset EEPROM
            ffInitDefault(); ffSaveEEPROM();
            Serial.println(F("[FF] RESET ve default + da luu EEPROM"));
        }
        Serial.print(F("[FF] curve PWM/vung(50 la): "));
        for (uint8_t z = 0; z < NBUCKETS; z++) { Serial.print((int)ffTable[z]); if (z < NBUCKETS - 1) Serial.print(','); }
        Serial.print(F("  (")); Serial.print(ffLoaded ? F("da hoc tu EEPROM") : F("default")); Serial.println(F(")"));
    }
    else if (c0 == 'P' || c0 == 'p')        // TEST stepper: P<n> = HA platform n full-step (mac dinh 80, toi da 400)
    {
        if (machineState == RUNNING) Serial.println(F("[P] dang chay me -> khong test stepper"));
        else {
            long v = atol(cmd + 1);
            if (v <= 0) v = 80; if (v > 400) v = 400;
            // v28.2 (review): TÔN TRỌNG trần hành trình như đường chạy thật — không có công tắc đáy,
            //   P liên tiếp không được vượt MAX_LOWER_STEPS (chống đụng đáy vít me).
            long room = (long)MAX_LOWER_STEPS - (long)stepperCurrentSteps;
            if (room <= 0) { Serial.println(F("[P] DA CHAM TRAN hanh trinh -> khong ha them. Go 'H' de home ve dinh.")); return; }
            if (v > room) { v = room; Serial.print(F("[P] cat xuong ")); Serial.print(v); Serial.println(F(" step (toi tran hanh trinh)")); }
            Serial.print(F("[P] TEST ha platform ")); Serial.print(v); Serial.println(F(" full-step (1/16 ustep)..."));
            machineState = RUNNING;             // mo gate machineStillOn cho lowerPlatform (blocking ngan, serial khong doc giua chung)
            lowerPlatform((uint16_t)v);
            machineState = IDLE;
            Serial.print(F("[P] xong. STEP_POS=")); Serial.print(stepperCurrentSteps);
            Serial.println(F(" full-step da ha. Go 'H' de home ve dinh (cham cong tac hanh trinh la dung)."));
            emitStatus();   // [review v28.3] báo Pi lim=0 ngay -> homed cập nhật đúng (khỏi chờ poll)
        }
    }
    else if (c0 == 'H' || c0 == 'h')        // TEST stepper: H = HOME — LUON di LEN toi CHAM cong tac top (ke ca STEP_POS=0)
    {
        if (machineState == RUNNING) Serial.println(F("[H] dang chay me -> khong home"));
        else { returnStepperHomeBlocking(); emitStatus(); }   // v28.3: báo Pi lim ngay sau home -> UI mở START
    }
    else if (c0 == 'L' || c0 == 'l')        // [review v28.3] L<n> = chinh buoc HA platform/10 la (live)
    {                                        //   banner + comment CO quang cao lenh nay nhung truoc day CHUA implement
        long v = atol(cmd + 1);
        if (v >= 4 && v <= 150) {
            stepsPerLower = (uint16_t)v;
            Serial.print(F("[CMD] stepsPerLower=")); Serial.print(stepsPerLower); Serial.println(F(" full-step/10 la"));
        } else Serial.println(F("[CMD] L: 4..150 (buoc ha moi 10 la)"));
    }
    else
    {
        Serial.println(F("[CMD] B1=CHAY B0=DUNG | S=status(ST) | G=DIAG | N<n>=so la | L<n>=buoc ha | F=FF, F0=reset | R=test rev | P<n>=ha stepper, H=home"));
    }
}

// ======================================================
// SETUP
// ======================================================
void setup()
{
    wdt_disable();

    pinMode(SENSOR_PIN, INPUT_PULLUP);
    pinMode(LIMIT_SW,   LIMIT_ACTIVE_LOW ? INPUT_PULLUP : INPUT);   // công tắc hành trình D7 (hở = an toàn)
    pinMode(ENC_A,      INPUT_PULLUP);
    pinMode(ENC_B,      INPUT_PULLUP);
    pinMode(MOTOR_IN1,  OUTPUT);
    pinMode(MOTOR_IN2,  OUTPUT);
    pinMode(STEP_PIN,   OUTPUT);
    pinMode(DIR_PIN,    OUTPUT);
    pinMode(MS_PIN1,    OUTPUT);
    pinMode(MS_PIN2,    OUTPUT);
    pinMode(MS_PIN3,    OUTPUT);

    digitalWrite(STEP_PIN, LOW);
    digitalWrite(DIR_PIN,  STEPPER_DOWN);
    setStepMode(true);                  // MS = 1/16 microstep LUÔN (cả hạ platform lẫn home -> đều ÊM) (v5.2)
    motorStop();

    attachInterrupt(digitalPinToInterrupt(ENC_A), encoderISR, RISING);

    Serial.begin(115200);
    delay(500);

    wdt_enable(WDTO_4S);

    ffLoadEEPROM();                 // v6.0: nạp đường cong feed-forward đã học (hoặc default nếu chưa có)

    Serial.println(F("========================================"));
    Serial.println(F("  SMART CARD FEEDER — STABLE v28 COUNT-RT        "));
    Serial.println(F("========================================"));
    Serial.print  (F("  Stack  : ")); Serial.print(totalCards); Serial.println(F(" cards"));
    Serial.print  (F("  Batch  : ")); Serial.print(batchTarget); Serial.println(F(" la (N<n> de doi, 0=keo het hoc)"));
    Serial.print  (F("  SPEED  : nominal ")); Serial.print((int)STEADY_SPEED); Serial.print(F(" c/s + cadence governor (giu nhip deu) | startSpeed hoc=")); Serial.print((int)startSpeed); Serial.println(F(" c/s"));
    Serial.println(F("  Logic  : CONST-SPEED + FEED-FORWARD theo so la (tu hoc, EEPROM) + PI vi chinh"));
    Serial.print  (F("  FF     : ")); Serial.print(ffLoaded ? F("EEPROM(da hoc)") : F("DEFAULT(chua hoc)"));
    Serial.print  (F(" curve=")); for (uint8_t z=0; z<NBUCKETS; z++){ Serial.print((int)ffTable[z]); if (z<NBUCKETS-1) Serial.print(','); } Serial.println();
    Serial.print  (F("  Stepper: "));
    if (STEPPER_ENABLED) Serial.println(F("ON"));
    else                 Serial.println(F("TAT (tam) - khong ha platform / khong home"));
    Serial.print  (F("  Platfm : ")); Serial.print(stepsPerLower); Serial.print(F(" step/")); Serial.print((int)CARDS_PER_LOWER); Serial.print(F(" la, tran ")); Serial.print(MAX_LOWER_STEPS); Serial.println(F(" step"));
    Serial.println(F("  Cmds   : B1=CHAY B0=DUNG | S=status | G=DIAG | N<n>=so la | V<n>=toc Pi | F=FF, F0=reset | R=test rev | P<n>=ha, H=home"));
    Serial.println(F("========================================"));
}

// ======================================================
// LOOP
// ======================================================
void loop()
{
    wdt_reset();

    // --- v5.20 LOOP-HEALTH: do tac dong cua log len control loop (observer effect) ---
    uint32_t nowUs = micros();
    uint32_t dUs   = nowUs - lastLoopUs;
    lastLoopUs = nowUs;
    if (dUs > 65000UL) dUs = 65000UL;
    if ((uint16_t)dUs > loopMaxUs) loopMaxUs = (uint16_t)dUs;
    loopIters++;
    if (millis() - loopHzMark >= 1000) {
        loopHz = (uint16_t)loopIters; loopIters = 0; loopHzMark = millis();
        int fr = freeRAM(); if (fr < minFreeRAM) minFreeRAM = fr;
    }

    handleSerialCommand();   // [v2] Nguồn BẬT/TẮT máy DUY NHẤT: lệnh serial B1/B0 từ Pi (công tắc A0 đã gỡ).

    if (machineState != RUNNING) return;    // CỔNG chạy = machineState (chỉ serial B1/B0 bật được)

    // ===== RUNNING =====
    if (machineState == RUNNING)
    {
        // ===== v28.3 DEADMAN (FAIL-SAFE): mất liên lạc Pi khi ĐANG CHẠY -> DỪNG MOTOR NGAY =====
        //   Pi treo / rớt USB / MẤT ĐIỆN Pi (mà rail 12V vẫn nuôi ATmega) -> Pi ngừng heartbeat.
        //   Không được để motor DC quay hoài (kéo bài vô tận / kẹt). Dừng + về IDLE an toàn,
        //   KHÔNG tự home (user phải bấm HOME để biết + kiểm tra). Đây là "phần mềm ngắt điện
        //   thì phần cứng dừng ngay" ở tầng chắc chắn nhất (không phụ thuộc DTR auto-reset).
        if ((uint32_t)(millis() - lastRxMs) > LINK_DEADMAN_MS) {
            motorStop();
            machineState = IDLE;
            runStatus = RS_ERROR; lastErr = EF_LINK;
            Serial.println(F("\n[LINKLOST] mat lien lac Pi (>1.5s im lang) khi dang chay -> DA DUNG MOTOR (an toan)."));
            emitStatus();
            return;
        }

        // ---- SENSOR (đo lá bằng encoder) + 2 tầng điều khiển ----
        uint8_t n = pollSensor();     // số lá vừa hoàn tất (0/1/cụm) + cập nhật trạng thái sensor
        outerControlUpdate();         // tầng ngoài: steady + startup + re-grip lùi
        velocityLoopUpdate();         // tầng trong: PI tốc độ (tự gate 50ms)

        // [WAIT] đếm thời gian KHÔNG thấy lá (mỗi 500ms) -> thấy khi nào sẽ quay ngược
        {
            static uint32_t lastWaitMs = 0;
            if (!sPresent && motorRunning) {
                uint32_t gap = millis() - sPresentSince;
                if (gap >= 500 && millis() - lastWaitMs >= 500) {
                    lastWaitMs = millis();
                    Serial.print(F("[WAIT] khong thay la ")); Serial.print(gap);
                    Serial.print(F("ms (quay nguoc khi >=")); Serial.print(NUDGE_GAP_MS); Serial.println(F("ms)"));
                }
            }
        }

        // ===== v6.4: MOTOR KHÔNG QUAY (PWM ra mà encoder đứng yên) -> chưa cấp điện motor / kẹt cứng =====
        //   Bắt NGAY ~1.5s (thay vì đợi GAP_STALL 13s). Chỉ kiểm 1 lần/mẻ, khi chưa đếm được lá nào.
        if (!nomoveChecked && motorRunning && cardCount == 0
            && (millis() - runStartMs) > (uint32_t)MOTOR_NOMOVE_MS)
        {
            nomoveChecked = true;
            long encNow = readEncoderAtomic(); if (encNow < 0) encNow = -encNow;
            if (encNow < (long)MOTOR_NOMOVE_COUNTS)        // PWM ra mà encoder gần như đứng = motor không quay
            {
                uint8_t pwmWas = motorPWM;
                motorStop();
                Serial.print(F("\n[NOMOTOR] PWM=")); Serial.print(pwmWas);
                Serial.print(F(" ra nhung encoder=")); Serial.print(encNow);
                Serial.println(F(" -> MOTOR KHONG QUAY (chua cap dien motor? / ket cung). KIEM TRA NGUON MOTOR."));
                machineState = IDLE;
                runStatus    = RS_ERROR;
                lastErr      = EF_NOMOTOR;
                emitStatus();
            }
        }

        // (v28.2 review: block "DRAIN xong" đã XÓA — finalDrain không bao giờ true từ v5.52,
        //  code chết lại chứa phép so millis tuyệt đối cuối cùng. DONE thật nằm ở khối đếm dưới.)

        if (n > 0)
        {
            uint32_t now = millis();
            uint32_t dtFull = now - lastCardMs;
            if (dtFull > 60000UL) dtFull = 60000UL;
            if (dtFull < 1)       dtFull = 1;
            lastDt     = (uint16_t)dtFull;
            lastCardMs = now;
            // v5.50: dtFilt = "ideal cadence" -> CHỈ học từ lá ĐƠN nhịp bình thường (bỏ cụm + lá kẹt/outlier)
            //   -> tham chiếu sạch để phát hiện lá ra CHẬM (dt>dtFilt*1.45) hay QUÁ NHANH (dt<dtFilt*0.62)
            if (n == 1 && dtFull < 2000UL) dtFilt += DT_ALPHA * ((float)dtFull - dtFilt);  // v7.0: 1000->2000, khong mu la cham -> chuan tham chieu khong troi ve huong nhanh

            // v6.6 GENTLE CADENCE GOVERNOR: giữ NHỊP lá ĐỀU cả mẻ (bù slip giảm khi chồng vơi).
            //   dt dài (lá ra chậm) -> tăng steadySpeed; dt ngắn (nhanh) -> giảm. Dùng dtFilt (đã lọc)
            //   + gain nhỏ + slew + bound -> bám XU HƯỚNG chậm, KHÔNG đua nhiễu (khác governor v5.54 đã bỏ).
            //   Chỉ sau startup + lá đơn nhịp hợp lệ. PWN-floor + feed-forward vẫn lo phần torque/tải.
            // v28.2 (review): TTL hết -> TẮT HẲN latch. Không tắt thì sau 49.7 ngày hiệu unsigned
            //   wrap lại < TTL -> lệnh V cũ "sống dậy" 10s. Tắt hẳn = hết ma.
            if (extSpeedOn && (uint32_t)(millis() - extSpeedSetMs) >= EXT_SPEED_TTL_MS)
                extSpeedOn = false;
            if (extSpeedOn) {
                // v7.4: MODEL tren Pi dang dieu toc (lenh V) -> governor noi bo NGHI
                steadySpeed = constrain(extSpeed, EXT_SPD_LO, 520.0f);  // [v23] cho model ha xuong V140
            } else if (n == 1 && cardCount >= STARTUP_CARDS && dtFull >= 80UL && dtFull <= 2500UL) {
                float adj = CAD_GAIN * (dtFilt - (float)cadTargetNow());   // v7.3: nhip theo pha
                adj = constrain(adj, -CAD_STEP_MAX, CAD_STEP_MAX);
                steadySpeed = constrain(steadySpeed + adj, cadSpdLoNow(), cadSpdHiNow());   // v18: sàn theo pha
            }
            // v19 FLAT-PACE dt-SERVO: chỉnh gripServo giữ nhịp dt ĐỀU (lá đơn, sau startup, dt hợp lệ).
            //   dt > đích (lá chậm) -> +grip (kéo mạnh hơn); dt < đích (nhanh) -> -grip. Bounded [MIN,MAX].
            if (n == 1 && cardCount >= SERVO_START_CARD && dtFull >= 80UL && dtFull <= 2500UL) {
                float gerr = (float)dtFull - (float)FLATPACE_DT_MS;
                gripServo += constrain(gerr * GSERVO_K, -GSERVO_DN, GSERVO_UP);
                gripServo  = constrain(gripServo, GSERVO_MIN, GSERVO_MAX);
            }

            // v6.8 TỰ HỌC tốc khởi động: trong cửa sổ HEAVY (sau ramp, đầu mẻ) -> EMA startSpeed theo
            //   steadySpeed governor đang dùng. Mẻ sau khởi động ĐÚNG tốc heavy-start -> 50 lá đầu đều luôn.
            if (n == 1 && cardCount >= STARTUP_CARDS && cardCount <= START_LEARN_END)
                startSpeed = constrain(startSpeed + START_LEARN_ALPHA * (steadySpeed - startSpeed), CAD_SPD_LO, CAD_HI_HEAVY);

            cardCount += n;
            lastStMs = millis(); emitStatus();   // v6.2 REAL-TIME: đẩy count đã CHỐT ngay (không đợi nhịp 250ms)
            jamRecoverCount = 0;   // v5.53: đếm được lá = feed OK -> reset bộ đếm gỡ kẹt

            // --- DIAG: gom số liệu theo vùng 50 lá (để TỔNG KẾT chốt nguyên nhân) ---
            {
                uint8_t b = (uint8_t)((cardCount - 1) / 50); if (b >= NBUCKETS) b = NBUCKETS - 1;
                cardBucket[b] += n; evtBucket[b]++;
                gapSumBucket[b] += pickupGap; dtSumBucket[b] += dtFull;
                pupSumBucket[b] += (uint32_t)lastPickupPWM;   // tong pickupPWM truoc update theo vung
                slipSumBucket[b] += gapDist;                  // tong quang duong slip theo vung
                if (pickupGap > gapMaxBucket[b]) gapMaxBucket[b] = pickupGap;  // gap worst-case (duoi phan bo)
                if (lastNudgeCount > 0) nudgedBucket[b]++;     // so LA can >=1 nudge (ti le grip kho)
                if (n >= 2) clumpBucket[b]++;
                else { sLenSumBucket[b] += lastLen; sLenCntBucket[b]++;
                       if (lastLen > sLenMaxBucket[b]) sLenMaxBucket[b] = lastLen; }
                nudgeBucket[b] += lastNudgeCount;
            }

            // LỌC nhịp (EMA); vừa thoát stall dài thì coi đúng nhịp (P không vọt ga)
            float instRate = (dtFull > STALL_DT_MS) ? TARGET_RATE_CPS : (1000.0 / (float)dtFull);
            filtRate += RATE_ALPHA * (instRate - filtRate);

            // --- v5.50: VISION -> TAY: cập nhật speedTrim theo CHẤT LƯỢNG feed của lá vừa ra ---
            //   CỤM / lá ra QUÁ NHANH (multi-feed)  -> trim ÂM  (chậm lại tách, chống vồ nhiều lá ở tail)
            //   lá ra CHẬM (dt dài) hoặc cần đề-ba   -> trim DƯƠNG (tăng tốc kéo lá KẾ ra)
            //   lá THƯỜNG                            -> trim *= DECAY -> VỀ ideal sau 1-2 lá
            // v6.0: CHỈ phản ứng SỰ KIỆN THẬT (cụm / vừa đề-ba), KHÔNG phản ứng theo nhịp dt
            //   (dt jitter là bình thường, phản ứng theo nó = giật bậc tốc). Chạy đều -> trim tự rã về 0
            //   -> bám STEADY_SPEED. Việc bù tải nặng/nhẹ là của feed-forward, không phải của trim.
            if (n >= 2) {
                speedTrim -= TRIM_BUMP_FAST;                     // cụm thật -> chậm lại tách (chống multi-feed)
                clumpCaution = CLUMP_CAUTION_CARDS;              // v6.9: vào chế độ CHẬM thận trọng sau cụm
            } else if (lastNudgeCount > 0) {
                speedTrim += TRIM_BUMP_SLOW;                     // vừa đề-ba xong -> giúp kéo lá kế ra
            } else {
                speedTrim *= TRIM_DECAY_CARD;                    // bình thường -> rã về 0 (bám tốc cố định)
            }
            // v6.9: GIỮ chậm xuyên mảng bài dính (cụm hay đi theo chùm) -> separator tách kịp, chống dính tiếp
            if (clumpCaution > 0) { clumpCaution--; if (speedTrim > -CLUMP_CAUTION_TRIM) speedTrim = -CLUMP_CAUTION_TRIM; }
            speedTrim = constrain(speedTrim, -TRIM_DOWN_MAX, TRIM_UP_MAX);
            if (n >= 2)
            {
                clumpEvents++;
                if (DEBUG_MODE)
                {
                    Serial.print(F("[CLUMP] ")); Serial.print(n);
                    Serial.print(F(" la 1 luc len=")); Serial.print(lastLen);
                    Serial.print(F(" normLen=")); Serial.print((int)normLen);
                    Serial.print(F(" ratio=")); Serial.print(lastLen / normLen, 2);
                    Serial.println(F(" -> dem bu + kep toc"));
                }
            }

            // Hạ platform theo SỐ lá (cụm nhảy nhiều lá vẫn đúng nhịp hạ)
            bool lowered = false;
            cardsSinceLower += n;
            while (STEPPER_ENABLED && cardsSinceLower >= CARDS_PER_LOWER)
            {
                cardsSinceLower -= CARDS_PER_LOWER;
                if (stepperCurrentSteps + (long)stepsPerLower <= (long)MAX_LOWER_STEPS) {
                    lowerPlatform(stepsPerLower);
                    lowered = true;
                } else if (!platformMaxWarned) {        // chạm trần 135mm -> NGỪNG hạ (an toàn), cảnh báo 1 lần
                    platformMaxWarned = true;
                    Serial.println(F("[LIMIT] platform cham tran hanh trinh -> NGUNG ha (KHONG dung day; pile cao dan)"));
                }
            }

            uint32_t rem = (batchTarget == 0 || cardCount >= batchTarget) ? 0 : (batchTarget - cardCount);
            Serial.print(F("[CARD] #")); Serial.print(cardCount);
            if (n >= 2) { Serial.print(F("(+")); Serial.print(n); Serial.print(F(")")); }
            Serial.print(F(" | REM="));  Serial.print(rem);
            Serial.print(F(" | dt="));   Serial.print(lastDt); Serial.print(F("ms "));
            Serial.print(1000.0f / (float)dtFull, 1); Serial.print(F("la/s avg")); Serial.print(filtRate, 1); // v5.21: nhip THAT (bo artifact clamp 2.0)
            Serial.print(F(" | len="));  Serial.print(lastLen);
            Serial.print(F(" | tgt="));  Serial.print((int)targetSpeed);
            Serial.print(F(" trim="));   Serial.print((int)speedTrim);          // v5.50: do lech quanh ideal
            Serial.print(F(" ff="));     Serial.print((int)feedForwardPWM());    // v6.0: PWM feed-forward dang dung
            Serial.print(F(" meas="));   Serial.print((int)measuredSpeed); Serial.print(F("c/s"));
            Serial.print(F(" | PWM="));  Serial.print(motorPWM);
            Serial.print(F(" | STEP=")); Serial.print(stepperCurrentSteps);
            Serial.print(F(" | md="));   Serial.print(modeStr(ctrlMode));
            Serial.print(F(" gap="));    Serial.print(pickupGap); Serial.print(F("ms"));
            Serial.print(F(" low="));    Serial.print(lowDurMs);  Serial.print(F("ms"));
            if (lastNudgeCount > 0)                     { Serial.print(F(" ndg=")); Serial.print(lastNudgeCount); }
            if (lastFloor > (float)PWM_HARD_MIN + 0.5f) { Serial.print(F(" flr=")); Serial.print((int)lastFloor); Serial.print(F("/")); Serial.print((int)lastPickupPWM); }
            Serial.print(F(" pup=")); Serial.print((int)lastPickupPWM); Serial.print(F("->")); Serial.print((int)pickupPWM);
            Serial.print(F(" cPWM=")); Serial.print(lastCatchPWM);                                  // PWM raw luc tom la
            Serial.print(F(" slip=")); Serial.print(gapDist); Serial.print(F("(")); Serial.print(slipRatio, 1); Serial.print(F("x)")); // con lan quay khong bao xa
            Serial.println(lowered ? F("  <== HA PLATFORM") : F(""));

            // ===== ĐẾM ĐỦ MẺ -> DỪNG NGAY (KHÔNG drain) =====
            // v5.52: bỏ finalDrain. Lá thứ batchTarget được đếm khi nó ĐÃ hoàn tất
            // thoát sensor (pollSensor trả "lá vừa hoàn tất"), nên KHÔNG cần chạy thêm.
            // Drain cũ (chạy thêm FINAL_DRAIN_MS=600ms ở tốc độ cao) kéo thêm ~5 lá mới
            // -> đếm LỐ 412->417. Dừng motor ngay tại target = dừng ĐÚNG số lá.
            if (batchTarget > 0 && cardCount >= batchTarget)
            {
                motorStop();
                Serial.print(F("\n[DONE] da dem ")); Serial.print(cardCount);
                Serial.print(F(" la (")); Serial.print(clumpEvents);
                Serial.println(F(" lan dinh cum) -> DUNG NGAY (dat target). TAT cong tac de home + chay me moi."));
                printRunSummary();
                machineState = IDLE;
                batchDone    = true;
                runStatus    = RS_DONE;   // báo Pi: đạt mục tiêu -> kết thúc mẻ
                lastErr      = EF_NONE;
                emitStatus();
            }
        }
        // ===== HẾT LÁ / MẤT ĂN KHỚP quá lâu -> DỪNG (không quay vô ích) =====
        else if (!sPresent && (millis() - sPresentSince) > (uint32_t)(cardCount == 0 ? GAP_STALL_START : GAP_STALL_MS))
        {
            motorStop();
            Serial.print(F("\n[STALL] khong co la ")); Serial.print(millis() - sPresentSince);
            Serial.println(F("ms -> DA DUNG MOTOR (het la hoac mat tiep xuc chong the)."));
            Serial.print(F("[SUMMARY] da nha ")); Serial.print(cardCount);
            Serial.print(F(" la, ")); Serial.print(clumpEvents);
            Serial.println(F(" lan dinh cum. TAT cong tac de home platform."));
            printRunSummary();
            machineState = IDLE;             // KHONG home o day -> chi home khi TAT cong tac
            runStatus    = RS_ERROR;         // báo Pi: hết lá / mất tiếp xúc -> kết thúc mẻ
            lastErr      = EF_STALL;
            emitStatus();
        }
        // ===== v5.53: LÁ KẸT CHE SENSOR -> TỰ GỠ (jam-recovery) rồi mới DỪNG =====
        //   Case thiếu trước đây: lá kẹt dưới sensor (sPresent=true mãi) -> không đếm, không vào
        //   GAP_STALL -> máy treo. Giờ: thử "đề ba" tống lá ra JAM_RECOVER_MAX lần; vẫn kẹt -> DỪNG.
        else if (sPresent && (millis() - sPresentSince) > (uint32_t)JAM_RECOVER_MS)
        {
            if (jamRecoverCount < JAM_RECOVER_MAX)
            {
                jamRecoverCount++;
                Serial.print(F("\n[JAMFIX] la ket che sensor -> thu go lan "));
                Serial.print(jamRecoverCount); Serial.print(F("/")); Serial.println(JAM_RECOVER_MAX);
                jamClearAttempt();
                sPresentSince = millis();       // cho lần đẩy này thời gian ăn trước khi tính kẹt lại
            }
            else
            {
                motorStop();
                // [STALL] prefix -> parser Pi bắt thành sự kiện "stall" -> app báo "Operation error".
                Serial.println(F("\n[STALL] la ket che sensor, da thu go khong duoc -> DUNG."));
                Serial.print(F("[SUMMARY] da nha ")); Serial.print(cardCount);
                Serial.println(F(" la truoc khi ket. Go la ket roi TAT/BAT cong tac chay lai."));
                printRunSummary();
                machineState = IDLE;
                runStatus    = RS_ERROR;        // báo Pi: kẹt -> kết thúc mẻ (KHÔNG treo)
                lastErr      = EF_STALL;
                emitStatus();
            }
        }

    }

    // [ST] moi ~250ms khi RUNNING -> Pi cap nhat realtime (st/n/tot/err/spd)
    if (machineState == RUNNING && millis() - lastStMs >= 250)
    {
        lastStMs = millis();
        emitStatus();
    }
    // [STAT] moi 2s khi RUNNING (nhe -> ko delay control)
    if (machineState == RUNNING && millis() - lastStatusPrint >= 2000)
    {
        lastStatusPrint = millis();
        Serial.print(F("[STAT] "));
        printStatus();
    }
    // [HEALTH] moi 4s khi RUNNING — do suc khoe + observer effect
    if (machineState == RUNNING && millis() - lastHealthMs >= 4000)
    {
        lastHealthMs = millis();
        printHealth();
    }

    delay(2);
}
