import os
import sys
# --- Aggressive Library Suppression (Must be set BEFORE imports) ---
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"
os.environ["AV_LOG_FORCE_NOCOLOR"] = "1"
os.environ["AV_LOG_LEVEL"] = "quiet"
os.environ["FFREPORT"] = "level=0"

# Silence urllib3 SSL warnings for aggressive recovery
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
import hashlib
import re
import contextlib
import requests
import webbrowser
import urllib.parse
from pathlib import Path
from PIL import Image, ImageTk
from telethon import TelegramClient, types, functions, utils, helpers
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from telethon.errors import FloodWaitError
from dotenv import load_dotenv
import mutagen
from concurrent.futures import ThreadPoolExecutor
from tkinterweb import HtmlFrame
import vlc

# --- Suppression Helper ---
@contextlib.contextmanager
def suppress_stdout_stderr():
    """A context manager that redirects stdout and stderr to devnull."""
    with open(os.devnull, 'w') as fnull:
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = fnull, fnull
        try:
            yield
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

# --- Silence OpenCV Warnings ---
os.environ["OPENCV_LOG_LEVEL"] = "FATAL"
os.environ["OPENCV_VIDEOIO_PRIORITY_MSMF"] = "0"

# --- Load Environment Variables ---
load_dotenv()

# --- Logging Configuration ---
class UIHandler(logging.Handler):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        # Match the terminal format
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        
    def emit(self, record):
        try:
            msg = self.format(record)
            self.callback(msg)
        except:
            pass

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
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

# --- Configuration Manager ---
class ConfigManager:
    @staticmethod
    def load():
        defaults = {
            "directories": [], 
            "password_hash": None, 
            "favorites": [], 
            "tags": {}, 
            "terabox_cookie": "",
            "tg_api_id": "",
            "tg_api_hash": "",
            "proxy_type": "None",
            "proxy_addr": "",
            "proxy_port": "",
            "bunkr_albums": []
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    defaults.update(data)
                    return defaults
            except:
                pass
        return defaults

    @staticmethod
    def get_telegram_creds():
        """Get creds from .env (dev) or config.json (release/user)."""
        env_id = os.getenv('TG_API_ID', '')
        env_hash = os.getenv('TG_API_HASH', '')
        
        if env_id and env_hash:
            return env_id, env_hash
            
        config = ConfigManager.load()
        return config.get("tg_api_id", ""), config.get("tg_api_hash", "")

    @staticmethod
    def save(config):
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)

    @staticmethod
    def hash_password(password):
        return hashlib.sha256(password.encode()).hexdigest()

# --- UI Utilities ---
def bind_mouse_wheel(canvas):
    def on_mouse_wheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
    canvas.bind_all("<MouseWheel>", on_mouse_wheel)

# --- Load Balancer (Thumbnail Generator) ---
class ThumbnailGenerator:
    def __init__(self, controller):
        self.controller = controller
        self.cache = {}
        self.request_queue = queue.PriorityQueue()
        self.counter = 0 
        self.stop_event = threading.Event()
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        self.session = requests.Session()
        # Modern Edge/Chrome UA for better acceptance
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        })

    def _worker_loop(self):
        while not self.stop_event.is_set():
            try:
                item = self.request_queue.get(timeout=1)
                priority, count, path_or_url, label, extra_headers = item
                
                if path_or_url in self.cache:
                    photo = self.cache[path_or_url]
                else:
                    if path_or_url.startswith('http'):
                        logger.info(f"Thumb: Downloading remote {path_or_url[:50]}...")
                        photo = self._download_remote_thumbnail(path_or_url, extra_headers)
                    else:
                        logger.info(f"Thumb: Extracting local {os.path.basename(path_or_url)}")
                        photo = self._extract_thumbnail(path_or_url)
                    
                    if photo:
                        self.cache[path_or_url] = photo
                        # logger.info(f"Thumb: Successfully processed.")
                
                if photo and label.winfo_exists():
                    self.controller.after(0, lambda p=photo, l=label: self._update_ui(p, l))
                
                self.request_queue.task_done()
                time.sleep(0.02) 
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Thumb Worker Error: {e}")
                continue

    def _update_ui(self, photo, label):
        try:
            label.configure(image=photo, text="")
            label.image = photo 
        except:
            pass

    def _download_remote_thumbnail(self, url, extra_headers=None):
        # Mirror clusters
        tb_mirrors = ["dm-data.1024terabox.com", "data.1024terabox.com", "www.terabox.app", "1024tera.com"]
        bunkr_mirrors = ["static.scdn.st", "media-files.bunkr.si", "i.bunkr.su", "static.bunkr.ru", "cdn.bunkr.black", "bunkr.is", "bunkr.media", "bunkr.ph", "bunkr.ws", "bunkr.site"]
        
        def _attempt(target_url, label_str, wait=4, mirror_ref=None):
            try:
                headers = self.session.headers.copy()
                if mirror_ref is not None:
                    if mirror_ref == "": # Explicitly remove if empty
                        if "Referer" in headers: del headers["Referer"]
                    else:
                        headers["Referer"] = mirror_ref
                elif extra_headers:
                    if isinstance(extra_headers, dict): headers.update(extra_headers)
                    elif isinstance(extra_headers, str): headers["Referer"] = extra_headers
                
                resp = self.session.get(target_url, timeout=wait, headers=headers, verify=False)
                if resp.status_code == 200:
                    from io import BytesIO
                    img = Image.open(BytesIO(resp.content))
                    img.thumbnail((200, 112))
                    return ImageTk.PhotoImage(img)
            except: pass
            return None

        # 1. TeraBox Optimization: Skip blocked primary immediately
        if "dm-data.terabox.com" in url:
             parsed = urllib.parse.urlparse(url)
             for m in tb_mirrors:
                 res = _attempt(parsed._replace(netloc=m).geturl(), "TB-Mirror", wait=3)
                 if res: return res
        
        # 2. Bunkr Optimization: Aggressive mirror & dynamic referer cycling
        if "bunkr" in url or "scdn.st" in url:
            parsed = urllib.parse.urlparse(url)
            # Try original first
            res = _attempt(url, "Bunkr-Primary", wait=4)
            if res: return res
            
            # Try swapping across all mirrors with dynamic referers
            for m in bunkr_mirrors:
                if m in parsed.netloc: continue
                m_root = "https://" + ".".join(m.split(".")[-2:]) + "/"
                # Try with root referer
                res = _attempt(parsed._replace(netloc=m).geturl(), "Bunkr-Mirror", wait=3, mirror_ref=m_root)
                if res: return res
                # Try without referer
                res = _attempt(parsed._replace(netloc=m).geturl(), "Bunkr-Mirror-NoRef", wait=3, mirror_ref="")
                if res: return res

        # 3. Standard attempt
        return _attempt(url, "Standard", wait=5)

    def _extract_thumbnail(self, file_path, size=(200, 112)):
        try:
            ext = os.path.splitext(file_path)[1].lower()
            if ext in {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}:
                img = Image.open(file_path)
                img.thumbnail(size)
                return ImageTk.PhotoImage(img)
            else:
                with suppress_stdout_stderr():
                    cap = cv2.VideoCapture(file_path)
                    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, total_frames // 10))
                    ret, frame = cap.read()
                    cap.release()
                    if ret:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        img = Image.fromarray(frame)
                        img.thumbnail(size)
                        return ImageTk.PhotoImage(img)
        except:
            pass
        return None

    def queue_thumbnail(self, path_or_url, label, priority=10, extra_headers=None):
        self.counter += 1
        self.request_queue.put((priority, self.counter, path_or_url, label, extra_headers))

# --- Tag Engine ---
class TagEngine:
    @staticmethod
    def detect_tags(file_path):
        tags = []
        try:
            # mutagen.File auto-detects format
            m = mutagen.File(file_path)
            if m:
                # Try common tag keys across formats
                for key in m.keys():
                    # Look for keys containing 'tag', 'keyword', 'genre', 'comment'
                    k = key.lower()
                    if any(x in k for x in ['tag', 'keyword', 'genre', 'comment', 'subject', 'category']):
                        val = m[key]
                        if isinstance(val, list):
                            tags.extend([str(v) for v in val])
                        else:
                            tags.append(str(val))
        except:
            pass
        return list(set([t.strip() for t in tags if t.strip() and len(t.strip()) < 50]))

