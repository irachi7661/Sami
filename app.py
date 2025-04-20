import os
import subprocess
import threading
import time
import signal
import requests
import hashlib
from flask import Flask, render_template, send_from_directory, abort, request, redirect, url_for, flash
from flask_cors import CORS
from collections import deque # ভিডিও কিউয়ের জন্য
import queue # থ্রেড থেকে স্ট্যাটাস পাওয়ার জন্য (অপশনাল)

# --- কনফিগারেশন ---
DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts"

VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
HLS_OUTPUT_FILE = os.path.join(STREAM_OUTPUT_DIR, "stream.m3u8")

# গ্লোবাল ভেরিয়েবল
video_queue = deque()
played_today = set()
current_ffmpeg_process = None
stop_event = threading.Event()
stream_lock = threading.Lock() # কিউ, FFmpeg প্রসেস এবং প্রি-ডাউনলোড স্টেটাস সিঙ্ক্রোনাইজ করার জন্য
currently_playing_url = None
default_video_path = None

# --- প্রি-ডাউনলোড স্টেটাস ভেরিয়েবল ---
next_video_url_to_download = None
next_video_download_path = None
next_video_download_thread = None
next_video_ready_event = threading.Event() # ডাউনলোড শেষ হলে এই ইভেন্ট সেট হবে
next_download_failed = False # ব্যাকগ্রাউন্ড ডাউনলোড ব্যর্থ হয়েছে কিনা জানার জন্য

app = Flask(__name__)
CORS(app)
app.secret_key = os.urandom(24)

# --- ডিরেক্টরি তৈরি ---
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

# --- Helper Functions ---

def get_safe_filename(url):
    """URL থেকে একটি নিরাপদ ফাইলের নাম তৈরি করে (হ্যাশ ব্যবহার করে)"""
    hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
    try:
        base_name = os.path.basename(url.split('?')[0])
        _, ext = os.path.splitext(base_name)
        if not ext or len(ext) > 5: ext = '.mp4'
    except Exception:
        ext = '.mp4'
    if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv']: ext = '.mp4'
    return f"video_{hashed_url}{ext}"

