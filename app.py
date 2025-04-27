import os
import subprocess
import threading
import time
import signal
import requests
import hashlib
import json # ffprobe থেকে আউটপুট পার্স করার জন্য
from flask import Flask, render_template, send_from_directory, abort, request, redirect, url_for, flash, jsonify
from flask_cors import CORS
from collections import deque # ভিডিও কিউয়ের জন্য
import traceback # বিস্তারিত এরর লগিং এর জন্য
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode # URL পার্সিং এর জন্য
import shutil # ডিরেক্টরি মুছে ফেলার জন্য

# --- কনফিগারেশন ---
DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts"

VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
# মাস্টার প্লেলিস্টের নাম পরিবর্তন করা হয়েছে
MASTER_HLS_OUTPUT_FILE = os.path.join(STREAM_OUTPUT_DIR, "master.m3u8")

# মাল্টি-বিটরেট সেটিংস
TARGET_QUALITIES = [
    # {'height': 1080, 'vb': '4000k', 'ab': '192k', 'name': '1080p'}, # প্রয়োজন হলে যোগ করুন
    {'height': 720, 'vb': '2500k', 'ab': '128k', 'name': '720p', 'preset': 'veryfast'},
    {'height': 480, 'vb': '1200k', 'ab': '96k', 'name': '480p', 'preset': 'veryfast'},
    {'height': 360, 'vb': '700k', 'ab': '64k', 'name': '360p', 'preset': 'veryfast'},
]
# যখন ভিডিও কপি করা হবে তখন অডিও বিটরেট কি হবে (যদি 480p বা কম হয় ইনপুট)
AUDIO_BITRATE_COPY_MODE = '128k'
COPY_THRESHOLD_HEIGHT = 480 # এই রেজোলিউশন বা এর কম হলে ভিডিও কপি করা হবে

# গ্লোবাল ভেরিয়েবল
video_queue = deque()
played_today = set()
current_ffmpeg_process = None
stop_event = threading.Event()
stream_lock = threading.Lock() # কিউ এবং ffmpeg প্রসেস অ্যাক্সেসের জন্য লক
currently_playing_url = None
default_video_path = None
current_stream_is_multibitrate = False # বর্তমানে মাল্টি-বিটরেট চলছে কিনা

app = Flask(__name__)
CORS(app) # সব ডোমেইন থেকে অ্যাক্সেসের অনুমতি দিন
app.secret_key = os.urandom(24)

# --- ডিরেক্টরি তৈরি ---
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

# --- Helper Functions ---

def ensure_dropbox_raw_param(url):
    """
    URL টি Dropbox লিঙ্ক হলে এবং শেষে raw=1 না থাকলে তা যোগ করে।
    """
    try:
        if not url or not (url.startswith('http://') or url.startswith('https://')):
            return url # অবৈধ বা খালি URL হলে কিছু না করে ফেরত দিন

        parsed_url = urlparse(url)

        # হোস্টনেম চেক (www.dropbox.com বা dropbox.com)
        if parsed_url.netloc.lower() == 'www.dropbox.com' or parsed_url.netloc.lower() == 'dropbox.com':
            query_params = parse_qs(parsed_url.query) # বর্তমান কোয়েরি প্যারামিটার পার্স করুন

            # raw=1 আছে কিনা চেক করুন
            if not ('raw' in query_params and query_params['raw'] == ['1']):
                print(f"🔧 Dropbox URL সনাক্ত হয়েছে, 'raw=1' যোগ করা হচ্ছে: {url[:80]}...")
                query_params['raw'] = ['1'] # raw=1 যোগ বা আপডেট করুন

                # নতুন কোয়েরি স্ট্রিং তৈরি করুন
                new_query = urlencode(query_params, doseq=True)

                # সম্পূর্ণ URL আবার তৈরি করুন
                modified_url = urlunparse((
                    parsed_url.scheme,
                    parsed_url.netloc,
                    parsed_url.path,
                    parsed_url.params,
                    new_query,
                    parsed_url.fragment
                ))
                print(f"   -> পরিবর্তিত URL: {modified_url[:80]}...")
                return modified_url
            else:
                 # raw=1 আগে থেকেই আছে
                 return url
        else:
            # Dropbox URL নয়
            return url
    except Exception as e:
        print(f"⚠️ URL '{url[:80]}...' পার্স বা মডিফাই করার সময় ত্রুটি: {e}")
        return url # ত্রুটি হলে আসল URL ফেরত দিন

def get_safe_filename(url):
    """URL থেকে একটি নিরাপদ ফাইলের নাম তৈরি করে (হ্যাশ ব্যবহার করে)"""
    try:
        parsed_url = urlparse(url)
        path_part = parsed_url.path
        base_name = os.path.basename(path_part)
        _, ext = os.path.splitext(base_name)

        # Use SHA1 hash of the *full* URL (including query params) for uniqueness
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]

        if not ext or len(ext) > 5:
             ext = '.mp4' # ডিফল্ট এক্সটেনশন

        # গ্রহণযোগ্য ভিডিও এক্সটেনশন চেক
        if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m3u8']:
             ext = '.mp4' # অগ্রহণযোগ্য হলে ডিফল্ট

        return f"video_{hashed_url}{ext}"
    except Exception as e:
        print(f"⚠️ ফাইলের নাম তৈরিতে সমস্যা ({url[:50]}...): {e}. একটি জেনেরিক নাম ব্যবহার করা হচ্ছে।")
        # Fallback to hashing the raw url if parsing fails
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        return f"video_{hashed_url}.mp4"


