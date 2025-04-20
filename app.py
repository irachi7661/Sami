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
DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/kw2rpr2vsl7hf9gaddtsg/VID_20250330_041149_786.mp4?rlkey=rb347g41y8r0ekqvu3vea2znp&st=xtb6m8gx&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts" # ডিফল্ট ভিডিওর জন্য একটি নির্দিষ্ট নাম

VIDEO_DIR = "videos" # ডাউনলোড করা ভিডিওগুলো এখানে থাকবে
STREAM_OUTPUT_DIR = "stream_output" # HLS আউটপুট এখানে তৈরি হবে
FFMPEG_SINGLE_INPUT_FILE = "current_input.mp4" # FFmpeg এর ইনপুট হিসেবে ব্যবহারের জন্য ফাইল (অথবা ডাইনামিক্যালি প্লেলিস্ট)
FFMPEG_PLAYLIST_FILE = "playlist.txt" # শুধুমাত্র একটি ভিডিওর পাথ থাকবে এখানে
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
    if url.lower().endswith('.mp4'):
        ext = '.mp4'
    elif url.lower().endswith('.ts'):
        ext = '.ts'
    elif url.lower().endswith('.mkv'):
        ext = '.mkv'
    elif url.lower().endswith('.avi'):
        ext = '.avi'
    else:
        ext = '.mp4' # ডিফল্ট এক্সটেনশন
    return f"video_{hashed_url}{ext}"

