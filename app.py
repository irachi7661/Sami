import os
import subprocess
import threading
import time
import signal
import requests
import hashlib
from flask import Flask, render_template, send_from_directory, abort, request, redirect, url_for, flash, jsonify
from flask_cors import CORS
from collections import deque # ভিডিও কিউয়ের জন্য
import traceback # বিস্তারিত এরর লগিং এর জন্য

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
stream_lock = threading.Lock() # কিউ এবং ffmpeg প্রসেস অ্যাক্সেসের জন্য লক
currently_playing_url = None
default_video_path = None

app = Flask(__name__)
CORS(app) # সব ডোমেইন থেকে অ্যাক্সেসের অনুমতি দিন
app.secret_key = os.urandom(24)

# --- ডিরেক্টরি তৈরি ---
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

# --- Helper Functions ---

def get_safe_filename(url):
    """URL থেকে একটি নিরাপদ ফাইলের নাম তৈরি করে (হ্যাশ ব্যবহার করে)"""
    hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10] # URL এর হ্যাশ
    try:
        base_name = os.path.basename(url.split('?')[0])
        _, ext = os.path.splitext(base_name)
        if not ext or len(ext) > 5:
             ext = '.mp4' # ডিফল্ট এক্সটেনশন
    except Exception:
        ext = '.mp4' # এরর হলে ডিফল্ট

    # গ্রহণযোগ্য ভিডিও এক্সটেনশন চেক
    if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m3u8']:
         ext = '.mp4' # অগ্রহণযোগ্য হলে ডিফল্ট

    return f"video_{hashed_url}{ext}"

