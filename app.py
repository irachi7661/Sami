import os
import subprocess
import threading
import time
import signal
import requests
import hashlib
import logging # লগিং মডিউল ইম্পোর্ট করা হলো
from flask import Flask, render_template, send_from_directory, abort, request, redirect, url_for, flash
from flask_cors import CORS
from collections import deque

# --- লগিং কনফিগারেশন ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')

# --- কনফিগারেশন ---
DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts"
VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
HLS_OUTPUT_FILE = os.path.join(STREAM_OUTPUT_DIR, "stream.m3u8")
PRE_DOWNLOAD_TIMEOUT = 60 # সেকেন্ড (পরবর্তী ভিডিও ডাউনলোডের জন্য সর্বোচ্চ অপেক্ষা)

# --- গ্লোবাল ভেরিয়েবল ---
video_queue = deque()
played_today = set()
current_ffmpeg_process = None
stop_event = threading.Event()
stream_lock = threading.Lock() # முக்கியம்: Ensure all shared state access is locked
currently_playing_url = None
default_video_path = None

# প্রি-ডাউনলোড স্টেট ভেরিয়েবল
next_video_url_to_download = None
next_video_download_path = None
next_video_download_thread = None
next_video_ready_event = threading.Event() # ডাউনলোড শেষ হলে বা ফেইল করলে সেট হবে

app = Flask(__name__)
CORS(app)
app.secret_key = os.urandom(24)

# --- ডিরেক্টরি তৈরি ---
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

# --- Helper Functions ---

def get_safe_filename(url):
    """URL থেকে একটি নিরাপদ ফাইলের নাম তৈরি করে (হ্যাশ ব্যবহার করে)"""
    try:
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        base_name = os.path.basename(url.split('?')[0])
        _, ext = os.path.splitext(base_name)
        if not ext or len(ext) > 6: ext = '.mp4'
    except Exception:
        ext = '.mp4'
    if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv']: ext = '.mp4'
    return f"video_{hashed_url}{ext}"

def download_video(url, output_filename, event_to_set=None):
    """একটি ভিডিও ডাউনলোড করে এবং ঐচ্ছিকভাবে একটি ইভেন্ট সেট করে"""
    filepath = os.path.join(VIDEO_DIR, output_filename)
    download_successful = False # ডাউনলোড সফল হয়েছে কিনা ট্র্যাক করার জন্য

    try:
        # চেক করুন ফাইলটি আগে থেকেই আছে কিনা এবং খালি নয়
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0 and output_filename != DEFAULT_VIDEO_FILENAME:
            logging.info(f"'{output_filename}' আগে থেকেই আছে এবং খালি নয়। ডাউনলোড স্কিপ করা হলো।")
            download_successful = True # যেহেতু ফাইল আছে, সফল ধরা যায়
            return filepath # ফাইল পাথ রিটার্ন করুন

        logging.info(f"ডাউনলোড শুরু হচ্ছে: {url[:80]}... -> {filepath}")
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True)
        response.raise_for_status() # HTTP ত্রুটি চেক করুন

        # ভিডিও ডাউনলোড লুপ
        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 2):
                if stop_event.is_set():
                    logging.warning("অ্যাপ বন্ধ হওয়ার সিগন্যালের কারণে ডাউনলোড বাতিল করা হয়েছে।")
                    # আংশিক ফাইল এখানেই মুছে ফেলা ভালো
                    if os.path.exists(filepath):
                        try: os.remove(filepath)
                        except OSError as e: logging.error(f"বাতিল ডাউনলোডের ফাইল মুছতে সমস্যা ({filepath}): {e}")
                    return None # None রিটার্ন করে ফাংশন শেষ করুন
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)
        # ডাউনলোড লুপ শেষ

        # ডাউনলোড শেষে ফাইলের সাইজ চেক করুন
        if downloaded_size == 0:
            logging.warning(f"ডাউনলোড সম্পন্ন হয়েছে কিন্তু ফাইলের সাইজ ০ ({filepath})। ফাইল মুছে ফেলা হচ্ছে।")
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except OSError as e: logging.error(f"০ বাইট ফাইল মুছতে সমস্যা ({filepath}): {e}")
            return None # ফাইলের সাইজ ০ হলে None রিটার্ন করুন

        # যদি কোড এই পর্যন্ত আসে, তার মানে ডাউনলোড সফল হয়েছে
        logging.info(f"সফলভাবে ডাউনলোড হয়েছে: {output_filename} (Size: {downloaded_size / (1024*1024):.2f} MB)")
        download_successful = True
        return filepath # সফল হলে ফাইলের পাথ রিটার্ন করুন

    except requests.exceptions.Timeout:
        logging.error(f"❌ ভিডিও ডাউনলোড টাইমআউট ({url[:80]}...)")
        # ব্যর্থ হলে ফাইল মুছুন (যদি তৈরি হয়ে থাকে)
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except OSError as e: logging.error(f"টাইমআউট ফাইল মুছতে সমস্যা ({filepath}): {e}")
        return None # 실패하면 None 반환
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ ভিডিও ডাউনলোড ব্যর্থ ({url[:80]}...): {e}")
        # ব্যর্থ হলে ফাইল মুছুন
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except OSError as e: logging.error(f"ব্যর্থতার ফাইল মুছতে সমস্যা ({filepath}): {e}")
        return None # 실패하면 None 반환
    except Exception as e:
        # অন্যান্য অপ্রত্যাশিত ত্রুটি
        logging.error(f"❌ ভিডিও সংরক্ষণ বা অন্য কোনো ত্রুটি ({url[:80]}...): {e}")
        # ব্যর্থ হলে ফাইল মুছুন
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except OSError as e: logging.error(f"ত্রুটির ফাইল মুছতে সমস্যা ({filepath}): {e}")
        return None # 실패하면 None 반환

    # finally ব্লকটি try/except কাঠামোর ঠিক পরেই আসবে
    finally:
        # ডাউনলোড সফল হোক বা ব্যর্থ, সংশ্লিষ্ট ইভেন্ট সেট করুন (যদি দেওয়া থাকে)
        # এটি নিশ্চিত করে যে ম্যানেজার থ্রেড জানতে পারে ডাউনলোড প্রচেষ্টা শেষ হয়েছে
        if event_to_set:
            logging.debug(f"Setting completion event for {url[:80]}...")
            event_to_set.set()
        # finally ব্লকের মধ্যে কোনো return স্টেটমেন্ট থাকা উচিত নয়,
        # কারণ এটি try/except ব্লকের return/exception কে ওভাররাইড করতে পারে।