def download_video(url, output_filename):
    """একটি ভিডিও ডাউনলোড করে নির্দিষ্ট ফাইলে সংরক্ষণ করে"""
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        # যদি ফাইল আগে থেকেই থাকে, আবার ডাউনলোড না করা (কিন্তু ডিফল্ট ভিডিও সবসময় চেক করা দরকার)
        if os.path.exists(filepath) and output_filename != DEFAULT_VIDEO_FILENAME:
            print(f"'{output_filename}' আগে থেকেই ডাউনলোড করা আছে।")
            return filepath

        print(f"ডাউনলোড শুরু হচ্ছে: {url} -> {filepath}")
        response = requests.get(url, stream=True, timeout=30) # ৩০ সেকেন্ড টাইমআউট
        response.raise_for_status()  # HTTP ত্রুটি থাকলে Exception তুলবে

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if stop_event.is_set(): # ডাউনলোড চলাকালীন বন্ধ করার সিগন্যাল চেক
                    print("ডাউনলোড বাতিল করা হয়েছে।")
                    # আংশিক ফাইল মুছে ফেলা যেতে পারে
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    return None
                f.write(chunk)

        print(f"সফলভাবে ডাউনলোড হয়েছে: {output_filename}")
        return filepath

    except requests.exceptions.Timeout:
        print(f"ভিডিও ডাউনলোড টাইমআউট ({url})")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে আংশিক ফাইল মুছুন
        return None
    except requests.exceptions.RequestException as e:
        print(f"ভিডিও ডাউনলোড ব্যর্থ ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে আংশিক ফাইল মুছুন
        return None
    except Exception as e:
        print(f"ভিডিও সংরক্ষণ বা অন্য কোনো ত্রুটি ({url}): {e}")
        if os.path.exists(filepath): os.remove(filepath) # ব্যর্থ হলে আংশিক ফাইল মুছুন
        return None

def create_single_video_playlist(video_path):
    """শুধুমাত্র একটি ভিডিওর পাথ দিয়ে playlist.txt তৈরি করে"""
    abs_video_path = os.path.abspath(video_path)
    with open(FFMPEG_PLAYLIST_FILE, "w") as f:
        f.write(f"file '{abs_video_path}'\n")
    print(f"'{FFMPEG_PLAYLIST_FILE}' তৈরি করা হয়েছে: {abs_video_path}")

def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেস বন্ধ করে"""
    global current_ffmpeg_process
    with stream_lock: # প্রসেস ভেরিয়েবল অ্যাক্সেস করার সময় লক করুন
        if current_ffmpeg_process:
            print(f"FFmpeg প্রসেস বন্ধ করা হচ্ছে (PID: {current_ffmpeg_process.pid})...")
            if current_ffmpeg_process.poll() is None: # যদি প্রসেস এখনও চালু থাকে
                try:
                    # প্রথমে SIGTERM পাঠিয়ে কিছুটা সময় দেওয়া
                    current_ffmpeg_process.terminate()
                    current_ffmpeg_process.wait(timeout=5) # ৫ সেকেন্ড অপেক্ষা
                    print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (terminate)।")
                except subprocess.TimeoutExpired:
                    print("FFmpeg প্রসেস terminate হয়নি, SIGKILL পাঠানো হচ্ছে...")
                    # যদি terminate কাজ না করে, তবে জোর করে বন্ধ করা (SIGKILL)
                    current_ffmpeg_process.kill()
                    current_ffmpeg_process.wait() # kill করার পর wait করতে হবে
                    print("FFmpeg প্রসেস সফলভাবে বন্ধ হয়েছে (kill)।")
                except Exception as e:
                    print(f"FFmpeg বন্ধ করার সময় ত্রুটি: {e}")
            else:
                print("FFmpeg প্রসেস আগে থেকেই বন্ধ ছিল।")
            current_ffmpeg_process = None # প্রসেস ভেরিয়েবল রিসেট করুন

def start_ffmpeg_stream(video_path, loop=False):
    """একটি নির্দিষ্ট ভিডিও ফাইল থেকে FFmpeg স্ট্রিম শুরু করে"""
    global current_ffmpeg_process

    stop_ffmpeg_stream() # নতুন স্ট্রিম শুরু করার আগে পুরনোটা বন্ধ করুন (যদি থাকে)

    # create_single_video_playlist(video_path) # প্লেলিস্ট তৈরি করুন

    ffmpeg_command_base = [
        'ffmpeg',
        '-re', # রিয়েল টাইমে ইনপুট পড়ুন
    ]

    # যদি লুপিং দরকার হয় (শুধুমাত্র ডিফল্ট ভিডিওর জন্য)
    if loop:
        # -stream_loop -1 সরাসরি ইনপুটের আগে দিতে হবে
        ffmpeg_command_base.extend(['-stream_loop', '-1'])

    # ইনপুট ফাইল যোগ করুন
    ffmpeg_command_base.extend(['-i', os.path.abspath(video_path)])

    # বাকি অপশনগুলো যোগ করুন
    ffmpeg_command_options = [
        '-c:v', 'libx264',
        '-preset', 'veryfast',
        '-b:v', '1500k', # ভিডিও বিটরেট
        '-maxrate', '1500k',
        '-bufsize', '3000k',
        '-g', '60', # কীফ্রেম ব্যবধান (২ সেকেন্ড @ ৩০fps)
        '-vf', 'scale=640:360', # রেজোলিউশন সেট করা (যেমন: 640x360)
        '-c:a', 'aac',
        '-b:a', '128k', # অডিও বিটরেট
        '-f', 'hls',
        '-hls_time', '4', # সেগমেন্ট সময় (সেকেন্ড)
        '-hls_list_size', '5', # প্লেলিস্টে সেগমেন্ট সংখ্যা
        '-hls_flags', 'delete_segments+omit_endlist', # পুরনো সেগমেন্ট মুছবে এবং লাইভ দেখাবে
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%03d.ts'),
        HLS_OUTPUT_FILE
    ]

    ffmpeg_command = ffmpeg_command_base + ffmpeg_command_options

    print("FFmpeg কমান্ড:", " ".join(ffmpeg_command))
    try:
        # DEVNULL ব্যবহার করে stdout হাইড করা, stderr পাইপ করা যাতে আমরা দেখতে পারি
        process = subprocess.Popen(ffmpeg_command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # stderr পড়ার জন্য একটি ছোট থ্রেড (অপশনাল, ডিবাগিংয়ের জন্য)
        def log_stderr(proc):
            if proc.stderr:
                for line in iter(proc.stderr.readline, b''):
                    if stop_event.is_set(): break # যদি বন্ধ করার সিগন্যাল আসে
                    print(f"FFmpeg stderr: {line.decode(errors='ignore').strip()}")
            print("FFmpeg stderr রিডিং শেষ।")

        stderr_thread = threading.Thread(target=log_stderr, args=(process,), daemon=True)
        stderr_thread.start()

        print(f"FFmpeg প্রসেস শুরু হয়েছে (PID: {process.pid}) ভিডিও: {video_path}, লুপ: {loop}")
        with stream_lock:
            current_ffmpeg_process = process # গ্লোবাল ভেরিয়েবলে প্রসেস সংরক্ষণ করুন
        return process # প্রসেস অবজেক্ট রিটার্ন করুন

    except Exception as e:
        print(f"FFmpeg শুরু করতে ব্যর্থ ({video_path}): {e}")
        with stream_lock:
            current_ffmpeg_process = None
        return None

# --- ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার ---
def stream_manager():
    """
    ব্যাকগ্রাউন্ডে চলে, ভিডিও কিউ এবং FFmpeg প্রসেস ম্যানেজ করে।
    কিউ থেকে ভিডিও নিয়ে প্লে করে, কিউ খালি থাকলে ডিফল্ট ভিডিও লুপ করে।
    """
    global currently_playing_url, default_video_path

    # প্রথমে ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা করুন
    print("ডিফল্ট ভিডিও ডাউনলোড করার চেষ্টা চলছে...")
    default_video_path = download_video(DEFAULT_VIDEO_URL, DEFAULT_VIDEO_FILENAME)
    if not default_video_path:
        print("🚨 সতর্কতা: ডিফল্ট ভিডিও ডাউনলোড করা যায়নি! ডিফল্ট প্লেব্যাক কাজ করবে না।")

    while not stop_event.is_set():
        next_video_path = None
        play_url = None
        loop_default = False

        with stream_lock: # কিউ এবং FFmpeg স্থিতি পরীক্ষা করার জন্য লক
            if current_ffmpeg_process and current_ffmpeg_process.poll() is None:
                # FFmpeg এখনও চলছে, কিছু করার দরকার নেই
                time.sleep(1) # অল্প সময় অপেক্ষা
                continue # লুপের পরবর্তী ইটারেশনে যান

            # FFmpeg চলছে না বা শেষ হয়ে গেছে
            if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                 print(f"FFmpeg (PID: {current_ffmpeg_process.pid}) শেষ হয়েছে। পরবর্তী ভিডিও খোঁজা হচ্ছে...")
                 current_ffmpeg_process = None # প্রসেস শেষ হয়েছে, রিসেট করুন
                 # আগের ভিডিওটি played_today সেটে যোগ করা যেতে পারে যদি দরকার হয়
                 if currently_playing_url and currently_playing_url != DEFAULT_VIDEO_URL:
                     played_today.add(currently_playing_url)
                 currently_playing_url = None


            # এখন পরবর্তী ভিডিও নির্ধারণ করুন
            if video_queue:
                play_url = video_queue.popleft() # কিউ থেকে প্রথম URL টি নিন
                print(f"কিউ থেকে নেওয়া হয়েছে: {play_url}")
                # এখানে আপনি চাইলে played_today চেক করতে পারেন, যদিও popleft করাই যথেষ্ট
                # if play_url in played_today:
                #    print(f"'{play_url}' আজকে ইতিমধ্যে প্লে হয়েছে, স্কিপ করা হচ্ছে।")
                #    play_url = None # এটি প্লে করবেনা, লুপ আবার চলবে
                #    continue # এই ইটারেশন স্কিপ করে পরেরবার আবার চেক করবে

                if play_url:
                    filename = get_safe_filename(play_url)
                    next_video_path = download_video(play_url, filename)
                    if not next_video_path:
                        print(f"ডাউনলোড ব্যর্থ: {play_url}. পরবর্তী ভিডিও চেষ্টা করা হবে।")
                        play_url = None # ডাউনলোড ব্যর্থ, এটি প্লে হবে না
                        continue # লুপের শুরুতে যান
            else:
                # কিউ খালি, ডিফল্ট ভিডিও প্লে করুন (যদি ডাউনলোড হয়ে থাকে)
                if default_video_path:
                    print("কিউ খালি, ডিফল্ট ভিডিও প্লে করা হবে (লুপ সহ)।")
                    next_video_path = default_video_path
                    play_url = DEFAULT_VIDEO_URL # চিহ্নিত করার জন্য যে ডিফল্ট প্লে হচ্ছে
                    loop_default = True
                else:
                    print("কিউ খালি এবং ডিফল্ট ভিডিও উপলব্ধ নেই। অপেক্ষা করা হচ্ছে...")
                    # কিছু করার নেই, অপেক্ষা করুন
                    time.sleep(5)
                    continue # লুপের শুরুতে যান

        # লক ছেড়ে দেওয়ার পর FFmpeg চালু করুন (যদি কোনো ভিডিও পাওয়া যায়)
        if next_video_path and play_url:
            print(f"FFmpeg চালু করা হচ্ছে: {next_video_path}, লুপ: {loop_default}")
            currently_playing_url = play_url # বর্তমানে প্লে হওয়া URL সেট করুন
            start_ffmpeg_stream(next_video_path, loop=loop_default)
            time.sleep(2) # FFmpeg শুরু হওয়ার জন্য একটু সময় দিন
        elif not video_queue and not default_video_path:
            # যদি কোনো ভিডিও না থাকে (কিউ খালি, ডিফল্ট নেই)
            time.sleep(5) # ৫ সেকেন্ড পর আবার চেক করুন

    print("স্ট্রিম ম্যানেজার থ্রেড বন্ধ হচ্ছে।")
    stop_ffmpeg_stream() # থ্রেড বন্ধ হওয়ার আগে নিশ্চিত করুন FFmpeg বন্ধ হয়েছে

# --- Flask Routes ---
@app.route('/')
def index():
    """ব্যবহারকারীর জন্য প্লেয়ার পেজ রেন্ডার করে"""
    return render_template('index.html')

@app.route('/admin')
def admin_panel():
    """অ্যাডমিন প্যানেল দেখায়"""
    # বর্তমান কিউয়ের একটি কপি পাঠাতে হবে যাতে রেস কন্ডিশন এড়ানো যায়
    with stream_lock:
        queue_snapshot = list(video_queue)
        current_status = currently_playing_url if current_ffmpeg_process and current_ffmpeg_process.poll() is None else "কোনো ভিডিও চলছে না / ডিফল্ট (যদি থাকে)"
        if current_status == DEFAULT_VIDEO_URL:
             current_status = "ডিফল্ট ভিডিও চলছে (লুপ)"

    return render_template('admin.html', queue=queue_snapshot, current_status=current_status, played=list(played_today))

@app.route('/admin/add', methods=['POST'])
def add_video():
    """কিউতে নতুন ভিডিও URL যোগ করে"""
    url = request.form.get('video_url')
    if url:
        # খুব সাধারণ URL ভ্যালিডেশন (শুধুমাত্র http/https দিয়ে শুরু হচ্ছে কিনা)
        if url.startswith('http://') or url.startswith('https://'):
            with stream_lock:
                video_queue.append(url)
                print(f"কিউতে যোগ করা হয়েছে: {url}")
                flash(f'"{url[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')

                # যদি ডিফল্ট ভিডিও চলছিল, তবে সেটি বন্ধ করে নতুন ভিডিও শুরু করার চেষ্টা করা যেতে পারে
                # অথবা শুধু কিউতে যোগ করলেই হবে, ম্যানেজার থ্রেড হ্যান্ডেল করবে
                # আপাতত, ম্যানেজার থ্রেডকে হ্যান্ডেল করতে দেওয়া যাক
                # if currently_playing_url == DEFAULT_VIDEO_URL and current_ffmpeg_process:
                #    print("ডিফল্ট ভিডিও চলছিল, নতুন ভিডিও আসায় বন্ধ করা হচ্ছে...")
                #    stop_ffmpeg_stream() # এটি করলে ম্যানেজার থ্রেড পরের ইটারেশনে নতুন ভিডিও ধরবে

            # অ্যাডমিন প্যানেলে রিডাইরেক্ট করুন
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
        print("ভিডিও কিউ খালি করা হয়েছে।")
        flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_played', methods=['POST'])
def clear_played():
    """'আজকে চালানো হয়েছে' তালিকা খালি করে"""
    with stream_lock:
        played_today.clear()
        print("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।")
        flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
    return redirect(url_for('admin_panel'))


@app.route('/stream/<path:filename>')
def stream(filename):
    """HLS ফাইল (.m3u8, .ts) সার্ভ করে"""
    # Security: Ensure the requested path is within the intended directory
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    file_abs_path = os.path.abspath(os.path.join(stream_abs_path, filename))

    # পাথ ট্র্যাভার্সাল অ্যাটাক রোধ করা
    if not file_abs_path.startswith(stream_abs_path):
        print(f"নিরাপত্তা লঙ্ঘন প্রচেষ্টা: {filename}")
        abort(404)

    # Ensure file exists before sending
    if not os.path.exists(file_abs_path):
        print(f"ফাইল পাওয়া যায়নি: {file_abs_path}")
        abort(404)

    # Cache Control Headers (optional but good for live streams)
    response = send_from_directory(stream_abs_path, filename)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# --- অ্যাপ্লিকেশন বন্ধ করার হ্যান্ডলার ---
def signal_handler(sig, frame):
    print("\nবন্ধ করার সিগন্যাল পাওয়া গেছে (Ctrl+C)...")
    stop_event.set() # সব থ্রেডকে বন্ধ করার জন্য ইভেন্ট সেট করুন
    print("FFmpeg এবং ব্যাকগ্রাউন্ড থ্রেড বন্ধ করার চেষ্টা চলছে...")
    # stop_ffmpeg_stream() # ম্যানেজার থ্রেড বন্ধ হওয়ার সময় এটি কল করবে
    # এখানে কিছু সময় অপেক্ষা করা ভালো যাতে থ্রেড বন্ধ হতে পারে
    time.sleep(1)
    print("অ্যাপ্লিকেশন বন্ধ হচ্ছে।")
    # প্রস্থান করার একটি উপায়, যদিও এটি সবসময় সেরা নয় ফ্লাস্কের জন্য
    os._exit(0) # ফোর্স এক্সিট, কারণ ফ্লাস্ক হয়তো অপেক্ষা করতে পারে

# --- প্রধান চালক ---
if __name__ == '__main__':
    print("অ্যাপ্লিকেশন শুরু হচ্ছে...")
    # সিগন্যাল হ্যান্ডলার সেট করুন (Ctrl+C)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # ব্যাকগ্রাউন্ড স্ট্রিম ম্যানেজার থ্রেড শুরু করুন
    manager_thread = threading.Thread(target=stream_manager, daemon=True)
    manager_thread.start()

    # Flask অ্যাপ চালু করুন
    print(f"Flask অ্যাপ চালু হচ্ছে http://0.0.0.0:5000 এ...")
    print(f"অ্যাডমিন প্যানেল: http://127.0.0.1:5000/admin (অথবা আপনার সার্ভার আইপি)")
    # use_reloader=False দেওয়া জরুরি যখন ব্যাকগ্রাউন্ড থ্রেড ব্যবহার করছেন,
    # নাহলে ফ্লাস্ক দুটি প্রসেস তৈরি করতে পারে এবং আপনার থ্রেড দুইবার চলতে পারে।
    app.run(host='0.0.0.0', port=5000, threaded=True, use_reloader=False)

    # এই অংশটি সাধারণত পৌঁছাবে না যদি না run() কোনো কারণে রিটার্ন করে
    print("Flask অ্যাপ বন্ধ হয়েছে।")
    stop_event.set() # নিশ্চিত করুন ইভেন্ট সেট করা হয়েছে
    manager_thread.join(timeout=5) # ম্যানেজার থ্রেড শেষ হওয়ার জন্য অপেক্ষা করুন
    stop_ffmpeg_stream() # ফাইনালি নিশ্চিত করুন FFmpeg বন্ধ
    print("প্রধান থ্রেড শেষ হয়েছে।")
