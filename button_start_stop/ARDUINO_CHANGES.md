# Phần A — Sửa firmware Arduino (production.ino)

> **Trạng thái:** chưa áp được vì `production.ino` đang nằm trên máy Mac, không có trên
> Pi này. Gửi file sang (dán vào chat, hoặc `scp` sang Pi) là mình áp ngay.
> Tài liệu này là bản vá chính xác mình sẽ thực hiện — giữ nguyên 100% logic cơ
> khí (PI motor, stepper, đếm lá, phát hiện cụm/kẹt), chỉ thêm cổng điều khiển +
> báo trạng thái cho Pi. Số dòng tham chiếu theo mô tả trong kế hoạch.

Giao thức hai bên: xem `PROTOCOL.md`.

---

## A1 — Tách `doMachineOn()` / `doMachineOff()`
Khối Bật/Tắt hiện nằm inline trong `loop()` (≈ dòng 998–1062), chỉ công tắc A0
gọi được. Cắt ra thành 2 hàm đặt trước `loop()` để cả công tắc lẫn lệnh serial
dùng chung:

```cpp
void doMachineOn() {
    // ... nguyên khối "MACHINE ON" cũ (init motor, stepper, reset đếm, ...) ...
    machineState = RUNNING;
}

void doMachineOff() {
    // ... nguyên khối "MACHINE OFF" cũ (stop motor, home, ...) ...
    machineState = IDLE;
}
```
Rủi ro thấp — chỉ di chuyển code. Verify bằng compile.

## A2 — Thêm lệnh `B1` / `B0` trong `handleSerialCommand()` (≈ dòng 880)
```cpp
// trong switch/if xử lý lệnh:
if (cmd == "B1") { if (machineState != RUNNING) doMachineOn();  return; }
if (cmd == "B0") { doMachineOff(); return; }
// N<n> đã có sẵn — giữ nguyên.
```

## A3 — Đổi điều kiện cổng chạy (≈ dòng 1067)
```cpp
// CŨ:  if (lastSwitchState != LOW) return;   // chỉ chạy khi công tắc bật
// MỚI:
if (machineState != RUNNING) return;          // chạy khi RUNNING (công tắc HOẶC B1)
```
Đây là mấu chốt để nút trên màn hình thật sự khởi động được máy.

## A4 — "Thao tác mới nhất thắng" (chống công tắc ↔ màn hình đánh nhau)
Công tắc A0 là kiểu duy trì. Chỉ kích hành động khi **đổi trạng thái (sườn)**,
không ép liên tục — nếu Pi đã `B0` mà công tắc còn đóng thì không tự chạy lại:
```cpp
bool sw = digitalRead(SWITCH_PIN);
if (sw != lastSwitchState) {            // chỉ xử lý khi có sườn
    if (sw == LOW) doMachineOn();        // vừa đóng → chạy
    else           doMachineOff();       // vừa mở → dừng
    lastSwitchState = sw;
}
```
(Lúc test để công tắc ở trạng thái MỞ thì nhánh này gần như không kích hoạt,
nhưng vẫn nên code cho chắc.)

## A5 — Dòng trạng thái gọn cho Pi (khuyến nghị)
In định kỳ ~250 ms khi chạy + ngay khi nhận `S`:
```cpp
void emitStatus() {
    Serial.print(F("ST st="));
    Serial.print(machineState == RUNNING ? F("RUN") : F("IDLE"));
    Serial.print(F(" n="));   Serial.print(cardCount);
    Serial.print(F(" tot=")); Serial.print(targetCount);
    Serial.print(F(" err=")); Serial.print(errCode);   // NONE/CLUMP/STALL/LIMIT
    Serial.print(F(" spd=")); Serial.println(currentPWM);
}
// trong loop(): if (millis() - lastStatus >= 250) { emitStatus(); lastStatus = millis(); }
// xử lý 'S': emitStatus();
```
Pi đọc được **cả** dòng `ST` này lẫn log cũ (`[CARD]`, `[STALL]`, `[CLUMP]`,
`[DONE]`, `[MACHINE]`), nên A5 là tuỳ chọn — có thì hiển thị mượt và nhẹ hơn.

## A6 — Cập nhật banner + help
- Banner `setup()` (≈ dòng 966) và help (≈ dòng 919): thêm `B1=CHAY  B0=DUNG  S=STATUS`.

---

## Kiểm chứng (mốc M1)
1. `arduino-cli compile` sạch (đã chạy được trên máy này trước đó).
2. Từ Pi: `echo "B1" > /dev/ttyACM0` → motor chạy; `echo "B0" > /dev/ttyACM0` → dừng.
3. `screen /dev/ttyACM0 115200` (hoặc Serial Monitor) thấy `ST ...` khi chạy.

Sau M1, app Pi5 (`./run.sh`) bấm BẮT ĐẦU/DỪNG là điều khiển máy thật.