def download_video(url, output_filename):
    """একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে"""
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        # ফাইল আগে থেকেই থাকলে এবং খালি না হলে ডাউনলোড এড়িয়ে যান
        if os.path.exists(filepath):
            try:
                if os.path.getsize(filepath) > 0:
                    print(f"ℹ️ '{output_filename}' ({url[:50]}...) আগে থেকেই ডাউনলোড করা আছে এবং খালি নয়।")
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
             print(f"⚠️ সতর্কতা: Content-Type '{content_type}' ভিডিও মনে হচ্ছে না ({url[:80]}...) । ডাউনলোড করার চেষ্টা করা হচ্ছে...")
             if 'dropbox.com' in url and 'raw=1' not in url:
                 print(f"   -> এটি Dropbox লিঙ্ক কিন্তু 'raw=1' নেই। সম্ভবত HTML পেজ ডাউনলোড হবে।")

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
        print(f"❌ ভিডিও ডাউনলোড টাইমআউট ({url[:80]}...)")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে ফাইল ডিলিট
        return None
    except requests.exceptions.SSLError as e:
        print(f"❌ SSL ত্রুটি ({url[:80]}...): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except requests.exceptions.RequestException as e:
        print(f"❌ ভিডিও ডাউনলোড ব্যর্থ ({url[:80]}...): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except Exception as e:
        print(f"❌ ভিডিও সংরক্ষণ বা অন্য কোনো ত্রুটি ({url[:80]}...): {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None

def get_video_resolution(video_path):
    """ভিডিও ফাইলের রেজোলিউশন (উচ্চতা) পেতে ffprobe ব্যবহার করে"""
    try:
        command = [
            'ffprobe',
            '-v', 'error',             # শুধুমাত্র এরর দেখান
            '-select_streams', 'v:0',   # প্রথম ভিডিও স্ট্রিম নির্বাচন করুন
            '-show_entries', 'stream=width,height', # প্রস্থ এবং উচ্চতা দেখান
            '-of', 'json',             # আউটপুট ফরম্যাট JSON
            video_path
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True, timeout=30)
        data = json.loads(result.stdout)

        if 'streams' in data and len(data['streams']) > 0 and 'height' in data['streams'][0]:
            height = data['streams'][0]['height']
            width = data['streams'][0].get('width', 0) # প্রস্থও পাওয়া যেতে পারে
            print(f"ℹ️ ভিডিও রেজোলিউশন সনাক্ত হয়েছে: {width}x{height}")
            return height
        else:
            print(f"⚠️ ffprobe আউটপুটে ভিডিও স্ট্রিম বা উচ্চতা পাওয়া যায়নি ({os.path.basename(video_path)})")
            return None
    except FileNotFoundError:
        print("❌ ত্রুটি: 'ffprobe' কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ যোগ করা আছে কিনা নিশ্চিত করুন।")
        return None
    except subprocess.CalledProcessError as e:
        print(f"❌ ffprobe চালাতে সমস্যা ({os.path.basename(video_path)}): {e}")
        print(f"   stderr: {e.stderr}")
        return None
    except subprocess.TimeoutExpired:
        print(f"❌ ffprobe টাইমআউট ({os.path.basename(video_path)})")
        return None
    except json.JSONDecodeError:
        print(f"❌ ffprobe JSON আউটপুট পার্স করতে সমস্যা ({os.path.basename(video_path)})")
        return None
    except Exception as e:
        print(f"❌ ভিডিও রেজোলিউশন পেতে অজানা ত্রুটি ({os.path.basename(video_path)}): {e}")
        return None


def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস নিরাপদে বন্ধ করে"""
    global current_ffmpeg_process, current_stream_is_multibitrate
    with stream_lock: # লক নিশ্চিত করুন
        process_to_stop = current_ffmpeg_process
        if process_to_stop and process_to_stop.poll() is None: # প্রসেস কি সত্যিই চলছে?
            print(f"⏳ FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {process_to_stop.pid})...")
            try:
                if os.name == 'nt': # উইন্ডোজের জন্য
                    # SIGINT পাঠানোর চেষ্টা করা ভালো, taskkill খুব জোর করে বন্ধ করে
                    # process_to_stop.send_signal(signal.CTRL_C_EVENT) # এটি কাজ নাও করতে পারে সবসময়
                    # process_to_stop.wait(timeout=5)
                    # print("   -> FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (SIGINT/CTRL_C)।")
                    # উপরেরটা নির্ভরযোগ্য না হলে taskkill ব্যবহার করুন:
                    subprocess.run(['taskkill', '/F', '/T', '/PID', str(process_to_stop.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
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

        # গ্লোবাল ভেরিয়েবল আপডেট
        if current_ffmpeg_process == process_to_stop:
             current_ffmpeg_process = None
             current_stream_is_multibitrate = False # রিসেট

def start_ffmpeg_stream(video_path, loop=False):
    """
    একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg HLS স্ট্রিম শুরু করে।
    ইনপুট রেজোলিউশনের উপর ভিত্তি করে:
    - যদি <= COPY_THRESHOLD_HEIGHT হয়, তাহলে ভিডিও কপি করে, অডিও AAC তে এনকোড করে।
    - যদি > COPY_THRESHOLD_HEIGHT হয়, তাহলে TARGET_QUALITIES অনুযায়ী মাল্টি-বিটরেট স্ট্রিম তৈরি করে।
    """
    global current_ffmpeg_process, current_stream_is_multibitrate

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        print(f"❌ FFmpeg শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    # চলমান প্রসেস থাকলে বন্ধ করুন
    stop_ffmpeg_stream()
    # time.sleep(0.2) # বন্ধ হওয়ার জন্য একটু সময় দিন - stop_ffmpeg_stream is blocking

    # পুরাতন সেগমেন্ট ফাইল এবং সাব-ডিরেক্টরি মুছে ফেলা
    print(f"🧹 পুরনো HLS ফাইল/ডিরেক্টরি মুছে ফেলা হচ্ছে ({STREAM_OUTPUT_DIR})...")
    try:
        if os.path.exists(STREAM_OUTPUT_DIR):
            for item in os.listdir(STREAM_OUTPUT_DIR):
                item_path = os.path.join(STREAM_OUTPUT_DIR, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path) # সাব-ডিরেক্টরি মুছুন
                    elif os.path.isfile(item_path) and (item.endswith('.ts') or item.endswith('.m3u8')):
                        os.remove(item_path) # .ts বা .m3u8 ফাইল মুছুন
                except OSError as e:
                    print(f"⚠️ পুরনো ফাইল/ডিরেক্টরি মুছতে সমস্যা: {e}")
        else:
             os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)
    except Exception as e:
        print(f"⚠️ স্ট্রিম আউটপুট ডিরেক্টরি পরিষ্কার করতে সমস্যা: {e}")

    # ভিডিওর রেজোলিউশন নির্ণয়
    input_height = get_video_resolution(abs_video_path)

    ffmpeg_command = []
    stream_mode = "" # লগিং এর জন্য

    # --- ডিসিশন: কপি নাকি ট্রান্সকোড? ---
    if input_height is None or input_height <= COPY_THRESHOLD_HEIGHT:
        # মোড: ভিডিও কপি, অডিও এনকোড (AAC)
        stream_mode = f"[ভিডিও কপি, অডিও {AUDIO_BITRATE_COPY_MODE}] (ইনপুট <= {COPY_THRESHOLD_HEIGHT}p বা অজানা)"
        current_stream_is_multibitrate = False

        ffmpeg_command_base = ['ffmpeg', '-re']
        if loop:
            ffmpeg_command_base.extend(['-stream_loop', '-1'])
        ffmpeg_command_base.extend(['-i', abs_video_path])

        ffmpeg_command_options = [
            '-c:v', 'copy',
            '-c:a', 'aac',
            '-b:a', AUDIO_BITRATE_COPY_MODE,
            '-ac', '2',
            '-ar', '44100',
            '-err_detect', 'ignore_err',
            '-ignore_unknown',
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+omit_endlist+program_date_time',
            '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%05d.ts'),
            MASTER_HLS_OUTPUT_FILE # কপি মোডে একটাই প্লেলিস্ট, মাস্টার প্লেলিস্ট নামেই রাখা যাক
        ]
        ffmpeg_command = ffmpeg_command_base + ffmpeg_command_options

    else:
        # মোড: মাল্টি-বিটরেট ট্রান্সকোডিং
        stream_mode = f"[মাল্টি-বিটরেট ট্রান্সকোডিং] (ইনপুট {input_height}p)"
        current_stream_is_multibitrate = True

        # ইনপুট রেজোলিউশনের চেয়ে বড় কোয়ালিটি বাদ দিন
        active_qualities = [q for q in TARGET_QUALITIES if q['height'] <= input_height]
        if not active_qualities:
             print(f"⚠️ ইনপুট ভিডিওর ({input_height}p) জন্য কোনো উপযুক্ত টার্গেট কোয়ালিটি পাওয়া যায়নি। সর্বনিম্ন কোয়ালিটি ({TARGET_QUALITIES[-1]['name']}) ব্যবহার করা হচ্ছে।")
             active_qualities = [TARGET_QUALITIES[-1]]
        else:
             print(f"🚀 তৈরি করা হবে: {', '.join([q['name'] for q in active_qualities])}")


        # মাল্টি-বিটরেট FFmpeg কমান্ড তৈরি
        ffmpeg_command_base = ['ffmpeg', '-re']
        if loop:
             ffmpeg_command_base.extend(['-stream_loop', '-1'])
        ffmpeg_command_base.extend(['-i', abs_video_path])

        # ইনপুট স্ট্রিম ম্যাপ করা (প্রতিটি আউটপুটের জন্য)
        map_commands = []
        for i in range(len(active_qualities)):
             map_commands.extend(['-map', '0:v:0', '-map', '0:a:0']) # ইনপুট ভিডিও এবং অডিও ম্যাপ করুন

        # ফিল্টার, কোডেক, বিটরেট সেটিংস (প্রতিটি আউটপুটের জন্য)
        filter_complex_parts = []
        codec_options = []
        var_stream_map_parts = [] # মাস্টার প্লেলিস্টের জন্য

        for i, quality in enumerate(active_qualities):
             # ভিডিও ফিল্টার (স্কেলিং)
             filter_complex_parts.append(f"[0:v]scale=w=-2:h={quality['height']}[v{i}]")
             # ভিডিও কোডেক সেটিংস
             codec_options.extend([
                 f'-map', f'[v{i}]', f'-c:v:{i}', 'libx264',
                 f'-b:v:{i}', quality['vb'],
                 f'-preset:{i}', quality.get('preset', 'veryfast'), # প্রিসেট ব্যবহার করুন
                 f'-profile:v:{i}', 'main', # সামঞ্জস্যের জন্য প্রোফাইল সেট করা যেতে পারে
                 f'-level:v:{i}', '4.0',     # লেভেল সেট করা যেতে পারে
                 # '-g', str(int(4 * 25)), # GOP size = hls_time * framerate (আনুমানিক) - প্রয়োজন হলে
                 # '-keyint_min', str(int(4*25)), # Min keyframe interval - প্রয়োজন হলে
                 f'-sc_threshold:{i}', '0' # দৃশ্য পরিবর্তনের জন্য কীফ্রেম জোর করবেনা
             ])
             # অডিও কোডেক সেটিংস (প্রতিটি আউটপুটের জন্য একই ইনপুট অডিও ব্যবহার)
             codec_options.extend([
                 f'-map', f'0:a:0', f'-c:a:{i}', 'aac',
                 f'-b:a:{i}', quality['ab'],
                 f'-ac:{i}', '2',
                 f'-ar:{i}', '44100'
             ])
             # মাস্টার প্লেলিস্টের জন্য ভেরিয়েন্ট স্ট্রিম ম্যাপিং
             var_stream_map_parts.append(f"v:{i},a:{i},name:{quality['name']}")

        # সব ফিল্টার একসাথে যোগ করুন
        filter_complex_command = ['-filter_complex', ";".join(filter_complex_parts)]

        # HLS সেটিংস
        hls_options = [
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+program_date_time', # omit_endlist বাদ দিন, কারণ মাস্টার প্লেলিস্টে ENDLIST থাকবে
            '-master_pl_name', os.path.basename(MASTER_HLS_OUTPUT_FILE), # মাস্টার প্লেলিস্টের ফাইলের নাম
            # সেগমেন্ট এবং ভেরিয়েন্ট প্লেলিস্টের জন্য সাব-ডিরেক্টরি ব্যবহার
            '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, '%v', 'segment%05d.ts'), # %v মানে ভেরিয়েন্ট নাম (e.g., 720p)
            '-var_stream_map', " ".join(var_stream_map_parts),
        ]

        # আউটপুট প্যাটার্ন (ভেরিয়েন্ট প্লেলিস্টের জন্য)
        output_pattern = os.path.join(STREAM_OUTPUT_DIR, '%v', 'playlist.m3u8')

        # সম্পূর্ণ কমান্ড একত্র করুন
        ffmpeg_command = (
            ffmpeg_command_base +
            map_commands +
            filter_complex_command +
            codec_options +
            hls_options +
            [output_pattern]
        )

    # --- FFmpeg প্রসেস শুরু করা ---
    print(f"🚀 FFmpeg কমান্ড ({stream_mode}):", " ".join(f'"{arg}"' if ' ' in arg else arg for arg in ffmpeg_command))

    try:
        # মাল্টি-বিটরেট মোডে আউটপুট সাব-ডিরেক্টরি তৈরি (যদি না থাকে)
        if current_stream_is_multibitrate:
            for quality in active_qualities:
                subdir = os.path.join(STREAM_OUTPUT_DIR, quality['name'])
                os.makedirs(subdir, exist_ok=True)
                print(f"   -> আউটপুট ডিরেক্টরি তৈরি/নিশ্চিত করা হয়েছে: {subdir}")

        # FFmpeg প্রসেস শুরু
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

        # stderr লগিং থ্রেড (আগের মতোই)
        def log_stderr(proc, path):
            if proc.stderr:
                try:
                    for line in iter(proc.stderr.readline, b''):
                        if stop_event.is_set(): break
                        line_str = line.decode('utf-8', errors='replace').strip()
                        if line_str:
                             # গুরুত্বপূর্ণ লগ মেসেজ ফিল্টার করা (যেমন এরর, ওয়ার্নিং)
                             if any(kw in line_str.lower() for kw in ['error', 'failed', 'invalid', 'warning', 'possible', 'deprecated']):
                                 # কিছু সাধারণ ওয়ার্নিং উপেক্ষা করা যেতে পারে (যেমন Non-monotonous DTS)
                                 if "non-monotonous dts" not in line_str.lower() and "deprecated pixel format" not in line_str.lower():
                                     print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
                             # else: # ডিবাগিং এর জন্য সব লাইন দেখতে চাইলে এটি আনকমেন্ট করুন
                             #    pass # print(f"  [FFmpeg stderr - {os.path.basename(path)}]: {line_str}")
                except Exception as e:
                     print(f"⚠️ FFmpeg stderr পড়তে সমস্যা: {e}")
                finally:
                     if proc.stderr: proc.stderr.close()
            # print(f"  [FFmpeg stderr রিডিং থ্রেড শেষ - {os.path.basename(path)}]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True)
        stderr_thread.start()

        print(f"✅ FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)} {stream_mode}, লুপ: {loop}")
        with stream_lock: # লক সহ গ্লোবাল ভেরিয়েবল আপডেট
            current_ffmpeg_process = process
            # current_stream_is_multibitrate ইতিমধ্যে সেট করা হয়েছে
        return process

    except FileNotFoundError:
        print(f"❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ যোগ করা আছে কিনা নিশ্চিত করুন।")
        with stream_lock:
            current_ffmpeg_process = None
            current_stream_is_multibitrate = False
        return None
    except Exception as e:
        print(f"❌ FFmpeg শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")
        traceback.print_exc() # বিস্তারিত এরর দেখান
        with stream_lock:
            current_ffmpeg_process = None
            current_stream_is_multibitrate = False
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
    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    temp_default_path = download_video(modified_default_url, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
         default_video_path = temp_default_path
         print(f"✅ ডিফল্ট ভিডিও প্রস্তুত: {default_video_path} (URL: {modified_default_url[:50]}...)")
    else:
         print(f"🚨 সতর্কতা: ডিফল্ট ভিডিও ({modified_default_url[:50]}...) ডাউনলোড করা যায়নি! ডিফল্ট প্লেব্যাক কাজ করবে না।")

    predownload_attempted_for_url = None # কোন URL প্রি-ডাউনলোডের চেষ্টা করা হয়েছে

    while not stop_event.is_set():
        next_video_path = None
        play_url = None # এটি হবে মডিফাইড URL যা প্লে করা হবে
        loop_default = False
        stop_default_and_process_queue = False # ডিফল্ট ভিডিও চলার সময় কিউতে আইটেম এলে এটি True হবে

        try:
            with stream_lock: # এক্সেস করার আগে লক নিন
                ffmpeg_is_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
                current_url_snapshot = currently_playing_url # বর্তমান অবস্থা কপি করুন (এটিও মডিফাইড URL হবে)

                # --- ডিসিশন লজিক (আগের মতই, শুধু প্রি-ডাউনলোড এবং স্ট্যাটাস আপডেটে মনোযোগ দিন) ---

                # 1. FFmpeg চলছে?
                if ffmpeg_is_running:
                    modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                    # 1a. কিউ ভিডিও চলছে এবং কিউতে আরও আইটেম আছে? পরেরটা প্রি-ডাউনলোড করুন
                    if current_url_snapshot != modified_default_url_snapshot and video_queue:
                        next_url_in_queue_raw = video_queue[0]
                        next_url_in_queue_modified = ensure_dropbox_raw_param(next_url_in_queue_raw)

                        if next_url_in_queue_modified != predownload_attempted_for_url:
                            print(f"🔎 প্রি-ডাউনলোডের জন্য চেক করা হচ্ছে: {next_url_in_queue_modified[:80]}...")
                            next_filename = get_safe_filename(next_url_in_queue_modified)
                            downloaded_path = download_video(next_url_in_queue_modified, next_filename)
                            if downloaded_path:
                                print(f"👍 প্রি-ডাউনলোড সম্পন্ন বা ফাইল আগে থেকেই আছে: {next_filename}")
                            else:
                                print(f"👎 প্রি-ডাউনলোড ব্যর্থ: {next_url_in_queue_modified[:80]}...")
                            predownload_attempted_for_url = next_url_in_queue_modified # চেষ্টা করা হয়েছে বলে মার্ক করুন

                    # 1b. ডিফল্ট ভিডিও চলছে কিন্তু কিউতে নতুন আইটেম এসেছে? ডিফল্ট বন্ধ করতে হবে
                    elif current_url_snapshot == modified_default_url_snapshot and video_queue:
                        print("🔄 ডিফল্ট ভিডিও চলছিল, কিন্তু কিউতে নতুন আইটেম এসেছে। ডিফল্ট বন্ধ করা হচ্ছে...")
                        stop_default_and_process_queue = True
                        predownload_attempted_for_url = None # প্রি-ডাউনলোড রিসেট

                    # 1c. অন্যান্য ক্ষেত্রে: কিছু করার নেই
                    else:
                        if current_url_snapshot != modified_default_url_snapshot and not video_queue:
                            predownload_attempted_for_url = None
                        pass

                # 2. FFmpeg চলছে না?
                else:
                    predownload_attempted_for_url = None # প্রি-ডাউনলোড রিসেট
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        print(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) স্বাভাবিকভাবে শেষ হয়েছে।")
                        modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                        if current_url_snapshot and current_url_snapshot != modified_default_url_snapshot:
                             played_today.add(current_url_snapshot)
                        current_ffmpeg_process = None # প্রসেস রিসেট
                        currently_playing_url = None # URL রিসেট
                        current_stream_is_multibitrate = False # মোড রিসেট

                    # 2a. কিউতে ভিডিও আছে?
                    if video_queue:
                        raw_url_from_queue = video_queue.popleft()
                        play_url = ensure_dropbox_raw_param(raw_url_from_queue)
                        print(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে (মডিফাইড): {play_url[:80]}...")
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename)
                        if not next_video_path:
                            print(f"❌ ডাউনলোড ব্যর্থ (প্লে করার জন্য): {play_url[:80]}... এটি স্কিপ করা হলো।")
                            play_url = None
                            currently_playing_url = None
                        else:
                             loop_default = False # কিউ ভিডিও লুপ হয় না
                             currently_playing_url = play_url

                    # 2b. কিউ খালি কিন্তু ডিফল্ট ভিডিও আছে?
                    elif default_video_path:
                        modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                        if current_url_snapshot != modified_default_url_snapshot:
                             print("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                        next_video_path = default_video_path
                        play_url = modified_default_url_snapshot
                        loop_default = True
                        currently_playing_url = play_url

                    # 2c. কিউ খালি এবং ডিফল্ট ভিডিও নেই?
                    else:
                        if current_url_snapshot:
                             print("⏳ অ্যাডমিন কিউ খালি এবং ডিফল্ট ভিডিও উপলব্ধ নেই। অপেক্ষা করা হচ্ছে...")
                        currently_playing_url = None
                        pass

            # --- অ্যাকশন ---
            if stop_default_and_process_queue:
                print("🛑 ডিফল্ট স্ট্রিম বন্ধ করা হচ্ছে...")
                stop_ffmpeg_stream()
                time.sleep(0.5)
                continue # পরের আইটেম প্রসেস করতে লুপের শুরুতে যান

            if next_video_path and play_url:
                print(f"🎬 FFmpeg শুরু করার প্রস্তুতি... ভিডিও: {os.path.basename(next_video_path)}, লুপ: {loop_default}")
                started_process = start_ffmpeg_stream(next_video_path, loop=loop_default)
                if not started_process:
                     with stream_lock:
                         if currently_playing_url == play_url:
                             currently_playing_url = None
                             current_stream_is_multibitrate = False # মোড রিসেট
                             print(f"⚠️ ব্যর্থ URL '{play_url[:80]}...' প্লে করা গেলো না।")

            # --- অপেক্ষা ---
            time.sleep(1) # ছোট இடைেত চেক করা ভাল, কারণ FFmpeg নিজেই আউটপুট তৈরি করছে

        except Exception as e:
             print(f"🚨🚨 স্ট্রিম ম্যানেজার লুপে মারাত্মক ত্রুটি: {e} 🚨🚨")
             traceback.print_exc()
             try:
                 stop_ffmpeg_stream()
             except Exception as stop_err:
                  print(f"🚨 ত্রুটির পর FFmpeg বন্ধ করতেও সমস্যা: {stop_err}")
             with stream_lock:
                 currently_playing_url = None
                 predownload_attempted_for_url = None
                 current_stream_is_multibitrate = False # রিসেট
             print("🔁 ৫ সেকেন্ড পর স্ট্রিম ম্যানেজার রিস্টার্ট করার চেষ্টা...")
             time.sleep(5)

    print("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    stop_ffmpeg_stream()


# --- Flask Routes ---

# HTML প্লেয়ার পেজ
@app.route('/')
def index():
    # প্লেয়ারকে মাস্টার প্লেলিস্টের URL দিন
    hls_url = url_for('stream', filename=os.path.basename(MASTER_HLS_OUTPUT_FILE), _external=True)
    return render_template('index.html', hls_url=hls_url)

# HTML অ্যাডমিন প্যানেল
@app.route('/admin')
def admin_panel():
    with stream_lock:
        queue_snapshot = list(video_queue)
        played_snapshot = list(played_today)
        current_url_snapshot = currently_playing_url
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        is_multibitrate = current_stream_is_multibitrate
        status_detail = ""
        if is_ffmpeg_running and video_queue:
            next_in_queue_raw = video_queue[0]
            status_detail = f" | এরপর কিউতে: {next_in_queue_raw[:50]}..."

    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    if is_ffmpeg_running:
        mode = "[মাল্টি-বিটরেট]" if is_multibitrate else "[ভিডিও কপি]"
        if current_url_snapshot == modified_default_url:
            # ডিফল্ট ভিডিও মাল্টি-বিটরেট মোডে চলতে পারে যদি এটি 480p এর বেশি হয়
            current_status = f"ডিফল্ট ভিডিও চলছে {mode} (লুপ){status_detail}"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}... {mode}{status_detail}"
        else:
            current_status = f"একটি ভিডিও চলছে (URL অজানা) {mode}"
    else:
        current_status = "⭕ কোনো ভিডিও চলছে না"
        if video_queue:
             current_status += f" | প্লে করার অপেক্ষায়: {video_queue[0][:50]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

# HTML ফর্ম থেকে ভিডিও যোগ (আগের মতোই)
@app.route('/admin/add', methods=['POST'])
def add_video_form():
    url_from_form = request.form.get('video_url', '').strip()
    if url_from_form:
        if url_from_form.startswith('http://') or url_from_form.startswith('https://'):
            url_to_add = ensure_dropbox_raw_param(url_from_form)
            with stream_lock:
                if url_to_add in video_queue:
                     flash(f'"{url_to_add[:50]}..." এই URL টি ইতিমধ্যে কিউতে আছে (সম্ভবত raw=1 সহ)।', 'warning')
                else:
                    video_queue.append(url_to_add)
                    print(f"📥 [অ্যাডমিন] কিউতে যোগ করা হয়েছে: {url_to_add}")
                    flash(f'"{url_to_add[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL! অনুগ্রহ করে http:// বা https:// দিয়ে শুরু হওয়া একটি URL দিন।', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

# HTML বাটন থেকে কিউ খালি করা (আগের মতোই)
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

# HTML বাটন থেকে 'আজকে চালানো হয়েছে' তালিকা খালি করা (আগের মতোই)
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

# --- API Routes --- (আগের মতোই)

# API: ভিডিও যোগ করা (GET)
@app.route('/add', methods=['GET'])
def add_video_api():
    url_from_request = request.args.get('link', '').strip()
    if not url_from_request:
        print("❌ [API Add] ব্যর্থ: 'link' প্যারামিটার পাওয়া যায়নি।")
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400
    if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
        print(f"❌ [API Add] ব্যর্থ: অবৈধ URL ফরম্যাট ({url_from_request[:50]}...)")
        return jsonify({'status': 'error', 'message': 'Invalid URL format.', 'url': url_from_request}), 400

    url_to_add = ensure_dropbox_raw_param(url_from_request)
    with stream_lock:
        if url_to_add in video_queue:
            print(f"⚠️ [API Add] ইতিমধ্যে কিউতে আছে: {url_to_add[:80]}...")
            return jsonify({'status': 'warning', 'message': 'Video already in queue.', 'url': url_to_add, 'original_url': url_from_request}), 200
        else:
            video_queue.append(url_to_add)
            print(f"✅ [API Add] কিউতে যোগ করা হয়েছে: {url_to_add[:80]}...")
            return jsonify({'status': 'success', 'message': 'Video added to queue.', 'url': url_to_add, 'original_url': url_from_request}), 200

# API: ভিডিও ডিলিট করা (GET)
@app.route('/delete', methods=['GET'])
def delete_video_api():
    link_param = request.args.get('link', '').strip()
    if not link_param:
        print("❌ [API Delete] ব্যর্থ: 'link' প্যারামিটার পাওয়া যায়নি।")
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400

    with stream_lock:
        if link_param.lower() == 'all':
            if video_queue:
                queue_len = len(video_queue)
                video_queue.clear()
                print(f"✅ [API Delete] সম্পূর্ণ কিউ খালি করা হয়েছে ({queue_len} টি আইটেম ছিল)।")
                return jsonify({'status': 'success', 'message': f'Queue cleared. {queue_len} items removed.'}), 200
            else:
                print("ℹ️ [API Delete] কিউ আগে থেকেই খালি ছিল (link=all)।")
                return jsonify({'status': 'info', 'message': 'Queue was already empty.'}), 200
        else:
            url_from_request = link_param
            if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
                 print(f"❌ [API Delete] ব্যর্থ: ডিলিটের জন্য অবৈধ URL ফরম্যাট ({url_from_request[:50]}...)")
                 return jsonify({'status': 'error', 'message': 'Invalid URL format for deletion.', 'url': url_from_request}), 400

            url_to_delete = ensure_dropbox_raw_param(url_from_request)
            current_playing_modified = ensure_dropbox_raw_param(currently_playing_url) if currently_playing_url else None
            default_url_modified = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)

            if url_to_delete == current_playing_modified and url_to_delete != default_url_modified:
                 print(f"❌ [API Delete] ব্যর্থ: বর্তমানে চলছে এমন ভিডিও ডিলিট করা যাবে না ({url_to_delete[:80]}...)")
                 return jsonify({'status': 'error', 'message': 'Cannot delete the currently playing video.', 'url': url_to_delete, 'original_url': url_from_request}), 403

            try:
                video_queue.remove(url_to_delete)
                print(f"✅ [API Delete] কিউ থেকে ডিলিট করা হয়েছে: {url_to_delete[:80]}...")
                return jsonify({'status': 'success', 'message': 'Video removed from queue.', 'url': url_to_delete, 'original_url': url_from_request}), 200
            except ValueError:
                print(f"❌ [API Delete] ব্যর্থ: ভিডিও কিউতে পাওয়া যায়নি ({url_to_delete[:80]}...)")
                return jsonify({'status': 'error', 'message': 'Video not found in queue.', 'url': url_to_delete, 'original_url': url_from_request}), 404

# --- HLS স্ট্রিম পরিবেশন (মাস্টার প্লেলিস্ট, ভেরিয়েন্ট প্লেলিস্ট এবং সেগমেন্ট) ---
@app.route('/stream/<path:filename>')
def stream(filename):
    """
    HLS ফাইলগুলো পরিবেশন করে। এটি মাস্টার প্লেলিস্ট (master.m3u8),
    ভেরিয়েন্ট প্লেলিস্ট (যেমন, 720p/playlist.m3u8) এবং
    সেগমেন্ট ফাইল (যেমন, 720p/segment00001.ts) হ্যান্ডেল করতে পারে।
    """
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    safe_base = os.path.normpath(stream_abs_path)
    # filename এ সাব-ডিরেক্টরি অন্তর্ভুক্ত থাকতে পারে (যেমন '720p/playlist.m3u8')
    file_abs_path = os.path.normpath(os.path.join(safe_base, filename))

    # ডিরেক্টরি ট্র্যাভার্সাল অ্যাটাক রোধ
    if not file_abs_path.startswith(safe_base):
        print(f"🚫 নিরাপত্তা লঙ্ঘন প্রচেষ্টা রোধ করা হয়েছে: {filename}")
        abort(403) # Forbidden

    # ফাইলটি আসলেই একটি ফাইল কিনা এবং আছে কিনা চেক করুন
    if not os.path.isfile(file_abs_path):
        # print(f"🔍 HLS ফাইল পাওয়া যায়নি: {file_abs_path}") # ডিবাগিং - খুব বেশি লগ তৈরি করতে পারে
        abort(404) # Not Found

    try:
        # send_from_directory সাব-ডিরেক্টরি হ্যান্ডেল করতে পারে
        # directory আর্গুমেন্ট হল বেস ডিরেক্টরি
        # filename আর্গুমেন্ট হল directory এর ভেতরের রিলেটিভ পাথ
        directory_part, file_part = os.path.split(filename)
        actual_directory = os.path.join(safe_base, directory_part)

        # print(f"📤 ফাইল পরিবেশন: directory='{actual_directory}', filename='{file_part}'") # ডিবাগিং

        response = send_from_directory(actual_directory, file_part, conditional=True)

        # ক্লায়েন্ট সাইড ক্যাশিং বন্ধ করার জন্য হেডার সেট করা
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    except FileNotFoundError:
         abort(404)
    except Exception as e:
        print(f"❌ স্ট্রিম ফাইল সার্ভ করার সময় ত্রুটি ({filename}): {e}")
        traceback.print_exc() # বিস্তারিত এরর লগ
        abort(500) # Internal Server Error

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার (আগের মতোই) ---
def signal_handler(sig, frame):
    if stop_event.is_set():
        print("⏳ ইতিমধ্যে বন্ধ করার প্রক্রিয়া চলছে...")
        return
    print("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set()
    print("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    # time.sleep(0.5)

    print("🚦 সিগন্যাল হ্যান্ডলার থেকে FFmpeg বন্ধ করার চেষ্টা...")
    stop_ffmpeg_stream()

    print("👋 অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    exit(0)

# --- প্রধান চালক ---
if __name__ == '__main__':
    print("*"*60)
    print("🚀 মাল্টি-বিটরেট লাইভ স্ট্রিম অ্যাপ্লিকেশন শুরু হচ্ছে...")
    print(f"   ✨ মোড: ইনপুট > {COPY_THRESHOLD_HEIGHT}p হলে ট্রান্সকোড ({', '.join([q['name'] for q in TARGET_QUALITIES])}), অন্যথায় ভিডিও কপি।")
    print("   🔧 বৈশিষ্ট্য: Dropbox URL-এ স্বয়ংক্রিয়ভাবে 'raw=1' যোগ করা হবে।")
    print(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📂 ভিডিও ডাউনলোড ডিরেক্টরি: {os.path.abspath(VIDEO_DIR)}")
    print(f"📺 স্ট্রিম আউটপুট ডিরেক্টরি: {os.path.abspath(STREAM_OUTPUT_DIR)}")
    print("*"*60)

    # সিগন্যাল হ্যান্ডলার সেটআপ
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার থ্রেড শুরু করুন
    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    host = '0.0.0.0'
    port = 5000
    print(f"🌍 Flask অ্যাপ http://{host}:{port} এ শোনার জন্য প্রস্তুত...")
    print(f"🔑 HTML অ্যাডমিন প্যানেল: http://127.0.0.1:{port}/admin")
    # প্লেয়ারের URL এখন মাস্টার প্লেলিস্ট ব্যবহার করবে
    print(f"👀 প্লেয়ার দেখুন: http://127.0.0.1:{port}/")
    print(f"   (প্লেয়ার স্বয়ংক্রিয়ভাবে '/stream/master.m3u8' লোড করবে)")
    print(f"⚙️ API Endpoints:")
    print(f"   - ভিডিও যোগ করুন (GET): http://127.0.0.1:{port}/add?link=VIDEO_URL")
    print(f"   - ভিডিও ডিলিট করুন (GET): http://127.0.0.1:{port}/delete?link=VIDEO_URL")
    print(f"   - সব কিউ ডিলিট করুন (GET): http://127.0.0.1:{port}/delete?link=all")
    print("\n🛑 অ্যাপ্লিকেশন বন্ধ করতে Ctrl+C চাপুন।")

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        print(f"Flask অ্যাপ চালাতে গিয়ে মারাত্মক ত্রুটি: {e}")
        traceback.print_exc()
    finally:
        print("\nFlask অ্যাপ বন্ধ হয়েছে বা হতে চলেছে...")
        if not stop_event.is_set():
            print("   -> stop_event সেট করা হচ্ছে...")
            stop_event.set()

        if manager_thread.is_alive():
            print("   -> ম্যানেজার থ্রেডকে বন্ধ হওয়ার জন্য অপেক্ষা করা হচ্ছে (১০ সেকেন্ড পর্যন্ত)...")
            manager_thread.join(timeout=10)
            if manager_thread.is_alive():
                 print("⚠️ ম্যানেজার থ্রেড নির্দিষ্ট সময়ের মধ্যে বন্ধ হয়নি।")

        print("   -> চূড়ান্তভাবে FFmpeg বন্ধ করার চেষ্টা...")
        stop_ffmpeg_stream()

        print("👋 প্রধান থ্রেড সমাপ্ত।")
