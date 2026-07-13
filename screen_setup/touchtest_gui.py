#!/usr/bin/env python3
"""
Bài test cảm ứng TRỰC QUAN trên màn hình LCD.
Hiển thị lần lượt 5 mục tiêu (4 góc + giữa). Bạn chạm vào mục tiêu đang sáng.
Chương trình đánh dấu chỗ bạn CHẠM (chấm đỏ) so với mục tiêu (vòng xanh),
và ghi kết quả ra file để phân tích hướng. Chạy trên DISPLAY=:0.
"""
import tkinter as tk
import time, json, os

TARGETS = [
    ("GIỮA",        0.50, 0.50),
    ("TRÊN-TRÁI",   0.12, 0.15),
    ("TRÊN-PHẢI",   0.88, 0.15),
    ("DƯỚI-PHẢI",   0.88, 0.85),
    ("DƯỚI-TRÁI",   0.12, 0.85),
]

class App:
    def __init__(self, root):
        self.root = root
        root.attributes("-fullscreen", True)
        root.configure(bg="black")
        self.W = root.winfo_screenwidth()
        self.H = root.winfo_screenheight()
        self.canvas = tk.Canvas(root, width=self.W, height=self.H,
                                bg="black", highlightthickness=0)
        self.canvas.pack()
        self.idx = 0
        self.results = []
        self.canvas.bind("<Button-1>", self.on_touch)
        self.canvas.bind("<B1-Motion>", self.on_touch)
        self.show_target()

    def show_target(self):
        self.canvas.delete("all")
        if self.idx >= len(TARGETS):
            self.finish(); return
        name, fx, fy = TARGETS[self.idx]
        tx, ty = int(fx*self.W), int(fy*self.H)
        self.tx, self.ty, self.tname = tx, ty, name
        # vòng tròn mục tiêu xanh
        r = 26
        self.canvas.create_oval(tx-r, ty-r, tx+r, ty+r, outline="#00ff66", width=4)
        self.canvas.create_oval(tx-4, ty-4, tx+4, ty+4, fill="#00ff66", outline="")
        self.canvas.create_text(self.W//2, self.H//2,
            text=f"Chạm vào\nVÒNG XANH\n({name})", fill="white",
            font=("DejaVu Sans", 16, "bold"), justify="center")

    def on_touch(self, ev):
        # chỉ ghi 1 điểm mỗi mục tiêu (điểm chạm đầu tiên), rồi CHỜ nhấc tay + chạm nút "TIẾP"
        if getattr(self, "_locked", False):
            return
        x, y = ev.x, ev.y
        self._locked = True
        self.canvas.create_oval(x-8, y-8, x+8, y+8, fill="#ff3333", outline="white", width=2)
        self.canvas.create_line(self.tx, self.ty, x, y, fill="#ffaa00", width=3)
        self.results.append({
            "target": self.tname, "tx": self.tx, "ty": self.ty,
            "touch_x": x, "touch_y": y
        })
        # hiện hướng dẫn: nhấc tay ra, đợi 1.5s rồi tự sang mục tiêu sau
        self.canvas.create_text(self.W//2, self.H-24,
            text="Tốt! Nhấc tay ra...", fill="#ffff00",
            font=("DejaVu Sans", 13, "bold"))
        self.canvas.after(1500, self.next_target)

    def next_target(self):
        self.idx += 1
        self.show_target()

    def finish(self):
        with open("/home/bbsw/ads7846-userspace/gui_test_result.json", "w") as f:
            json.dump(self.results, f, ensure_ascii=False, indent=2)
        self.canvas.delete("all")
        self.canvas.create_text(self.W//2, self.H//2,
            text="XONG!\nĐã ghi kết quả.\n(tự đóng sau 3s)", fill="#00ff66",
            font=("DejaVu Sans", 18, "bold"), justify="center")
        self.root.after(3000, self.root.destroy)

if __name__ == "__main__":
    os.environ.setdefault("DISPLAY", ":0")
    root = tk.Tk()
    App(root)
    root.mainloop()
