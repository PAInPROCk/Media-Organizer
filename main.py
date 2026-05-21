import os
import shutil
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import asyncio
import threading
import queue
import time
import math
import logging
import json
import random
import cv2
from PIL import Image, ImageTk
from telethon import TelegramClient, types, functions, utils, helpers
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from dotenv import load_dotenv

# --- Load Environment Variables ---
load_dotenv()

# --- Logging Configuration ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logging.getLogger('telethon').setLevel(logging.WARNING)
logger = logging.getLogger("MediaSuggester")

# --- Constants & Config ---
CONFIG_FILE = "config.json"
MEDIA_EXTENSIONS = {
    # Videos
    '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm',
    # Pictures
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'
}

# Telegram Credentials from .env
API_ID = os.getenv('TG_API_ID', '')
API_HASH = os.getenv('TG_API_HASH', '')

# --- Configuration Manager ---
class ConfigManager:
    @staticmethod
    def load():
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {"directories": []}

    @staticmethod
    def save(config):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)

# --- Telegram Manager (Existing Logic) ---
class TelegramManager:
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.thread.start()

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _init_client(self, phone_cb, code_cb, pass_cb):
        if not self.client:
            self.client = TelegramClient(
                'media_organizer_session', 
                self.api_id, 
                self.api_hash,
                connection=ConnectionTcpAbridged
            )
            await self.client.start(phone=phone_cb, code_callback=code_cb, password=pass_cb)

    async def _fast_upload(self, file_path, progress_cb, filename):
        file_size = os.path.getsize(file_path)
        if file_size <= 1024 * 1024: part_size = 32 * 1024
        elif file_size <= 10 * 1024 * 1024: part_size = 64 * 1024
        elif file_size <= 375 * 1024 * 1024: part_size = 128 * 1024
        elif file_size <= 750 * 1024 * 1024: part_size = 256 * 1024
        else: part_size = 512 * 1024
            
        parts_count = math.ceil(file_size / part_size)
        file_id = helpers.generate_random_long()
        is_big = file_size > 10 * 1024 * 1024
        
        start_time = time.time()
        sent_bytes = 0
        lock = asyncio.Lock()
        worker_count = 4
        part_queue = asyncio.Queue()
        for i in range(parts_count): await part_queue.put(i)

        async def upload_worker(worker_id):
            nonlocal sent_bytes
            sender = None
            try:
                if worker_id == 0: sender = self.client._sender
                else:
                    try: sender = await self.client._borrow_exported_sender(self.client.session.dc_id)
                    except: sender = self.client._sender

                while True:
                    try: part_index = part_queue.get_nowait()
                    except asyncio.QueueEmpty: break
                    
                    with open(file_path, 'rb') as f:
                        f.seek(part_index * part_size)
                        data = f.read(part_size)
                    
                    try:
                        req = functions.upload.SaveBigFilePartRequest(file_id, part_index, parts_count, data) if is_big else functions.upload.SaveFilePartRequest(file_id, part_index, data)
                        await (self.client(req) if sender == self.client._sender else sender.send(req))
                        async with lock:
                            sent_bytes += len(data)
                            elapsed = time.time() - start_time
                            speed = sent_bytes / elapsed if elapsed > 0 else 0
                            progress_cb(sent_bytes, file_size, speed, filename)
                    except:
                        await part_queue.put(part_index)
                        await asyncio.sleep(1)
                    finally:
                        part_queue.task_done()
            finally:
                if sender and sender != self.client._sender:
                    try: await self.client._return_exported_sender(sender)
                    except: pass

        workers = [asyncio.create_task(upload_worker(i)) for i in range(worker_count)]
        await part_queue.join()
        for w in workers: w.cancel()
        
        if is_big: return types.InputFileBig(file_id, parts_count, filename)
        else: return types.InputFile(file_id, parts_count, filename, "")

    def upload_files(self, file_paths, phone_cb, code_cb, pass_cb, file_progress_cb, overall_progress_cb, done_cb, error_cb):
        async def _upload():
            try:
                await self._init_client(phone_cb, code_cb, pass_cb)
                for i, path in enumerate(file_paths):
                    file_name = os.path.basename(path)
                    input_file = await self._fast_upload(path, file_progress_cb, file_name)
                    await self.client.send_file('me', input_file)
                    overall_progress_cb(i + 1, len(file_paths))
                done_cb(len(file_paths))
            except Exception as e:
                error_cb(str(e))
        asyncio.run_coroutine_threadsafe(_upload(), self.loop)