def download_video(url, output_filename):
    """একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে"""
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        # ফাইল আগে থেকেই থাকলে এবং খালি না হলে ডাউনলোড এড়িয়ে যান
        if os.path.exists(filepath):
            try:
                if os.path.getsize(filepath) > 0:
                    print(f"ℹ️ '{output_filename}' আগে থেকেই ডাউনলোড করা আছে এবং খালি নয়। ডাউনলোড করা হচ্ছে না।")
                    return filepath
                else:
                    print(f"⚠️ '{output_filename}' আগে থেকেই ছিল কিন্তু খালি। আবার ডাউনলোড করা হচ্ছে।")
            except OSError as e:
                 print(f"⚠️ ফাইল সাইজ চেক করতে সমস্যা '{filepath}': {e}। আবার ডাউনলোড করা হচ্ছে।")

        print(f"⏬ ডাউনলোড শুরু হচ্ছে: {url} -> {filepath}")
        # ইউজার এজেন্ট সেট করা, কিছু সার্ভার বট ব্লক করে
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True) # রিডাইরেক্ট ফলো করুন
        response.raise_for_status() # HTTP এরর চেক

        # Content-Type চেক (সম্ভাব্য নন-ভিডিও ফাইল সনাক্ত করার চেষ্টা)
        content_type = response.headers.get('content-type', '').lower()
        problematic_types = ['text/html', 'application/json'] # এগুলো ভিডিও হওয়ার সম্ভাবনা কম
        is_likely_video = 'video' in content_type or 'mpegurl' in content_type or 'octet-stream' in content_type or not any(ptype in content_type for ptype in problematic_types)

        if not is_likely_video:
             print(f"⚠️ সতর্কতা: Content-Type '{content_type}' ভিডিও মনে হচ্ছে না ({url})। তবুও ডাউনলোড করার চেষ্টা করা হচ্ছে...")

        # ফাইল লেখা
        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 4): # চাঙ্ক সাইজ বাড়ানো হয়েছে
                if stop_event.is_set(): # অ্যাপ বন্ধ হয়ে গেলে ডাউনলোড বাতিল
                    print("🛑 ডাউনলোড বাতিল করা হয়েছে (অ্যাপ বন্ধ)।")
                    if os.path.exists(filepath): os.remove(filepath)
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

        # ডাউনলোড শেষে ফাইল সাইজ চেক
        if downloaded_size == 0:
             print(f"❌ ডাউনলোড সম্পন্ন হয়েছে কিন্তু ফাইলের সাইজ ০ ({filepath})। সম্ভবত সমস্যা আছে।")
             if os.path.exists(filepath): os.remove(filepath) # খালি ফাইল মুছে ফেলা
             return None

        print(f"✅ সফলভাবে ডাউনলোড হয়েছে: {output_filename} (Size: {downloaded_size / (1024 * 1024):.2f} MB)")
        return filepath

    except requests.exceptions.Timeout:
        print(f"❌ ভিডিও ডাউনলোড টাইমআউট ({url})")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে ফাইল ডিলিট
        return None
    except requests.exceptions.SSLError as e:
        print(f"❌ SSL ত্রুটি ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ ভিডিও ডাউনলোড ব্যর্থ ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except Exception as e:
        print(f"❌ ভিডিও সংরক্ষণ বা অন্য কোনো ত্রুটি ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None

def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস নিরাপদে বন্ধ করে"""
    global current_ffmpeg_process
    with stream_lock: # লক নিশ্চিত করুন
        process_to_stop = current_ffmpeg_process
        if process_to_stop and process_to_stop.poll() is None: # প্রসেস কি সত্যিই চলছে?
            print(f"⏳ FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {process_to_stop.pid})...")
            try:
                if os.name == 'nt': # উইন্ডোজের জন্য
                    subprocess.run(['taskkill', '/F', '/PID', str(process_to_stop.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    print("   -> FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (taskkill)।")
                else: # লিনাক্স/ম্যাকের জন্য
                    process_to_stop.terminate() # প্রথমে SIGTERM পাঠান
                    try:
                        process_to_stop.wait(timeout=5) # বন্ধ হওয়ার জন্য অপেক্ষা করুন
                        print("   -> FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (terminate)।")
                    except subprocess.TimeoutExpired: # যদি terminate কাজ না করে
                        print("   -> FFmpeg প্রসেস terminate হয়নি, SIGKILL পাঠানো হচ্ছে...")
                        process_to_stop.kill() # SIGKILL পাঠান
                        process_to_stop.wait() # নিশ্চিত করুন বন্ধ হয়েছে
                        print("   -> FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (kill)।")
            except Exception as e:
                print(f"⚠️ FFmpeg (PID: {process_to_stop.pid}) বন্ধ করার সময় ত্রুটি: {e}")
        elif process_to_stop:
             print("ℹ️ FFmpeg প্রসেস বন্ধ করার চেষ্টা করার সময় দেখা গেলো এটি আগে থেকেই বন্ধ ছিল।")
        else:
             print("ℹ️ কোনো FFmpeg প্রসেস বন্ধ করার জন্য পাওয়া যায়নি।")

        # গ্লোবাল ভেরিয়েবল আপডেট
        if current_ffmpeg_process == process_to_stop:
             current_ffmpeg_process = None


def start_ffmpeg_stream(video_path, loop=False):
    """
    একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg HLS স্ট্রিম শুরু করে।
    ভিডিও স্ট্রিম কপি করে, অডিও AAC তে এনকোড করে।
    """
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        print(f"❌ FFmpeg শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    # চলমান প্রসেস থাকলে বন্ধ করুন (নিরাপত্তার জন্য)
    print("   -> শুরু করার আগে পুরনো FFmpeg প্রসেস (যদি থাকে) বন্ধ করা হচ্ছে...")
    stop_ffmpeg_stream()
    time.sleep(0.2) # বন্ধ হওয়ার জন্য একটু সময় দিন

    # পুরাতন সেগমেন্ট ফাইল মুছে ফেলা
    print(f"   -> পুরনো HLS সেগমেন্ট ফাইল মুছে ফেলা হচ্ছে ({STREAM_OUTPUT_DIR})...")
    try:
        if os.path.exists(STREAM_OUTPUT_DIR):
             for f in os.listdir(STREAM_OUTPUT_DIR):
                 if f.endswith('.ts') or f.endswith('.m3u8'):
                     try:
                         os.remove(os.path.join(STREAM_OUTPUT_DIR, f))
                     except OSError as e:
                         print(f"⚠️ পুরনো সেগমেন্ট মুছতে সমস্যা: {e}")
        else:
             os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)
    except Exception as e:
        print(f"⚠️ স্ট্রিম আউটপুট ডিরেক্টরি পরিষ্কার করতে সমস্যা: {e}")


    ffmpeg_command_base = [
        'ffmpeg',
        '-re', # ইনপুট নেটিভ ফ্রেমরেটে পড়ুন
    ]

    # লুপ করার অপশন
    if loop:
        ffmpeg_command_base.extend(['-stream_loop', '-1'])

    ffmpeg_command_base.extend(['-i', abs_video_path])

    # FFmpeg অপশনস (ভিডিও কপি, অডিও এনকোড)
    ffmpeg_command_options = [
        # ভিডিও অপশনস: ভিডিও স্ট্রিম সরাসরি কপি (দ্রুত, কোয়ালিটি লস নেই)
        '-c:v', 'copy',

        # অডিও অপশনস: অডিও স্ট্রিম AAC তে এনকোড (সাধারণত সামঞ্জস্যপূর্ণ)
        '-c:a', 'aac',      # অডিও কোডেক AAC
        '-b:a', '128k',     # অডিও বিটরেট
        '-ac', '2',         # স্টেরিও অডিও চ্যানেল
        '-ar', '44100',     # অডিও স্যাম্পল রেট

        # HLS আউটপুট অপশনস
        '-f', 'hls',                     # আউটপুট ফরম্যাট HLS
        '-hls_time', '4',                # সেগমেন্ট দৈর্ঘ্য (সেকেন্ড)
        '-hls_list_size', '6',           # প্লেলিস্টে ফাইলের সংখ্যা (পুরনো সেগমেন্ট ডিলিট হবে)
        '-hls_flags', 'delete_segments+omit_endlist+program_date_time', # পুরনো সেগমেন্ট মুছুন, লাইভ স্ট্রিমের জন্য endlist বাদ দিন, টাইমস্ট্যাম্প যোগ করুন
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%05d.ts'), # সেগমেন্ট ফাইলের নাম প্যাটার্ন
        HLS_OUTPUT_FILE                  # মাস্টার প্লেলিস্ট ফাইলের নাম
    ]

    ffmpeg_command = ffmpeg_command_base + ffmpeg_command_options

    print("🚀 FFmpeg কমান্ড (ভিডিও কপি):", " ".join(f'"{arg}"' if ' ' in arg else arg for arg in ffmpeg_command)) # স্পেস সহ আর্গুমেন্ট কোট করুন

    try:
        # DEVNULL stdout ব্যবহার করে টার্মিনাল ক্ল্যাটার কমানো
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

        # stderr লগিং এর জন্য আলাদা থ্রেড (শুধুমাত্র এরর দেখানোর জন্য)
        def log_stderr(proc, path):
            if proc.stderr:
                try:
                    for line in iter(proc.stderr.readline, b''):
                        if stop_event.is_set(): break
                        line_str = line.decode('utf-8', errors='replace').strip()
                        if line_str:
                             # '-c copy' ব্যবহার করার সময় কিছু warning স্বাভাবিক, যেমন timestamp বা keyframe সংক্রান্ত
                             if 'warning' in line_str.lower() or 'error' in line_str.lower() or 'failed' in line_str.lower():
                                print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
                             # else: # ডিবাগিং এর জন্য সব লাইন দেখতে চাইলে এটি আনকমেন্ট করুন
                             #    print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
                except Exception as e:
                     print(f"⚠️ FFmpeg stderr পড়তে সমস্যা: {e}")
                finally:
                     if proc.stderr: proc.stderr.close() # stderr বন্ধ করুন
            print(f"  [FFmpeg stderr রিডিং থ্রেড শেষ - {os.path.basename(path)}]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True)
        stderr_thread.start()

        print(f"✅ FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)} [ভিডিও কপি], লুপ: {loop}")
        with stream_lock: # লক সহ গ্লোবাল ভেরিয়েবল আপডেট
            current_ffmpeg_process = process
        return process

    except FileNotFoundError:
        print(f"❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ যোগ করা আছে কিনা নিশ্চিত করুন।")
        with stream_lock: current_ffmpeg_process = None # ব্যর্থ হলে প্রসেস null করুন
        return None
    except Exception as e:
        print(f"❌ FFmpeg শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")
        print("   ℹ️ এটি ইনপুট ভিডিও কোডেক (H.264/AAC না?) বা HLS এর সাথে সামঞ্জস্যতার সমস্যার কারণে হতে পারে।")
        with stream_lock: current_ffmpeg_process = None # ব্যর্থ হলে প্রসেস null করুন
        return None

# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার ---
def stream_manager():
    """
    ব্যাকগ্রাউন্ডে চলে, ভিডিও কিউ এবং FFmpeg প্রসেস ম্যানেজ করে।
    অ্যাডমিন কিউকে অগ্রাধিকার দেয়। কিউ খালি থাকলে ডিফল্ট ভিডিও লুপ করে।
    কিউতে নতুন আইটেম আসলে ডিফল্ট ভিডিও বন্ধ করে।
    একটি ভিডিও চলার সময় পরের ভিডিওটি প্রি-ডাউনলোড করার চেষ্টা করে।
    """
    global currently_playing_url, default_video_path, current_ffmpeg_process

    print("⏳ ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা চলছে...")
    temp_default_path = download_video(DEFAULT_VIDEO_URL, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
         default_video_path = temp_default_path
         print(f"✅ ডিফল্ট ভিডিও প্রস্তুত: {default_video_path}")
    else:
         print("🚨 সতর্কতা: ডিফল্ট ভিডিও ডাউনলোড করা যায়নি! ডিফল্ট প্লেব্যাক কাজ করবে না।")

    predownload_attempted_for_url = None # কোন URL প্রি-ডাউনলোডের চেষ্টা করা হয়েছে

    while not stop_event.is_set():
        next_video_path = None
        play_url = None
        loop_default = False
        stop_default_and_process_queue = False # ডিফল্ট ভিডিও চলার সময় কিউতে আইটেম এলে এটি True হবে

        try:
            with stream_lock: # এক্সেস করার আগে লক নিন
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
                current_url_snapshot = currently_playing_url # বর্তমান অবস্থা কপি করুন

                # --- ডিসিশন লজিক ---

                # 1. FFmpeg চলছে?
                if ffmpeg_is_running:
                    # 1a. কিউ ভিডিও চলছে এবং কিউতে আরও আইটেম আছে? পরেরটা প্রি-ডাউনলোড করুন
                    if current_url_snapshot != DEFAULT_VIDEO_URL and video_queue:
                        next_url_in_queue = video_queue[0]
                        if next_url_in_queue != predownload_attempted_for_url:
                            print(f"🔎 প্রি-ডাউনলোডের জন্য চেক করা হচ্ছে: {next_url_in_queue[:80]}...")
                            next_filename = get_safe_filename(next_url_in_queue)
                            downloaded_path = download_video(next_url_in_queue, next_filename)
                            if downloaded_path:
                                print(f"👍 প্রি-ডাউনলোড সম্পন্ন বা ফাইল আগে থেকেই আছে: {next_filename}")
                            else:
                                print(f"👎 প্রি-ডাউনলোড ব্যর্থ: {next_url_in_queue[:80]}...")
                            predownload_attempted_for_url = next_url_in_queue # চেষ্টা করা হয়েছে বলে মার্ক করুন

                    # 1b. ডিফল্ট ভিডিও চলছে কিন্তু কিউতে নতুন আইটেম এসেছে? ডিফল্ট বন্ধ করতে হবে
                    elif current_url_snapshot == DEFAULT_VIDEO_URL and video_queue:
                        print("🔄 ডিফল্ট ভিডিও চলছিল, কিন্তু কিউতে নতুন আইটেম এসেছে। ডিফল্ট বন্ধ করা হচ্ছে...")
                        stop_default_and_process_queue = True
                        predownload_attempted_for_url = None # প্রি-ডাউনলোড রিসেট

                    # 1c. অন্যান্য ক্ষেত্রে (কিউ ভিডিও চলছে কিন্তু কিউ খালি, অথবা ডিফল্ট চলছে ও কিউ খালি): কিছু করার নেই
                    else:
                        # কিউ খালি হলে প্রি-ডাউনলোড রিসেট
                        if current_url_snapshot != DEFAULT_VIDEO_URL and not video_queue:
                            predownload_attempted_for_url = None
                        pass # শুধু অপেক্ষা করুন

                # 2. FFmpeg চলছে না?
                else:
                    predownload_attempted_for_url = None # প্রি-ডাউনলোড রিসেট
                    # 2a. আগের প্রসেস শেষ হয়েছে কিনা চেক করুন
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        print(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) স্বাভাবিকভাবে শেষ হয়েছে।")
                        if current_url_snapshot and current_url_snapshot != DEFAULT_VIDEO_URL:
                             played_today.add(current_url_snapshot) # প্লে করা লিস্টে যোগ করুন
                        current_ffmpeg_process = None # প্রসেস রিসেট
                        currently_playing_url = None # URL রিসেট

                    # 2b. কিউতে ভিডিও আছে?
                    if video_queue:
                        play_url = video_queue.popleft() # প্রথম আইটেমটি নিন
                        print(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে: {play_url[:80]}...")
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename) # ডাউনলোড করুন
                        if not next_video_path:
                            print(f"❌ ডাউনলোড ব্যর্থ (প্লে করার জন্য): {play_url[:80]}... এটি স্কিপ করা হলো।")
                            play_url = None # প্লে করা যাবে না
                            currently_playing_url = None # URL রিসেট
                        else:
                             loop_default = False # কিউ ভিডিও লুপ হয় না
                             currently_playing_url = play_url # বর্তমান URL সেট করুন

                    # 2c. কিউ খালি কিন্তু ডিফল্ট ভিডিও আছে?
                    elif default_video_path:
                        # যদি আগেরবার অন্য ভিডিও চলছিল, তবেই মেসেজ দেখান
                        if current_url_snapshot != DEFAULT_VIDEO_URL:
                             print("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                        next_video_path = default_video_path
                        play_url = DEFAULT_VIDEO_URL
                        loop_default = True # ডিফল্ট ভিডিও লুপ হবে
                        currently_playing_url = play_url # বর্তমান URL সেট করুন

                    # 2d. কিউ খালি এবং ডিফল্ট ভিডিও নেই?
                    else:
                        if current_url_snapshot: # যদি কিছু চলছিল আগে
                             print("⏳ অ্যাডমিন কিউ খালি এবং ডিফল্ট ভিডিও উপলব্ধ নেই। অপেক্ষা করা হচ্ছে...")
                        currently_playing_url = None # কিছুই চলছে না
                        pass # শুধু অপেক্ষা করুন

            # --- অ্যাকশন ---

            # যদি ডিফল্ট বন্ধ করার প্রয়োজন হয়
            if stop_default_and_process_queue:
                print("🛑 ডিফল্ট স্ট্রিম বন্ধ করা হচ্ছে...")
                stop_ffmpeg_stream()
                time.sleep(0.5) # বন্ধ হওয়ার জন্য একটু সময়
                continue # লুপের শুরুতে ফিরে যান পরের আইটেম প্রসেস করতে

            # যদি নতুন ভিডিও প্লে করার জন্য পাওয়া যায়
            if next_video_path and play_url:
                print(f"🎬 FFmpeg শুরু করা হচ্ছে... ভিডিও: {os.path.basename(next_video_path)}, লুপ: {loop_default}")
                started_process = start_ffmpeg_stream(next_video_path, loop=loop_default)
                if not started_process:
                     # FFmpeg শুরু না হলে, URL রিসেট করুন যাতে আবার চেষ্টা না করে
                     with stream_lock:
                         if currently_playing_url == play_url:
                             currently_playing_url = None
                             print(f"⚠️ ব্যর্থ URL '{play_url[:80]}...' প্লে করা গেলো না।")
                 # নতুন প্রসেস শুরু হলে কিছুক্ষণ অপেক্ষা করুন চালু হওয়ার জন্য
                time.sleep(0.5)

            # --- অপেক্ষা ---
            # FFmpeg চললে অল্প সময় পর পর চেক করুন
            if ffmpeg_is_running:
                 time.sleep(1)
            # FFmpeg না চললে এবং প্লে করার কিছু না থাকলে বেশি সময় অপেক্ষা করুন
            elif not next_video_path:
                 time.sleep(3)
            # অন্যথায় (যেমন,刚 শুরু হয়েছে বা বন্ধ হয়েছে) অল্প অপেক্ষা করুন
            else:
                 time.sleep(0.5)

        except Exception as e:
             print(f"🚨🚨 স্ট্রিম ম্যানেজার লুপে মারাত্মক ত্রুটি: {e} 🚨🚨")
             traceback.print_exc() # বিস্তারিত এরর প্রিন্ট করুন
             # নিরাপদে FFmpeg বন্ধ করার চেষ্টা করুন
             try:
                 stop_ffmpeg_stream()
             except Exception as stop_err:
                  print(f"🚨 ত্রুটির পর FFmpeg বন্ধ করতেও সমস্যা: {stop_err}")
             # অবস্থা রিসেট করার চেষ্টা করুন
             with stream_lock:
                 currently_playing_url = None
                 predownload_attempted_for_url = None
             print("🔁 ৫ সেকেন্ড পর স্ট্রিম ম্যানেজার রিস্টার্ট করার চেষ্টা...")
             time.sleep(5)

    print("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    # থ্রেড বন্ধ হওয়ার আগে নিশ্চিত করুন FFmpeg বন্ধ হয়েছে
    stop_ffmpeg_stream()

# --- Flask Routes ---

# HTML প্লেয়ার পেজ
@app.route('/')
def index():
    return render_template('index.html')

# HTML অ্যাডমিন প্যানেল
@app.route('/admin')
def admin_panel():
    with stream_lock: # ডেটা পড়ার সময়ও লক ব্যবহার করুন
        queue_snapshot = list(video_queue)
        played_snapshot = list(played_today)
        current_url_snapshot = currently_playing_url
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        status_detail = ""
        if is_ffmpeg_running and video_queue:
            next_in_queue = video_queue[0]
            status_detail = f" | এরপর কিউতে: {next_in_queue[:50]}..."

    if is_ffmpeg_running:
        mode = "[ভিডিও কপি]" if current_url_snapshot != DEFAULT_VIDEO_URL else "(লুপ)"
        if current_url_snapshot == DEFAULT_VIDEO_URL:
            current_status = f"ডিফল্ট ভিডিও চলছে {mode}{status_detail}"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}... {mode}{status_detail}"
        else:
            current_status = "একটি ভিডিও চলছে (URL অজানা)" # যদি কোনোভাবে URL null হয়ে যায়
    else:
        current_status = "⭕ কোনো ভিডিও চলছে না"
        if video_queue:
             current_status += f" | প্লে করার অপেক্ষায়: {video_queue[0][:50]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

# HTML ফর্ম থেকে ভিডিও যোগ
@app.route('/admin/add', methods=['POST'])
def add_video_form():
    url = request.form.get('video_url', '').strip()
    if url:
        if url.startswith('http://') or url.startswith('https://'):
            with stream_lock:
                if url in video_queue:
                     flash(f'"{url[:50]}..." এই URL টি ইতিমধ্যে কিউতে আছে।', 'warning')
                else:
                    video_queue.append(url)
                    print(f"📥 [অ্যাডমিন] কিউতে যোগ করা হয়েছে: {url}")
                    flash(f'"{url[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL! অনুগ্রহ করে http:// বা https:// দিয়ে শুরু হওয়া একটি URL দিন।', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

# HTML বাটন থেকে কিউ খালি করা
@app.route('/admin/clear_queue', methods=['POST'])
def clear_queue_form():
    with stream_lock:
        if video_queue:
            video_queue.clear()
            print("🗑️ [অ্যাডমিন] ভিডিও কিউ খালি করা হয়েছে।")
            flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
        else:
             flash('ভিডিও কিউ আগে থেকেই খালি ছিল।', 'info')
    return redirect(url_for('admin_panel'))

# HTML বাটন থেকে 'আজকে চালানো হয়েছে' তালিকা খালি করা
@app.route('/admin/clear_played', methods=['POST'])
def clear_played_form():
    with stream_lock:
        if played_today:
            played_today.clear()
            print("🗑️ [অ্যাডমিন] 'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।")
            flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
        else:
             flash("'আজকে চালানো হয়েছে' তালিকা আগে থেকেই খালি ছিল।", 'info')
    return redirect(url_for('admin_panel'))

# --- নতুন API Routes ---

# API: ভিডিও যোগ করা (GET)
@app.route('/add', methods=['GET'])
def add_video_api():
    url = request.args.get('link', '').strip()
    if not url:
        print("❌ [API Add] ব্যর্থ: 'link' প্যারামিটার পাওয়া যায়নি।")
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400 # Bad Request

    if not (url.startswith('http://') or url.startswith('https://')):
        print(f"❌ [API Add] ব্যর্থ: অবৈধ URL ফরম্যাট ({url[:50]}...)")
        return jsonify({'status': 'error', 'message': 'Invalid URL format. Must start with http:// or https://', 'url': url}), 400 # Bad Request

    with stream_lock:
        if url in video_queue:
            print(f"⚠️ [API Add] ইতিমধ্যে কিউতে আছে: {url[:80]}...")
            return jsonify({'status': 'warning', 'message': 'Video already in queue.', 'url': url}), 200 # OK কিন্তু ওয়ার্নিং
        else:
            video_queue.append(url)
            print(f"✅ [API Add] কিউতে যোগ করা হয়েছে: {url[:80]}...")
            return jsonify({'status': 'success', 'message': 'Video added to queue.', 'url': url}), 200 # OK

# API: ভিডিও ডিলিট করা (GET)
@app.route('/delete', methods=['GET'])
def delete_video_api():
    link_param = request.args.get('link', '').strip()

    if not link_param:
        print("❌ [API Delete] ব্যর্থ: 'link' প্যারামিটার পাওয়া যায়নি।")
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400 # Bad Request

    with stream_lock:
        # কেস ১: সব ডিলিট (/delete?link=all)
        if link_param.lower() == 'all':
            if video_queue:
                queue_len = len(video_queue)
                video_queue.clear()
                print(f"✅ [API Delete] সম্পূর্ণ কিউ খালি করা হয়েছে ({queue_len} টি আইটেম ছিল)।")
                return jsonify({'status': 'success', 'message': f'Queue cleared. {queue_len} items removed.'}), 200 # OK
            else:
                print("ℹ️ [API Delete] কিউ আগে থেকেই খালি ছিল (link=all)।")
                return jsonify({'status': 'info', 'message': 'Queue was already empty.'}), 200 # OK কিন্তু ইনফো

        # কেস ২: নির্দিষ্ট URL ডিলিট (/delete?link=URL)
        else:
            url_to_delete = link_param
            if not (url_to_delete.startswith('http://') or url_to_delete.startswith('https://')):
                 print(f"❌ [API Delete] ব্যর্থ: ডিলিটের জন্য অবৈধ URL ফরম্যাট ({url_to_delete[:50]}...)")
                 return jsonify({'status': 'error', 'message': 'Invalid URL format for deletion.', 'url': url_to_delete}), 400 # Bad Request

            # চলছে এমন ভিডিও ডিলিট করা যাবে না
            if url_to_delete == currently_playing_url and currently_playing_url != DEFAULT_VIDEO_URL:
                 print(f"❌ [API Delete] ব্যর্থ: বর্তমানে চলছে এমন ভিডিও ডিলিট করা যাবে না ({url_to_delete[:80]}...)")
                 return jsonify({'status': 'error', 'message': 'Cannot delete the currently playing video.', 'url': url_to_delete}), 403 # Forbidden

            # কিউ থেকে ডিলিট করার চেষ্টা
            try:
                # deque থেকে সরাসরি remove ব্যবহার করা যায়, ValueError দেয় যদি না পাওয়া যায়
                video_queue.remove(url_to_delete)
                print(f"✅ [API Delete] কিউ থেকে ডিলিট করা হয়েছে: {url_to_delete[:80]}...")
                return jsonify({'status': 'success', 'message': 'Video removed from queue.', 'url': url_to_delete}), 200 # OK
            except ValueError:
                # যদি URL কিউতে না পাওয়া যায়
                print(f"❌ [API Delete] ব্যর্থ: ভিডিও কিউতে পাওয়া যায়নি ({url_to_delete[:80]}...)")
                return jsonify({'status': 'error', 'message': 'Video not found in queue.', 'url': url_to_delete}), 404 # Not Found

# --- HLS স্ট্রিম পরিবেশন ---
@app.route('/stream/<path:filename>')
def stream(filename):
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    safe_base = os.path.normpath(stream_abs_path)
    file_abs_path = os.path.normpath(os.path.join(safe_base, filename))

    # ডিরেক্টরি ট্র্যাভার্সাল অ্যাটাক রোধ
    if not file_abs_path.startswith(safe_base):
        print(f"🚫 নিরাপত্তা লঙ্ঘন প্রচেষ্টা রোধ করা হয়েছে: {filename}")
        abort(403) # Forbidden

    # ফাইলটি আসলেই একটি ফাইল কিনা এবং আছে কিনা চেক করুন
    if not os.path.isfile(file_abs_path):
        # print(f"🔍 ফাইল পাওয়া যায়নি: {file_abs_path}") # ডিবাগিং এর জন্য
        # ফাইল না থাকলে 404 দেওয়া স্বাভাবিক, বিশেষ করে সেগমেন্ট ডিলিট হলে
        abort(404) # Not Found

    try:
        response = send_from_directory(safe_base, filename, conditional=True)
        # ক্লায়েন্ট সাইড ক্যাশিং বন্ধ করার জন্য হেডার সেট করা (লাইভ স্ট্রিমের জন্য গুরুত্বপূর্ণ)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    except FileNotFoundError:
         # send_from_directory নিজেও FileNotFoundError দিতে পারে
         abort(404)
    except Exception as e:
        print(f"❌ স্ট্রিম ফাইল সার্ভ করার সময় ত্রুটি ({filename}): {e}")
        abort(500) # Internal Server Error

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার ---
def signal_handler(sig, frame):
    if stop_event.is_set():
        print("⏳ ইতিমধ্যে বন্ধ করার প্রক্রিয়া চলছে...")
        return
    print("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set() # সব থ্রেডকে বন্ধ হতে বলুন
    print("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    time.sleep(0.5) # একটু সময় দিন অন্যান্য থ্রেডকে সিগন্যাল রিসিভ করতে

    # সরাসরি FFmpeg বন্ধ করার চেষ্টা করুন যদি এটি এখনো চলে
    print("🚦 সিগন্যাল হ্যান্ডলার থেকে FFmpeg বন্ধ করার চেষ্টা...")
    stop_ffmpeg_stream() # এটি 내부적으로 লক ব্যবহার করে

    print("👋 অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    # os._exit(0) ব্যবহার করা যেতে পারে যদি থ্রেডগুলো ঠিকমতো বন্ধ না হয়
    exit(0)

# --- প্রধান চালক ---
if __name__ == '__main__':
    print("*"*60)
    print("🚀 লাইভ স্ট্রিম অ্যাপ্লিকেশন শুরু হচ্ছে...")
    print("   ✨ মোড: ভিডিও কপি, অডিও এনকোড")
    print(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📂 ভিডিও ডাউনলোড ডিরেক্টরি: {os.path.abspath(VIDEO_DIR)}")
    print(f"📺 স্ট্রিম আউটপুট ডিরেক্টরি: {os.path.abspath(STREAM_OUTPUT_DIR)}")
    print("*"*60)

    # সিগন্যাল হ্যান্ডলার সেটআপ (Ctrl+C এর জন্য)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler) # SIGTERM হ্যান্ডেল করা ভালো অভ্যাস

    # ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার থ্রেড শুরু করুন
    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    host = '0.0.0.0' # সব নেটওয়ার্ক ইন্টারফেসে শুনুন
    port = 5000
    print(f"🌍 Flask অ্যাপ http://{host}:{port} এ শোনার জন্য প্রস্তুত...")
    print(f"🔑 HTML অ্যাডমিন প্যানেল: http://127.0.0.1:{port}/admin")
    print(f"👀 প্লেয়ার দেখুন: http://127.0.0.1:{port}/")
    print(f"⚙️ API Endpoints:")
    print(f"   - ভিডিও যোগ করুন (GET): http://127.0.0.1:{port}/add?link=VIDEO_URL")
    print(f"   - ভিডিও ডিলিট করুন (GET): http://127.0.0.1:{port}/delete?link=VIDEO_URL")
    print(f"   - সব কিউ ডিলিট করুন (GET): http://127.0.0.1:{port}/delete?link=all")
    print("\n🛑 অ্যাপ্লিকেশন বন্ধ করতে Ctrl+C চাপুন।")

    try:
        # threaded=True Flask কে মাল্টি-থ্রেডেড অনুরোধ হ্যান্ডেল করতে সাহায্য করে
        # use_reloader=False ডিবাগিংয়ের সময় অটো-রিলোড বন্ধ করে, প্রোডাকশনে দরকার নেই
        # debug=False প্রোডাকশনে ডিবাগ মোড বন্ধ রাখুন
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        print(f"Flask অ্যাপ চালাতে গিয়ে মারাত্মক ত্রুটি: {e}")
        traceback.print_exc()
    finally:
        print("\nFlask অ্যাপ বন্ধ হয়েছে বা হতে চলেছে...")
        if not stop_event.is_set():
            print("   -> stop_event সেট করা হচ্ছে...")
            stop_event.set() # নিশ্চিত করুন ইভেন্ট সেট হয়েছে

        # ম্যানেজার থ্রেডকে শেষ হওয়ার জন্য কিছু সময় দিন
        if manager_thread.is_alive():
            print("   -> ম্যানেজার থ্রেডকে বন্ধ হওয়ার জন্য অপেক্ষা করা হচ্ছে (১০ সেকেন্ড পর্যন্ত)...")
            manager_thread.join(timeout=10)
            if manager_thread.is_alive():
                 print("⚠️ ম্যানেজার থ্রেড নির্দিষ্ট সময়ের মধ্যে বন্ধ হয়নি।")

        print("   -> চূড়ান্তভাবে FFmpeg বন্ধ করার চেষ্টা...")
        stop_ffmpeg_stream() # শেষবারের মতো নিশ্চিত করুন ffmpeg বন্ধ হয়েছে

        print("👋 প্রধান থ্রেড সমাপ্ত।")
