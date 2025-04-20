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
DEFAULT_VIDEO_FILENAME = "default_video.ts"

VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
HLS_OUTPUT_FILE = os.path.join(STREAM_OUTPUT_DIR, "stream.m3u8")

# গ্লোবাল ভেরিয়েবল
video_queue = deque()
played_today = set()
current_ffmpeg_process = None
stop_event = threading.Event()
stream_lock = threading.Lock()
currently_playing_url = None
default_video_path = None

app = Flask(__name__)
CORS(app)
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
             ext = '.mp4'
    except Exception:
        ext = '.mp4'

    if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m3u8']:
         ext = '.mp4'

    return f"video_{hashed_url}{ext}"

def download_video(url, output_filename):
    """একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে"""
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
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
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get('content-type', '').lower()
        problematic_types = ['text/html', 'application/json']
        is_likely_video = 'video' in content_type or 'mpegurl' in content_type or 'octet-stream' in content_type or not any(ptype in content_type for ptype in problematic_types)

        if not is_likely_video:
             print(f"⚠️ সতর্কতা: Content-Type '{content_type}' ভিডিও মনে হচ্ছে না ({url})। তবুও ডাউনলোড করার চেষ্টা করা হচ্ছে...")

        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 4):
                if stop_event.is_set():
                    print("🛑 ডাউনলোড বাতিল করা হয়েছে (অ্যাপ বন্ধ)।")
                    if os.path.exists(filepath): os.remove(filepath)
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

        if downloaded_size == 0:
             print(f"❌ ডাউনলোড সম্পন্ন হয়েছে কিন্তু ফাইলের সাইজ ০ ({filepath})। সম্ভবত সমস্যা আছে।")
             if os.path.exists(filepath): os.remove(filepath)
             return None

        print(f"✅ সফলভাবে ডাউনলোড হয়েছে: {output_filename} (Size: {downloaded_size / (1024 * 1024):.2f} MB)")
        return filepath

    except requests.exceptions.Timeout:
        print(f"❌ ভিডিও ডাউনলোড টাইমআউট ({url})")
        if os.path.exists(filepath): os.remove(filepath)
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
    """চলমান FFmpeg প্রসেস বন্ধ করে"""
    global current_ffmpeg_process
    with stream_lock:
        process_to_stop = current_ffmpeg_process
        if process_to_stop:
            print(f"⏳ FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {process_to_stop.pid})...")
            if process_to_stop.poll() is None:
                try:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/F', '/PID', str(process_to_stop.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (taskkill)।")
                    else:
                        process_to_stop.terminate()
                        try:
                            process_to_stop.wait(timeout=5)
                            print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (terminate)।")
                        except subprocess.TimeoutExpired:
                            print("FFmpeg প্রসেস terminate হয়নি, SIGKILL পাঠানো হচ্ছে...")
                            process_to_stop.kill()
                            process_to_stop.wait()
                            print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (kill)।")
                except Exception as e:
                    print(f"⚠️ FFmpeg বন্ধ করার সময় ত্রুটি: {e}")
            else:
                print("ℹ️ FFmpeg প্রসেস বন্ধ করার চেষ্টা করার সময় দেখা গেলো এটি আগে থেকেই বন্ধ ছিল।")

            if current_ffmpeg_process == process_to_stop:
                 current_ffmpeg_process = None