# --- Telegram Manager ---
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

    async def _init_client(self, ph, co, pw):
        if not self.client:
            self.client = TelegramClient('media_organizer_session', self.api_id, self.api_hash, connection=ConnectionTcpAbridged)
            await self.client.start(phone=ph, code_callback=co, password=pw)

    async def _fast_upload(self, path, progress_cb, filename):
        size = os.path.getsize(path)
        # 512KB is generally optimal for Telegram
        ps = 512 * 1024 if size > 10 * 1024 * 1024 else 128 * 1024
        pc = math.ceil(size / ps)
        fid = helpers.generate_random_long()
        big = size > 10 * 1024 * 1024
        st = time.time()
        sb = 0
        last_update = 0
        lock = asyncio.Lock()
        q = asyncio.Queue()
        for i in range(pc):
            await q.put(i)

        # Open file once to avoid repeated overhead
        fp = open(path, 'rb')

        async def worker(wid):
            nonlocal sb, last_update
            s = None
            # Only attempt to borrow senders for additional workers
            if wid > 0:
                try:
                    # Some DCs do not allow exporting to themselves.
                    # We catch the specific error and fallback to the main client.
                    s = await self.client._borrow_exported_sender(self.client.session.dc_id)
                except Exception as e:
                    # If export fails, this worker will share the main client connection.
                    # Telethon's main client is thread-safe and has internal locks.
                    pass
            
            try:
                while True:
                    try:
                        idx = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    
                    async with lock:
                        fp.seek(idx * ps)
                        data = fp.read(ps)
                    
                    try:
                        if big:
                            req = functions.upload.SaveBigFilePartRequest(fid, idx, pc, data)
                        else:
                            req = functions.upload.SaveFilePartRequest(fid, idx, data)
                        
                        if s:
                            await s.send(req)
                        else:
                            # Using self.client(req) is the most reliable way when export fails.
                            await self.client(req)
                        
                        async with lock:
                            sb += len(data)
                            now = time.time()
                            if now - last_update > 0.5 or sb == size:
                                el = now - st
                                sp = sb / el if el > 0 else 0
                                progress_cb(sb, size, sp, filename)
                                last_update = now
                    except FloodWaitError as fe:
                        await asyncio.sleep(fe.seconds)
                        await q.put(idx)
                    except Exception as e:
                        logger.error(f"Upload error at part {idx}: {e}")
                        await q.put(idx)
                        await asyncio.sleep(1)
                    finally:
                        q.task_done()
            finally:
                if s:
                    try:
                        await self.client._return_exported_sender(s)
                    except:
                        pass

        # Use 4 workers for stability; 8+ often triggers flood waits on many connections
        tasks = [asyncio.create_task(worker(i)) for i in range(4)]
        try:
            await q.join()
        finally:
            for t in tasks:
                t.cancel()
            # Wait for tasks to finish cancellation to avoid 'Task was destroyed' warnings
            await asyncio.gather(*tasks, return_exceptions=True)
            fp.close()
        
        if big:
            return types.InputFileBig(fid, pc, filename)
        else:
            return types.InputFile(fid, pc, filename, "")

    def upload_files(self, paths, ph, co, pw, fp, op, done, err):
        async def _upload():
            try:
                await self._init_client(ph, co, pw)
                for i, p in enumerate(paths):
                    inf = await self._fast_upload(p, fp, os.path.basename(p))
                    await self.client.send_file('me', inf)
                    op(i + 1, len(paths))
                done(len(paths))
            except Exception as e:
                err(str(e))
        asyncio.run_coroutine_threadsafe(_upload(), self.loop)

# --- Base Media Grid Frame ---
class MediaGridFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.setup_ui()

    def setup_ui(self, title="Media Grid"):
        self.header = ttk.Frame(self, padding=10)
        self.header.pack(fill=tk.X)
        self.lbl_title = ttk.Label(self.header, text=title, font=("Segoe UI", 14, "bold"))
        self.lbl_title.pack(side=tk.LEFT)
        
        self.canvas = tk.Canvas(self, highlightthickness=0, bg="#f0f0f0")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: bind_mouse_wheel(self.canvas))

    def create_card(self, i, path, config, cols=4, click_callback=None):
        card = ttk.Frame(self.scrollable_frame, padding=5)
        card.grid(row=i//cols, column=i%cols, padx=10, pady=10, sticky="n")
        
        lbl_thumb = ttk.Label(card, text="Loading...", cursor="hand2")
        lbl_thumb.pack()
        
        info = ttk.Frame(card)
        info.pack(fill=tk.X, pady=(5,0))
        
        name = os.path.basename(path)
        dname = name[:20] + "..." if len(name) > 23 else name
        lbl_name = ttk.Label(info, text=dname, font=("Segoe UI", 9, "bold"), cursor="hand2")
        lbl_name.pack(side=tk.LEFT)
        
        fav_text = "⭐" if path in config["favorites"] else "☆"
        btn_fav = tk.Label(info, text=fav_text, foreground="gold", cursor="hand2")
        btn_fav.pack(side=tk.RIGHT, padx=5)
        btn_fav.bind("<Button-1>", lambda e, p=path, b=btn_fav: self.toggle_favorite(p, b))
        
        lbl_path = ttk.Label(card, text=os.path.dirname(path), font=("Segoe UI", 7), foreground="gray", wraplength=180, cursor="hand2")
        lbl_path.pack()

        def on_click(e, p=path):
            if click_callback:
                click_callback(p)
            else:
                try:
                    os.startfile(p)
                except Exception as ex:
                    messagebox.showerror("Error", str(ex))
        
        def on_double_click(e, p=path):
            try:
                os.startfile(p)
            except Exception as ex:
                messagebox.showerror("Error", str(ex))

        for w in (lbl_thumb, lbl_name, lbl_path):
            w.bind("<Button-1>", on_click)
            w.bind("<Double-Button-1>", on_double_click)
            
        ext = os.path.splitext(path)[1].lower()
        if ext in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm'}:
            btns = ttk.Frame(card)
            btns.pack(pady=2)
            ttk.Button(btns, text="System", width=8, command=lambda p=path: os.startfile(p)).pack(side=tk.LEFT, padx=2)
            # Pass the raw OS path directly to the Embedded VLC player
            ttk.Button(btns, text="In-App", width=8, command=lambda p=path, n=name: self.controller.play_in_app(p, n)).pack(side=tk.LEFT, padx=2)

        self.controller.thumb_gen.queue_thumbnail(path, lbl_thumb, priority=1)

    def toggle_favorite(self, path, label):
        config = ConfigManager.load()
        if path in config["favorites"]:
            config["favorites"].remove(path)
            label.config(text="☆")
        else:
            config["favorites"].append(path)
            label.config(text="⭐")
        ConfigManager.save(config)
        
        if isinstance(self, FavoritesFrame) or (hasattr(self, "current_tag") and self.current_tag):
             if hasattr(self, "refresh_suggestions"):
                 self.refresh_suggestions()
             elif hasattr(self, "refresh_tag_media"):
                 self.refresh_tag_media()

# --- Page Frames ---

# --- TeraBox Manager ---
# --- Bunkr Manager ---
class BunkrManager:
    def __init__(self):
        self.session = requests.Session()
        # Add retries for 503 and other temporary errors
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
        })

    def list_album_files(self, album_url):
        try:
            # Increased timeout to 45s for very slow mirrors
            resp = self.session.get(album_url, timeout=45)
            if resp.status_code != 200: 
                logger.error(f"Bunkr Album HTTP {resp.status_code}")
                return [{"name": f"Error {resp.status_code}: Mirror Busy", "url": album_url, "error": True}]
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            files = []
            items = soup.find_all('div', class_='theItem') or \
                    soup.find_all('div', class_='grid-images_box') or \
                    soup.find_all('div', class_='box')
            
            for item in items:
                link_tag = item.find('a')
                name_tag = item.find('p') or item.find('span', class_='name')
                name = ""
                if name_tag:
                    name = name_tag.text.strip()
                elif link_tag and link_tag.has_attr('title'):
                    name = link_tag['title']
                
                if link_tag and link_tag.has_attr('href'):
                    file_url = link_tag['href']
                    if not file_url.startswith('http'):
                        file_url = urllib.parse.urljoin(album_url, file_url)
                        
                    thumb_tag = item.find('img')
                    thumb = None
                    if thumb_tag:
                        thumb = thumb_tag.get('data-src') or thumb_tag.get('src')
                        # Skip placeholders
                        if thumb and ('video.svg' in thumb or 'image.svg' in thumb):
                            thumb = None
                    
                    if thumb and not thumb.startswith('http'):
                        thumb = urllib.parse.urljoin(album_url, thumb)

                    if not name: name = os.path.basename(file_url)

                    files.append({
                        "name": name,
                        "url": file_url,
                        "thumb": thumb,
                        "referer": album_url
                    })
            return files
        except requests.exceptions.RetryError:
            logger.error(f"Bunkr Timeout: {album_url} after retries")
            return [{"name": "Error: Mirror Offline/Timed Out", "url": album_url, "error": True}]
        except Exception as e:
            logger.error(f"Bunkr Scrape Error: {e}")
            return []

    def get_direct_link(self, file_page_url):
        try:
            # Essential to set Referer for the file page request
            ref = "/".join(file_page_url.split("/")[:3]) + "/"
            headers = {"Referer": ref}
            resp = self.session.get(file_page_url, timeout=15, headers=headers)
            if resp.status_code != 200: return None
            
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # 1. Look for explicit Download link (common in newer mirrors)
            dl_btn = soup.find('a', string=re.compile(r'Download', re.I)) or \
                     soup.find('a', class_=re.compile(r'btn|download|ic-download', re.I)) or \
                     soup.find('a', href=re.compile(r'cdn', re.I))
            
            if dl_btn and dl_btn.has_attr('href'):
                href = dl_btn['href']
                if not href.startswith('http'): href = urllib.parse.urljoin(file_page_url, href)
                if 'cdn' in href: return href
            
            # 2. Check for <video> sources
            video = soup.find('video')
            if video:
                # Try sources
                sources = video.find_all('source')
                for s in sources:
                    if s.has_attr('src'):
                        src = s['src']
                        if not src.startswith('http'): src = urllib.parse.urljoin(file_page_url, src)
                        return src
                if video.has_attr('src'):
                    src = video['src']
                    if not src.startswith('http'): src = urllib.parse.urljoin(file_page_url, src)
                    return src
            
            # 3. Check for specific CDN link patterns in scripts or text
            # Often links look like https://cdn[0-9].bunkr.si/...
            match = re.search(r'https?://[a-zA-Z0-9.-]+\.bunkr\.[a-z0-9]+/([a-zA-Z0-9._\-/]+)', resp.text)
            if match:
                found = match.group(0)
                if any(ext in found.lower() for ext in MEDIA_EXTENSIONS):
                    return found
            
            # 4. JSON / Script extraction (mirror specific)
            scripts = soup.find_all('script')
            for s in scripts:
                if s.string:
                    # Look for URLs in JSON-like structures (props, file, source)
                    m = re.search(r'\"(https?://[^\"]+cdn[^\"]+)\"', s.string)
                    if m: return m.group(1).replace('\\/', '/')
                    
                    # Pattern for "file":"..."
                    m2 = re.search(r'\"file\"\s*:\s*\"([^\"]+)\"', s.string)
                    if m2:
                        found = m2.group(1).replace('\\/', '/')
                        if not found.startswith('http'): found = urllib.parse.urljoin(file_page_url, found)
                        return found

            return None
        except Exception as e:
            logger.error(f"Direct link error: {e}")
            return None

class BunkrFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.bunkr_manager = BunkrManager()
        self.setup_ui()

    def setup_ui(self):
        h = ttk.Frame(self, padding=10)
        h.pack(fill=tk.X)
        ttk.Label(h, text="Bunkr Albums", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        
        self.btn_manage = ttk.Button(h, text="Manage Albums", command=self.manage_albums)
        self.btn_manage.pack(side=tk.RIGHT, padx=10)
        
        ttk.Button(h, text="Refresh All", command=self.refresh_suggestions).pack(side=tk.RIGHT)
        
        self.grid_f = MediaGridFrame(self, self.controller)
        self.grid_f.pack(fill=tk.BOTH, expand=True)

    def manage_albums(self):
        config = ConfigManager.load()
        albums = config.get("bunkr_albums", [])
        
        d = tk.Toplevel(self)
        d.title("Manage Bunkr Albums")
        d.geometry("500x400")
        d.transient(self)
        d.grab_set()

        ttk.Label(d, text="Album URLs (one per line):").pack(padx=10, pady=5, anchor="w")
        t = tk.Text(d, height=15)
        t.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        t.insert("1.0", "\n".join(albums))

        def save():
            raw = t.get("1.0", tk.END).strip()
            new_list = [l.strip() for l in raw.split("\n") if l.strip()]
            config["bunkr_albums"] = new_list
            ConfigManager.save(config)
            d.destroy()
            self.refresh_suggestions()

        ttk.Button(d, text="Save & Reload", command=save).pack(pady=10)

    def refresh_suggestions(self):
        config = ConfigManager.load()
        albums = config.get("bunkr_albums", [])
        
        for w in self.grid_f.scrollable_frame.winfo_children():
            w.destroy()

        if not albums:
            ttk.Label(self.grid_f.scrollable_frame, text="No Bunkr albums added yet. Click 'Manage Albums' to add some.", padding=20).pack()
            return

        ttk.Label(self.grid_f.scrollable_frame, text="Fetching album contents...").pack(pady=20)

        def _bg_load():
            all_files = []
            for url in albums:
                files = self.bunkr_manager.list_album_files(url)
                all_files.extend(files)
            
            self.after(0, lambda: self._display_files(all_files))

        threading.Thread(target=_bg_load, daemon=True).start()

    def _display_files(self, all_files):
        for w in self.grid_f.scrollable_frame.winfo_children():
            w.destroy()

        if not all_files:
            ttk.Label(self.grid_f.scrollable_frame, text="No files found in albums.", padding=20).pack()
            return

        for i, f in enumerate(all_files):
            self.create_bunkr_card(i, f)

    def create_bunkr_card(self, i, file_info):
        name = file_info.get("name", "Unknown")
        url = file_info.get("url")
        thumb_url = file_info.get("thumb")
        referer = file_info.get("referer")
        is_error = file_info.get("error", False)
        
        card = ttk.Frame(self.grid_f.scrollable_frame, padding=5)
        card.grid(row=i//4, column=i%4, padx=10, pady=10, sticky="n")
        
        icon = "⚠️" if is_error else "🎬"
        lbl_thumb = ttk.Label(card, text=icon, font=("Segoe UI", 24), cursor="hand2")
        lbl_thumb.pack()
        
        if is_error:
            lbl_name = ttk.Label(card, text=name, font=("Segoe UI", 9), foreground="red", wraplength=180)
            lbl_name.pack(pady=5)
            ttk.Button(card, text="Check Status", command=lambda: webbrowser.open("https://status.bunkr.ru/")).pack(pady=2)
            return

        if thumb_url:
             self.controller.thumb_gen.queue_thumbnail(thumb_url, lbl_thumb, priority=5, extra_headers={"Referer": referer})

        dname = name[:20] + "..." if len(name) > 23 else name
        lbl_name = ttk.Label(card, text=dname, font=("Segoe UI", 9, "bold"), wraplength=180, cursor="hand2")
        lbl_name.pack(pady=5)

        def on_click(e):
            webbrowser.open(url)

        lbl_thumb.bind("<Button-1>", on_click)
        lbl_name.bind("<Button-1>", on_click)

        ext = os.path.splitext(name)[1].lower()
        if ext in {'.mp4', '.mkv', '.avi', '.mov', '.webm'}:
            btns = ttk.Frame(card)
            btns.pack(pady=2)
            ttk.Button(btns, text="VLC", width=6, command=lambda u=url, n=name: self.play_in_embedded_vlc(u, n)).pack(side=tk.LEFT, padx=2)
            ttk.Button(btns, text="In-App", width=7, command=lambda u=url, n=name: self.controller.play_in_browser(u, n)).pack(side=tk.LEFT, padx=2)
            ttk.Button(btns, text="📥", width=3, command=lambda u=url, n=name: self.download_bunkr_file(u, n)).pack(side=tk.LEFT, padx=2)

    def download_bunkr_file(self, page_url, name):
        def _resolve():
            logger.info(f"Bunkr: Resolving download for {name}...")
            dlink = self.bunkr_manager.get_direct_link(page_url)
            if dlink:
                ref = "/".join(page_url.split("/")[:3]) + "/"
                headers = {
                    "Referer": ref,
                    "User-Agent": self.bunkr_manager.session.headers.get("User-Agent")
                }
                self.after(0, lambda: self.controller.start_cloud_download(dlink, name, headers))
            else:
                self.after(0, lambda: messagebox.showerror("Error", "Could not resolve Bunkr download link."))
        
        threading.Thread(target=_resolve, daemon=True).start()

    def play_in_embedded_vlc(self, page_url, title):
        def _resolve():
            dlink = self.bunkr_manager.get_direct_link(page_url)
            if not dlink:
                self.after(0, lambda: messagebox.showerror("Bunkr", "Could not extract direct link for VLC."))
                return
            
            ref = "/".join(page_url.split("/")[:3]) + "/"
            options = [f":http-referrer={ref}", f":http-user-agent={self.bunkr_manager.session.headers.get('User-Agent')}"]
            self.after(0, lambda: self.controller.play_in_app(dlink, title, options))

        threading.Thread(target=_resolve, daemon=True).start()

class TeraBoxManager:
    def __init__(self, cookie):
        self.cookie = cookie
        self.session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=100, pool_block=True)
        self.session.mount("https://", adapter)
        
        c_str = cookie.strip()
        if c_str and "=" not in c_str:
            c_str = f"ndus={c_str}"
            
        self.domain = "dm.1024terabox.com"
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Cookie": c_str,
            "Referer": f"https://{self.domain}/main",
            "Connection": "keep-alive"
        })

    def list_files(self, directory="/", page=1):
        domains = ["dm.1024terabox.com", "www.terabox.app", "1024tera.com", "www.terabox.com"]
        for d in domains:
            try:
                self.domain = d
                self.session.headers.update({"Referer": f"https://{d}/main"})
                base_url = f"https://{d}/api/list"
                query = (
                    f"dir={urllib.parse.quote(directory)}&"
                    f"order=time&desc=1&num=100&page={page}&"
                    "app_id=250528&web=1&channel=dubox&clienttype=0&dlink=1"
                )
                resp = self.session.get(f"{base_url}?{query}", timeout=12)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("errno") == 0: return data.get("list", [])
            except: continue
        return []

    def get_stream_link(self, path):
        """Advanced bypass: Extract the actual m3u8 stream from the web player."""
        try:
            domain = self.domain or "dm.1024terabox.com"
            url = f"https://{domain}/play/video?path={urllib.parse.quote(path)}&t=-1"
            logger.info(f"VLC: Scraping stream data from web player...")
            
            headers = self.session.headers.copy()
            headers.update({
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate"
            })
            
            resp = self.session.get(url, headers=headers, timeout=15)
            if resp.status_code == 200:
                # 1. Look for m3u8
                match = re.search(r'"(https?:[^\"]+\.m3u8[^\"]+)"', resp.text)
                if match:
                    m3u8_url = match.group(1).replace("\\/", "/")
                    logger.info("VLC: Extracted m3u8 stream!")
                    return m3u8_url
                # 2. Look for mp4
                match_mp4 = re.search(r'"(https?:[^\"]+\.mp4[^\"]+)"', resp.text)
                if match_mp4:
                    mp4_url = match_mp4.group(1).replace("\\/", "/")
                    logger.info("VLC: Extracted mp4 stream!")
                    return mp4_url
            logger.error(f"VLC: Stream extraction failed (HTTP {resp.status_code})")
        except Exception as e:
            logger.error(f"VLC: Stream bypass error: {e}")
        return None

    def get_dlink(self, fs_id):
        domains = ["dm.1024terabox.com", "www.1024tera.com", "www.terabox.app"]
        for domain in domains:
            for aid in ["250528", "7092"]:
                try:
                    url = f"https://{domain}/rest/2.0/xpan/multimedia"
                    params = {"method": "filemetas", "fsids": f"[{fs_id}]", "dlink": "1", "app_id": aid, "clienttype": "0"}
                    resp = self.session.get(url, params=params, timeout=10)
                    data = resp.json()
                    if data.get("errno") == 0 and data.get("list"):
                        dlink = data["list"][0].get("dlink")
                        if dlink: return dlink
                except: continue
        return None

    def resolve_share_link(self, share_url):
        try:
            surl = ""
            if "/s/" in share_url: surl = share_url.split("/s/")[1].split("?")[0]
            elif "surl=" in share_url: surl = share_url.split("surl=")[1].split("&")[0]
            
            for domain in ["www.terabox.app", "dm.1024terabox.com"]:
                api_url = f"https://{domain}/share/list?surl={surl}"
                resp = self.session.get(api_url, timeout=15)
                data = resp.json()
                if data.get("errno") == 0 and data.get("list"):
                    item = data["list"][0]
                    return {"dlink": item.get("dlink"), "title": item.get("server_filename"), "fs_id": item.get("fs_id")}
        except: pass
        return None

    def get_download_link(self, fs_id):
        return f"https://{self.domain}/main?category=all&path=%2F"

