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

# --- কনফিগারেশন ---
DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts" # ডিফল্ট ভিডিওর জন্য একটি নির্দিষ্ট নাম

VIDEO_DIR = "videos" # ডাউনলোড করা ভিডিওগুলো এখানে থাকবে
STREAM_OUTPUT_DIR = "stream_output" # HLS আউটপুট এখানে তৈরি হবে
# FFMPEG_SINGLE_INPUT_FILE = "current_input.mp4" # আর ব্যবহৃত হচ্ছে না
FFMPEG_PLAYLIST_FILE = "playlist.txt" # এই ফাইলে একটি ভিডিওর পাথ থাকবে, কিন্তু FFmpeg কমান্ডে সরাসরি পাথ ব্যবহার করা হচ্ছে
HLS_OUTPUT_FILE = os.path.join(STREAM_OUTPUT_DIR, "stream.m3u8")

# গ্লোবাল ভেরিয়েবল
video_queue = deque() # অ্যাডমিন দ্বারা যোগ করা ভিডিও URL-এর কিউ
played_today = set() # আজকে চালানো ভিডিওর URL ট্র্যাক করার জন্য (অ্যাপ রিস্টার্ট হলে রিসেট হবে)
current_ffmpeg_process = None
stop_event = threading.Event() # থ্রেড ও FFmpeg বন্ধ করার জন্য
stream_lock = threading.Lock() # কিউ এবং FFmpeg প্রসেস অ্যাক্সেস সিঙ্ক্রোনাইজ করার জন্য
currently_playing_url = None # বর্তমানে কোন URL টি প্লে হচ্ছে বা প্লে হওয়ার জন্য প্রস্তুত
default_video_path = None # ডাউনলোড করা ডিফল্ট ভিডিওর পাথ

app = Flask(__name__)
CORS(app) # সব রুটের জন্য CORS সক্রিয় করা
app.secret_key = os.urandom(24) # flash বার্তার জন্য Secret Key

# --- ডিরেক্টরি তৈরি ---
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

# --- Helper Functions ---

def get_safe_filename(url):
    """URL থেকে একটি নিরাপদ ফাইলের নাম তৈরি করে (হ্যাশ ব্যবহার করে)"""
    hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10] # URL এর হ্যাশ
    # ফাইলের এক্সটেনশন অনুমান করার চেষ্টা (সরল পদ্ধতি)
    try:
        base_name = os.path.basename(url.split('?')[0]) # Query string বাদ দিয়ে ফাইলের নাম নিন
        _, ext = os.path.splitext(base_name)
        if not ext or len(ext) > 5: # যদি এক্সটেনশন না থাকে বা খুব লম্বা হয়
             ext = '.mp4' # ডিফল্ট
    except Exception:
        ext = '.mp4' # কোনো সমস্যা হলে ডিফল্ট

    # পরিচিত ভিডিও এক্সটেনশন ব্যবহার করুন
    if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv']:
         ext = '.mp4' # অজানা হলে mp4 ধরুন

    return f"video_{hashed_url}{ext}"

