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
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode # URL পার্সিং এর জন্য
import shutil # ডিরেক্টরি মোছার জন্য

# --- কনফিগারেশন ---
DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts"

VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
MASTER_PLAYLIST_NAME = "stream.m3u8" # মাস্টার প্লেলিস্টের নাম
HLS_MASTER_PLAYLIST_PATH = os.path.join(STREAM_OUTPUT_DIR, MASTER_PLAYLIST_NAME)

# --- নতুন: মাল্টি-বিটরেট কনফিগারেশন ---
# এখানে কোয়ালিটি লেভেলগুলো ডিফাইন করুন (রেজোলিউশন, ভিডিও বিটরেট, অডিও বিটরেট)
# ফরম্যাট: {'width': <px>, 'v_bitrate': '<kbps>k', 'a_bitrate': '<kbps>k', 'name': '<variant_name>'}
# নামগুলো সাব-ডিরেক্টরি এবং প্লেলিস্টে ব্যবহৃত হবে
QUALITY_LEVELS = [
    {'width': 1280, 'v_bitrate': '2000k', 'a_bitrate': '128k', 'name': '720p'},
    {'width': 854,  'v_bitrate': '1000k', 'a_bitrate': '96k',  'name': '480p'},
    {'width': 640,  'v_bitrate': '600k',  'a_bitrate': '64k',  'name': '360p'},
]
FFMPEG_PRESET = 'veryfast' # এনকোডিং স্পিড (ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow)
HLS_SEGMENT_DURATION = 4 # সেগমেন্ট দৈর্ঘ্য (সেকেন্ড)
HLS_LIST_SIZE = 6 # প্লেলিস্টে সেগমেন্ট সংখ্যা

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

# --- Helper Functions (ensure_dropbox_raw_param, get_safe_filename, download_video) ---
# এই ফাংশনগুলো আগের মতোই থাকবে। নিচে শুধু প্রয়োজন অনুযায়ী দেখানো হচ্ছে।

def ensure_dropbox_raw_param(url):
    """
    URL টি Dropbox লিঙ্ক হলে এবং শেষে raw=1 না থাকলে তা যোগ করে।
    (কোনো পরিবর্তন নেই)
    """
    try:
        if not url or not (url.startswith('http://') or url.startswith('https://')):
            return url
        parsed_url = urlparse(url)
        if parsed_url.netloc.lower() in ['www.dropbox.com', 'dropbox.com']:
            query_params = parse_qs(parsed_url.query)
            if not ('raw' in query_params and query_params['raw'] == ['1']):
                # print(f"🔧 Dropbox URL সনাক্ত হয়েছে, 'raw=1' যোগ করা হচ্ছে: {url[:80]}...") # কম ভার্বোস লগিং
                query_params['raw'] = ['1']
                new_query = urlencode(query_params, doseq=True)
                modified_url = urlunparse((
                    parsed_url.scheme, parsed_url.netloc, parsed_url.path,
                    parsed_url.params, new_query, parsed_url.fragment
                ))
                # print(f"   -> পরিবর্তিত URL: {modified_url[:80]}...")
                return modified_url
            else:
                 return url
        else:
            return url
    except Exception as e:
        print(f"⚠️ URL '{url[:80]}...' পার্স বা মডিফাই করার সময় ত্রুটি: {e}")
        return url


def get_safe_filename(url):
    """URL থেকে একটি নিরাপদ ফাইলের নাম তৈরি করে (হ্যাশ ব্যবহার করে)"""
    """ (কোনো পরিবর্তন নেই) """
    try:
        parsed_url = urlparse(url)
        path_part = parsed_url.path
        base_name = os.path.basename(path_part)
        _, ext = os.path.splitext(base_name)
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        if not ext or len(ext) > 5: ext = '.mp4'
        valid_exts = ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m3u8']
        if ext.lower() not in valid_exts: ext = '.mp4'
        return f"video_{hashed_url}{ext}"
    except Exception as e:
        print(f"⚠️ ফাইলের নাম তৈরিতে সমস্যা ({url[:50]}...): {e}. একটি জেনেরিক নাম ব্যবহার করা হচ্ছে।")
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        return f"video_{hashed_url}.mp4"