# --- UI Utilities ---
def bind_mouse_wheel(canvas):
    def on_mouse_wheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", on_mouse_wheel)

# --- Thumbnail Generator ---
class ThumbnailGenerator:
    def __init__(self):
        self.cache = {}

    def get_thumbnail(self, file_path, size=(200, 112)):
        if file_path in self.cache:
            return self.cache[file_path]
        
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}:
                img = Image.open(file_path)
                img.thumbnail(size)
                photo = ImageTk.PhotoImage(img)
                self.cache[file_path] = photo
                return photo
            else:
                cap = cv2.VideoCapture(file_path)
                total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                cap.set(cv2.CAP_PROP_POS_FRAMES, total_frames // 10)
                ret, frame = cap.read()
                if ret:
                    frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    img = Image.fromarray(frame)
                    img.thumbnail(size)
                    photo = ImageTk.PhotoImage(img)
                    self.cache[file_path] = photo
                    cap.release()
                    return photo
                cap.release()
        except Exception as e:
            logger.error(f"Thumbnail error for {file_path}: {e}")
        return None

# --- Main Application Frame Classes ---

class HomeFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.thumb_gen = ThumbnailGenerator()
        self.setup_ui()

    def setup_ui(self):
        header = ttk.Frame(self, padding=10)
        header.pack(fill=tk.X)
        ttk.Label(header, text="Recommended for You", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(header, text="Refresh", command=self.refresh_suggestions).pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#f0f0f0")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas, style="Card.TFrame")
        
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        # Enable smooth scrolling
        self.canvas.bind("<Enter>", lambda e: bind_mouse_wheel(self.canvas))

    def refresh_suggestions(self):
        for widget in self.scrollable_frame.winfo_children():
            widget.destroy()

        config = ConfigManager.load()
        all_media = []
        for d in config["directories"]:
            if os.path.exists(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                            all_media.append(os.path.join(root, f))
        
        if not all_media:
            ttk.Label(self.scrollable_frame, text="No media found. Add directories in Settings.", padding=20).pack()
            return

        suggestions = random.sample(all_media, min(len(all_media), 20))
        
        # Grid settings
        cols = 4
        for i, path in enumerate(suggestions):
            frame = ttk.Frame(self.scrollable_frame, padding=5, cursor="hand2")
            frame.grid(row=i//cols, column=i%cols, padx=10, pady=10, sticky="n")
            
            # Thumbnail Placeholder
            lbl_thumb = ttk.Label(frame, text="Loading...", cursor="hand2")
            lbl_thumb.pack()
            
            name = os.path.basename(path)
            if len(name) > 25: name = name[:22] + "..."
            
            lbl_name = ttk.Label(frame, text=name, font=("Segoe UI", 9, "bold"), wraplength=180, cursor="hand2")
            lbl_name.pack(pady=(5,0))
            
            lbl_path = ttk.Label(frame, text=os.path.dirname(path), font=("Segoe UI", 7), foreground="gray", wraplength=180, cursor="hand2")
            lbl_path.pack()

            # Bind click event to EVERYTHING inside the card
            def open_file(e, p=path):
                try:
                    os.startfile(p)
                except Exception as ex:
                    messagebox.showerror("Error", f"Could not open file: {ex}")

            frame.bind("<Button-1>", open_file)
            lbl_thumb.bind("<Button-1>", open_file)
            lbl_name.bind("<Button-1>", open_file)
            lbl_path.bind("<Button-1>", open_file)
            
            # Generate thumbnail in background thread
            threading.Thread(target=self.load_thumb, args=(path, lbl_thumb), daemon=True).start()

    def load_thumb(self, path, label):
        photo = self.thumb_gen.get_thumbnail(path)
        if photo:
            self.controller.after(0, lambda: label.configure(image=photo, text=""))
            # Keep reference to avoid GC
            label.image = photo

class OrganizerFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.source_dir = ""
        self.all_files = []
        self.filtered_files = []
        self.check_vars = {}
        self.tg_manager = None
        self.setup_ui()

    def setup_ui(self):
        # Top Browse
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Browse Folder", command=self.browse_folder).pack(side=tk.LEFT, padx=5)
        self.lbl_path = ttk.Label(top, text="No folder selected", foreground="gray")
        self.lbl_path.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        # Search
        search_f = ttk.Frame(self, padding=10)
        search_f.pack(fill=tk.X)
        ttk.Label(search_f, text="Search:").pack(side=tk.LEFT, padx=5)
        self.entry_search = ttk.Entry(search_f)
        self.entry_search.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.entry_search.bind("<KeyRelease>", lambda e: self.filter_files())
        ttk.Button(search_f, text="Clear", command=self.clear_search).pack(side=tk.LEFT, padx=5)

        # Controls
        ctrl = ttk.Frame(self, padding=5)
        ctrl.pack(fill=tk.X)
        ttk.Button(ctrl, text="Select All Visible", command=self.select_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="Deselect All Visible", command=self.deselect_all).pack(side=tk.LEFT, padx=5)

        # List
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: bind_mouse_wheel(self.canvas))

        # Progress
        self.progress_frame = ttk.Frame(self, padding=10)
        self.progress_frame.pack(fill=tk.X)
        self.lbl_overall = ttk.Label(self.progress_frame, text="Overall Progress: 0/0")
        self.lbl_overall.pack(fill=tk.X)
        self.overall_bar = ttk.Progressbar(self.progress_frame, mode='determinate')
        self.overall_bar.pack(fill=tk.X, pady=2)
        self.lbl_file = ttk.Label(self.progress_frame, text="Current File: Ready")
        self.lbl_file.pack(fill=tk.X)
        self.file_bar = ttk.Progressbar(self.progress_frame, mode='determinate')
        self.file_bar.pack(fill=tk.X, pady=2)
        self.lbl_stats = ttk.Label(self.progress_frame, text="Speed: 0 KB/s", font=('Segoe UI', 8, 'italic'))
        self.lbl_stats.pack(fill=tk.X)

        # Actions
        bot = ttk.Frame(self, padding=10)
        bot.pack(fill=tk.X)
        self.btn_cloud = ttk.Button(bot, text="Upload to Telegram", command=self.upload_to_cloud)
        self.btn_cloud.pack(side=tk.LEFT, padx=5)
        ttk.Button(bot, text="Move to New Folder", command=self.move_to_new).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bot, text="Move to Existing Folder", command=self.move_to_existing).pack(side=tk.RIGHT, padx=5)

    def browse_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.source_dir = d
            self.lbl_path.config(text=d, foreground="black")
            self.scan_files()

    def scan_files(self):
        if not self.source_dir: return
        self.all_files = []
        try:
            for root, _, files in os.walk(self.source_dir):
                for f in files:
                    if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                        self.all_files.append(os.path.relpath(os.path.join(root, f), self.source_dir))
            self.all_files.sort()
            self.filter_files()
        except Exception as e: messagebox.showerror("Error", str(e))

    def filter_files(self):
        term = self.entry_search.get().lower()
        self.filtered_files = [f for f in self.all_files if term in f.lower()]
        for w in self.scrollable_frame.winfo_children(): w.destroy()
        self.check_vars = {}
        for rel in self.filtered_files:
            var = tk.BooleanVar()
            self.check_vars[rel] = var
            ttk.Checkbutton(self.scrollable_frame, text=rel, variable=var).pack(anchor="w", padx=5, pady=2)

    def clear_search(self):
        self.entry_search.delete(0, tk.END)
        self.filter_files()

    def select_all(self):
        for v in self.check_vars.values(): v.set(True)

    def deselect_all(self):
        for v in self.check_vars.values(): v.set(False)

    def upload_to_cloud(self):
        if not API_ID: return messagebox.showerror("Error", "Check .env for TG_API_ID")
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel: return messagebox.showwarning("Warning", "Select files first")
        if not self.tg_manager: self.tg_manager = TelegramManager(API_ID, API_HASH)
        
        full_paths = [os.path.join(self.source_dir, p) for p in sel]
        self.btn_cloud.config(state=tk.DISABLED)
        
        self.tg_manager.upload_files(
            full_paths,
            lambda: self.controller.ask_string_threadsafe("Login", "Phone:"),
            lambda: self.controller.ask_string_threadsafe("Login", "Code:"),
            lambda: self.controller.ask_string_threadsafe("Login", "2FA:"),
            lambda s, t, sp, fn: self.after(0, lambda: self._up_f(s, t, sp, fn)),
            lambda c, t: self.after(0, lambda: self._up_o(c, t)),
            lambda count: self.after(0, lambda: self._up_done(count)),
            lambda err: self.after(0, lambda: self._up_err(err))
        )

    def _up_f(self, s, t, sp, fn):
        p = (s/t)*100 if t>0 else 0
        self.file_bar['value'] = p
        self.lbl_file.config(text=f"File: {fn}")
        speed = f"{sp/(1024*1024):.2f} MB/s" if sp > 1024*1024 else f"{sp/1024:.2f} KB/s"
        self.lbl_stats.config(text=f"Speed: {speed} | {p:.1f}%")

    def _up_o(self, c, t):
        self.overall_bar['value'] = (c/t)*100
        self.lbl_overall.config(text=f"Overall: {c}/{t}")

    def _up_done(self, count):
        self.btn_cloud.config(state=tk.NORMAL)
        messagebox.showinfo("Success", f"Uploaded {count} files")

    def _up_err(self, err):
        self.btn_cloud.config(state=tk.NORMAL)
        messagebox.showerror("Error", err)

    def move_to_new(self):
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel: return
        n = simpledialog.askstring("New", "Folder Name:")
        if not n: return
        t = os.path.join(self.source_dir, n)
        if not os.path.exists(t): os.makedirs(t)
        self.perf_move(sel, t)

    def move_to_existing(self):
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel: return
        t = filedialog.askdirectory(initialdir=self.source_dir)
        if t: self.perf_move(sel, t)

    def perf_move(self, sel, target):
        c = 0
        for p in sel:
            src, dst = os.path.join(self.source_dir, p), os.path.join(target, os.path.basename(p))
            if os.path.exists(dst) and not messagebox.askyesno("Overwrite", f"Overwrite {os.path.basename(p)}?"): continue
            shutil.move(src, dst); c += 1
        messagebox.showinfo("Done", f"Moved {c} files"); self.scan_files()

class SettingsFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.setup_ui()

    def setup_ui(self):
        ttk.Label(self, text="Watched Directories", font=("Segoe UI", 12, "bold")).pack(pady=10)
        
        list_frame = ttk.Frame(self)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20)
        
        self.dir_listbox = tk.Listbox(list_frame, font=("Segoe UI", 10))
        self.dir_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        sb = ttk.Scrollbar(list_frame, command=self.dir_listbox.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.dir_listbox.config(yscrollcommand=sb.set)
        
        btn_frame = ttk.Frame(self, padding=10)
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Add Directory", command=self.add_dir).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="Remove Selected", command=self.remove_dir).pack(side=tk.LEFT, padx=5)
        
        self.load_list()

    def load_list(self):
        self.dir_listbox.delete(0, tk.END)
        config = ConfigManager.load()
        for d in config["directories"]:
            self.dir_listbox.insert(tk.END, d)

    def add_dir(self):
        d = filedialog.askdirectory()
        if d:
            config = ConfigManager.load()
            if d not in config["directories"]:
                config["directories"].append(d)
                ConfigManager.save(config)
                self.load_list()

    def remove_dir(self):
        sel = self.dir_listbox.curselection()
        if sel:
            d = self.dir_listbox.get(sel)
            config = ConfigManager.load()
            config["directories"].remove(d)
            ConfigManager.save(config)
            self.load_list()

# --- Main App Shell ---

class MediaSuggesterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Offline Media Suggester")
        self.geometry("1100x800")
        
        # Styles
        style = ttk.Style()
        style.configure("Sidebar.TFrame", background="#2c3e50")
        style.configure("Sidebar.TButton", padding=10, width=20)
        
        # Main Layout
        self.sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=200)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)

        self.main_container = ttk.Frame(self)
        self.main_container.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        self.frames = {}
        for F in (HomeFrame, OrganizerFrame, SettingsFrame):
            frame = F(self.main_container, self)
            self.frames[F] = frame
            frame.grid(row=0, column=0, sticky="nsew")

        self.main_container.grid_rowconfigure(0, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)

        # Sidebar Buttons
        ttk.Label(self.sidebar, text="MEDIA APP", foreground="white", font=("Segoe UI", 12, "bold"), padding=20).pack()
        ttk.Button(self.sidebar, text="🏠 Home", style="Sidebar.TButton", command=lambda: self.show_frame(HomeFrame)).pack(pady=5)
        ttk.Button(self.sidebar, text="📁 Organizer", style="Sidebar.TButton", command=lambda: self.show_frame(OrganizerFrame)).pack(pady=5)
        ttk.Button(self.sidebar, text="⚙ Settings", style="Sidebar.TButton", command=lambda: self.show_frame(SettingsFrame)).pack(pady=5)

        self.show_frame(HomeFrame)

    def show_frame(self, context):
        frame = self.frames[context]
        frame.tkraise()
        if context == HomeFrame:
            frame.refresh_suggestions()

    def ask_string_threadsafe(self, title, prompt):
        res_queue = queue.Queue()
        self.after(0, lambda: res_queue.put(simpledialog.askstring(title, prompt)))
        return res_queue.get()

if __name__ == "__main__":
    app = MediaSuggesterApp()
    app.mainloop()