def background_download_task(url, output_path, completion_event):
    """ব্যাকগ্রাউন্ডে একটি ভিডিও ডাউনলোড করার টাস্ক"""
    thread_name = threading.current_thread().name
    logging.info(f"[{thread_name}] ব্যাকগ্রাউন্ড ডাউনলোড শুরু: {url[:80]}...")
    downloaded_file = download_video(url, os.path.basename(output_path), completion_event)
    if downloaded_file:
        logging.info(f"[{thread_name}] ব্যাকগ্রাউন্ড ডাউনলোড সম্পন্ন: {os.path.basename(output_path)}")
    else:
        logging.error(f"[{thread_name}] ব্যাকগ্রাউন্ড ডাউনলোড ব্যর্থ: {url[:80]}...")
    # completion_event download_video ফাংশনেই সেট হয়ে যাবে finally ব্লকের মাধ্যমে

def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস বন্ধ করে"""
    global current_ffmpeg_process, currently_playing_url
    # এই ফাংশনটি stream_lock এর ভিতরে বা বাইরে কল হতে পারে, তাই এটি নিজে লক অ্যাকোয়ার করবে
    with stream_lock:
        process_to_stop = current_ffmpeg_process
        if process_to_stop:
            pid = process_to_stop.pid # Store PID for logging
            logging.info(f"FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {pid})...")
            if process_to_stop.poll() is None:
                try:
                    process_to_stop.terminate()
                    process_to_stop.wait(timeout=5)
                    logging.info(f"FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (terminate) (PID: {pid})।")
                except subprocess.TimeoutExpired:
                    logging.warning(f"FFmpeg প্রসেস terminate হয়নি (PID: {pid}), SIGKILL পাঠানো হচ্ছে...")
                    process_to_stop.kill()
                    process_to_stop.wait()
                    logging.info(f"FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (kill) (PID: {pid})।")
                except Exception as e:
                    logging.error(f"FFmpeg বন্ধ করার সময় ত্রুটি (PID: {pid}): {e}")
            else:
                logging.info(f"FFmpeg প্রসেস (PID: {pid}) আগে থেকেই বন্ধ ছিল।")

            # গ্লোবাল ভেরিয়েবল রিসেট করুন
            if current_ffmpeg_process == process_to_stop:
                current_ffmpeg_process = None
                # currently_playing_url ম্যানেজার লুপ হ্যান্ডেল করবে

def start_ffmpeg_stream(video_path, loop=False):
    """একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg স্ট্রিম শুরু করে"""
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        logging.error(f"❌ FFmpeg শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    ffmpeg_cmd_list = [
        'ffmpeg', '-hide_banner', '-loglevel', 'warning', # লগিং কমানো
        '-re',
        *(['-stream_loop', '-1'] if loop else []), # লুপ অপশন যোগ করুন যদি দরকার হয়
        '-i', abs_video_path,
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-b:v', '1500k', '-maxrate', '1500k', '-bufsize', '3000k',
        '-g', '60', '-vf', 'scale=640:360',
        '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '44100',
        '-f', 'hls', '-hls_time', '4', '-hls_list_size', '5',
        '-hls_flags', 'delete_segments+omit_endlist',
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%03d.ts'),
        HLS_OUTPUT_FILE
    ]

    logging.info(f"🚀 FFmpeg কমান্ড: {' '.join(ffmpeg_cmd_list)}")
    try:
        # stderr পাইপ করা, stdout বন্ধ
        process = subprocess.Popen(ffmpeg_cmd_list, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def log_stderr(proc, path):
            if proc.stderr:
                for line in iter(proc.stderr.readline, b''):
                    if stop_event.is_set(): break
                    line_str = line.decode(errors='ignore').strip()
                    if line_str: logging.warning(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}") # Warning হিসেবে লগ করা ভালো
            logging.info(f"  [FFmpeg stderr রিডিং শেষ - {os.path.basename(path)}]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True, name=f"FFmpegLog-{os.path.basename(video_path)}")
        stderr_thread.start()

        logging.info(f"✅ FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)}, লুপ: {loop}")
        with stream_lock:
            current_ffmpeg_process = process
        return process

    except FileNotFoundError:
        logging.critical("❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ যোগ করা আছে কিনা নিশ্চিত করুন।")
    except Exception as e:
        logging.error(f"❌ FFmpeg শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")

    # ব্যর্থ হলে রিসোর্স ক্লিনআপ
    with stream_lock:
        current_ffmpeg_process = None
    return None

# --- প্রি-ডাউনলোড শুরু করার হেল্পার ---
def start_next_video_download():
    """কিউয়ের পরবর্তী ভিডিওটির ব্যাকগ্রাউন্ড ডাউনলোড শুরু করে (যদি প্রয়োজন হয় এবং সম্ভব হয়)"""
    global next_video_url_to_download, next_video_download_path, next_video_download_thread, next_video_ready_event

    with stream_lock:
        # যদি ইতিমধ্যে একটি ডাউনলোড চলে, তবে নতুন করে শুরু করবেন না
        if next_video_download_thread and next_video_download_thread.is_alive():
            logging.info("একটি প্রি-ডাউনলোড ইতিমধ্যে চলছে।")
            return

        # কিউতে পরবর্তী আইটেম আছে কিনা দেখুন (পপ না করে)
        if len(video_queue) > 0:
            next_url = video_queue[0] # শুধুমাত্র দেখুন, কিউ থেকে সরাবেন না
            target_filename = get_safe_filename(next_url)
            target_path = os.path.join(VIDEO_DIR, target_filename)

            # যদি এই ফাইলটি ইতিমধ্যে বিদ্যমান থাকে, তবে ডাউনলোড শুরু করার দরকার নেই
            if os.path.exists(target_path) and os.path.getsize(target_path) > 0:
                 logging.info(f"পরবর্তী ভিডিও '{target_filename}' ইতিমধ্যে ডাউনলোড করা আছে। প্রি-ডাউনলোড দরকার নেই।")
                 # স্টেট সেট করুন যাতে পরবর্তী সুইচ এটি ব্যবহার করতে পারে
                 next_video_url_to_download = next_url
                 next_video_download_path = target_path
                 next_video_ready_event.set() # এটি প্রস্তুত
                 next_video_download_thread = None
                 return

            # ডাউনলোড শুরু করুন
            logging.info(f"পরবর্তী ভিডিওর জন্য প্রি-ডাউনলোড শুরু হচ্ছে: {next_url[:80]}...")
            next_video_url_to_download = next_url
            next_video_download_path = target_path
            next_video_ready_event.clear() # ইভেন্ট রিসেট করুন

            next_video_download_thread = threading.Thread(
                target=background_download_task,
                args=(next_url, target_path, next_video_ready_event),
                daemon=True,
                name=f"PreDownloader-{target_filename}"
            )
            try:
                next_video_download_thread.start()
            except RuntimeError as e:
                 logging.error(f"প্রি-ডাউনলোড থ্রেড শুরু করতে ব্যর্থ: {e}")
                 # স্টেট রিসেট করুন
                 next_video_url_to_download = None
                 next_video_download_path = None
                 next_video_download_thread = None
                 next_video_ready_event.set() # ব্যর্থতা বোঝাতে ইভেন্ট সেট করুন

        else:
            # কিউ খালি, কোনো প্রি-ডাউনলোড শুরু করা যাবে না
            logging.info("কিউ খালি, প্রি-ডাউনলোড করার কিছু নেই।")
            # নিশ্চিত করুন যে পুরনো স্টেট পরিষ্কার আছে
            next_video_url_to_download = None
            next_video_download_path = None
            next_video_download_thread = None
            next_video_ready_event.set() # কিছু করার নেই, তাই ইভেন্ট সেট রাখুন

# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার (প্রি-ডাউনলোড লজিক সহ) ---
def stream_manager():
    """ভিডিও কিউ এবং FFmpeg প্রসেস ম্যানেজ করে, প্রি-ডাউনলোডিং ব্যবহার করে"""
    global currently_playing_url, default_video_path, current_ffmpeg_process
    global next_video_url_to_download, next_video_download_path, next_video_download_thread, next_video_ready_event

    # শুরুতে ডিফল্ট ভিডিও ডাউনলোড
    logging.info("⏳ ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা চলছে...")
    temp_default_path = download_video(DEFAULT_VIDEO_URL, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
        default_video_path = temp_default_path
        logging.info(f"✅ ডিফল্ট ভিডিও প্রস্তুত: {default_video_path}")
    else:
        logging.warning("🚨 ডিফল্ট ভিডিও ডাউনলোড করা যায়নি! ডিফল্ট প্লেব্যাক কাজ করবে না।")

    while not stop_event.is_set():
        video_to_play_path = None
        url_to_play = None
        loop_default = False
        start_pre_download_after_play = False # প্লে শুরু করার পর প্রি-ডাউনলোড ট্রিগার করার ফ্ল্যাগ

        try:
            with stream_lock:
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None

                # --- ধাপ ১: যদি FFmpeg চলে ---
                if ffmpeg_is_running:
                    # ডিফল্ট ভিডিও চলছে এবং কিউতে আইটেম এসেছে কিনা চেক করুন
                    if currently_playing_url == DEFAULT_VIDEO_URL and video_queue:
                        logging.info("🔄 ডিফল্ট ভিডিও চলছিল, কিউতে নতুন আইটেম আসায় বন্ধ করা হচ্ছে...")
                        stop_ffmpeg_stream() # এটি লক ছেড়ে দেওয়ার পর কার্যকর হবে
                        # প্রি-ডাউনলোড স্টেট রিসেট করুন কারণ আমরা এখন কিউ থেকে শুরু করব
                        next_video_url_to_download = None
                        next_video_download_path = None
                        if next_video_download_thread and next_video_download_thread.is_alive():
                             logging.info("চলমান প্রি-ডাউনলোড থ্রেড বন্ধ করার চেষ্টা করা হচ্ছে...")
                             # এখানে থ্রেড বন্ধ করার কোনো ভালো উপায় নেই, ইভেন্ট দিয়েও লাভ হবে না
                             # শুধু রেফারেন্স মুছে ফেলা যাক, থ্রেড নিজে শেষ হবে
                             next_video_download_thread = None
                        next_video_ready_event.set()
                        time.sleep(0.5) # বন্ধ হওয়ার জন্য সময় দিন
                        continue # লুপ পুনরায় শুরু করুন
                    else:
                        # স্বাভাবিকভাবে চলছে, কিছু করার দরকার নেই
                        time.sleep(1) # অল্প অপেক্ষা
                        continue

                # --- ধাপ ২: যদি FFmpeg বন্ধ থাকে বাพึ่ง শেষ হয়েছে ---
                else:
                    # যদি প্রসেস শেষ হয়ে থাকে, তবে তা পরিষ্কার করুন
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        logging.info(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) শেষ হয়েছে।")
                        if currently_playing_url and currently_playing_url != DEFAULT_VIDEO_URL:
                            played_today.add(currently_playing_url)
                        current_ffmpeg_process = None # রিসোর্স পরিষ্কার করুন
                        # currently_playing_url এখানে None করা উচিত না, কারণ এটি দিয়ে বুঝব কী চলছিল

                    # এখন পরবর্তী ভিডিও নির্ধারণ করুন
                    # --- অগ্রাধিকার ১: প্রি-ডাউনলোড করা ভিডিও (যদি থাকে এবং প্রস্তুত হয়) ---
                    if next_video_url_to_download and next_video_download_path:
                        logging.info(f"প্রি-ডাউনলোড করা ভিডিও ({os.path.basename(next_video_download_path)}) ব্যবহার করার চেষ্টা চলছে...")
                        # অপেক্ষা করুন ডাউনলোড শেষ হওয়ার জন্য (যদি প্রয়োজন হয়)
                        is_ready = next_video_ready_event.wait(timeout=PRE_DOWNLOAD_TIMEOUT)
                        if is_ready and os.path.exists(next_video_download_path) and os.path.getsize(next_video_download_path) > 0:
                            logging.info("✅ প্রি-ডাউনলোড করা ভিডিও প্রস্তুত।")
                            video_to_play_path = next_video_download_path
                            url_to_play = next_video_url_to_download
                            loop_default = False
                            # এই URL টি কিউ থেকে সরাতে হবে
                            if video_queue and video_queue[0] == url_to_play:
                                video_queue.popleft()
                            # প্রি-ডাউনলোড স্টেট রিসেট করুন
                            next_video_url_to_download = None
                            next_video_download_path = None
                            next_video_download_thread = None
                            start_pre_download_after_play = True # পরবর্তীটার প্রি-ডাউনলোড শুরু করতে হবে
                        else:
                            logging.error(f"❌ প্রি-ডাউনলোড করা ভিডিও প্রস্তুত নয় বা ফাইল সমস্যাযুক্ত (Timeout: {not is_ready}, Path: {next_video_download_path})।")
                            # ব্যর্থ স্টেট পরিষ্কার করুন
                            next_video_url_to_download = None
                            next_video_download_path = None
                            next_video_download_thread = None
                            # লুপ চলবে এবং কিউ থেকে স্বাভাবিকভাবে চেষ্টা করবে

                    # --- অগ্রাধিকার ২: অ্যাডমিন কিউ (যদি প্রি-ডাউনলোড ব্যবহার না হয়) ---
                    if not video_to_play_path and video_queue:
                        url_to_play = video_queue.popleft()
                        logging.info(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে (সিঙ্ক্রোনাস ডাউনলোড): {url_to_play[:80]}...")
                        filename = get_safe_filename(url_to_play)
                        # সিঙ্ক্রোনাস ডাউনলোড
                        video_to_play_path = download_video(url_to_play, filename)
                        if not video_to_play_path:
                            logging.error(f"❌ সিঙ্ক্রোনাস ডাউনলোড ব্যর্থ: {url_to_play[:80]}... পরবর্তী আইটেম চেষ্টা করা হবে।")
                            url_to_play = None # প্লে করা যাবে না
                        else:
                            loop_default = False
                            start_pre_download_after_play = True # পরবর্তীটার প্রি-ডাউনলোড শুরু করতে হবে

                    # --- বিকল্প: ডিফল্ট ভিডিও (যদি উপরের কিছুই না পাওয়া যায়) ---
                    elif not video_to_play_path and default_video_path:
                        logging.info("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                        video_to_play_path = default_video_path
                        url_to_play = DEFAULT_VIDEO_URL
                        loop_default = True
                        start_pre_download_after_play = False # ডিফল্টের পর প্রি-ডাউনলোড হবে না

                    # --- কিছুই করার নেই ---
                    elif not video_to_play_path:
                        logging.info("⏳ অ্যাডমিন কিউ খালি এবং ডিফল্ট ভিডিও উপলব্ধ নেই। অপেক্ষা করা হচ্ছে...")
                        time.sleep(5)
                        continue

            # --- ধাপ ৩: অ্যাকশন (লকের বাইরে) ---
            if video_to_play_path and url_to_play:
                 # ক্লিনআপ: পুরনো HLS ফাইল মুছে ফেলা (অপশনাল কিন্তু ভালো)
                 try:
                     for f in os.listdir(STREAM_OUTPUT_DIR):
                         if f.endswith('.ts') or f.endswith('.m3u8.tmp'): # Keep main m3u8
                             os.remove(os.path.join(STREAM_OUTPUT_DIR, f))
                 except OSError as e:
                      logging.warning(f"পুরনো HLS সেগমেন্ট মুছতে সমস্যা: {e}")

                 logging.info(f"প্লে শুরু হচ্ছে: {os.path.basename(video_to_play_path)}, URL: {url_to_play[:80]}..., লুপ: {loop_default}")
                 with stream_lock: # currently_playing_url সেট করার জন্য লক দরকার
                     currently_playing_url = url_to_play

                 started_process = start_ffmpeg_stream(video_to_play_path, loop=loop_default)

                 if started_process:
                     # সফলভাবে প্লে শুরু হলে, পরবর্তী ভিডিওর প্রি-ডাউনলোড শুরু করুন (যদি ফ্ল্যাগ সেট থাকে)
                     if start_pre_download_after_play:
                         logging.info("পরবর্তী ভিডিওর জন্য প্রি-ডাউনলোড চেক/শুরু করা হচ্ছে...")
                         start_next_video_download() # এটি ব্যাকগ্রাউন্ডে শুরু হবে
                     time.sleep(2) # FFmpeg স্ট্যাবিলাইজ হওয়ার জন্য সময় দিন
                 else:
                     # FFmpeg শুরু করতে ব্যর্থ হলে, playing url রিসেট করুন
                     logging.error("FFmpeg শুরু করতে ব্যর্থ হয়েছে।")
                     with stream_lock:
                         currently_playing_url = None
                     time.sleep(5) # আবার চেষ্টা করার আগে অপেক্ষা করুন

            # যদি কোনো ভিডিও প্লে করার জন্য না পাওয়া যায়, তবে কিছুক্ষণ অপেক্ষা করুন
            elif not ffmpeg_is_running:
                 time.sleep(3)

        except Exception as e:
            logging.exception("🚨 স্ট্রিম ম্যানেজার লুপে অপ্রত্যাশিত ত্রুটি:") # Use logging.exception for full traceback
            time.sleep(10) # গুরুতর ত্রুটির পর বেশি সময় অপেক্ষা করুন

    # --- থ্রেড বন্ধ হওয়ার সময় ---
    logging.info("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    if next_video_download_thread and next_video_download_thread.is_alive():
        logging.info("চলমান প্রি-ডাউনলোড থ্রেড শেষ হওয়ার জন্য অপেক্ষা করা হচ্ছে...")
        # এখানে join() করা উচিত নয় কারণ এটি অ্যাপ্লিকেশন বন্ধ হওয়া বিলম্বিত করতে পারে
        # Daemon থ্রেড নিজে থেকেই বন্ধ হয়ে যাবে
    stop_ffmpeg_stream()

# --- Flask Routes (কোনো পরিবর্তন দরকার নেই) ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin_panel():
    with stream_lock:
        queue_snapshot = list(video_queue)
        played_snapshot = list(played_today)
        current_url_snapshot = currently_playing_url
        next_downloading_url = next_video_url_to_download
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        is_predownload_running = next_video_download_thread and next_video_download_thread.is_alive()

    status_text = ""
    if is_ffmpeg_running:
        if current_url_snapshot == DEFAULT_VIDEO_URL:
            status_text = "ডিফল্ট ভিডিও চলছে (লুপ)"
        elif current_url_snapshot:
            status_text = f"চলছে: {current_url_snapshot[:80]}..."
        else:
            status_text = "একটি ভিডিও চলছে (URL অজানা)"
    else:
        status_text = "কোনো ভিডিও চলছে না"

    if is_predownload_running and next_downloading_url:
        status_text += f" [পরবর্তী ডাউনলোড চলছে: {next_downloading_url[:50]}...]"

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=status_text,
                           played=played_snapshot)

@app.route('/admin/add', methods=['POST'])
def add_video():
    url = request.form.get('video_url')
    should_start_predownload = False
    if url:
        if url.startswith('http://') or url.startswith('https://'):
            with stream_lock:
                video_queue.append(url)
                logging.info(f"📥 কিউতে যোগ করা হয়েছে: {url}")
                flash(f'"{url[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
                # যদি FFmpeg না চলে এবং কোনো প্রি-ডাউনলোড না চলে, তবে নতুন যোগ করাটির প্রি-ডাউনলোড শুরু করা যেতে পারে
                # অথবা যদি ডিফল্ট ভিডিও চলে, তবে ম্যানেজার লুপ এটি হ্যান্ডেল করবে
                if not (current_ffmpeg_process and current_ffmpeg_process.poll() is None) and \
                   not (next_video_download_thread and next_video_download_thread.is_alive()):
                    # শুধুমাত্র যদি এটিই কিউয়ের একমাত্র আইটেম হয়
                    if len(video_queue) == 1:
                         should_start_predownload = True

            if should_start_predownload:
                 logging.info("FFmpeg বন্ধ এবং অন্য কোনো ডাউনলোড চলছে না। নতুন আইটেমের প্রি-ডাউনলোড শুরু হচ্ছে...")
                 start_next_video_download()

            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL! অনুগ্রহ করে http:// বা https:// দিয়ে শুরু হওয়া একটি URL দিন।', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_queue', methods=['POST'])
def clear_queue():
    with stream_lock:
        video_queue.clear()
        logging.info("🗑️ ভিডিও কিউ খালি করা হয়েছে।")
        # চলমান প্রি-ডাউনলোড বন্ধ করার চেষ্টা করা উচিত (যদিও কঠিন)
        global next_video_url_to_download, next_video_download_path, next_video_download_thread
        if next_video_download_thread and next_video_download_thread.is_alive():
             logging.warning("কিউ খালি করা হয়েছে, কিন্তু একটি প্রি-ডাউনলোড চলছিল। এটি নিজে শেষ হবে।")
             # স্টেট রিসেট করুন
             next_video_url_to_download = None
             next_video_download_path = None
             next_video_download_thread = None
             next_video_ready_event.set() # বাতিল বোঝাতে সেট করুন

        flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_played', methods=['POST'])
def clear_played():
    with stream_lock:
        played_today.clear()
        logging.info("🗑️ 'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।")
        flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
    return redirect(url_for('admin_panel'))

@app.route('/stream/<path:filename>')
def stream(filename):
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    file_abs_path = os.path.abspath(os.path.join(stream_abs_path, filename))
    if not file_abs_path.startswith(stream_abs_path):
        logging.warning(f"🚫 নিরাপত্তা লঙ্ঘন প্রচেষ্টা: {filename}")
        abort(404)
    if not os.path.exists(file_abs_path):
        abort(404)

    response = send_from_directory(stream_abs_path, filename, conditional=True)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    # response.headers['Access-Control-Allow-Origin'] = '*' # CORS হেডার Flask-CORS দ্বারা হ্যান্ডেল করা উচিত
    return response

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার ---
def signal_handler(sig, frame):
    logging.info("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set() # সব থ্রেডকে বন্ধ করার জন্য ইভেন্ট সেট করুন
    logging.info("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    # প্রধান থ্রেড এখানে কিছুক্ষণ অপেক্ষা করতে পারে
    time.sleep(2)
    logging.info("👋 অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    os._exit(0) # ফোর্স এক্সিট

# --- প্রধান চালক ---
if __name__ == '__main__':
    print("*"*50)
    logging.info("🚀 অ্যাপ্লিকেশন শুরু হচ্ছে...")
    logging.info(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    logging.info(f"🌍 Flask অ্যাপ চালু হচ্ছে http://0.0.0.0:5000 এ...")
    logging.info(f"🔑 অ্যাডমিন প্যানেল: http://127.0.0.1:5000/admin (অথবা আপনার সার্ভার আইপি)")

    try:
        app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False, debug=False)
    except KeyboardInterrupt: # Use KeyboardInterrupt for graceful exit if run() somehow exits
        logging.info("KeyboardInterrupt received in main thread.")
        stop_event.set()

    logging.info("Flask অ্যাপ স্বাভাবিকভাবে বন্ধ হয়েছে।")
    if manager_thread.is_alive():
         logging.info("ম্যানেজার থ্রেড শেষ হওয়ার জন্য অপেক্ষা করা হচ্ছে...")
         manager_thread.join(timeout=5)
    stop_ffmpeg_stream() # Final check
    logging.info("প্রধান থ্রেড শেষ হয়েছে।")
    print("*"*50)