def download_video(url, output_filename):
    """একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে"""
    """ (কোনো পরিবর্তন নেই, তবে ডাউনলোড লজিক যেমন আছে তেমন থাকবে) """
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        if os.path.exists(filepath):
            try:
                if os.path.getsize(filepath) > 0:
                    print(f"ℹ️ '{output_filename}' ({url[:50]}...) আগে থেকেই ডাউনলোড করা আছে।")
                    return filepath
                else:
                    print(f"⚠️ '{output_filename}' খালি ছিল। আবার ডাউনলোড করা হচ্ছে।")
            except OSError as e:
                 print(f"⚠️ ফাইল সাইজ চেক করতে সমস্যা '{filepath}': {e}। আবার ডাউনলোড করা হচ্ছে।")

        print(f"⏬ ডাউনলোড শুরু হচ্ছে: {url} -> {filepath}")
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get('content-type', '').lower()
        problematic_types = ['text/html', 'application/json']
        is_likely_video = 'video' in content_type or 'mpegurl' in content_type or 'octet-stream' in content_type or not any(ptype in content_type for ptype in problematic_types)
        if not is_likely_video:
             print(f"⚠️ সতর্কতা: Content-Type '{content_type}' ভিডিও মনে হচ্ছে না ({url[:80]}...)")
             if 'dropbox.com' in url and 'raw=1' not in url:
                 print(f"   -> এটি Dropbox লিঙ্ক কিন্তু 'raw=1' নেই। HTML পেজ ডাউনলোড হতে পারে।")

        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 4):
                if stop_event.is_set():
                    print("🛑 ডাউনলোড বাতিল।")
                    if os.path.exists(filepath): os.remove(filepath)
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

        if downloaded_size == 0:
             print(f"❌ ডাউনলোড সম্পন্ন কিন্তু ফাইলের সাইজ ০ ({filepath})।")
             if os.path.exists(filepath): os.remove(filepath)
             return None

        print(f"✅ সফলভাবে ডাউনলোড হয়েছে: {output_filename} (Size: {downloaded_size / (1024 * 1024):.2f} MB)")
        return filepath

    except requests.exceptions.Timeout:
        print(f"❌ ভিডিও ডাউনলোড টাইমআউট ({url[:80]}...)")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ ভিডিও ডাউনলোড ব্যর্থ ({url[:80]}...): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except Exception as e:
        print(f"❌ ভিডিও সংরক্ষণ বা অন্য ত্রুটি ({url[:80]}...): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None

# --- পরিবর্তিত: FFmpeg Functions ---

def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস নিরাপদে বন্ধ করে"""
    """ (কোনো পরিবর্তন নেই) """
    global current_ffmpeg_process
    with stream_lock:
        process_to_stop = current_ffmpeg_process
        if process_to_stop and process_to_stop.poll() is None:
            print(f"⏳ FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {process_to_stop.pid})...")
            try:
                if os.name == 'nt': # উইন্ডোজ
                    subprocess.run(['taskkill', '/F', '/PID', str(process_to_stop.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else: # লিনাক্স/ম্যাক
                    process_to_stop.terminate()
                    try:
                        process_to_stop.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        print("   -> FFmpeg terminate হয়নি, SIGKILL পাঠানো হচ্ছে...")
                        process_to_stop.kill()
                        process_to_stop.wait()
                print(f"   -> FFmpeg প্রসেস (PID: {process_to_stop.pid}) বন্ধ হয়েছে।")
            except Exception as e:
                print(f"⚠️ FFmpeg (PID: {process_to_stop.pid}) বন্ধ করার সময় ত্রুটি: {e}")
        elif process_to_stop:
             print(f"ℹ️ FFmpeg প্রসেস (PID: {process_to_stop.pid}) আগে থেকেই বন্ধ ছিল।")

        if current_ffmpeg_process == process_to_stop:
             current_ffmpeg_process = None


# --- *** মূল পরিবর্তন এখানে *** ---
def start_ffmpeg_stream(video_path, loop=False):
    """
    একটি নির্দিষ্ট ভিডিও ফাইল থেকে মাল্টি-বিটরেট FFmpeg HLS স্ট্রিম শুরু করে।
    ভিডিও libx264 এবং অডিও AAC তে এনকোড করে।
    """
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        print(f"❌ FFmpeg শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    # চলমান প্রসেস থাকলে বন্ধ করুন
    stop_ffmpeg_stream()

    # --- পুরনো HLS ফাইল এবং ডিরেক্টরি মুছে ফেলা ---
    print(f"🧹 পুরনো HLS ফাইল এবং ডিরেক্টরি মুছে ফেলা হচ্ছে ({STREAM_OUTPUT_DIR})...")
    try:
        if os.path.exists(STREAM_OUTPUT_DIR):
             # ডিরেক্টরির ভেতরের সব ফাইল ও সাব-ডিরেক্টরি মুছে ফেলুন
             for item_name in os.listdir(STREAM_OUTPUT_DIR):
                 item_path = os.path.join(STREAM_OUTPUT_DIR, item_name)
                 try:
                     if os.path.isfile(item_path) or os.path.islink(item_path):
                         os.unlink(item_path)
                     elif os.path.isdir(item_path):
                         shutil.rmtree(item_path)
                 except Exception as e:
                     print(f"⚠️ পুরনো আইটেম মুছতে সমস্যা '{item_path}': {e}")
        else:
             os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True) # যদি না থাকে তবে তৈরি করুন

        # --- নতুন কোয়ালিটির জন্য সাব-ডিরেক্টরি তৈরি ---
        for level in QUALITY_LEVELS:
            level_dir = os.path.join(STREAM_OUTPUT_DIR, level['name'])
            os.makedirs(level_dir, exist_ok=True)
            print(f"   -> '{level_dir}' ডিরেক্টরি তৈরি বা নিশ্চিত করা হয়েছে।")

    except Exception as e:
        print(f"⚠️ স্ট্রিম আউটপুট ডিরেক্টরি পরিষ্কার বা তৈরি করতে সমস্যা: {e}")
        return None # ডিরেক্টরি তৈরি না হলে শুরু করা যাবে না

    # --- FFmpeg কমান্ড তৈরি ---
    ffmpeg_command_base = ['ffmpeg']

    # লুপ করার অপশন (ইনপুটের জন্য)
    if loop:
        ffmpeg_command_base.extend(['-stream_loop', '-1'])

    # ইনপুট ফাইল
    ffmpeg_command_base.extend(['-i', abs_video_path])

    # ফিল্টার কমপ্লেক্স (ভিডিও স্প্লিট এবং স্কেল করার জন্য)
    filter_complex_parts = []
    output_maps = []
    var_stream_map_parts = [] # ভ্যারিয়েন্ট স্ট্রিম ম্যাপ তৈরি করার জন্য

    # ভিডিও স্প্লিট করা (যতগুলো কোয়ালিটি লেভেল, ততগুলো ভাগে)
    split_outputs = "".join(f"[v{i}]" for i in range(len(QUALITY_LEVELS)))
    filter_complex_parts.append(f"[0:v]split={len(QUALITY_LEVELS)}{split_outputs}")

    # প্রতিটি কোয়ালিটির জন্য স্কেলিং এবং ম্যাপিং তৈরি
    for i, level in enumerate(QUALITY_LEVELS):
        # স্কেলিং: -2 ব্যবহার করে aspect ratio ঠিক রাখা হয়
        filter_complex_parts.append(f"[v{i}]scale=w={level['width']}:h=-2[v{i}out]")
        # আউটপুট ম্যাপিং (এই স্কেল করা ভিডিও)
        output_maps.extend(['-map', f'[v{i}out]'])
        # আউটপুট ম্যাপিং (ইনপুট অডিও - যদি থাকে)
        output_maps.extend(['-map', '0:a?']) # '?' মানে অডিও স্ট্রিম ঐচ্ছিক
        # ভ্যারিয়েন্ট ম্যাপ স্ট্রিং তৈরি (v:index,a:index,name:levelname)
        # এখানে আউটপুট ভিডিও ইনডেক্স i, এবং অডিও ইনডেক্সও i (কারণ প্রতি ভিডিওর সাথে একটি অডিও ম্যাপ হচ্ছে)
        var_stream_map_parts.append(f"v:{i},a:{i},name:{level['name']}")

    # ফিল্টার কমপ্লেক্স স্ট্রিং তৈরি
    filter_complex_str = ";".join(filter_complex_parts)

    # এনকোডিং অপশনস (প্রতিটি ম্যাপ করা স্ট্রিমের জন্য)
    encoding_options = []
    for i, level in enumerate(QUALITY_LEVELS):
        encoding_options.extend([
            # ভিডিও এনকোডিং (আউটপুট স্ট্রিম i এর জন্য)
            f'-c:v:{i}', 'libx264',
            f'-b:v:{i}', level['v_bitrate'],
            f'-preset:v:{i}', FFMPEG_PRESET,
            f'-profile:v:{i}', 'main', # সামঞ্জস্যের জন্য মেইন প্রোফাইল
            f'-level:v:{i}', '4.0',    # লেভেল
             '-g:v', str(HLS_SEGMENT_DURATION * 25), # GOP size (fps অনুমান করে)
             '-keyint_min:v', str(HLS_SEGMENT_DURATION * 25),
             '-sc_threshold:v', '0',

            # অডিও এনকোডিং (আউটপুট স্ট্রিম i এর জন্য)
            f'-c:a:{i}', 'aac',
            f'-b:a:{i}', level['a_bitrate'],
            f'-ac:a:{i}', '2',      # স্টেরিও
            f'-ar:a:{i}', '44100',  # স্যাম্পল রেট
        ])

    # HLS আউটপুট অপশনস
    hls_options = [
        '-f', 'hls',
        '-hls_time', str(HLS_SEGMENT_DURATION),
        '-hls_list_size', str(HLS_LIST_SIZE),
        '-hls_flags', 'delete_segments+omit_endlist+program_date_time',
        '-master_pl_name', MASTER_PLAYLIST_NAME, # মাস্টার প্লেলিস্টের নাম
        # সেগমেন্ট ফাইলের নাম প্যাটার্ন (সাব-ডিরেক্টরি সহ)
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, '%v', 'segment_%05d.ts'),
        # ভ্যারিয়েন্ট স্ট্রিম ম্যাপ (কোয়ালিটি লেভেল অনুযায়ী)
        '-var_stream_map', " ".join(var_stream_map_parts),
        # মাস্টার প্লেলিস্ট আউটপুট পাথ (যদিও master_pl_name ব্যবহার করা হয়েছে, এটি লাগবে)
         os.path.join(STREAM_OUTPUT_DIR, '%v', 'playlist.m3u8') # প্রতিটি ভ্যারিয়েন্টের প্লেলিস্ট পাথ
        # দ্রষ্টব্য: উপরের পাথটি FFmpeg যেভাবে কাজ করে তার উপর নির্ভর করে। কিছু ভার্সনে শুধু `-master_pl_name` দিলেই চলে।
        # অথবা মাস্টার প্লেলিস্টের পাথ শেষে দিতে হতে পারে। নিচে মাস্টার প্লেলিস্ট পাথ যোগ করা হলো।
    ]

    # সম্পূর্ণ FFmpeg কমান্ড
    #ffmpeg_command = (
    #    ffmpeg_command_base +
    #    ['-filter_complex', filter_complex_str] +
    #    output_maps +
    #    encoding_options +
    #    hls_options +
    #    [HLS_MASTER_PLAYLIST_PATH] # মাস্টার প্লেলিস্টের পাথ শেষে যোগ করা হয়েছে
    #)
    # বিকল্প গঠন: hls_options এর মধ্যে আউটপুট পাথ অন্তর্ভুক্ত না করে, শেষে যোগ করা
    ffmpeg_command = (
       ffmpeg_command_base +
       ['-filter_complex', filter_complex_str] +
       output_maps +
       encoding_options +
       ['-f', 'hls'] +
       ['-hls_time', str(HLS_SEGMENT_DURATION)] +
       ['-hls_list_size', str(HLS_LIST_SIZE)] +
       ['-hls_flags', 'delete_segments+omit_endlist+program_date_time'] +
       ['-master_pl_name', MASTER_PLAYLIST_NAME] +
       ['-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, '%v', 'segment_%05d.ts')] +
       ['-var_stream_map', " ".join(var_stream_map_parts)] +
       [HLS_MASTER_PLAYLIST_PATH] # আউটপুট হিসেবে মাস্টার প্লেলিস্ট
    )


    print("🚀 FFmpeg কমান্ড (মাল্টি-বিটরেট এনকোডিং):")
    print("   ", " ".join(f'"{arg}"' if ' ' in arg else arg for arg in ffmpeg_command)) # স্পেস সহ আর্গুমেন্ট কোট করুন

    try:
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

        # stderr লগিং থ্রেড (আগের মতোই)
        def log_stderr(proc, path):
            if proc.stderr:
                try:
                    for line in iter(proc.stderr.readline, b''):
                        if stop_event.is_set(): break
                        line_str = line.decode('utf-8', errors='replace').strip()
                        if line_str:
                            # গুরুত্বপূর্ণ এরর বা ওয়ার্নিং লগ করা
                            keywords = ['error', 'failed', 'invalid', 'warning', 'unable', 'cannot']
                            if any(keyword in line_str.lower() for keyword in keywords):
                               print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
                            # else: # ডিবাগিং এর জন্য সব লাইন দেখতে চাইলে আনকমেন্ট করুন
                            #    print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
                except Exception as e:
                     print(f"⚠️ FFmpeg stderr পড়তে সমস্যা: {e}")
                finally:
                     if proc.stderr: proc.stderr.close()
            # print(f"  [FFmpeg stderr রিডিং থ্রেড শেষ - {os.path.basename(path)}]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True)
        stderr_thread.start()

        print(f"✅ FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)} [মাল্টি-বিটরেট এনকোডিং], লুপ: {loop}")
        with stream_lock:
            current_ffmpeg_process = process
        return process

    except FileNotFoundError:
        print(f"❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি।")
        with stream_lock: current_ffmpeg_process = None
        return None
    except Exception as e:
        print(f"❌ FFmpeg শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")
        traceback.print_exc() # বিস্তারিত এরর দেখান
        with stream_lock: current_ffmpeg_process = None
        return None


# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার (stream_manager) ---
# এই ফাংশনে কোনো বড় পরিবর্তন দরকার নেই, কারণ এটি শুধু ভিডিও পাথ নির্বাচন করে
# start_ffmpeg_stream কে কল করে। start_ffmpeg_stream এখন মাল্টি-বিটরেট তৈরি করবে।
# তবে লগিং বার্তা আপডেট করা যেতে পারে।

def stream_manager():
    """
    ব্যাকগ্রাউন্ডে চলে, ভিডিও কিউ এবং FFmpeg প্রসেস ম্যানেজ করে।
    (কার্যকারিতা আগের মতোই, শুধু লগিং পরিবর্তিত হতে পারে)
    """
    global currently_playing_url, default_video_path, current_ffmpeg_process

    print("⏳ ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা চলছে...")
    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    temp_default_path = download_video(modified_default_url, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
         default_video_path = temp_default_path
         print(f"✅ ডিফল্ট ভিডিও প্রস্তুত: {default_video_path}")
    else:
         print(f"🚨 সতর্কতা: ডিফল্ট ভিডিও ডাউনলোড করা যায়নি!")

    predownload_attempted_for_url = None

    while not stop_event.is_set():
        next_video_path = None
        play_url = None
        loop_default = False
        stop_default_and_process_queue = False

        try:
            with stream_lock:
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
                current_url_snapshot = currently_playing_url
                modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL) # প্রতিটি লুপে চেক করুন

                # ডিসিশন লজিক (আগের মতোই)
                if ffmpeg_is_running:
                    if current_url_snapshot != modified_default_url_snapshot and video_queue:
                        next_url_in_queue_raw = video_queue[0]
                        next_url_in_queue_modified = ensure_dropbox_raw_param(next_url_in_queue_raw)
                        if next_url_in_queue_modified != predownload_attempted_for_url:
                            # print(f"🔎 প্রি-ডাউনলোডের জন্য চেক: {next_url_in_queue_modified[:80]}...") # কম ভার্বোস
                            next_filename = get_safe_filename(next_url_in_queue_modified)
                            downloaded_path = download_video(next_url_in_queue_modified, next_filename)
                            # if downloaded_path: print(f"👍 প্রি-ডাউনলোড সম্পন্ন: {next_filename}")
                            # else: print(f"👎 প্রি-ডাউনলোড ব্যর্থ: {next_url_in_queue_modified[:80]}...")
                            predownload_attempted_for_url = next_url_in_queue_modified
                    elif current_url_snapshot == modified_default_url_snapshot and video_queue:
                        print("🔄 ডিফল্ট ভিডিও চলছিল, কিউতে আইটেম এসেছে। ডিফল্ট বন্ধ করা হচ্ছে...")
                        stop_default_and_process_queue = True
                        predownload_attempted_for_url = None
                    else:
                        if current_url_snapshot != modified_default_url_snapshot and not video_queue:
                            predownload_attempted_for_url = None
                        pass
                else: # FFmpeg চলছে না
                    predownload_attempted_for_url = None
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        print(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) শেষ হয়েছে।")
                        if current_url_snapshot and current_url_snapshot != modified_default_url_snapshot:
                             played_today.add(current_url_snapshot)
                        current_ffmpeg_process = None
                        currently_playing_url = None

                    if video_queue:
                        raw_url_from_queue = video_queue.popleft()
                        play_url = ensure_dropbox_raw_param(raw_url_from_queue)
                        print(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে: {play_url[:80]}...")
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename)
                        if not next_video_path:
                            print(f"❌ ডাউনলোড ব্যর্থ, স্কিপ করা হলো: {play_url[:80]}...")
                            play_url = None
                            currently_playing_url = None
                        else:
                             loop_default = False
                             currently_playing_url = play_url
                    elif default_video_path:
                        if current_url_snapshot != modified_default_url_snapshot:
                             print("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                        next_video_path = default_video_path
                        play_url = modified_default_url_snapshot
                        loop_default = True
                        currently_playing_url = play_url
                    else:
                        if current_url_snapshot:
                             print("⏳ অ্যাডমিন কিউ খালি, ডিফল্ট নেই। অপেক্ষা...")
                        currently_playing_url = None
                        pass

            # অ্যাকশন
            if stop_default_and_process_queue:
                print("🛑 ডিফল্ট স্ট্রিম বন্ধ করা হচ্ছে...")
                stop_ffmpeg_stream()
                time.sleep(0.5)
                continue

            if next_video_path and play_url:
                # --- গুরুত্বপূর্ণ: এখানে এখন মাল্টি-বিটরেট শুরু হবে ---
                print(f"🎬 FFmpeg (মাল্টি-বিটরেট) শুরু করা হচ্ছে... ভিডিও: {os.path.basename(next_video_path)}, লুপ: {loop_default}")
                started_process = start_ffmpeg_stream(next_video_path, loop=loop_default)
                if not started_process:
                     with stream_lock:
                         if currently_playing_url == play_url:
                             currently_playing_url = None
                             print(f"⚠️ ব্যর্থ URL '{play_url[:80]}...' প্লে করা গেলো না।")

            # অপেক্ষা
            if ffmpeg_is_running:
                 time.sleep(1)
            elif not next_video_path:
                 time.sleep(3)
            else:
                 time.sleep(0.5)

        except Exception as e:
             print(f"🚨🚨 স্ট্রিম ম্যানেজার লুপে মারাত্মক ত্রুটি: {e} 🚨🚨")
             traceback.print_exc()
             try: stop_ffmpeg_stream()
             except Exception as stop_err: print(f"🚨 ত্রুটির পর FFmpeg বন্ধ করতেও সমস্যা: {stop_err}")
             with stream_lock:
                 currently_playing_url = None
                 predownload_attempted_for_url = None
             print("🔁 ৫ সেকেন্ড পর স্ট্রিম ম্যানেজার রিস্টার্ট করার চেষ্টা...")
             time.sleep(5)

    print("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    stop_ffmpeg_stream()


# --- Flask Routes ---

# HTML প্লেয়ার পেজ ('/') - কোনো পরিবর্তন নেই
@app.route('/')
def index():
    # index.html ফাইলটি নিশ্চিত করবে যে এটি এখন /stream/stream.m3u8 ব্যবহার করছে
    return render_template('index.html')

# HTML অ্যাডমিন প্যানেল ('/admin') - স্ট্যাটাস মেসেজ আপডেট করা যেতে পারে
@app.route('/admin')
def admin_panel():
    with stream_lock:
        queue_snapshot = list(video_queue) # আসল URL দেখাচ্ছে
        played_snapshot = list(played_today) # মডিফাইড URL দেখাচ্ছে
        current_url_snapshot = currently_playing_url # মডিফাইড URL
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        status_detail = ""
        if is_ffmpeg_running and video_queue:
            next_in_queue_raw = video_queue[0]
            status_detail = f" | এরপর: {next_in_queue_raw[:50]}..."

    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    if is_ffmpeg_running:
        # --- পরিবর্তিত স্ট্যাটাস ---
        mode = "[মাল্টি-বিটরেট এনকোডিং]" if current_url_snapshot != modified_default_url else "(লুপ, মাল্টি-বিটরেট)"
        if current_url_snapshot == modified_default_url:
            current_status = f"ডিফল্ট ভিডিও চলছে {mode}{status_detail}"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}... {mode}{status_detail}"
        else:
            current_status = "একটি ভিডিও চলছে (URL অজানা, মাল্টি-বিটরেট)"
    else:
        current_status = "⭕ কোনো ভিডিও চলছে না"
        if video_queue:
             current_status += f" | অপেক্ষায়: {video_queue[0][:50]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

# HTML ফর্ম থেকে ভিডিও যোগ ('/admin/add') - কোনো পরিবর্তন নেই
@app.route('/admin/add', methods=['POST'])
def add_video_form():
    url_from_form = request.form.get('video_url', '').strip()
    if url_from_form:
        if url_from_form.startswith('http://') or url_from_form.startswith('https://'):
            url_to_add = ensure_dropbox_raw_param(url_from_form)
            with stream_lock:
                if url_to_add in video_queue:
                     flash(f'"{url_to_add[:50]}..." ইতিমধ্যে কিউতে আছে।', 'warning')
                else:
                    video_queue.append(url_to_add)
                    print(f"📥 [অ্যাডমিন] কিউতে যোগ করা হয়েছে: {url_to_add}")
                    flash(f'"{url_to_add[:50]}..." যোগ করা হয়েছে।', 'success')
            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL!', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

# HTML বাটন থেকে কিউ খালি করা ('/admin/clear_queue') - কোনো পরিবর্তন নেই
@app.route('/admin/clear_queue', methods=['POST'])
def clear_queue_form():
    with stream_lock:
        if video_queue:
            video_queue.clear()
            print("🗑️ [অ্যাডমিন] ভিডিও কিউ খালি করা হয়েছে।")
            flash('ভিডিও কিউ খালি করা হয়েছে।', 'success')
        else:
             flash('কিউ আগে থেকেই খালি ছিল।', 'info')
    return redirect(url_for('admin_panel'))

# HTML বাটন থেকে প্লেড তালিকা খালি করা ('/admin/clear_played') - কোনো পরিবর্তন নেই
@app.route('/admin/clear_played', methods=['POST'])
def clear_played_form():
    with stream_lock:
        if played_today:
            played_today.clear()
            print("🗑️ [অ্যাডমিন] প্লেড তালিকা খালি করা হয়েছে।")
            flash("প্লেড তালিকা খালি করা হয়েছে।", 'success')
        else:
             flash("প্লেড তালিকা আগে থেকেই খালি ছিল।", 'info')
    return redirect(url_for('admin_panel'))

# API Routes ('/add', '/delete') - কোনো পরিবর্তন নেই
@app.route('/add', methods=['GET'])
def add_video_api():
    url_from_request = request.args.get('link', '').strip()
    if not url_from_request:
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400
    if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
        return jsonify({'status': 'error', 'message': 'Invalid URL format.', 'url': url_from_request}), 400
    url_to_add = ensure_dropbox_raw_param(url_from_request)
    with stream_lock:
        if url_to_add in video_queue:
            return jsonify({'status': 'warning', 'message': 'Video already in queue.', 'url': url_to_add}), 200
        else:
            video_queue.append(url_to_add)
            print(f"✅ [API Add] কিউতে যোগ করা হয়েছে: {url_to_add[:80]}...")
            return jsonify({'status': 'success', 'message': 'Video added to queue.', 'url': url_to_add}), 200

@app.route('/delete', methods=['GET'])
def delete_video_api():
    link_param = request.args.get('link', '').strip()
    if not link_param:
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400

    with stream_lock:
        if link_param.lower() == 'all':
            if video_queue:
                queue_len = len(video_queue)
                video_queue.clear()
                print(f"✅ [API Delete] কিউ খালি করা হয়েছে ({queue_len} আইটেম)।")
                return jsonify({'status': 'success', 'message': f'Queue cleared. {queue_len} items removed.'}), 200
            else:
                return jsonify({'status': 'info', 'message': 'Queue was already empty.'}), 200
        else:
            url_from_request = link_param
            if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
                 return jsonify({'status': 'error', 'message': 'Invalid URL format for deletion.', 'url': url_from_request}), 400
            url_to_delete = ensure_dropbox_raw_param(url_from_request)
            current_playing_modified = ensure_dropbox_raw_param(currently_playing_url) if currently_playing_url else None
            default_url_modified = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)

            if url_to_delete == current_playing_modified and url_to_delete != default_url_modified:
                 print(f"❌ [API Delete] ব্যর্থ: বর্তমানে চলছে এমন ভিডিও ({url_to_delete[:80]}...) ডিলিট করা যাবে না।")
                 return jsonify({'status': 'error', 'message': 'Cannot delete the currently playing video.', 'url': url_to_delete}), 403

            try:
                video_queue.remove(url_to_delete)
                print(f"✅ [API Delete] কিউ থেকে ডিলিট করা হয়েছে: {url_to_delete[:80]}...")
                return jsonify({'status': 'success', 'message': 'Video removed from queue.', 'url': url_to_delete}), 200
            except ValueError:
                print(f"❌ [API Delete] ব্যর্থ: ভিডিও কিউতে পাওয়া যায়নি ({url_to_delete[:80]}...)")
                return jsonify({'status': 'error', 'message': 'Video not found in queue.', 'url': url_to_delete}), 404


# --- HLS স্ট্রিম পরিবেশন ('/stream/<path:filename>') ---
# এই রুটে কোনো পরিবর্তন দরকার নেই। এটি এখন stream.m3u8 (মাস্টার প্লেলিস্ট)
# এবং সাব-ডিরেক্টরি থেকে আসা অন্যান্য m3u8 ও ts ফাইল পরিবেশন করবে।
@app.route('/stream/<path:filename>')
def stream(filename):
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    safe_base = os.path.normpath(stream_abs_path)
    # filename এখন সাব-ডিরেক্টরি 포함 করতে পারে (e.g., 720p/segment_00001.ts)
    file_abs_path = os.path.normpath(os.path.join(safe_base, filename))

    # ডিরেক্টরি ট্র্যাভার্সাল রোধ (আগের মতোই)
    if not file_abs_path.startswith(safe_base):
        print(f"🚫 নিরাপত্তা লঙ্ঘন প্রচেষ্টা: {filename}")
        abort(403)

    # ফাইল আছে কিনা এবং এটি ফাইল কিনা চেক (আগের মতোই)
    if not os.path.isfile(file_abs_path):
        # print(f"🔍 ফাইল পাওয়া যায়নি: {file_abs_path}") # 404 স্বাভাবিক
        abort(404)

    try:
        response = send_from_directory(safe_base, filename, conditional=True)
        # ক্যাশিং বন্ধ করার হেডার (আগের মতোই)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response
    except FileNotFoundError:
         abort(404)
    except Exception as e:
        print(f"❌ স্ট্রিম ফাইল ({filename}) সার্ভ করার সময় ত্রুটি: {e}")
        abort(500)

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার (signal_handler) ---
# কোনো পরিবর্তন নেই
def signal_handler(sig, frame):
    if stop_event.is_set(): return
    print("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে...")
    stop_event.set()
    print("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    print("🚦 সিগন্যাল হ্যান্ডলার থেকে FFmpeg বন্ধ করার চেষ্টা...")
    # এখানে stop_ffmpeg_stream কল করার প্রয়োজন নেই, কারণ stream_manager এবং finally ব্লক এটি করবে।
    # সরাসরি exit না করে থ্রেডগুলোকে শেষ হওয়ার সুযোগ দেওয়া ভালো।
    # exit(0) # থ্রেড শেষ না হলে এটা ব্যবহার করা যেতে পারে

# --- প্রধান চালক (__main__) ---
if __name__ == '__main__':
    print("*"*60)
    print("🚀 লাইভ স্ট্রিম অ্যাপ্লিকেশন শুরু হচ্ছে...")
    # --- পরিবর্তিত স্ট্যাটাস মেসেজ ---
    print("   ✨ মোড: মাল্টি-বিটরেট HLS এনকোডিং")
    print(f"   🔧 কোয়ালিটি লেভেলস: {[lvl['name'] for lvl in QUALITY_LEVELS]}")
    print("   🔧 Dropbox URL-এ স্বয়ংক্রিয় 'raw=1' যোগ করা হবে")
    print(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📂 ভিডিও ডাউনলোড ডিরেক্টরি: {os.path.abspath(VIDEO_DIR)}")
    print(f"📺 স্ট্রিম আউটপুট ডিরেক্টরি: {os.path.abspath(STREAM_OUTPUT_DIR)}")
    print(f"   🎬 মাস্টার প্লেলিস্ট: /{os.path.basename(STREAM_OUTPUT_DIR)}/{MASTER_PLAYLIST_NAME}")
    print("*"*60)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    host = '0.0.0.0'
    port = 5000
    print(f"🌍 Flask অ্যাপ http://{host}:{port} এ শোনার জন্য প্রস্তুত...")
    print(f"🔑 অ্যাডমিন প্যানেল: http://127.0.0.1:{port}/admin")
    print(f"👀 প্লেয়ার দেখুন: http://127.0.0.1:{port}/")
    print(f"⚙️ API Endpoints:")
    print(f"   - যোগ করুন: http://127.0.0.1:{port}/add?link=URL")
    print(f"   - ডিলিট করুন: http://127.0.0.1:{port}/delete?link=URL")
    print(f"   - সব ডিলিট করুন: http://127.0.0.1:{port}/delete?link=all")
    print("\n🛑 বন্ধ করতে Ctrl+C চাপুন।")

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        print(f"Flask অ্যাপ চালাতে গিয়ে মারাত্মক ত্রুটি: {e}")
        traceback.print_exc()
    finally:
        print("\nFlask অ্যাপ বন্ধ হচ্ছে...")
        if not stop_event.is_set():
            print("   -> stop_event সেট করা হচ্ছে...")
            stop_event.set()

        if manager_thread.is_alive():
            print("   -> ম্যানেজার থ্রেড বন্ধ হওয়ার জন্য অপেক্ষা (১০ সেকেন্ড)...")
            manager_thread.join(timeout=10)
            if manager_thread.is_alive():
                 print("⚠️ ম্যানেজার থ্রেড নির্দিষ্ট সময়ে বন্ধ হয়নি।")

        print("   -> চূড়ান্তভাবে FFmpeg বন্ধ করার চেষ্টা...")
        stop_ffmpeg_stream()

        print("👋 প্রধান থ্রেড সমাপ্ত।")