def start_ffmpeg_stream(video_path, loop=False):
    """
    একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg স্ট্রিম শুরু করে।
    **পরিবর্তন:** ভিডিও স্ট্রিম কপি করা হবে, শুধু অডিও এনকোড করা হবে।
    """
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        print(f"❌ FFmpeg শুরু করা যাচ্ছে না, ফাইল পাওয়া যায়নি: {abs_video_path}")
        return None

    ffmpeg_command_base = [
        'ffmpeg',
        '-re', # ইনপুট রিয়েল টাইমে পড়ার চেষ্টা
    ]

    if loop:
        ffmpeg_command_base.extend(['-stream_loop', '-1'])

    ffmpeg_command_base.extend(['-i', abs_video_path])

    # পুরাতন সেগমেন্ট ফাইল মুছে ফেলা
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


    # --- FFmpeg অপশনস (ভিডিও কপি, অডিও এনকোড) ---
    ffmpeg_command_options = [
        # ভিডিও অপশনস: ভিডিও স্ট্রিম সরাসরি কপি করুন
        '-c:v', 'copy',

        # অডিও অপশনস: অডিও স্ট্রিম AAC তে এনকোড করুন
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ac', '2',
        '-ar', '44100',

        # HLS আউটপুট অপশনস
        '-f', 'hls',
        '-hls_time', '4',      # সেগমেন্ট দৈর্ঘ্য (সেকেন্ড)
        '-hls_list_size', '6', # প্লেলিস্টে ফাইলের সংখ্যা
        '-hls_flags', 'delete_segments+omit_endlist+program_date_time',
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%05d.ts'),
        HLS_OUTPUT_FILE
    ]
    # ---------------------------------------------

    ffmpeg_command = ffmpeg_command_base + ffmpeg_command_options

    print("🚀 FFmpeg কমান্ড (ভিডিও কপি):", " ".join(ffmpeg_command))
    try:
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL)

        def log_stderr(proc, path):
            if proc.stderr:
                try:
                    for line in iter(proc.stderr.readline, b''):
                        if stop_event.is_set(): break
                        line_str = line.decode('utf-8', errors='replace').strip()
                        if line_str:
                            # '-c copy' ব্যবহার করার সময় কিছু warning বা error আসতে পারে যা স্বাভাবিক
                            # যেমন: "Timestamps are unset in a packet" বা keyframe 관련 মেসেজ
                            print(f"  [FFmpeg - {os.path.basename(path)}]: {line_str}")
                except Exception as e:
                     print(f"⚠️ FFmpeg stderr পড়তে সমস্যা: {e}")
                finally:
                     if proc.stderr: proc.stderr.close()
            print(f"  [FFmpeg stderr রিডিং শেষ - {os.path.basename(path)}]")

        stderr_thread = threading.Thread(target=log_stderr, args=(process, video_path), daemon=True)
        stderr_thread.start()

        print(f"✅ FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {os.path.basename(video_path)} [ভিডিও কপি], লুপ: {loop}")
        with stream_lock:
            current_ffmpeg_process = process
        return process

    except FileNotFoundError:
        print(f"❌ ত্রুটি: 'ffmpeg' কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ যোগ করা আছে কিনা নিশ্চিত করুন।")
        with stream_lock: current_ffmpeg_process = None
        return None
    except Exception as e:
        # '-c copy' তে প্রায়ই এনকোডিং এর চেয়ে ভিন্ন ধরনের ত্রুটি হতে পারে
        print(f"❌ FFmpeg শুরু করতে ব্যর্থ ({os.path.basename(video_path)}): {e}")
        print("   ℹ️ এটি ইনপুট ভিডিও কোডেক (H.264 নয়?) বা HLS এর সাথে সামঞ্জস্যতার সমস্যার কারণে হতে পারে।")
        with stream_lock: current_ffmpeg_process = None
        return None

# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার (আগের মতোই, কোনো পরিবর্তন নেই) ---
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

                if ffmpeg_is_running:
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
                            predownload_attempted_for_url = next_url_in_queue

                    elif current_url_snapshot == DEFAULT_VIDEO_URL and video_queue:
                        print("🔄 ডিফল্ট ভিডিও চলছিল, কিন্তু কিউতে নতুন আইটেম এসেছে। ডিফল্ট বন্ধ করা হচ্ছে...")
                        stop_default_and_process_queue = True
                        predownload_attempted_for_url = None

                    else:
                        if current_url_snapshot != DEFAULT_VIDEO_URL and not video_queue:
                            predownload_attempted_for_url = None
                        pass
                else:
                    predownload_attempted_for_url = None
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        print(f"🏁 FFmpeg (PID: {current_ffmpeg_process.pid}) শেষ হয়েছে।")
                        if current_url_snapshot and current_url_snapshot != DEFAULT_VIDEO_URL:
                             played_today.add(current_url_snapshot)
                        current_ffmpeg_process = None
                        currently_playing_url = None

                    if video_queue:
                        play_url = video_queue.popleft()
                        print(f"▶️ অ্যাডমিন কিউ থেকে নেওয়া হয়েছে: {play_url[:80]}...")
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename)
                        if not next_video_path:
                            print(f"❌ ডাউনলোড ব্যর্থ (প্লে করার জন্য): {play_url[:80]}... এটি স্কিপ করা হলো।")
                            play_url = None
                            currently_playing_url = None
                        else:
                             loop_default = False
                             currently_playing_url = play_url

                    elif default_video_path:
                        if current_url_snapshot != DEFAULT_VIDEO_URL:
                             print("ℹ️ অ্যাডমিন কিউ খালি। ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                        next_video_path = default_video_path
                        play_url = DEFAULT_VIDEO_URL
                        loop_default = True
                        currently_playing_url = play_url

                    else:
                        if current_url_snapshot:
                             print("⏳ অ্যাডমিন কিউ খালি এবং ডিফল্ট ভিডিও উপলব্ধ নেই। অপেক্ষা করা হচ্ছে...")
                        currently_playing_url = None
                        pass

            if stop_default_and_process_queue:
                print("🛑 ডিফল্ট স্ট্রিম বন্ধ করা হচ্ছে...")
                stop_ffmpeg_stream()
                time.sleep(0.5)
                continue

            if next_video_path and play_url:
                print(f"🎬 FFmpeg শুরু করা হচ্ছে... ভিডিও: {os.path.basename(next_video_path)}, লুপ: {loop_default}")
                started_process = start_ffmpeg_stream(next_video_path, loop=loop_default)
                if not started_process:
                     with stream_lock:
                         if currently_playing_url == play_url:
                             currently_playing_url = None
                             print(f"⚠️ ব্যর্থ URL '{play_url[:80]}...' প্লে করা গেলো না।")

            if ffmpeg_is_running:
                 time.sleep(1)
            elif not next_video_path:
                 time.sleep(3)
            else:
                 time.sleep(0.5)

        except Exception as e:
             print(f"🚨🚨 স্ট্রিম ম্যানেজার লুপে মারাত্মক ত্রুটি: {e} 🚨🚨")
             import traceback
             traceback.print_exc()
             try:
                 stop_ffmpeg_stream()
             except Exception as stop_err:
                  print(f"🚨 ত্রুটির পর FFmpeg বন্ধ করতেও সমস্যা: {stop_err}")
             with stream_lock:
                 currently_playing_url = None
                 predownload_attempted_for_url = None
             print("🔁 ৫ সেকেন্ড পর স্ট্রিম ম্যানেজার রিস্টার্ট করার চেষ্টা...")
             time.sleep(5)

    print("🛑 স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    stop_ffmpeg_stream()

# --- Flask Routes (আগের মতোই, কোনো পরিবর্তন নেই) ---
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/admin')
def admin_panel():
    with stream_lock:
        queue_snapshot = list(video_queue)
        played_snapshot = list(played_today)
        current_url_snapshot = currently_playing_url
        is_ffmpeg_running = current_ffmpeg_process and current_ffmpeg_process.poll() is None
        status_detail = ""
        if is_ffmpeg_running:
             next_in_queue = video_queue[0] if video_queue else None
             if next_in_queue:
                  status_detail = f" | এরপর কিউতে আছে: {next_in_queue[:50]}..."

    if is_ffmpeg_running:
        # স্ট্যাটাসে উল্লেখ করা যে ভিডিও কপি হচ্ছে
        mode = "[ভিডিও কপি]" if current_url_snapshot != DEFAULT_VIDEO_URL else "(লুপ)"
        if current_url_snapshot == DEFAULT_VIDEO_URL:
            current_status = f"ডিফল্ট ভিডিও চলছে {mode}{status_detail}"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}... {mode}{status_detail}"
        else:
            current_status = "একটি ভিডিও চলছে (URL অজানা)"
    else:
        current_status = "কোনো ভিডিও চলছে না"
        if video_queue:
             current_status += f" | প্লে করার অপেক্ষায়: {video_queue[0][:50]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

@app.route('/admin/add', methods=['POST'])
def add_video():
    url = request.form.get('video_url', '').strip()
    if url:
        if url.startswith('http://') or url.startswith('https://'):
            with stream_lock:
                if url in video_queue:
                     flash(f'"{url[:50]}..." এই URL টি ইতিমধ্যে কিউতে আছে।', 'warning')
                else:
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
    with stream_lock:
        if video_queue:
            video_queue.clear()
            print("🗑️ ভিডিও কিউ খালি করা হয়েছে।")
            flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
        else:
             flash('ভিডিও কিউ আগে থেকেই খালি ছিল।', 'info')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_played', methods=['POST'])
def clear_played():
    with stream_lock:
        if played_today:
            played_today.clear()
            print("🗑️ 'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।")
            flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
        else:
             flash("'আজকে চালানো হয়েছে' তালিকা আগে থেকেই খালি ছিল।", 'info')
    return redirect(url_for('admin_panel'))

@app.route('/stream/<path:filename>')
def stream(filename):
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    try:
        safe_base = os.path.normpath(stream_abs_path)
        file_abs_path = os.path.normpath(os.path.join(safe_base, filename))

        if not file_abs_path.startswith(safe_base):
            print(f"🚫 নিরাপত্তা লঙ্ঘন প্রচেষ্টা রোধ করা হয়েছে: {filename}")
            abort(403)

        if not os.path.isfile(file_abs_path):
            abort(404)

        response = send_from_directory(safe_base, filename, conditional=True)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    except FileNotFoundError:
         abort(404)
    except Exception as e:
        print(f"❌ স্ট্রিম ফাইল সার্ভ করার সময় ত্রুটি ({filename}): {e}")
        abort(500)

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার (আগের মতোই) ---
def signal_handler(sig, frame):
    if stop_event.is_set():
        print("⏳ ইতিমধ্যে বন্ধ করার প্রক্রিয়া চলছে...")
        return
    print("\n🚦 বন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set()
    print("⏳ FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার জন্য অপেক্ষা করা হচ্ছে...")
    time.sleep(1)
    if current_ffmpeg_process and current_ffmpeg_process.poll() is None:
         print("🚦 সিগন্যাল হ্যান্ডলার থেকে সরাসরি FFmpeg বন্ধ করার চেষ্টা...")
         stop_ffmpeg_stream()

    print("👋 অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    exit(0)

# --- প্রধান চালক (আগের মতোই) ---
if __name__ == '__main__':
    print("*"*50)
    print("🚀 লাইভ স্ট্রিম অ্যাপ্লিকেশন শুরু হচ্ছে...")
    print("   ✨ মোড: ভিডিও কপি, অডিও এনকোড") # মোড উল্লেখ করা হলো
    print(f"⏰ বর্তমান সময়: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📂 ভিডিও ডাউনলোড ডিরেক্টরি: {os.path.abspath(VIDEO_DIR)}")
    print(f"📺 স্ট্রিম আউটপুট ডিরেক্টরি: {os.path.abspath(STREAM_OUTPUT_DIR)}")
    print("*"*50)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    host = '0.0.0.0'
    port = 5000
    print(f"🌍 Flask অ্যাপ http://{host}:{port} এ শোনার জন্য প্রস্তুত...")
    print(f"🔑 অ্যাডমিন প্যানেল অ্যাক্সেস করুন: http://127.0.0.1:{port}/admin")
    print(f"👀 প্লেয়ার দেখুন: http://127.0.0.1:{port}/")
    print("🛑 অ্যাপ্লিকেশন বন্ধ করতে Ctrl+C চাপুন।")

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        print(f"Flask অ্যাপ চালাতে গিয়ে ত্রুটি: {e}")
    finally:
        print("Flask অ্যাপ বন্ধ হয়েছে বা হতে চলেছে।")
        if not stop_event.is_set():
            stop_event.set()
        if manager_thread.is_alive():
            print("ম্যানেজার থ্রেডকে বন্ধ হওয়ার জন্য অপেক্ষা করা হচ্ছে...")
            manager_thread.join(timeout=10)
            if manager_thread.is_alive():
                 print("⚠️ ম্যানেজার থ্রেড নির্দিষ্ট সময়ের মধ্যে বন্ধ হয়নি।")
        print("🧹 রিসোর্স পরিষ্কার করা হচ্ছে...")
        stop_ffmpeg_stream()
        print("👋 প্রধান থ্রেড সমাপ্ত।")
