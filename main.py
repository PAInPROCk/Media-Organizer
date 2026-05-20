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
# Silence internal Telethon noise to focus on speed
logging.getLogger('telethon').setLevel(logging.WARNING)
logger = logging.getLogger("MediaOrganizer")

# --- Telegram Configuration ---
# Credentials are now loaded from a .env file (not committed to GitHub)
API_ID = os.getenv('TG_API_ID', '')
API_HASH = os.getenv('TG_API_HASH', '')

class TelegramManager:
    """Handles Telegram operations in a separate thread to keep the UI responsive."""
    def __init__(self, api_id, api_hash):
        self.api_id = api_id
        self.api_hash = api_hash
        self.client = None
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_event_loop, daemon=True)
        logger.info("Initializing Telegram Manager thread...")
        self.thread.start()

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        logger.info("Event loop started.")
        self.loop.run_forever()

    async def _init_client(self, phone_cb, code_cb, pass_cb):
        if not self.client:
            logger.info("Creating new TelegramClient (TcpAbridged)...")
            self.client = TelegramClient(
                'media_organizer_session', 
                self.api_id, 
                self.api_hash,
                connection=ConnectionTcpAbridged
            )
            await self.client.start(
                phone=phone_cb,
                code_callback=code_cb,
                password=pass_cb
            )
            logger.info(f"Connected to DC: {self.client.session.dc_id}")

    async def _fast_upload(self, file_path, progress_cb, filename):
        """Stable high-speed upload using a focused Worker Pool."""
        file_size = os.path.getsize(file_path)
        
        # Optimal part size (512KB for files > 10MB)
        if file_size <= 10 * 1024 * 1024:
            part_size = 128 * 1024
        else:
            part_size = 512 * 1024
            
        parts_count = math.ceil(file_size / part_size)
        file_id = helpers.generate_random_long()
        is_big = file_size > 10 * 1024 * 1024
        
        logger.info(f"Starting Upload: {filename} ({file_size / (1024*1024):.2f} MB)")

        start_time = time.time()
        sent_bytes = 0
        lock = asyncio.Lock()
        
        part_queue = asyncio.Queue()
        for i in range(parts_count):
            await part_queue.put(i)

        # Worker count (4 is more stable for high latency / DC5 connections)
        worker_count = 4
        
        async def upload_worker(worker_id):
            nonlocal sent_bytes
            sender = None
            try:
                # Slot 0 uses main client, others borrow
                if worker_id == 0:
                    sender = self.client._sender
                else:
                    try:
                        sender = await self.client._borrow_exported_sender(self.client.session.dc_id)
                    except:
                        sender = self.client._sender

                while True:
                    try:
                        part_index = part_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    
                    with open(file_path, 'rb') as f:
                        f.seek(part_index * part_size)
                        data = f.read(part_size)
                    
                    try:
                        if is_big:
                            req = functions.upload.SaveBigFilePartRequest(file_id, part_index, parts_count, data)
                        else:
                            req = functions.upload.SaveFilePartRequest(file_id, part_index, data)
                        
                        if sender == self.client._sender:
                            await self.client(req)
                        else:
                            await sender.send(req)
                        
                        async with lock:
                            sent_bytes += len(data)
                            elapsed = time.time() - start_time
                            speed = sent_bytes / elapsed if elapsed > 0 else 0
                            progress_cb(sent_bytes, file_size, speed, filename)
                    except Exception as e:
                        # logger.warning(f"Part {part_index} retry due to: {e}")
                        await part_queue.put(part_index)
                        await asyncio.sleep(1) # Backoff
                    finally:
                        part_queue.task_done()
            finally:
                if sender and sender != self.client._sender:
                    try: await self.client._return_exported_sender(sender)
                    except: pass

        # Start workers
        workers = [asyncio.create_task(upload_worker(i)) for i in range(worker_count)]
        await part_queue.join()
        for w in workers: w.cancel()
        
        total_time = time.time() - start_time
        avg_speed = (file_size / total_time) / (1024*1024) if total_time > 0 else 0
        logger.info(f"Upload Complete! Avg Speed: {avg_speed:.2f} MB/s")

        if is_big: return types.InputFileBig(file_id, parts_count, filename)
        else: return types.InputFile(file_id, parts_count, filename, "")

    def upload_files(self, file_paths, phone_cb, code_cb, pass_cb, file_progress_cb, overall_progress_cb, done_cb, error_cb):
        async def _upload():
            try:
                await self._init_client(phone_cb, code_cb, pass_cb)
                total_files = len(file_paths)
                for i, path in enumerate(file_paths):
                    file_name = os.path.basename(path)
                    logger.info(f"Processing file {i+1}/{total_files}: {file_name}")
                    
                    input_file = await self._fast_upload(path, file_progress_cb, file_name)
                    
                    logger.info("Registering file with Telegram...")
                    await self.client.send_file('me', input_file)
                    
                    overall_progress_cb(i + 1, total_files)
                
                logger.info("All files uploaded successfully.")
                done_cb(total_files)
            except Exception as e:
                logger.error(f"Upload session failed: {e}")
                error_cb(str(e))

        asyncio.run_coroutine_threadsafe(_upload(), self.loop)

class MediaOrganizerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Media Organizer + Cloud")
        self.root.geometry("800x800")
        self.root.minsize(600, 650)

        # Configuration
        self.media_extensions = {
            # Videos
            '.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm',
            # Pictures
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp'
        }

        # State
        self.source_dir = ""
        self.all_files = [] 
        self.filtered_files = [] 
        self.check_vars = {} 
        self.tg_manager = None

        self.setup_ui()

    def setup_ui(self):
        # --- Top Section ---
        top_frame = ttk.Frame(self.root, padding="10")
        top_frame.pack(fill=tk.X)
        ttk.Button(top_frame, text="Browse Folder", command=self.browse_folder).pack(side=tk.LEFT, padx=5)
        self.lbl_path = ttk.Label(top_frame, text="No folder selected", foreground="gray")
        self.lbl_path.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        # --- Search ---
        search_frame = ttk.Frame(self.root, padding="10")
        search_frame.pack(fill=tk.X)
        ttk.Label(search_frame, text="Search:").pack(side=tk.LEFT, padx=5)
        self.entry_search = ttk.Entry(search_frame)
        self.entry_search.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.entry_search.bind("<KeyRelease>", lambda e: self.filter_files())
        ttk.Button(search_frame, text="Clear", command=self.clear_search).pack(side=tk.LEFT, padx=5)

        # --- Selection ---
        ctrl_frame = ttk.Frame(self.root, padding="5 10")
        ctrl_frame.pack(fill=tk.X)
        ttk.Button(ctrl_frame, text="Select All Visible", command=self.select_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl_frame, text="Deselect All Visible", command=self.deselect_all).pack(side=tk.LEFT, padx=5)

        # --- List ---
        list_container = ttk.Frame(self.root, padding="10")
        list_container.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Canvas(list_container, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # --- Progress Section (Always present but updated dynamically) ---
        self.progress_frame = ttk.Frame(self.root, padding="10")
        self.progress_frame.pack(fill=tk.X)
        
        # Overall Progress
        self.lbl_overall = ttk.Label(self.progress_frame, text="Overall Progress: 0/0", font=('Segoe UI', 9, 'bold'))
        self.lbl_overall.pack(fill=tk.X)
        self.overall_bar = ttk.Progressbar(self.progress_frame, mode='determinate')
        self.overall_bar.pack(fill=tk.X, pady=(2, 10))

        # Current File Progress
        self.lbl_file = ttk.Label(self.progress_frame, text="Current File: Ready")
        self.lbl_file.pack(fill=tk.X)
        self.file_bar = ttk.Progressbar(self.progress_frame, mode='determinate')
        self.file_bar.pack(fill=tk.X, pady=(2, 5))
        
        # Speed and Stats
        self.lbl_stats = ttk.Label(self.progress_frame, text="Speed: 0 KB/s | 0% completed", font=('Segoe UI', 9, 'italic'))
        self.lbl_stats.pack(fill=tk.X)

        # --- Actions ---
        bottom_frame = ttk.Frame(self.root, padding="10")
        bottom_frame.pack(fill=tk.X)
        self.btn_cloud = ttk.Button(bottom_frame, text="Upload to Telegram Cloud", command=self.upload_to_cloud)
        self.btn_cloud.pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom_frame, text="Move to New Folder", command=self.move_to_new).pack(side=tk.RIGHT, padx=5)
        ttk.Button(bottom_frame, text="Move to Existing Folder", command=self.move_to_existing).pack(side=tk.RIGHT, padx=5)

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
            for root, dirs, files in os.walk(self.source_dir):
                for item in files:
                    full_path = os.path.join(root, item)
                    rel_path = os.path.relpath(full_path, self.source_dir)
                    _, ext = os.path.splitext(item.lower())
                    if ext in self.media_extensions:
                        self.all_files.append(rel_path)
            self.all_files.sort()
            self.filter_files()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def filter_files(self):
        term = self.entry_search.get().lower()
        self.filtered_files = [f for f in self.all_files if term in f.lower()]
        self.update_list_ui()

    def clear_search(self):
        self.entry_search.delete(0, tk.END)
        self.filter_files()

    def update_list_ui(self):
        for w in self.scrollable_frame.winfo_children(): w.destroy()
        self.check_vars = {}
        for rel_path in self.filtered_files:
            var = tk.BooleanVar()
            self.check_vars[rel_path] = var
            ttk.Checkbutton(self.scrollable_frame, text=rel_path, variable=var).pack(anchor="w", padx=5, pady=2)

    def select_all(self):
        for v in self.check_vars.values(): v.set(True)

    def deselect_all(self):
        for v in self.check_vars.values(): v.set(False)

    # --- Thread-Safe Input ---
    def ask_string_threadsafe(self, title, prompt):
        res_queue = queue.Queue()
        self.root.after(0, lambda: res_queue.put(simpledialog.askstring(title, prompt)))
        return res_queue.get()

    def get_telegram_phone(self):
        return self.ask_string_threadsafe("Telegram Login", "Enter phone number (e.g. +91...):")

    def get_telegram_code(self):
        return self.ask_string_threadsafe("Telegram Login", "Enter the code from Telegram:")

    def get_telegram_password(self):
        return self.ask_string_threadsafe("Telegram Login", "Enter your 2FA Password (if any):")

    def upload_to_cloud(self):
        if API_ID == 'YOUR_API_ID':
            messagebox.showerror("Config Error", "Please set API_ID and API_HASH.")
            return

        selected = [p for p, v in self.check_vars.items() if v.get()]
        if not selected:
            messagebox.showwarning("No selection", "Please select files.")
            return

        if not self.tg_manager:
            self.tg_manager = TelegramManager(API_ID, API_HASH)

        full_paths = [os.path.join(self.source_dir, p) for p in selected]
        self.btn_cloud.config(state=tk.DISABLED)

        # Reset UI
        self.overall_bar['value'] = 0
        self.file_bar['value'] = 0
        self.lbl_overall.config(text=f"Overall Progress: 0/{len(selected)}")
        self.lbl_file.config(text="Starting upload...")
        self.lbl_stats.config(text="Speed: 0 KB/s | 0% completed")

        self.tg_manager.upload_files(
            full_paths,
            self.get_telegram_phone,
            self.get_telegram_code,
            self.get_telegram_password,
            lambda s, t, sp, fn: self.root.after(0, lambda: self._update_file_progress(s, t, sp, fn)),
            lambda current, total: self.root.after(0, lambda: self._update_overall_progress(current, total)),
            lambda count: self.root.after(0, lambda: self._upload_complete(count)),
            lambda err: self.root.after(0, lambda: self._upload_error(err))
        )

    def _update_file_progress(self, sent, total, speed, filename):
        percent = (sent / total) * 100 if total > 0 else 0
        self.file_bar['value'] = percent
        self.lbl_file.config(text=f"Current File: {filename}")
        
        # Format speed
        if speed > 1024 * 1024:
            speed_str = f"{speed / (1024 * 1024):.2f} MB/s"
        elif speed > 1024:
            speed_str = f"{speed / 1024:.2f} KB/s"
        else:
            speed_str = f"{speed:.2f} B/s"
            
        self.lbl_stats.config(text=f"Speed: {speed_str} | {percent:.1f}% of this file completed")

    def _update_overall_progress(self, current, total):
        self.overall_bar['value'] = (current / total) * 100
        self.lbl_overall.config(text=f"Overall Progress: {current}/{total}")

    def _upload_complete(self, count):
        self.btn_cloud.config(state=tk.NORMAL)
        self.lbl_file.config(text="All uploads complete!")
        self.lbl_stats.config(text="Finished.")
        messagebox.showinfo("Success", f"Uploaded {count} files to Telegram!")

    def _upload_error(self, err):
        self.btn_cloud.config(state=tk.NORMAL)
        self.lbl_file.config(text="Upload failed.")
        messagebox.showerror("Telegram Error", f"An error occurred: {err}")

    # --- Move Logic ---
    def move_to_new(self):
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel: return
        name = simpledialog.askstring("New Folder", "Folder Name:")
        if not name: return
        target = os.path.join(self.source_dir, name)
        if not os.path.exists(target): os.makedirs(target)
        self.perform_move(sel, target)

    def move_to_existing(self):
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel: return
        target = filedialog.askdirectory(initialdir=self.source_dir)
        if target: self.perform_move(sel, target)

    def perform_move(self, sel, target):
        try:
            count = 0
            for p in sel:
                src = os.path.join(self.source_dir, p)
                dst = os.path.join(target, os.path.basename(p))
                if os.path.exists(dst):
                    if not messagebox.askyesno("Exists", f"Overwrite {os.path.basename(p)}?"): continue
                shutil.move(src, dst)
                count += 1
            messagebox.showinfo("Done", f"Moved {count} files.")
            self.scan_files()
        except Exception as e: messagebox.showerror("Error", str(e))

if __name__ == "__main__":
    root = tk.Tk()
    app = MediaOrganizerApp(root)
    root.mainloop()