# --- Page Frames ---

class TeraBoxFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.current_dir = "/"
        self.page = 1
        self.tb_manager = None
        self.btn_load_more = None
        self.setup_ui()

    def setup_ui(self):
        h = ttk.Frame(self, padding=10)
        h.pack(fill=tk.X)
        ttk.Label(h, text="TeraBox Cloud", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        
        self.btn_back = ttk.Button(h, text="Back", command=self.go_back)
        self.btn_back.pack(side=tk.LEFT, padx=10)
        
        self.lbl_path = ttk.Label(h, text="/", font=("Segoe UI", 10, "italic"))
        self.lbl_path.pack(side=tk.LEFT, padx=5)

        ttk.Button(h, text="Refresh", command=self.refresh_suggestions).pack(side=tk.RIGHT)
        
        # --- NEW: Paste & Play Share Link ---
        sf = ttk.LabelFrame(self, text="Paste Share Link to Stream", padding=5)
        sf.pack(fill=tk.X, padx=10, pady=5)
        self.entry_share = ttk.Entry(sf)
        self.entry_share.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=5)
        self.entry_share.insert(0, "https://terabox.com/s/...")
        self.entry_share.bind("<FocusIn>", lambda e: self.entry_share.delete(0, tk.END) if "..." in self.entry_share.get() else None)
        ttk.Button(sf, text="▶ Stream Now", command=self.stream_share_link).pack(side=tk.LEFT, padx=5)
        
        self.grid_f = MediaGridFrame(self, self.controller)
        self.grid_f.pack(fill=tk.BOTH, expand=True)
        
        # Load More Button
        self.footer = ttk.Frame(self, padding=5)
        self.footer.pack(fill=tk.X)
        self.btn_load_more = ttk.Button(self.footer, text="Load More Files...", command=self.load_more)

    def stream_share_link(self):
        url = self.entry_share.get().strip()
        if "/s/" not in url and "surl=" not in url:
            return messagebox.showerror("Error", "Please paste a valid TeraBox share link.")
        
        config = ConfigManager.load()
        cookie = config.get("terabox_cookie", "")
        if not self.tb_manager:
            self.tb_manager = TeraBoxManager(cookie)

        def _resolve():
            logger.info("TeraBox: Attempting to bypass share link...")
            data = self.tb_manager.resolve_share_link(url)
            if data and data.get("dlink"):
                logger.info(f"TeraBox: Bypassed share link successfully: {data['title']}")
                ua = self.tb_manager.session.headers.get("User-Agent")
                # For share links, we usually don't need a specific referer other than the site root
                options = [f":http-user-agent={ua}"]
                self.after(0, lambda: self.controller.play_in_app(data["dlink"], data["title"], options))
            else:
                logger.warning("TeraBox: Bypass failed, falling back to browser player.")
                self.after(0, lambda: self.controller.play_in_browser(url, "TeraBox Shared Content"))

        threading.Thread(target=_resolve, daemon=True).start()

    def refresh_suggestions(self):
        self.page = 1
        config = ConfigManager.load()
        cookie = config.get("terabox_cookie", "")
        if not cookie:
            messagebox.showwarning("TeraBox", "Please set your TeraBox 'ndus' cookie in Settings first.")
            return

        if not self.tb_manager or self.tb_manager.cookie != cookie:
            self.tb_manager = TeraBoxManager(cookie)

        for w in self.grid_f.scrollable_frame.winfo_children():
            w.destroy()
        
        if self.btn_load_more:
            self.btn_load_more.pack_forget()

        ttk.Label(self.grid_f.scrollable_frame, text="Loading cloud files...").pack(pady=20)

        def _bg_load():
            try:
                files = self.tb_manager.list_files(self.current_dir, page=self.page)
                self.after(0, lambda: self._display_files(files, clear=True))
            except Exception as e:
                logger.error(f"TeraBox BG Load Error: {e}")
                self.after(0, lambda: messagebox.showerror("TeraBox", "Failed to load files."))

        threading.Thread(target=_bg_load, daemon=True).start()

    def load_more(self):
        self.page += 1
        if self.btn_load_more:
            self.btn_load_more.config(state=tk.DISABLED, text="Loading more...")
        
        def _bg_load():
            files = self.tb_manager.list_files(self.current_dir, page=self.page)
            self.after(0, lambda: self._display_files(files, clear=False))
            
        threading.Thread(target=_bg_load, daemon=True).start()

    def _display_files(self, files, clear=True):
        if clear:
            for w in self.grid_f.scrollable_frame.winfo_children():
                w.destroy()

        if not files and clear:
            ttk.Label(self.grid_f.scrollable_frame, text="No files found or auth error.", padding=20).pack()
            return

        # Current count for grid positioning
        start_idx = len([w for w in self.grid_f.scrollable_frame.winfo_children() if isinstance(w, ttk.Frame)])
        
        for i, f in enumerate(files):
            self.create_remote_card(start_idx + i, f)

        # Show load more if we got 100 items
        if len(files) >= 100 and self.btn_load_more:
            self.btn_load_more.pack(pady=10)
            self.btn_load_more.config(state=tk.NORMAL, text="Load More Files...")
        elif self.btn_load_more:
            self.btn_load_more.pack_forget()

    def create_remote_card(self, i, file_info):
        is_dir = file_info.get("isdir") == 1
        name = file_info.get("server_filename", "Unknown")
        path = file_info.get("path")
        # Use url3 (High Res) -> url2 -> url1
        thumbs = file_info.get("thumbs", {})
        thumb_url = thumbs.get("url3") or thumbs.get("url2") or thumbs.get("url1")
        fs_id = file_info.get("fs_id")
        dlink = file_info.get("dlink") 
        
        card = ttk.Frame(self.grid_f.scrollable_frame, padding=5)
        card.grid(row=i//4, column=i%4, padx=10, pady=10, sticky="n")
        
        icon = "📁" if is_dir else "🎬"
        lbl_thumb = ttk.Label(card, text=icon, font=("Segoe UI", 24), cursor="hand2")
        lbl_thumb.pack()
        
        if thumb_url and not is_dir and self.tb_manager:
             # TeraBox thumbnails require the ndus cookie and specific referer
             tb_headers = {
                 "Referer": f"https://{self.tb_manager.domain}/main",
                 "Cookie": self.tb_manager.session.headers.get("Cookie", "")
             }
             self.controller.thumb_gen.queue_thumbnail(thumb_url, lbl_thumb, priority=5, extra_headers=tb_headers)

        dname = name[:20] + "..." if len(name) > 23 else name
        lbl_name = ttk.Label(card, text=dname, font=("Segoe UI", 9, "bold"), wraplength=180, cursor="hand2")
        lbl_name.pack(pady=5)

        def on_click(e):
            if is_dir:
                self.current_dir = path
                self.lbl_path.config(text=path)
                self.refresh_suggestions()
            else:
                domain = self.tb_manager.domain if self.tb_manager else "dm.1024terabox.com"
                # Use the search-based URL which is confirmed to work for the user
                encoded_name = urllib.parse.quote(name)
                webbrowser.open(f"https://{domain}/main?category=all&search={encoded_name}")

        lbl_thumb.bind("<Button-1>", on_click)
        lbl_name.bind("<Button-1>", on_click)

        if not is_dir:
            ext = os.path.splitext(name)[1].lower()
            if ext in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm'}:
                # Container for buttons
                btns = ttk.Frame(card)
                btns.pack(pady=2)
                
                # Play in VLC (Embedded)
                ttk.Button(btns, text="VLC", width=6, command=lambda f=fs_id, d=dlink, n=name, p=path: self.play_in_embedded_vlc(f, d, n, p)).pack(side=tk.LEFT, padx=2)
                
                # Play In-App (Browser)
                domain = self.tb_manager.domain if self.tb_manager else "dm.1024terabox.com"
                encoded_name = urllib.parse.quote(name)
                play_url = f"https://{domain}/main?category=all&search={encoded_name}"
                ttk.Button(btns, text="In-App", width=6, command=lambda u=play_url, n=name: self.controller.play_in_browser(u, n)).pack(side=tk.LEFT, padx=2)

                # Open in PC App (using protocol handler)
                ttk.Button(btns, text="App", width=6, command=lambda n=name: self.open_in_pc_app(n)).pack(side=tk.LEFT, padx=2)
                
                # Download
                ttk.Button(btns, text="📥", width=3, command=lambda f=fs_id, d=dlink, n=name, p=path: self.download_terabox_file(f, d, n, p)).pack(side=tk.LEFT, padx=2)

    def download_terabox_file(self, fs_id, dlink=None, title="Video", path=None):
        if not self.tb_manager: return
        
        def _resolve():
            logger.info(f"TeraBox: Resolving download for {title}...")
            # Use same resolution logic as VLC
            final_link = dlink or self.tb_manager.get_dlink(fs_id)
            
            if not final_link and path:
                logger.info("TeraBox: API failed. Using Web Stream link for download...")
                domain = self.tb_manager.domain or "dm.1024terabox.com"
                final_link = f"https://{domain}/play/video?path={urllib.parse.quote(path)}&t=-1"
            
            if final_link:
                cookie = self.tb_manager.session.headers.get("Cookie", "")
                ua = self.tb_manager.session.headers.get("User-Agent", "")
                headers = {
                    "User-Agent": ua,
                    "Cookie": cookie,
                    "Referer": f"https://{self.tb_manager.domain}/main"
                }
                self.after(0, lambda: self.controller.start_cloud_download(final_link, title, headers))
            else:
                self.after(0, lambda: messagebox.showerror("Error", "Could not resolve TeraBox download link."))

        threading.Thread(target=_resolve, daemon=True).start()

    def play_in_embedded_vlc(self, fs_id, dlink=None, title="Video", path=None):
        if not self.tb_manager: return
        
        def _resolve():
            logger.info("VLC: Attempting to resolve playback link...")
            # 1. Try direct link from API first
            final_link = dlink or self.tb_manager.get_dlink(fs_id)
            
            # 2. If API fails (common), use the Web Player Scraper
            if not final_link and path:
                logger.info("VLC: API resolution failed. Scraping stream manifest...")
                final_link = self.tb_manager.get_stream_link(path)
            
            if not final_link:
                self.after(0, lambda: messagebox.showerror("TeraBox", "Could not extract direct link for VLC."))
                return

            cookie = self.tb_manager.session.headers.get("Cookie", "")
            ua = self.tb_manager.session.headers.get("User-Agent", "")
            # Mirror-specific referer is MANDATORY for TeraBox streaming
            ref = f"https://{self.tb_manager.domain}/main"
            options = [
                f":http-user-agent={ua}", 
                f":http-header=Cookie: {cookie}",
                f":http-referrer={ref}"
            ]
            
            self.after(0, lambda: self.controller.play_in_app(final_link, title, options))

        threading.Thread(target=_resolve, daemon=True).start()

    def open_in_pc_app(self, filename):
        # Most TeraBox PC versions will respond to search queries if triggered via a redirect
        # Or we can try to launch the protocol if we had a surl, but for private files
        # the best bet is the browser-to-app handoff.
        domain = self.tb_manager.domain if self.tb_manager else "dm.1024terabox.com"
        encoded_name = urllib.parse.quote(filename)
        # Opening this in browser should trigger the "Open in TeraBox App" prompt if installed
        webbrowser.open(f"https://{domain}/main?category=all&search={encoded_name}")

    def play_in_vlc(self, fs_id, dlink=None):
        if not self.tb_manager:
            return
        
        final_link = dlink or self.tb_manager.get_dlink(fs_id)
        if not final_link:
            messagebox.showerror("TeraBox", "Could not extract direct link for VLC.")
            return

        vlc_paths = [
            r"C:\Program Files\VideoLAN\VLC\vlc.exe",
            r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe"
        ]
        vlc_exe = next((p for p in vlc_paths if os.path.exists(p)), "vlc")
        
        import subprocess
        try:
            cookie = self.tb_manager.session.headers.get("Cookie", "")
            ua = self.tb_manager.session.headers.get("User-Agent", "")
            cmd = [
                vlc_exe,
                final_link,
                f"--http-user-agent={ua}",
                f"--http-header=Cookie: {cookie}"
            ]
            subprocess.Popen(cmd)
        except Exception as e:
            messagebox.showerror("VLC Error", f"Could not launch VLC: {e}")

    def go_back(self):
        if self.current_dir == "/":
            return
        parts = self.current_dir.rstrip("/").split("/")
        self.current_dir = "/".join(parts[:-1]) or "/"
        self.lbl_path.config(text=self.current_dir)
        self.refresh_suggestions()

class SplashFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.setup_ui()

    def setup_ui(self):
        c = ttk.Frame(self)
        c.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        ttk.Label(c, text="🎬", font=("Segoe UI", 48)).pack()
        ttk.Label(c, text="MEDIA SUGGESTER", font=("Segoe UI", 18, "bold")).pack(pady=10)
        self.after(2000, self.check_security)

    def check_security(self):
        config = ConfigManager.load()
        if config["password_hash"]:
            self.controller.show_frame(LoginFrame)
        else:
            self.controller.show_frame(HomeFrame)

class LoginFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.setup_ui()

    def setup_ui(self):
        c = ttk.Frame(self, padding=20)
        c.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        ttk.Label(c, text="Lock Account", font=("Segoe UI", 14, "bold")).pack(pady=(0, 20))
        self.entry_pass = ttk.Entry(c, show="*", width=30, font=("Segoe UI", 12))
        self.entry_pass.pack(pady=5)
        self.entry_pass.bind("<Return>", lambda e: self.attempt_login())
        self.entry_pass.focus()
        ttk.Button(c, text="Enter App", command=self.attempt_login, width=20).pack(pady=10)

    def attempt_login(self):
        config = ConfigManager.load()
        p = self.entry_pass.get()
        if ConfigManager.hash_password(p) == config["password_hash"]:
            self.controller.show_frame(HomeFrame)
            self.entry_pass.delete(0, tk.END)
        else:
            messagebox.showerror("Access Denied", "Incorrect password.")

class HomeFrame(MediaGridFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        self.lbl_title.config(text="Recommended for You")
        sf = ttk.Frame(self.header)
        sf.pack(side=tk.RIGHT, padx=10)
        self.btn_search = ttk.Button(sf, text="🔍", width=3, command=self.refresh_suggestions)
        self.btn_search.pack(side=tk.LEFT)
        self.entry_search = ttk.Entry(sf, width=30)
        self.entry_search.pack(side=tk.LEFT, padx=5)
        self.entry_search.bind("<KeyRelease>", lambda e: self.refresh_suggestions())
        self.btn_clear = ttk.Button(sf, text="✖", width=3, command=self.clear_search)
        self.btn_clear.pack(side=tk.LEFT)
        ttk.Button(self.header, text="Refresh Feed", command=self.refresh_suggestions).pack(side=tk.RIGHT, padx=5)

    def clear_search(self):
        self.entry_search.delete(0, tk.END)
        self.refresh_suggestions()

    def refresh_suggestions(self):
        for w in self.scrollable_frame.winfo_children():
            w.destroy()
        config = ConfigManager.load()
        all_media = []
        term = self.entry_search.get().lower()
        for d in config["directories"]:
            if os.path.exists(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                            fp = os.path.normpath(os.path.join(root, f))
                            if not term or term in f.lower():
                                all_media.append(fp)
        
        if not all_media:
            ttk.Label(self.scrollable_frame, text="No matches found.", padding=20).pack()
            return
        
        sug = all_media[:40] if term else random.sample(all_media, min(len(all_media), 20))
        for i, p in enumerate(sug):
            self.create_card(i, p, config)

class FavoritesFrame(MediaGridFrame):
    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        self.lbl_title.config(text="Your Favorites")

    def refresh_suggestions(self):
        for w in self.scrollable_frame.winfo_children():
            w.destroy()
        config = ConfigManager.load()
        favs = [p for p in config["favorites"] if os.path.exists(p)]
        if not favs:
            ttk.Label(self.scrollable_frame, text="No favorites yet.", padding=20).pack()
            return
        for i, p in enumerate(favs):
            self.create_card(i, p, config)

class TagsFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.current_tag = None
        self.selected_path = None
        self.setup_ui()

    def setup_ui(self):
        h = ttk.Frame(self, padding=10)
        h.pack(fill=tk.X)
        ttk.Label(h, text="Tags & Collections", font=("Segoe UI", 14, "bold")).pack(side=tk.LEFT)
        ttk.Button(h, text="Auto-Detect Metadata Tags", command=self.run_auto_detect).pack(side=tk.RIGHT)
        
        self.pw = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        self.pw.pack(fill=tk.BOTH, expand=True)
        
        left = ttk.Frame(self.pw, padding=10)
        self.pw.add(left, weight=1)
        ttk.Label(left, text="Collections", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tag_listbox = tk.Listbox(left, font=("Segoe UI", 10))
        self.tag_listbox.pack(fill=tk.BOTH, expand=True, pady=5)
        self.tag_listbox.bind("<<ListboxSelect>>", self.on_tag_select)
        
        cf = ttk.Frame(left)
        cf.pack(fill=tk.X)
        self.entry_new = ttk.Entry(cf)
        self.entry_new.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(cf, text="+", width=3, command=self.create_global_tag).pack(side=tk.RIGHT)
        ttk.Button(left, text="Delete Collection", command=self.delete_global_tag).pack(fill=tk.X, pady=5)
        
        rm = ttk.Frame(self.pw)
        self.pw.add(rm, weight=4)
        sf = ttk.Frame(rm, padding=5)
        sf.pack(fill=tk.X)
        ttk.Label(sf, text="Search Files:").pack(side=tk.LEFT)
        self.entry_file_search = ttk.Entry(sf, width=40)
        self.entry_file_search.pack(side=tk.LEFT, padx=5)
        self.entry_file_search.bind("<KeyRelease>", lambda e: self.refresh_tag_media())
        ttk.Button(sf, text="✖", width=3, command=self.clear_tag_search).pack(side=tk.LEFT)
        
        self.grid_f = MediaGridFrame(rm, self.controller)
        self.grid_f.pack(fill=tk.BOTH, expand=True)
        
        self.details_f = ttk.LabelFrame(rm, text="Selected File Details", padding=10)
        self.details_f.pack(fill=tk.X, padx=5, pady=5)

        sel_header = ttk.Frame(self.details_f)
        sel_header.pack(fill=tk.X)
        self.lbl_sel = ttk.Label(sel_header, text="No file selected", font=("Segoe UI", 9, "italic"))
        self.lbl_sel.pack(side=tk.LEFT, anchor="w")

        # Clear Selection Button
        self.btn_clear_sel = ttk.Button(sel_header, text="✖", width=3, command=self.clear_selection)
        self.btn_clear_sel.pack(side=tk.RIGHT)

        te = ttk.Frame(self.details_f)
        te.pack(fill=tk.X, pady=5)
        ttk.Label(te, text="Tags:").pack(side=tk.LEFT)
        self.lbl_tags = ttk.Label(te, text="", font=("Segoe UI", 9, "bold"), foreground="#2980b9")
        self.lbl_tags.pack(side=tk.LEFT, padx=10)
        
        at = ttk.Frame(self.details_f)
        at.pack(fill=tk.X)
        self.entry_add = ttk.Entry(at, width=20)
        self.entry_add.pack(side=tk.LEFT)
        ttk.Button(at, text="Add Tag", command=self.add_tag).pack(side=tk.LEFT, padx=5)
        ttk.Button(at, text="Remove Tag", command=self.remove_tag).pack(side=tk.LEFT)

    def clear_tag_search(self):
        self.entry_file_search.delete(0, tk.END)
        self.refresh_tag_media()

    def clear_selection(self):
        self.selected_path = None
        self.lbl_sel.config(text="No file selected", font=("Segoe UI", 9, "italic"))
        self.lbl_tags.config(text="")

    def load_tags(self):
        self.tag_listbox.delete(0, tk.END)
        self.tag_listbox.insert(tk.END, "[All Media]")
        cfg = ConfigManager.load()
        for t in sorted(cfg["tags"].keys()):
            self.tag_listbox.insert(tk.END, t)
        if not self.current_tag:
            self.tag_listbox.selection_set(0)
            self.current_tag = "[All Media]"
        self.refresh_tag_media()

    def on_tag_select(self, e):
        sel = self.tag_listbox.curselection()
        if not sel:
            return
        self.current_tag = self.tag_listbox.get(sel[0])
        self.refresh_tag_media()

    def refresh_tag_media(self):
        for w in self.grid_f.scrollable_frame.winfo_children():
            w.destroy()
        cfg = ConfigManager.load()
        term = self.entry_file_search.get().lower()
        if self.current_tag == "[All Media]":
            paths = []
            for d in cfg["directories"]:
                if os.path.exists(d):
                    for root, _, files in os.walk(d):
                        for f in files:
                            if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                                paths.append(os.path.join(root, f))
        else:
            paths = cfg["tags"].get(self.current_tag, [])
        
        filtered = [p for p in paths if os.path.exists(p) and (not term or term in os.path.basename(p).lower())]
        for i, p in enumerate(filtered[:50]):
            self.grid_f.create_card(i, p, cfg, click_callback=self.select_file)

    def select_file(self, p):
        self.selected_path = p
        self.lbl_sel.config(text=f"File: {os.path.basename(p)}", font=("Segoe UI", 9, "bold"))
        self.update_tags_ui()

    def update_tags_ui(self):
        if not self.selected_path:
            return
        cfg = ConfigManager.load()
        tags = [t for t, ps in cfg["tags"].items() if self.selected_path in ps]
        self.lbl_tags.config(text=", ".join(sorted(tags)) if tags else "None")

    def add_tag(self):
        if not self.selected_path:
            return messagebox.showwarning("Warning", "Select a file first.")
        t = self.entry_add.get().strip()
        if not t:
            return
        cfg = ConfigManager.load()
        if t not in cfg["tags"]:
            cfg["tags"][t] = []
        if self.selected_path not in cfg["tags"][t]:
            cfg["tags"][t].append(self.selected_path)
            ConfigManager.save(cfg)
            self.update_tags_ui()
            self.load_tags()
            self.entry_add.delete(0, tk.END)

    def remove_tag(self):
        if not self.selected_path:
            return
        t = self.entry_add.get().strip()
        if not t:
            return messagebox.showinfo("Tip", "Type the tag name to remove.")
        cfg = ConfigManager.load()
        if t in cfg["tags"] and self.selected_path in cfg["tags"][t]:
            cfg["tags"][t].remove(self.selected_path)
            ConfigManager.save(cfg)
            self.update_tags_ui()
            self.refresh_tag_media()

    def create_global_tag(self):
        n = self.entry_new.get().strip()
        if n:
            cfg = ConfigManager.load()
            if n not in cfg["tags"]:
                cfg["tags"][n] = []
                ConfigManager.save(cfg)
                self.load_tags()
            self.entry_new.delete(0, tk.END)

    def delete_global_tag(self):
        if not self.current_tag or self.current_tag == "[All Media]":
            return
        if messagebox.askyesno("Confirm", f"Delete '{self.current_tag}'?"):
            cfg = ConfigManager.load()
            if self.current_tag in cfg["tags"]:
                del cfg["tags"][self.current_tag]
                ConfigManager.save(cfg)
                self.current_tag = "[All Media]"
                self.load_tags()

    def run_auto_detect(self):
        cfg = ConfigManager.load()
        count = 0
        for d in cfg["directories"]:
            if os.path.exists(d):
                for root, _, files in os.walk(d):
                    for f in files:
                        if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                            p = os.path.join(root, f)
                            found = TagEngine.detect_tags(p)
                            for t in found:
                                if t not in cfg["tags"]:
                                    cfg["tags"][t] = []
                                if p not in cfg["tags"][t]:
                                    cfg["tags"][t].append(p)
                                    count += 1
        ConfigManager.save(cfg)
        self.load_tags()
        messagebox.showinfo("Auto-Detect", f"Associated {count} instances.")

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
        top = ttk.Frame(self, padding=10)
        top.pack(fill=tk.X)
        ttk.Button(top, text="Browse Folder", command=self.browse_folder).pack(side=tk.LEFT, padx=5)
        self.lbl_path = ttk.Label(top, text="No folder selected", foreground="gray")
        self.lbl_path.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        
        sf = ttk.Frame(self, padding=10)
        sf.pack(fill=tk.X)
        ttk.Label(sf, text="Search:").pack(side=tk.LEFT, padx=5)
        self.entry_search = ttk.Entry(sf)
        self.entry_search.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        self.entry_search.bind("<KeyRelease>", lambda e: self.filter_files())
        ttk.Button(sf, text="Clear", command=self.clear_search).pack(side=tk.LEFT, padx=5)
        
        ctrl = ttk.Frame(self, padding=5)
        ctrl.pack(fill=tk.X)
        ttk.Button(ctrl, text="Select All", command=self.select_all).pack(side=tk.LEFT, padx=5)
        ttk.Button(ctrl, text="Deselect All", command=self.deselect_all).pack(side=tk.LEFT, padx=5)
        
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Enter>", lambda e: bind_mouse_wheel(self.canvas))
        
        prog = ttk.Frame(self, padding=10)
        prog.pack(fill=tk.X)
        self.lbl_overall = ttk.Label(prog, text="Overall: 0/0")
        self.lbl_overall.pack(fill=tk.X)
        self.overall_bar = ttk.Progressbar(prog, mode='determinate')
        self.overall_bar.pack(fill=tk.X, pady=2)
        self.lbl_file = ttk.Label(prog, text="Ready")
        self.lbl_file.pack(fill=tk.X)
        self.file_bar = ttk.Progressbar(prog, mode='determinate')
        self.file_bar.pack(fill=tk.X, pady=2)
        self.lbl_stats = ttk.Label(prog, text="Speed: 0 KB/s", font=('Segoe UI', 8, 'italic'))
        self.lbl_stats.pack(fill=tk.X)
        
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
        if not self.source_dir:
            return
        self.all_files = []
        try:
            for root, _, files in os.walk(self.source_dir):
                for f in files:
                    if os.path.splitext(f)[1].lower() in MEDIA_EXTENSIONS:
                        self.all_files.append(os.path.relpath(os.path.join(root, f), self.source_dir))
            self.all_files.sort()
            self.filter_files()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def filter_files(self):
        term = self.entry_search.get().lower()
        self.filtered_files = [f for f in self.all_files if term in f.lower()]
        for w in self.scrollable_frame.winfo_children():
            w.destroy()
        self.check_vars = {}
        for rel in self.filtered_files:
            var = tk.BooleanVar()
            self.check_vars[rel] = var
            ttk.Checkbutton(self.scrollable_frame, text=rel, variable=var).pack(anchor="w", padx=5, pady=2)

    def clear_search(self):
        self.entry_search.delete(0, tk.END)
        self.filter_files()

    def select_all(self):
        for v in self.check_vars.values():
            v.set(True)

    def deselect_all(self):
        for v in self.check_vars.values():
            v.set(False)

    def upload_to_cloud(self):
        tid, thash = ConfigManager.get_telegram_creds()
        if not tid:
            return messagebox.showerror("Error", "Telegram API ID not found. Set it in Settings or .env")
        
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel:
            return messagebox.showwarning("Warning", "Select files first")
        
        if not self.tg_manager or self.tg_manager.api_id != tid:
            self.tg_manager = TelegramManager(tid, thash)
            
        fpaths = [os.path.join(self.source_dir, p) for p in sel]
        self.btn_cloud.config(state=tk.DISABLED)
        self.tg_manager.upload_files(fpaths, lambda: self.controller.ask_string_threadsafe("Login", "Phone:"), lambda: self.controller.ask_string_threadsafe("Login", "Code:"), lambda: self.controller.ask_string_threadsafe("Login", "2FA:"), lambda s, t, sp, fn: self.after(0, lambda: self._up_f(s, t, sp, fn)), lambda c, t: self.after(0, lambda: self._up_o(c, t)), lambda count: self.after(0, lambda: self._up_done(count)), lambda err: self.after(0, lambda: self._up_err(err)))

    def _up_f(self, s, t, sp, fn):
        p = (s / t) * 100 if t > 0 else 0
        self.file_bar['value'] = p
        self.lbl_file.config(text=f"File: {fn}")
        speed = f"{sp/(1024*1024):.2f} MB/s" if sp > 1024 * 1024 else f"{sp/1024:.2f} KB/s"
        self.lbl_stats.config(text=f"Speed: {speed} | {p:.1f}%")

    def _up_o(self, c, t):
        self.overall_bar['value'] = (c / t) * 100
        self.lbl_overall.config(text=f"Overall: {c}/{t}")

    def _up_done(self, count):
        self.btn_cloud.config(state=tk.NORMAL)
        messagebox.showinfo("Success", f"Uploaded {count} files")

    def _up_err(self, err):
        self.btn_cloud.config(state=tk.NORMAL)
        messagebox.showerror("Error", err)

    def move_to_new(self):
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel:
            return
        n = simpledialog.askstring("New", "Folder Name:")
        if not n:
            return
        t = os.path.join(self.source_dir, n)
        if not os.path.exists(t):
            os.makedirs(t)
        self.perf_move(sel, t)

    def move_to_existing(self):
        sel = [p for p, v in self.check_vars.items() if v.get()]
        if not sel:
            return
        t = filedialog.askdirectory(initialdir=self.source_dir)
        if t:
            self.perf_move(sel, t)

    def perf_move(self, sel, target):
        c = 0
        for p in sel:
            src, dst = os.path.join(self.source_dir, p), os.path.join(target, os.path.basename(p))
            if os.path.exists(dst) and not messagebox.askyesno("Overwrite", f"Overwrite {os.path.basename(p)}?"):
                continue
            shutil.move(src, dst)
            c += 1
        messagebox.showinfo("Done", f"Moved {c} files")
        self.scan_files()

class SettingsFrame(ttk.Frame):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.check_vars = {}
        self.setup_ui()

    def setup_ui(self):
        h = ttk.Frame(self, padding=10)
        h.pack(fill=tk.X)
        ttk.Label(h, text="Watched Directories", font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT)
        ttk.Button(h, text="Set/Change Password", command=self.set_password).pack(side=tk.RIGHT)
        
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        self.scrollable_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y, in_=self)
        self.canvas.bind("<Enter>", lambda e: bind_mouse_wheel(self.canvas))
        
        b = ttk.Frame(self, padding=10)
        b.pack(fill=tk.X)
        ttk.Button(b, text="+ Add Directory", command=self.add_dir_guarded).pack(side=tk.LEFT, padx=5)
        ttk.Button(b, text="- Remove Selected", command=self.remove_dir_guarded).pack(side=tk.LEFT, padx=5)
        
        # Telegram Config
        tg_f = ttk.LabelFrame(self, text="Telegram Configuration", padding=10)
        tg_f.pack(fill=tk.X, padx=20, pady=10)
        
        config = ConfigManager.load()
        # API ID
        rid = ttk.Frame(tg_f)
        rid.pack(fill=tk.X, pady=2)
        ttk.Label(rid, text="API ID:", width=12).pack(side=tk.LEFT)
        self.entry_tg_id = ttk.Entry(rid)
        self.entry_tg_id.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry_tg_id.insert(0, config.get("tg_api_id", ""))
        
        # API HASH
        rh = ttk.Frame(tg_f)
        rh.pack(fill=tk.X, pady=2)
        ttk.Label(rh, text="API HASH:", width=12).pack(side=tk.LEFT)
        self.entry_tg_hash = ttk.Entry(rh, show="*")
        self.entry_tg_hash.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry_tg_hash.insert(0, config.get("tg_api_hash", ""))
        
        ttk.Button(tg_f, text="Save Telegram Credentials", command=self.save_tg_creds).pack(pady=5)

        # TeraBox Config
        tb_f = ttk.LabelFrame(self, text="TeraBox Configuration", padding=10)
        tb_f.pack(fill=tk.X, padx=20, pady=10)
        ttk.Label(tb_f, text="ndus Cookie:").pack(side=tk.LEFT)
        self.entry_cookie = ttk.Entry(tb_f, show="*", width=50)
        self.entry_cookie.pack(side=tk.LEFT, padx=10, fill=tk.X, expand=True)
        self.entry_cookie.insert(0, config.get("terabox_cookie", ""))
        ttk.Button(tb_f, text="Save Cookie", command=self.save_cookie).pack(side=tk.LEFT)

        self.load_list()

    def save_tg_creds(self):
        tid = self.entry_tg_id.get().strip()
        thash = self.entry_tg_hash.get().strip()
        config = ConfigManager.load()
        config["tg_api_id"] = tid
        config["tg_api_hash"] = thash
        ConfigManager.save(config)
        messagebox.showinfo("Telegram", "Credentials saved to local config.")

    def save_cookie(self):
        c = self.entry_cookie.get().strip()
        config = ConfigManager.load()
        config["terabox_cookie"] = c
        ConfigManager.save(config)
        messagebox.showinfo("TeraBox", "Cookie saved.")

    def load_list(self):
        for w in self.scrollable_frame.winfo_children():
            w.destroy()
        config = ConfigManager.load()
        self.check_vars = {}
        for d in config["directories"]:
            var = tk.BooleanVar()
            self.check_vars[d] = var
            ttk.Checkbutton(self.scrollable_frame, text=d, variable=var).pack(anchor="w", padx=5, pady=2)

    def verify_action(self):
        config = ConfigManager.load()
        if not config["password_hash"]:
            return True
        p = simpledialog.askstring("Security", "Confirm password:", show="*")
        if p and ConfigManager.hash_password(p) == config["password_hash"]:
            return True
        messagebox.showerror("Unauthorized", "Invalid password.")
        return False

    def set_password(self):
        p1 = simpledialog.askstring("Security", "New password:", show="*")
        if p1 is None:
            return
        p2 = simpledialog.askstring("Security", "Confirm password:", show="*")
        if p1 == p2:
            config = ConfigManager.load()
            config["password_hash"] = ConfigManager.hash_password(p1)
            ConfigManager.save(config)
            messagebox.showinfo("Success", "Password updated.")
        else:
            messagebox.showerror("Error", "Passwords do not match.")

    def add_dir_guarded(self):
        if not self.verify_action():
            return
        d = filedialog.askdirectory()
        if d:
            config = ConfigManager.load()
            if d not in config["directories"]:
                config["directories"].append(d)
                ConfigManager.save(config)
                self.load_list()

    def remove_dir_guarded(self):
        if not self.verify_action():
            return
        config = ConfigManager.load()
        to_remove = [d for d, v in self.check_vars.items() if v.get()]
        if not to_remove:
            return
        for d in to_remove:
            config["directories"].remove(d)
        ConfigManager.save(config)
        self.load_list()

# --- Video Player (Browser-based fallback) ---
class BrowserPlayer(tk.Toplevel):
    def __init__(self, parent, url, title="Web Player"):
        super().__init__(parent)
        self.title(title)
        self.geometry("1000x750")
        
        top = ttk.Frame(self, padding=5)
        top.pack(fill=tk.X)
        ttk.Label(top, text=f"Web Content: {title[:50]}...", font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT, padx=10)
        ttk.Button(top, text="Close", command=self.destroy).pack(side=tk.RIGHT)

        self.browser = HtmlFrame(self)
        self.browser.pack(fill=tk.BOTH, expand=True)
        self.after(200, lambda: self.browser.load_url(url))
        self.transient(parent)

# --- Video Player (Embedded VLC) ---
class VideoPlayer(tk.Toplevel):
    def __init__(self, parent, source, title="Video Player", options=None):
        super().__init__(parent)
        self.title(title)
        self.geometry("1000x700")
        self.configure(bg="black")
        
        # Ensure window has full decorations (Restore Minimize/Maximize)
        self.resizable(True, True)
        self.attributes("-toolwindow", 0)
        self.lift()
        
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        
        # VLC Setup optimized for HTTP streaming on Windows
        vlc_args = [
            '--no-video-title-show',
            '--avcodec-hw=none',
            '--vout=direct3d11', 
            '--network-caching=3000',
            '--quiet'
        ]
        self.instance = vlc.Instance(vlc_args)
        self.player = self.instance.media_player_new()
        
        # UI
        self.video_frame = tk.Frame(self, bg="black")
        self.video_frame.pack(fill=tk.BOTH, expand=True)
        
        controls = ttk.Frame(self, padding=5)
        controls.pack(fill=tk.X)
        
        self.btn_play = ttk.Button(controls, text="⏸ Pause", width=10, command=self.toggle_pause)
        self.btn_play.pack(side=tk.LEFT, padx=5)
        
        ttk.Button(controls, text="⏹ Stop", width=10, command=self.stop).pack(side=tk.LEFT, padx=5)
        
        self.scale_var = tk.DoubleVar()
        self.scale = ttk.Scale(controls, from_=0, to=1000, orient=tk.HORIZONTAL, variable=self.scale_var, command=self.on_scale)
        self.scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=10)
        
        ttk.Label(controls, text="🔊").pack(side=tk.LEFT)
        self.vol_var = tk.IntVar(value=70)
        self.vol_scale = ttk.Scale(controls, from_=0, to=100, orient=tk.HORIZONTAL, variable=self.vol_var, command=self.set_volume, length=80)
        self.vol_scale.pack(side=tk.LEFT, padx=5)

        self.media_source = source
        self.media_options = options
        self.is_updating = True
        self.is_seeking = False

        self.wait_visibility()
        self.after(500, self._start_playback)
        self.update_progress()

    def _start_playback(self):
        try:
            path = self.media_source
            if path.startswith("file:///"):
                path = urllib.parse.unquote(path[8:])
                if ":" not in path[:3]: path = path.lstrip("/")
            
            if os.path.exists(path):
                path = os.path.abspath(os.path.normpath(path))

            # HWND must be set after visibility
            h = self.video_frame.winfo_id()
            if sys.platform == "win32":
                self.player.set_hwnd(h)
            elif sys.platform == "linux":
                self.player.set_xwindow(h)
            else:
                self.player.set_nsobject(h)
                
            self.media = self.instance.media_new(path)
            if self.media_options:
                for opt in self.media_options:
                    self.media.add_option(opt)
            
            self.player.set_media(self.media)
            self.player.play()
            logger.info(f"VLC Playback Start: {path[:100]}...")
        except Exception as e:
            logger.error(f"VLC Playback Error: {e}")

    def update_progress(self):
        if not self.is_updating: return
        try:
            state = self.player.get_state()
            if state == vlc.State.Playing and not self.is_seeking:
                pos = self.player.get_position() * 1000
                self.scale_var.set(pos)
                self.btn_play.config(text="⏸ Pause")
            elif state == vlc.State.Paused:
                self.btn_play.config(text="▶ Play")
            elif state == vlc.State.Ended:
                self.btn_play.config(text="▶ Play")
                self.scale_var.set(0)
            
            self.after(500, self.update_progress)
        except: pass

    def on_scale(self, val):
        self.is_seeking = True
        self.player.set_position(float(val) / 1000.0)
        # Briefly block progress update to prevent slider jitter
        self.after(200, self._end_seek)

    def _end_seek(self):
        self.is_seeking = False

    def toggle_pause(self):
        # Explicit state check for resume reliability
        state = self.player.get_state()
        if state == vlc.State.Playing:
            self.player.pause()
            self.btn_play.config(text="▶ Play")
        else:
            self.player.play()
            self.btn_play.config(text="⏸ Pause")

    def stop(self):
        self.player.stop()
        self.btn_play.config(text="▶ Play")
        self.scale_var.set(0)

    def set_volume(self, val):
        self.player.audio_set_volume(int(float(val)))

    def _on_close(self):
        self.is_updating = False
        try:
            self.player.stop()
            self.player.release()
            self.instance.release()
        except: pass
        self.destroy()

# --- Main App Shell ---

class MediaSuggesterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Media Suggester Pro")
        self.geometry("1100x800")
        self.thumb_gen = ThumbnailGenerator(self)
        
        self.log_history = []
        self.log_widget = None # Track open log window
        
        style = ttk.Style()
        style.configure("Sidebar.TFrame", background="#2c3e50")
        style.configure("Sidebar.TButton", padding=10, width=20)
        self.sidebar = ttk.Frame(self, style="Sidebar.TFrame", width=200)
        self.sidebar.pack(side=tk.LEFT, fill=tk.Y)
        self.sidebar.pack_propagate(False)
        self.main_container = ttk.Frame(self)
        self.main_container.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        self.frames = {}
        for F in (SplashFrame, LoginFrame, HomeFrame, FavoritesFrame, TagsFrame, TeraBoxFrame, BunkrFrame, OrganizerFrame, SettingsFrame):
            frame = F(self.main_container, self)
            self.frames[F] = frame
            frame.grid(row=0, column=0, sticky="nsew")
        self.main_container.grid_rowconfigure(0, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)
        self.side_items = ttk.Frame(self.sidebar, style="Sidebar.TFrame")
        ttk.Label(self.side_items, text="MEDIA APP", foreground="white", font=("Segoe UI", 12, "bold"), padding=20, background="#2c3e50").pack()
        ttk.Button(self.side_items, text="🏠 Home", style="Sidebar.TButton", command=lambda: self.show_frame(HomeFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="⭐ Favorites", style="Sidebar.TButton", command=lambda: self.show_frame(FavoritesFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="🏷 Tags", style="Sidebar.TButton", command=lambda: self.show_frame(TagsFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="☁ TeraBox (Beta)", style="Sidebar.TButton", command=lambda: self.show_frame(TeraBoxFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="📦 Bunkr (Beta)", style="Sidebar.TButton", command=lambda: self.show_frame(BunkrFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="📁 Organizer", style="Sidebar.TButton", command=lambda: self.show_frame(OrganizerFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="⚙ Settings", style="Sidebar.TButton", command=lambda: self.show_frame(SettingsFrame)).pack(pady=5)
        ttk.Button(self.side_items, text="📜 View Logs", style="Sidebar.TButton", command=self.show_log_window).pack(pady=5)
        
        # Status Bar at bottom of sidebar
        self.lbl_status = ttk.Label(self.sidebar, text="Ready", foreground="#bdc3c7", background="#2c3e50", font=("Segoe UI", 8), wraplength=180, padding=10)
        self.lbl_status.pack(side=tk.BOTTOM, fill=tk.X)
        
        # Log handler integration
        handler = UIHandler(self.update_status)
        logging.getLogger().addHandler(handler)
        
        self.show_frame(SplashFrame)

    def update_status(self, msg):
        self.log_history.append(msg)
        if len(self.log_history) > 200: self.log_history.pop(0)
        
        # Update the mini status bar
        self.after(0, lambda: self.lbl_status.config(text=msg.split(" - ")[-1][:100]))
        
        # Update live log window if open
        if self.log_widget and self.log_widget.winfo_exists():
            self.after(0, lambda m=msg: self._append_to_log_widget(m))

    def _append_to_log_widget(self, msg):
        try:
            self.log_widget.config(state=tk.NORMAL)
            self.log_widget.insert(tk.END, msg + "\n")
            self.log_widget.see(tk.END)
            self.log_widget.config(state=tk.DISABLED)
        except: pass

    def show_log_window(self):
        log_win = tk.Toplevel(self)
        log_win.title("Live Diagnostic Logs")
        log_win.geometry("800x500")
        
        txt = tk.Text(log_win, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 10))
        txt.pack(fill=tk.BOTH, expand=True)
        
        # Initialize with history
        for line in self.log_history:
            txt.insert(tk.END, line + "\n")
        txt.see(tk.END)
        txt.config(state=tk.DISABLED)
        self.log_widget = txt # Link for live updates

    def play_in_app(self, source, title, options=None):
        VideoPlayer(self, source, title, options)

    def play_in_browser(self, url, title):
        BrowserPlayer(self, url, title)

    def show_frame(self, context):
        frame = self.frames[context]
        frame.tkraise()
        if context in (SplashFrame, LoginFrame):
            self.side_items.pack_forget()
        else:
            self.side_items.pack(fill=tk.Y)
        
        if hasattr(frame, "refresh_suggestions"):
            frame.refresh_suggestions()
        elif hasattr(frame, "refresh_tag_media"):
            frame.refresh_tag_media()
            
        if context == SettingsFrame:
            frame.load_list()
        if context == TagsFrame:
            frame.load_tags()

    def ask_string_threadsafe(self, title, prompt):
        res_queue = queue.Queue()
        self.after(0, lambda: res_queue.put(simpledialog.askstring(title, prompt)))
        return res_queue.get()

    def start_cloud_download(self, url, filename, headers=None):
        """Standardized background downloader for Bunkr and TeraBox."""
        save_path = filedialog.asksaveasfilename(defaultextension=os.path.splitext(filename)[1], initialfile=filename)
        if not save_path: return

        def _task():
            try:
                logger.info(f"Download: Starting {filename}...")
                self.update_status(f"Downloading {filename}...")
                
                resp = requests.get(url, headers=headers, stream=True, timeout=30, verify=False)
                if resp.status_code == 200:
                    with open(save_path, 'wb') as f:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk: f.write(chunk)
                    logger.info(f"Download: Finished {filename} successfully.")
                    self.after(0, lambda: messagebox.showinfo("Download", f"Successfully saved to:\n{save_path}"))
                else:
                    logger.error(f"Download failed: HTTP {resp.status_code}")
                    self.after(0, lambda: messagebox.showerror("Download Error", f"Server returned error {resp.status_code}"))
            except Exception as e:
                logger.error(f"Download error: {e}")
                self.after(0, lambda: messagebox.showerror("Download Error", str(e)))

        threading.Thread(target=_task, daemon=True).start()

if __name__ == "__main__":
    app = MediaSuggesterApp()
    app.mainloop()