def download_video(url, output_filename, download_event=None, failure_flag_setter=None):
    """
    একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে।
    যদি download_event ও failure_flag_setter দেওয়া হয়, তবে ব্যাকগ্রাউন্ড থ্রেডের জন্য উপযুক্ত।
    """
    filepath = os.path.join(VIDEO_DIR, output_filename)
    success = False
    try:
        if os.path.exists(filepath) and output_filename != DEFAULT_VIDEO_FILENAME:
            if os.path.getsize(filepath) > 0:
                print(f"  [ডাউনলোড] '{output_filename}' আগে থেকেই আছে।")
                success = True
                return filepath # যদি আগে থেকেই থাকে, সফলভাবে রিটার্ন করুন

        print(f"  [ডাউনলোড শুরু] {url[:70]}... -> {output_filename}")
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True) # টাইমআউট বাড়ান যায়
        response.raise_for_status()

        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 4): # চাঙ্ক সাইজ বাড়ানো যেতে পারে
                if stop_event.is_set():
                    print(f"  [ডাউনলোড বাতিল] {output_filename}")
                    if os.path.exists(filepath): os.remove(filepath)
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

        if downloaded_size == 0:
            print(f"  [ডাউনলোড ব্যর্থ] ফাইলের সাইজ ০: {output_filename}")
            if os.path.exists(filepath): os.remove(filepath)
            return None

        print(f"  [ডাউনলোড সফল] {output_filename} (Size: {downloaded_size / (1024*1024):.2f} MB)")
        success = True
        return filepath

    except requests.exceptions.Timeout:
        print(f"  [ডাউনলোড টাইমআউট] {url[:70]}...")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except requests.exceptions.RequestException as e:
        print(f"  [ডাউনলোড ব্যর্থ] {url[:70]}... : {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except Exception as e:
        print(f"  [ডাউনলোড ত্রুটি] {url[:70]}... : {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    finally:
        # যদি এটি ব্যাকগ্রাউন্ড থ্রেড থেকে কল করা হয়
        if download_event:
            if not success and failure_flag_setter:
                failure_flag_setter() # ব্যর্থতা চিহ্নিত করুন
            download_event.set() # ইভেন্ট সেট করুন (সফল বা ব্যর্থ উভয় ক্ষেত্রেই)

def download_next_video_thread_func(url, filename, event, lock):
    """ব্যাকগ্রাউন্ডে পরবর্তী ভিডিও ডাউনলোড করার থ্রেড ফাংশন"""
    global next_download_failed
    print(f"  🚀 প্রি-ডাউনলোড থ্রেড শুরু: {filename}")

    # ব্যর্থতা চিহ্নিত করার জন্য একটি লোকাল ফাংশন যা লক ব্যবহার করে
    def set_failure_flag():
        with lock:
            global next_download_failed
            next_download_failed = True
            print(f"  ❌ প্রি-ডাউনলোড ব্যর্থতা চিহ্নিত: {filename}")

    downloaded_path = download_video(url, filename, download_event=event, failure_flag_setter=set_failure_flag)

    if downloaded_path:
        print(f"  ✅ প্রি-ডাউনলোড থ্রেড সম্পন্ন: {filename}")
    else:
        # ব্যর্থতা ইতিমধ্যে set_failure_flag দ্বারা চিহ্নিত হয়েছে
        pass
        # ইভেন্ট download_video ফাংশনের finally ব্লকে সেট করা হয়েছে

def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস বন্ধ করে"""
    global current_ffmpeg_process, currently_playing_url
    with stream_lock:
        process_to_stop = current_ffmpeg_process
        if process_to_stop:
            print(f"  [FFmpeg] বন্ধ করা হচ্ছে (PID: {process_to_stop.pid})...")
            if process_to_stop.poll() is None:
                try:
                    process_to_stop.terminate()
                    process_to_stop.wait(timeout=3) # কম টাইমআউট যথেষ্ট হতে পারে
                except subprocess.TimeoutExpired:
                    print("  [FFmpeg] terminate হয়নি, kill করা হচ্ছে...")
                    process_to_stop.kill()
                    process_to_stop.wait()
                except Exception as e:
                    print(f"  [FFmpeg] বন্ধ করার সময় ত্রুটি: {e}")
            else:
                 print("  [FFmpeg] বন্ধ করার সময় দেখা গেলো এটি আগে থেকেই বন্ধ।")

            # শুধুমাত্র যদি এই প্রসেসটিই বর্তমানে registrado থাকে
            if current_ffmpeg_process == process_to_stop:
                 current_ffmpeg_process = None
                 # currently_playing_url এখানে রিসেট করা ঠিক না, ম্যানেজার লুপ করবে

def start_ffmpeg_stream(video_path, loop=False):
    """একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg স্ট্রিম শুরু করে"""
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        print(f"❌ [FFmpeg] শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    ffmpeg_cmd_base = ['ffmpeg', '-re']
    if loop: ffmpeg_cmd_base.extend(['-stream_loop', '-1'])
    ffmpeg_cmd_base.extend(['-i', abs_video_path])

    ffmpeg_cmd_options = [
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-b:v', '1500k', '-maxrate', '1500k', '-bufsize', '3000k', '-g', '60',
        '-vf', 'scale=640:360',
        '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '44100',
        '-f', 'hls', '-hls_time', '4', '-hls_list_size', '5',
        '-hls_flags', 'delete_segments+omit_endlist',
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%03d.ts'),
        HLS_OUTPUT_FILE
    ]
    ffmpeg_command = ffmpeg_cmd_base + ffmpeg_cmd_options

    print(f"🚀 [FFmpeg] কমান্ড: {' '.join(ffmpeg_command)}")
    try:
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        def log_stderr(proc, path):
            if proc.stderr:
                for line in iter(proc.stderr.readline, b''):
                    if stop_event.is_set(): break
                    line_str = line.decode(errors='ignore').strip()
                    # if line_str and ('frame=' in line_str or 'error' in line_str.lower()): # শুধু দরকারি লাইন প্রিন্ট করা
                    if line_str:
                        print(f"    [ffmpeg-{proc.pid}] {line_str}")
            print(f"    [ffmpeg-{proc.pid} stderr রিডিং শেষ]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True)
        stderr_thread.start()

        print(f"✅ [FFmpeg] শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)}, লুপ: {loop}")
        with stream_lock:
            current_ffmpeg_process = process
        return process

    except FileNotFoundError:
        print(f"❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি।")
        with stream_lock: current_ffmpeg_process = None
        return None
    except Exception as e:
        print(f"❌ [FFmpeg] শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")
        with stream_lock: current_ffmpeg_process = None
        return None

# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার (প্রি-ডাউনলোড লজিক সহ) ---
def stream_manager():
    """
    ব্যাকগ্রাউন্ডে চলে, ভিডিও কিউ এবং FFmpeg প্রসেস ম্যানেজ করে।
    পরবর্তী ভিডিও প্রি-ডাউনলোড করার চেষ্টা করে।
    """
    global currently_playing_url, default_video_path, current_ffmpeg_process
    global next_video_url_to_download, next_video_download_path, next_video_download_thread, next_video_ready_event, next_download_failed

    print("⏳ ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা চলছে...")
    temp_default_path = download_video(DEFAULT_VIDEO_URL, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
        default_video_path = temp_default_path
        print(f"✅ ডিফল্ট ভিডিও প্রস্তুত: {default_video_path}")
    else:
        print("🚨 সতর্কতা: ডিফল্ট ভিডিও ডাউনলোড করা যায়নি!")

    while not stop_event.is_set():
        try:
            # --- স্টেটাস চেক এবং পরবর্তী অ্যাকশন নির্ধারণ ---
            current_video_path_to_play = None
            url_to_play = None
            play_looped = False
            start_next_download_info = None # (url, filename) Tuple

            with stream_lock:
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None

                # --- কেস ১: FFmpeg চলছে ---
                if ffmpeg_is_running:
                    # ডিফল্ট ভিডিও ইন্টারাপশন চেক
                    if currently_playing_url == DEFAULT_VIDEO_URL and video_queue:
                        print("🔄 ডিফল্ট ভিডিও চলছিল, কিন্তু কিউতে আইটেম এসেছে। ডিফল্ট বন্ধ করা হচ্ছে...")
                        stop_ffmpeg_stream() # লক এর মধ্যেই কল করা হচ্ছে
                        ffmpeg_is_running = False # স্ট্যাটাস আপডেট
                        # প্রি-ডাউনলোড থ্রেড যদি চলে, তাকেও বন্ধ করা বা ইগনোর করা দরকার? আপাতত ইগনোর করি।
                        if next_video_download_thread and next_video_download_thread.is_alive():
                             print("  ⚠️ ডিফল্ট বন্ধ করার সময় প্রি-ডাউনলোড চলছিল, সেটি চলতে দেওয়া হচ্ছে (যদি পরের আইটেম সেটাই হয়)...")

                    else:
                        # FFmpeg চলছে, প্রি-ডাউনলোড শুরু করা দরকার কিনা দেখি
                        # (যদি ইতিমধ্যে চালু না থাকে এবং এটি ডিফল্ট ভিডিও না হয়)
                        if currently_playing_url != DEFAULT_VIDEO_URL and not next_video_url_to_download and video_queue:
                             peek_next_url = video_queue[0] # শুধু দেখুন, তুলবেন না
                             next_filename = get_safe_filename(peek_next_url)
                             print(f"  [প্রি-ডাউনলোড] কিউতে পরবর্তী ({peek_next_url[:70]}...) পাওয়া গেছে, ডাউনলোড শুরু করার প্রস্তুতি।")
                             # স্টেটাস সেট করুন যাতে লকের বাইরে থ্রেড শুরু করা যায়
                             start_next_download_info = (peek_next_url, next_filename)
                             next_video_url_to_download = peek_next_url # ভবিষ্যতের জন্য স্টোর করুন
                             next_video_download_path = os.path.join(VIDEO_DIR, next_filename)

                        time.sleep(0.5) # FFmpeg চললে অল্প অপেক্ষা
                        continue # লুপের পরবর্তী ইটারেশন

                # --- কেস ২: FFmpeg বন্ধ আছে বাพึ่ง শেষ হয়েছে ---
                else:
                    # যদি প্রসেস শেষ হয়ে থাকে, রিসোর্স ক্লিন করুন
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        print(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) শেষ হয়েছে।")
                        if currently_playing_url and currently_playing_url != DEFAULT_VIDEO_URL:
                            played_today.add(currently_playing_url)
                        current_ffmpeg_process = None
                        # currently_playing_url এখানে রিসেট হবে না, নিচে সেট হবে যদি নতুন ভিডিও প্লে হয়

                    # পরবর্তী ভিডিও খুঁজুন
                    # --- প্রথম চেষ্টা: প্রি-ডাউনলোড করা ভিডিও ---
                    if next_video_url_to_download and next_video_download_path:
                        print(f"  [প্রি-ডাউনলোড] স্ট্যাটাস চেক করা হচ্ছে: {os.path.basename(next_video_download_path)}")
                        is_ready = next_video_ready_event.wait(timeout=0.1) # অল্প সময় অপেক্ষা করে দেখি রেডি কিনা

                        if not is_ready and next_video_download_thread and next_video_download_thread.is_alive():
                             print("  [প্রি-ডাউনলোড] এখনও চলছে, শেষ হওয়ার জন্য অপেক্ষা করা হচ্ছে (সর্বোচ্চ ১০ সেকেন্ড)...")
                             next_video_download_thread.join(timeout=10) # থ্রেড শেষ হওয়ার জন্য অপেক্ষা
                             is_ready = next_video_ready_event.is_set() # আবার চেক

                        if is_ready:
                            print(f"  [প্রি-ডাউনলোড] ইভেন্ট সেট, ডাউনলোড সম্পন্ন বা ব্যর্থ।")
                            if not next_download_failed and os.path.exists(next_video_download_path) and os.path.getsize(next_video_download_path) > 0:
                                print(f"  ✅ [প্রি-ডাউনলোড] ভিডিও প্রস্তুত: {os.path.basename(next_video_download_path)}")
                                current_video_path_to_play = next_video_download_path
                                url_to_play = next_video_url_to_download
                                play_looped = False
                                # প্রি-ডাউনলোড স্টেটাস রিসেট করুন
                                next_video_url_to_download = None
                                next_video_download_path = None
                                next_video_download_thread = None
                                next_video_ready_event.clear()
                                next_download_failed = False
                            else:
                                print(f"  ❌ [প্রি-ডাউনলোড] ব্যর্থ হয়েছে বা ফাইল সমস্যাযুক্ত: {next_video_url_to_download[:70]}...")
                                # স্টেটাস রিসেট করুন এবং স্বাভাবিক ফ্লোতে যান
                                next_video_url_to_download = None
                                next_video_download_path = None
                                next_video_download_thread = None
                                next_video_ready_event.clear()
                                next_download_failed = False
                                # এখানে continue না করে নিচে কিউ চেক করতে দিন
                        else:
                             print("  ⚠️ [প্রি-ডাউনলোড] অপেক্ষা করার পরও রেডি হয়নি বা থ্রেড শেষ হয়নি। স্বাভাবিক পদ্ধতিতে যাওয়া হচ্ছে।")
                             # স্টেটাস রিসেট করুন
                             next_video_url_to_download = None
                             next_video_download_path = None
                             # থ্রেডকে চলতে দেওয়া যেতে পারে, অথবা বন্ধ করার চেষ্টা করা? আপাতত চলতে দেই।
                             # next_video_download_thread = None # থ্রেড হ্যান্ডেল হারাবেন
                             next_video_ready_event.clear()
                             next_download_failed = False
                             # নিচে কিউ চেক করতে দিন

                    # --- দ্বিতীয় চেষ্টা: অ্যাডমিন কিউ (যদি প্রি-ডাউনলোড ব্যবহার না হয় বা ব্যর্থ হয়) ---
                    if not current_video_path_to_play and video_queue:
                         url_to_play = video_queue.popleft()
                         print(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে: {url_to_play[:70]}...")
                         filename = get_safe_filename(url_to_play)
                         # সিঙ্ক্রোনাস ডাউনলোড
                         print(f"  ⏳ সিঙ্ক্রোনাস ডাউনলোড শুরু হচ্ছে: {filename}")
                         sync_download_path = download_video(url_to_play, filename)
                         if sync_download_path:
                             print(f"  ✅ সিঙ্ক্রোনাস ডাউনলোড সফল: {filename}")
                             current_video_path_to_play = sync_download_path
                             play_looped = False
                         else:
                             print(f"  ❌ সিঙ্ক্রোনাস ডাউনলোড ব্যর্থ: {url_to_play[:70]}...")
                             url_to_play = None # প্লে করা যাবে না
                             continue # পরবর্তী ইটারেশনে যান

                    # --- তৃতীয় চেষ্টা: ডিফল্ট ভিডিও (যদি অন্য কিছু না থাকে) ---
                    elif not current_video_path_to_play and default_video_path:
                        print("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও ব্যবহার করা হচ্ছে।")
                        current_video_path_to_play = default_video_path
                        url_to_play = DEFAULT_VIDEO_URL
                        play_looped = True

                    # --- কিছুই করার নেই ---
                    elif not current_video_path_to_play:
                        print("⏳ অ্যাডমিন কিউ খালি এবং ডিফল্ট ভিডিও নেই। অপেক্ষা...")
                        time.sleep(5)
                        continue

            # --- অ্যাকশন ফেজ (লকের বাইরে) ---

            # প্লে করার জন্য ভিডিও পাওয়া গেলে FFmpeg শুরু করুন
            if current_video_path_to_play and url_to_play:
                 with stream_lock: # currently_playing_url সেট করার জন্য লক দরকার
                     currently_playing_url = url_to_play
                 # FFmpeg শুরু করুন
                 started_process = start_ffmpeg_stream(current_video_path_to_play, loop=play_looped)

                 if started_process:
                     # FFmpeg সফলভাবে শুরু হলে, পরবর্তী ভিডিওর প্রি-ডাউনলোড শুরু করুন (যদি থাকে)
                     if not play_looped: # যদি ডিফল্ট ভিডিও না হয়
                         with stream_lock:
                             if video_queue and not next_video_url_to_download: # যদি কিউতে আইটেম থাকে এবং প্রি-ডাউনলোড চালু না থাকে
                                 peek_next_url = video_queue[0]
                                 next_filename = get_safe_filename(peek_next_url)
                                 print(f"  [প্রি-ডাউনলোড] পরবর্তী ({peek_next_url[:70]}...) পাওয়া গেছে, ডাউনলোড শুরু করার প্রস্তুতি।")
                                 start_next_download_info = (peek_next_url, next_filename)
                                 next_video_url_to_download = peek_next_url
                                 next_video_download_path = os.path.join(VIDEO_DIR, next_filename)
                     time.sleep(1) # FFmpeg স্টার্ট হওয়ার জন্য অল্প সময় দিন
                 else:
                     # FFmpeg শুরু ব্যর্থ হলে, URL রিসেট করুন
                     with stream_lock:
                         currently_playing_url = None
                     time.sleep(3) # ব্যর্থতার পর একটু অপেক্ষা

            # যদি প্রি-ডাউনলোড শুরু করার তথ্য থাকে (লকের বাইরে থ্রেড শুরু করা ভালো)
            if start_next_download_info:
                 next_url, next_file = start_next_download_info
                 with stream_lock: # থ্রেড শুরু করার আগে স্টেটাস ভেরিয়েবল ঠিক আছে কিনা নিশ্চিত করুন
                     # ইভেন্ট রিসেট করুন এবং ব্যর্থতার ফ্ল্যাগ পরিষ্কার করুন
                     next_video_ready_event.clear()
                     next_download_failed = False
                     # থ্রেড তৈরি এবং শুরু করুন
                     next_video_download_thread = threading.Thread(
                         target=download_next_video_thread_func,
                         args=(next_url, next_file, next_video_ready_event, stream_lock), # লক পাস করা হচ্ছে
                         daemon=True # ডেইমন থ্রেড যাতে মূল প্রোগ্রাম বন্ধ হলে এটিও বন্ধ হয়ে যায়
                     )
                     next_video_download_thread.start()
                     print(f"  [প্রি-ডাউনলোড] থ্রেড '{next_video_download_thread.name}' শুরু করা হয়েছে: {next_file}")


        except Exception as e:
            print(f"🚨🚨 স্ট্রিম ম্যানেজার লুপে মারাত্মক ত্রুটি: {e} 🚨🚨")
            import traceback
            traceback.print_exc()
            # সমস্যা গুরুতর হলে থ্রেড বন্ধ করে দেওয়া যেতে পারে
            # stop_event.set()
            time.sleep(10) # মারাত্মক ত্রুটির পর দীর্ঘক্ষণ অপেক্ষা

    # --- থ্রেড বন্ধ হওয়ার সময় ---
    print("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    # নিশ্চিত করুন FFmpeg বন্ধ হয়েছে
    stop_ffmpeg_stream()
    # ব্যাকগ্রাউন্ড ডাউনলোড থ্রেড যদি চলে, তাকে বন্ধ করার সিগন্যাল দেওয়া যেতে পারে,
    # কিন্তু ডেইমন হওয়ায় এটি নিজে থেকেই বন্ধ হবে। অপেক্ষা করা যেতে পারে।
    # if next_video_download_thread and next_video_download_thread.is_alive():
    #    print("  ⏳ প্রি-ডাউনলোড থ্রেড শেষ হওয়ার জন্য অপেক্ষা...")
    #    next_video_download_thread.join(timeout=5)

# --- Flask Routes (No changes needed here, kept for completeness) ---
@app.route('/')
def index():
    """ব্যবহারকারীর জন্য প্লেয়ার পেজ রেন্ডার করে"""
    return render_template('index.html')

@app.route('/admin')
def admin_panel():
    """অ্যাডমিন প্যানেল দেখায়"""
    with stream_lock: # ডেটা পড়ার সময়ও লক ব্যবহার করা নিরাপদ
        queue_snapshot = list(video_queue)
        played_snapshot = list(played_today)
        current_url_snapshot = currently_playing_url
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        next_downloading = next_video_url_to_download

    if is_ffmpeg_running:
        if current_url_snapshot == DEFAULT_VIDEO_URL:
            current_status = "ডিফল্ট ভিডিও চলছে (লুপ)"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}..."
        else:
            current_status = "একটি ভিডিও চলছে (URL অজানা)"
    else:
        current_status = "কোনো ভিডিও চলছে না"

    if next_downloading:
         current_status += f" | প্রি-ডাউনলোডিং: {next_downloading[:60]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

@app.route('/admin/add', methods=['POST'])
def add_video():
    """কিউতে নতুন ভিডিও URL যোগ করে"""
    url = request.form.get('video_url')
    if url:
        if url.startswith('http://') or url.startswith('https://'):
            with stream_lock:
                video_queue.append(url)
                print(f"📥 কিউতে যোগ করা হয়েছে: {url}")
                flash(f'"{url[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL! অনুগ্রহ করে http:// বা https:// দিয়ে শুরু হওয়া একটি URL দিন।', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_queue', methods=['POST'])
def clear_queue():
    """ভিডিও কিউ খালি করে"""
    with stream_lock:
        video_queue.clear()
        # চলমান প্রি-ডাউনলোড বন্ধ করা বা বাতিল করা উচিত?
        global next_video_url_to_download, next_video_download_path, next_video_download_thread, next_download_failed
        next_video_url_to_download = None
        next_video_download_path = None
        # থ্রেডকে ইন্টারাপ্ট করা কঠিন, তবে স্টেটাস রিসেট করলে ম্যানেজার এটি ব্যবহার করবে না
        next_video_ready_event.clear()
        next_download_failed = False
        print("🗑️ ভিডিও কিউ এবং প্রি-ডাউনলোড স্টেটাস খালি করা হয়েছে।")
        flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_played', methods=['POST'])
def clear_played():
    """'আজকে চালানো হয়েছে' তালিকা খালি করে"""
    with stream_lock:
        played_today.clear()
        print("🗑️ 'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।")
        flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
    return redirect(url_for('admin_panel'))

@app.route('/stream/<path:filename>')
def stream(filename):
    """HLS ফাইল (.m3u8, .ts) সার্ভ করে"""
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    file_abs_path = os.path.abspath(os.path.join(stream_abs_path, filename))
    if not file_abs_path.startswith(stream_abs_path): abort(404)
    if not os.path.exists(file_abs_path): abort(404)

    response = send_from_directory(stream_abs_path, filename, conditional=True)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার ---
def signal_handler(sig, frame):
    print("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set() # সব থ্রেডকে বন্ধ করার জন্য ইভেন্ট সেট করুন (ডাউনলোড সহ)
    print("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    time.sleep(2)
    print("👋 অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    os._exit(0)

# --- প্রধান চালক ---
if __name__ == '__main__':
    print("🚀 অ্যাপ্লিকেশন শুরু হচ্ছে...")
    print(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')} ({time.tzname[0]})")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    print(f"🌍 Flask অ্যাপ চালু হচ্ছে http://0.0.0.0:5000 এ...")
    print(f"🔑 অ্যাডমিন প্যানেল: http://127.0.0.1:5000/admin (অথবা আপনার সার্ভার আইপি)")
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False, debug=False)

    print("Flask অ্যাপ স্বাভাবিকভাবে বন্ধ হয়েছে।")
    stop_event.set()
    if manager_thread.is_alive():
        manager_thread.join(timeout=5)
    stop_ffmpeg_stream()
    print("প্রধান থ্রেড শেষ হয়েছে।")