def download_video(url, output_filename):
    """একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে"""
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        # যদি ফাইল আগে থেকেই থাকে এবং এটি ডিফল্ট ভিডিও না হয়, তবে আবার ডাউনলোড না করা
        if os.path.exists(filepath) and output_filename != DEFAULT_VIDEO_FILENAME:
            # ফাইলের সাইজ চেক করা যেতে পারে (অপশনাল)
            if os.path.getsize(filepath) > 0:
                 print(f"'{output_filename}' আগে থেকেই ডাউনলোড করা আছে এবং খালি নয়।")
                 return filepath
            else:
                 print(f"'{output_filename}' আগে থেকেই ছিল কিন্তু খালি। আবার ডাউনলোড করা হচ্ছে।")

        # ডিফল্ট ভিডিও সবসময় চেক করা বা নতুন করে ডাউনলোড করা ভালো হতে পারে যদি এটি পরিবর্তনশীল হয়
        # তবে এখানে আমরা ধরে নিচ্ছি এটি স্থির, তাই যদি থাকে তবে ব্যবহার করব

        print(f"ডাউনলোড শুরু হচ্ছে: {url} -> {filepath}")
        headers = {'User-Agent': 'Mozilla/5.0'} # কিছু সার্ভারের জন্য User-Agent দরকার হতে পারে
        response = requests.get(url, stream=True, timeout=30, headers=headers, allow_redirects=True) # ৩০ সেকেন্ড টাইমআউট, রিডাইরেক্ট অনুসরণ করুন
        response.raise_for_status()  # HTTP ত্রুটি থাকলে Exception তুলবে

        # Content-Type চেক (অপশনাল)
        content_type = response.headers.get('content-type')
        if content_type and not content_type.startswith('video/') and not content_type.startswith('application/'):
             print(f"⚠️ সতর্কতা: Content-Type '{content_type}' ভিডিও মনে হচ্ছে না ({url})")
             # আপনি এখানে ডাউনলোড বাতিল করতে পারেন: return None

        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192):
                if stop_event.is_set(): # ডাউনলোড চলাকালীন বন্ধ করার সিগন্যাল চেক
                    print("ডাউনলোড বাতিল করা হয়েছে।")
                    if os.path.exists(filepath): os.remove(filepath) # আংশিক ফাইল মুছুন
                    return None
                if chunk: # ফিল্টার আউট keep-alive new chunks
                    f.write(chunk)
                    downloaded_size += len(chunk)

        # ডাউনলোড শেষে ফাইল সাইজ চেক
        if downloaded_size == 0:
             print(f"ডাউনলোড সম্পন্ন হয়েছে কিন্তু ফাইলের সাইজ ০ ({filepath})। সম্ভবত সমস্যা আছে।")
             if os.path.exists(filepath): os.remove(filepath)
             return None

        print(f"সফলভাবে ডাউনলোড হয়েছে: {output_filename} (Size: {downloaded_size / 1024:.2f} KB)")
        return filepath

    except requests.exceptions.Timeout:
        print(f"❌ ভিডিও ডাউনলোড টাইমআউট ({url})")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে আংশিক ফাইল মুছুন
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ ভিডিও ডাউনলোড ব্যর্থ ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে আংশিক ফাইল মুছুন
        return None
    except Exception as e:
        print(f"❌ ভিডিও সংরক্ষণ বা অন্য কোনো ত্রুটি ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে আংশিক ফাইল মুছুন
        return None

# def create_single_video_playlist(video_path):
#     """FFmpeg সরাসরি ফাইল পাথ ব্যবহার করে, তাই প্লেলিস্ট আর দরকার নেই"""
#     pass

def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস বন্ধ করে"""
    global current_ffmpeg_process
    # লক ব্যবহার করা গুরুত্বপূর্ণ কারণ অ্যাডমিন রুট থেকেও এটি কল হতে পারে (ভবিষ্যতে)
    # এবং ম্যানেজার থ্রেডও ব্যবহার করে
    with stream_lock:
        process_to_stop = current_ffmpeg_process # একটি লোকাল ভেরিয়েবলে কপি করুন
        if process_to_stop:
            print(f"FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {process_to_stop.pid})...")
            if process_to_stop.poll() is None: # যদি প্রসেস এখনও চালু থাকে
                try:
                    # প্রথমে SIGTERM পাঠিয়ে কিছুটা সময় দেওয়া
                    process_to_stop.terminate()
                    process_to_stop.wait(timeout=5) # ৫ সেকেন্ড অপেক্ষা
                    print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (terminate)।")
                except subprocess.TimeoutExpired:
                    print("FFmpeg প্রসেস terminate হয়নি, SIGKILL পাঠানো হচ্ছে...")
                    # যদি terminate কাজ না করে, তবে জোর করে বন্ধ করা (SIGKILL)
                    process_to_stop.kill()
                    process_to_stop.wait() # kill করার পর wait করতে হবে রিসোর্স মুক্ত করার জন্য
                    print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (kill)।")
                except Exception as e:
                    # ProcessLookupError ঘটতে পারে যদি প্রসেস ঠিক আগেই শেষ হয়ে যায়
                    print(f"FFmpeg বন্ধ করার সময় ত্রুটি: {e}")
            else:
                print("FFmpeg প্রসেস বন্ধ করার চেষ্টা করার সময় দেখা গেলো এটি আগে থেকেই বন্ধ ছিল।")
            # গ্লোবাল ভেরিয়েবল রিসেট করুন শুধুমাত্র যদি এটি সেই প্রসেসই হয় যা আমরা বন্ধ করতে চেয়েছিলাম
            if current_ffmpeg_process == process_to_stop:
                 current_ffmpeg_process = None
                 # currently_playing_url এখানে রিসেট করা উচিত না, কারণ ম্যানেজার এটি হ্যান্ডেল করে

def start_ffmpeg_stream(video_path, loop=False):
    """একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg স্ট্রিম শুরু করে"""
    global current_ffmpeg_process

    # নতুন স্ট্রিম শুরু করার আগে পুরনোটা বন্ধ করুন (যদি থাকে)
    # stop_ffmpeg_stream() # ম্যানেজার লুপ থেকে কল করাই ভালো

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        print(f"❌ FFmpeg শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    ffmpeg_command_base = [
        'ffmpeg',
        '-re', # রিয়েল টাইমে ইনপুট পড়ুন (লাইভ স্ট্রিমের জন্য গুরুত্বপূর্ণ)
    ]

    # যদি লুপিং দরকার হয় (শুধুমাত্র ডিফল্ট ভিডিওর জন্য)
    if loop:
        # -stream_loop -1 সরাসরি ইনপুটের আগে দিতে হবে
        ffmpeg_command_base.extend(['-stream_loop', '-1'])

    # ইনপুট ফাইল যোগ করুন
    ffmpeg_command_base.extend(['-i', abs_video_path])

    # আউটপুট ডিরেক্টরি আগে থেকেই মুছে ফেলা ভালো হতে পারে (সেগমেন্ট ক্লিনআপ)
    # এটি নিশ্চিত করে যে পুরনো সেগমেন্টগুলো প্লেলিস্টে আসবে না
    # for f in os.listdir(STREAM_OUTPUT_DIR):
    #     if f.endswith('.ts') or f.endswith('.m3u8'):
    #         os.remove(os.path.join(STREAM_OUTPUT_DIR, f))


    # বাকি অপশনগুলো যোগ করুন
    ffmpeg_command_options = [
        '-c:v', 'libx264',
        '-preset', 'veryfast', # CPU ব্যবহার কমায়, কোয়ালিটি সামান্য কমতে পারে
        '-tune', 'zerolatency', # লাইভ স্ট্রিমের জন্য ল্যাটেন্সি কমানোর চেষ্টা
        '-b:v', '1500k', # ভিডিও বিটরেট (নেটওয়ার্ক অনুযায়ী পরিবর্তনীয়)
        '-maxrate', '1500k', # সর্বোচ্চ বিটরেট
        '-bufsize', '3000k', # বাফার সাইজ (বিটরেটের দ্বিগুণ)
        '-g', '60', # কীফ্রেম ব্যবধান (২ সেকেন্ড @ ৩০fps)
        '-vf', 'scale=640:360', # আউটপুট রেজোলিউশন (প্রয়োজনে পরিবর্তন করুন)
        '-c:a', 'aac',         # অডিও কোডেক
        '-b:a', '128k',        # অডিও বিটরেট
        '-ac', '2',            # স্টেরিও অডিও চ্যানেল
        '-ar', '44100',        # অডিও স্যাম্পেল রেট
        '-f', 'hls',           # আউটপুট ফরম্যাট HLS
        '-hls_time', '4',      # প্রতিটি সেগমেন্টের সময়কাল (সেকেন্ড)
        '-hls_list_size', '5', # প্লেলিস্টে সেগমেন্টের সংখ্যা (কম হলে দ্রুত আপডেট হয়)
        '-hls_flags', 'delete_segments+omit_endlist', # পুরনো সেগমেন্ট মুছবে এবং লাইভ দেখাবে (omit_endlist গুরুত্বপূর্ণ)
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%03d.ts'), # সেগমেন্ট ফাইলের নাম প্যাটার্ন
        HLS_OUTPUT_FILE        # মাস্টার প্লেলিস্ট ফাইলের পাথ
    ]

    ffmpeg_command = ffmpeg_command_base + ffmpeg_command_options

    print("🚀 FFmpeg কমান্ড:", " ".join(ffmpeg_command))
    try:
        # stderr পাইপ করা যাতে আমরা এরর বা ওয়ার্নিং দেখতে পারি
        # stdout=subprocess.DEVNULL - ffmpeg এর স্ট্যান্ডার্ড আউটপুট বেশিরভাগই অপ্রয়োজনীয়
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # stderr পড়ার জন্য একটি ছোট থ্রেড (ডিবাগিংয়ের জন্য)
        def log_stderr(proc, path):
            if proc.stderr:
                for line in iter(proc.stderr.readline, b''):
                    if stop_event.is_set(): break
                    line_str = line.decode(errors='ignore').strip()
                    if line_str: # খালি লাইন প্রিন্ট না করা
                        print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
            print(f"  [FFmpeg stderr রিডিং শেষ - {os.path.basename(path)}]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True)
        stderr_thread.start()

        print(f"✅ FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)}, লুপ: {loop}")
        with stream_lock:
            current_ffmpeg_process = process # গ্লোবাল ভেরিয়েবলে প্রসেস সংরক্ষণ করুন
        return process

    except FileNotFoundError:
        print(f"❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ যোগ করা আছে কিনা নিশ্চিত করুন।")
        with stream_lock:
             current_ffmpeg_process = None
        return None
    except Exception as e:
        print(f"❌ FFmpeg শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")
        with stream_lock:
            current_ffmpeg_process = None
        return None

# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার (আপডেটেড লজিক সহ) ---
def stream_manager():
    """
    ব্যাকগ্রাউন্ডে চলে, ভিডিও কিউ এবং FFmpeg প্রসেস ম্যানেজ করে।
    সর্বদা অ্যাডমিন কিউকে অগ্রাধিকার দেয়। কিউ খালি থাকলেই কেবল ডিফল্ট ভিডিও লুপ করে।
    কিউতে নতুন আইটেম আসলে ডিফল্ট ভিডিও বন্ধ করে দেয়।
    """
    global currently_playing_url, default_video_path, current_ffmpeg_process

    # প্রথমে ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা করুন (অ্যাপ শুরুতে একবার)
    print("⏳ ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা চলছে...")
    temp_default_path = download_video(DEFAULT_VIDEO_URL, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
         default_video_path = temp_default_path # সফল হলে গ্লোবাল ভেরিয়েবল সেট করুন
         print(f"✅ ডিফল্ট ভিডিও প্রস্তুত: {default_video_path}")
    else:
         print("🚨 সতর্কতা: ডিফল্ট ভিডিও ডাউনলোড করা যায়নি! ডিফল্ট প্লেব্যাক কাজ করবে না।")

    while not stop_event.is_set():
        next_video_path = None
        play_url = None
        loop_default = False
        stop_default_and_process_queue = False # ডিফল্ট বন্ধ করে কিউ প্রসেস করার ফ্ল্যাগ

        try: # পুরো লুপটি try-except ব্লকে রাখা ভালো
            with stream_lock: # সিঙ্ক্রোনাইজেশন অপরিহার্য
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None

                # --- ধাপ ১: যদি FFmpeg চলে ---
                if ffmpeg_is_running:
                    # চেক করুন: ডিফল্ট ভিডিও চলছে *এবং* অ্যাডমিন কিউতে আইটেম যোগ হয়েছে?
                    if currently_playing_url == DEFAULT_VIDEO_URL and video_queue:
                        print("🔄 ডিফল্ট ভিডিও চলছিল, কিন্তু কিউতে নতুন আইটেম এসেছে। ডিফল্ট বন্ধ করা হচ্ছে...")
                        stop_default_and_process_queue = True # ফ্ল্যাগ সেট করুন, লক ছাড়ার পর বন্ধ করা হবে
                    else:
                        # অ্যাডমিনের ভিডিও চলছে, অথবা ডিফল্ট চলছে কিন্তু কিউ এখনও খালি। অপেক্ষা করুন।
                        pass # time.sleep(1) এখানে தேவையில்லை, লুপ দ্রুত চলবে

                # --- ধাপ ২: যদি FFmpeg বন্ধ থাকে বাพึ่ง শেষ হয়েছে ---
                else:
                    # যদি প্রসেস শেষ হয়ে থাকে, তবে তা পরিষ্কার করুন
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        print(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) শেষ হয়েছে।")
                        # প্লে হওয়া ভিডিওটিকে played_today তে যোগ করুন (যদি এটি ডিফল্ট না হয়)
                        if currently_playing_url and currently_playing_url != DEFAULT_VIDEO_URL:
                             played_today.add(currently_playing_url)
                        # রিসোর্স পরিষ্কার করুন
                        current_ffmpeg_process = None
                        currently_playing_url = None

                    # এখন পরবর্তী ভিডিও নির্ধারণ করুন (যেহেতু FFmpeg চলছে না)
                    # --- অগ্রাধিকার: অ্যাডমিন কিউ ---
                    if video_queue:
                        play_url = video_queue.popleft()
                        print(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে: {play_url[:80]}...") # URL ছোট করে দেখানো
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename)
                        if not next_video_path:
                            print(f"❌ ডাউনলোড ব্যর্থ: {play_url[:80]}... পরবর্তী আইটেম চেষ্টা করা হবে।")
                            play_url = None # এটি প্লে করা যাবে না
                            # এখানে continue না করে লুপটিকে স্বাভাবিকভাবে চলতে দেওয়া ভালো
                        else:
                             # লুপ সেট করবেন না অ্যাডমিনের ভিডিওর জন্য
                             loop_default = False

                    # --- বিকল্প: ডিফল্ট ভিডিও (শুধুমাত্র যদি কিউ খালি থাকে এবং FFmpeg না চলে) ---
                    elif default_video_path:
                        print("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                        next_video_path = default_video_path
                        play_url = DEFAULT_VIDEO_URL
                        loop_default = True # ডিফল্ট ভিডিও লুপ করবে

                    # --- কিছুই করার নেই ---
                    else:
                        print("⏳ অ্যাডমিন কিউ খালি এবং ডিফল্ট ভিডিও উপলব্ধ নেই। অপেক্ষা করা হচ্ছে...")
                        pass # time.sleep(5) নিচে সরানো হয়েছে

            # --- ধাপ ৩: অ্যাকশন (লকের বাইরে) ---

            # যদি ডিফল্ট স্ট্রিম বন্ধ করার সিদ্ধান্ত নেওয়া হয়
            if stop_default_and_process_queue:
                print("🛑 ডিফল্ট স্ট্রিম বন্ধ করা হচ্ছে...")
                stop_ffmpeg_stream()
                time.sleep(0.5) # FFmpeg বন্ধ হওয়ার জন্য সামান্য সময় দিন
                continue # অবিলম্বে লুপটি পুনরায় শুরু করুন যাতে কিউ থেকে নতুন আইটেমটি নেওয়া যায়

            # যদি প্লে করার জন্য কোনো ভিডিও পাওয়া যায়
            if next_video_path and play_url:
                print(f" başlatılıyor... Video: {os.path.basename(next_video_path)}, Döngü: {loop_default}")
                # currently_playing_url সেট করা গুরুত্বপূর্ণ start_ffmpeg_stream এর *আগে*
                with stream_lock:
                     currently_playing_url = play_url
                # FFmpeg শুরু করুন
                started_process = start_ffmpeg_stream(next_video_path, loop=loop_default)
                if started_process:
                     time.sleep(2) # FFmpeg শুরু হওয়ার জন্য একটু সময় দিন
                else:
                     # FFmpeg শুরু করতে ব্যর্থ হলে, playing url রিসেট করুন
                     with stream_lock:
                         currently_playing_url = None
                     # সম্ভবত পরবর্তী ইটারেশনে আবার চেষ্টা করা হবে

            # যদি কোনো অ্যাকশন না নেওয়া হয় (যেমন কিউ খালি, ডিফল্ট নেই, বা ডাউনলোড ব্যর্থ)
            elif not ffmpeg_is_running: # শুধুমাত্র যদি FFmpeg না চলে তবেই অপেক্ষা করুন
                 time.sleep(3) # ৩ সেকেন্ড পর আবার চেক করুন

            # যদি FFmpeg চলতে থাকে (এবং এটি ডিফল্ট নয় বা ডিফল্ট কিন্তু কিউ খালি), তাহলে অল্প অপেক্ষা করুন
            elif ffmpeg_is_running:
                 time.sleep(1)


        except Exception as e:
             print(f"🚨 স্ট্রিম ম্যানেজার লুপে অপ্রত্যাশিত ত্রুটি: {e}")
             import traceback
             traceback.print_exc() # সম্পূর্ণ ট্রেসব্যাক প্রিন্ট করুন
             time.sleep(5) # ত্রুটির পর কিছুক্ষণ অপেক্ষা করুন

    # --- থ্রেড বন্ধ হওয়ার সময় ---
    print("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    stop_ffmpeg_stream() # নিশ্চিত করুন FFmpeg বন্ধ হয়েছে

# --- Flask Routes ---
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

    if is_ffmpeg_running:
        if current_url_snapshot == DEFAULT_VIDEO_URL:
            current_status = "ডিফল্ট ভিডিও চলছে (লুপ)"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}..."
        else:
            current_status = "একটি ভিডিও চলছে (URL অজানা)" # এটি হওয়া উচিত নয়
    else:
        current_status = "কোনো ভিডিও চলছে না"

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

@app.route('/admin/add', methods=['POST'])
def add_video():
    """কিউতে নতুন ভিডিও URL যোগ করে"""
    url = request.form.get('video_url')
    if url:
        # খুব সাধারণ URL ভ্যালিডেশন
        if url.startswith('http://') or url.startswith('https://'):
            with stream_lock:
                video_queue.append(url)
                print(f"📥 কিউতে যোগ করা হয়েছে: {url}")
                flash(f'"{url[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
            # কোনো অ্যাকশন নেওয়ার দরকার নেই, ম্যানেজার থ্রেড নিজে থেকেই হ্যান্ডেল করবে
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
        print("🗑️ ভিডিও কিউ খালি করা হয়েছে।")
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

    # পাথ ট্র্যাভার্সাল অ্যাটাক রোধ করা
    if not file_abs_path.startswith(stream_abs_path):
        print(f"🚫 নিরাপত্তা লঙ্ঘন প্রচেষ্টা: {filename}")
        abort(404)

    # ফাইল আছে কিনা নিশ্চিত করুন
    if not os.path.exists(file_abs_path):
        # print(f"❓ ফাইল পাওয়া যায়নি: {file_abs_path}") # লগিং কমানো যেতে পারে
        abort(404)

    # ক্যাশ কন্ট্রোল হেডার (লাইভ স্ট্রিমের জন্য গুরুত্বপূর্ণ)
    response = send_from_directory(stream_abs_path, filename, conditional=True) # Conditional GET ব্যবহার করুন
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    # CORS হেডার (যদি Flask-CORS যথেষ্ট না হয় বা নির্দিষ্ট রুটের জন্য দরকার হয়)
    # response.headers['Access-Control-Allow-Origin'] = '*'
    return response

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার ---
def signal_handler(sig, frame):
    print("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set() # সব থ্রেডকে বন্ধ করার জন্য ইভেন্ট সেট করুন
    print("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    # এখানে সরাসরি stop_ffmpeg_stream() কল না করাই ভালো, ম্যানেজার থ্রেড করবে
    # তবে কিছু সময় অপেক্ষা করা উচিত যাতে ম্যানেজার থ্রেড কাজটি করতে পারে
    # manager_thread.join(timeout=10) # যদি থ্রেড অবজেক্ট এখানে অ্যাক্সেসযোগ্য হতো
    time.sleep(2) # প্রধান থ্রেডকে কিছুটা সময় দিন
    print("👋 অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    os._exit(0) # ফোর্স এক্সিট

# --- প্রধান চালক ---
if __name__ == '__main__':
    print("🚀 অ্যাপ্লিকেশন শুরু হচ্ছে...")
    print(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    # সিগন্যাল হ্যান্ডলার সেট করুন (Ctrl+C)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার থ্রেড শুরু করুন
    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    # Flask অ্যাপ চালু করুন
    print(f"🌍 Flask অ্যাপ চালু হচ্ছে http://0.0.0.0:5000 এ...")
    print(f"🔑 অ্যাডমিন প্যানেল: http://127.0.0.1:5000/admin (অথবা আপনার সার্ভার আইপি)")
    # use_reloader=False দেওয়া জরুরি যখন ব্যাকগ্রাউন্ড থ্রেড ব্যবহার করছেন
    # threaded=True মাল্টিপল রিকোয়েস্ট হ্যান্ডেল করতে সাহায্য করে
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False, debug=False) # Debug মোড বন্ধ রাখুন প্রোডাকশনে

    # এই অংশটি সাধারণত পৌঁছাবে না কারণ app.run() ব্লক করে এবং signal_handler প্রস্থান করে
    print("Flask অ্যাপ স্বাভাবিকভাবে বন্ধ হয়েছে।")
    stop_event.set()
    if manager_thread.is_alive():
        manager_thread.join(timeout=5)
    stop_ffmpeg_stream()
    print("প্রধান থ্রেড শেষ হয়েছে।")
