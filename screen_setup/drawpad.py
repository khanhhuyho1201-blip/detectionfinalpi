#!/usr/bin/env python3
"""
Bảng vẽ đơn giản: di ngón tay tới đâu, chấm vàng theo tới đó + vẽ vệt.
Hiển thị nhãn 4 mép (TRÊN/DƯỚI/TRÁI/PHẢI) để bạn đối chiếu.
Ghi lại toàn bộ điểm chạm ra file để phân tích hướng.
Không cần chạm trúng gì cả — chỉ cần di ngón tay và quan sát.
"""
import tkinter as tk
import json, os, time

class Pad:
    def __init__(self, root):
        self.root = root
        root.attributes("-fullscreen", True)
        root.configure(bg="black")
        self.W = root.winfo_screenwidth()
        self.H = root.winfo_screenheight()
        self.c = tk.Canvas(root, width=self.W, height=self.H, bg="black",
                           highlightthickness=0)
        self.c.pack()
        # nhãn 4 mép để đối chiếu
        self.c.create_text(self.W//2, 14, text="▲ TRÊN ▲", fill="#4488ff",
                           font=("DejaVu Sans", 12, "bold"))
        self.c.create_text(self.W//2, self.H-14, text="▼ DƯỚI ▼", fill="#4488ff",
                           font=("DejaVu Sans", 12, "bold"))
        self.c.create_text(40, self.H//2, text="TRÁI", fill="#4488ff",
                           font=("DejaVu Sans", 12, "bold"))
        self.c.create_text(self.W-40, self.H//2, text="PHẢI", fill="#4488ff",
                           font=("DejaVu Sans", 12, "bold"))
        self.c.create_text(self.W//2, self.H//2-30,
                           text="Di ngón tay quanh màn hình\nChấm vàng sẽ đi theo.\n(tự đóng sau 40s)",
                           fill="#888888", font=("DejaVu Sans", 11), justify="center")
        self.pts = []
        self.dot = None
        self.c.bind("<Button-1>", self.on)
        self.c.bind("<B1-Motion>", self.on)
        root.after(40000, self.finish)

    def on(self, ev):
        x, y = ev.x, ev.y
        self.pts.append((x, y))
        # vệt xanh mờ
        self.c.create_oval(x-2, y-2, x+2, y+2, fill="#225522", outline="")
        # chấm vàng hiện tại
        if self.dot: self.c.delete(self.dot)
        self.dot = self.c.create_oval(x-9, y-9, x+9, y+9, fill="#ffdd00",
                                      outline="white", width=2)

    def finish(self):
        with open("/home/bbsw/ads7846-userspace/drawpad_points.json", "w") as f:
            json.dump(self.pts, f)
        self.root.destroy()

if __name__ == "__main__":
    os.environ.setdefault("DISPLAY", ":0")
    r = tk.Tk()
    Pad(r)
    r.mainloop()
