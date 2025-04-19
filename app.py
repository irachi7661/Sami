import os
import subprocess
import threading
import time
import requests
from flask import Flask, render_template, send_from_directory, Response
import logging
import signal # সিগন্যাল হ্যান্ডেল করার জন্য

# --- কনফিগারেশন ---
# আপনার ৫টি ভিডিওর আসল লিঙ্ক এখানে যোগ করুন
VIDEO_LINKS = [
    "https://video-lga3-2.xx.fbcdn.net/o1/v/t2/f2/m69/AQOAZ1LES6fFIExDpc9KWOa_GgAwLZDm6dX_n4bIjVupvTOkQj5QXSQzO8iYro0Ry5S5twRs2VTmi9lBydwA0foP.mp4?strext=1&_nc_cat=107&_nc_sid=8bf8fe&_nc_ht=video-lga3-2.xx.fbcdn.net&_nc_ohc=kkIdpW8g928Q7kNvwEV-xH0&efg=eyJ2ZW5jb2RlX3RhZyI6Inhwdl9wcm9ncmVzc2l2ZS5GQUNFQk9PSy4uQzIuNTQwLnN2ZV9zZCIsInhwdl9hc3NldF9pZCI6MTE4NzMzMzE4OTYwNDA1NSwidmlfdXNlY2FzZV9pZCI6MTAxMjMsImR1cmF0aW9uX3MiOjE5MCwidXJsZ2VuX3NvdXJjZSI6Ind3dyJ9&ccb=17-1&_nc_zt=28&oh=00_AfHq9fGiXSTZAi7aawZcHGJGblvnZh0YMwsFiNmvBUMXkQ&oe=68099D17&dl=1",
    "https://video-lga3-2.xx.fbcdn.net/o1/v/t2/f2/m69/AQOAZ1LES6fFIExDpc9KWOa_GgAwLZDm6dX_n4bIjVupvTOkQj5QXSQzO8iYro0Ry5S5twRs2VTmi9lBydwA0foP.mp4?strext=1&_nc_cat=107&_nc_sid=8bf8fe&_nc_ht=video-lga3-2.xx.fbcdn.net&_nc_ohc=kkIdpW8g928Q7kNvwEV-xH0&efg=eyJ2ZW5jb2RlX3RhZyI6Inhwdl9wcm9ncmVzc2l2ZS5GQUNFQk9PSy4uQzIuNTQwLnN2ZV9zZCIsInhwdl9hc3NldF9pZCI6MTE4NzMzMzE4OTYwNDA1NSwidmlfdXNlY2FzZV9pZCI6MTAxMjMsImR1cmF0aW9uX3MiOjE5MCwidXJsZ2VuX3NvdXJjZSI6Ind3dyJ9&ccb=17-1&_nc_zt=28&oh=00_AfHq9fGiXSTZAi7aawZcHGJGblvnZh0YMwsFiNmvBUMXkQ&oe=68099D17&dl=1",
    "https://video-lga3-2.xx.fbcdn.net/o1/v/t2/f2/m69/AQOAZ1LES6fFIExDpc9KWOa_GgAwLZDm6dX_n4bIjVupvTOkQj5QXSQzO8iYro0Ry5S5twRs2VTmi9lBydwA0foP.mp4?strext=1&_nc_cat=107&_nc_sid=8bf8fe&_nc_ht=video-lga3-2.xx.fbcdn.net&_nc_ohc=kkIdpW8g928Q7kNvwEV-xH0&efg=eyJ2ZW5jb2RlX3RhZyI6Inhwdl9wcm9ncmVzc2l2ZS5GQUNFQk9PSy4uQzIuNTQwLnN2ZV9zZCIsInhwdl9hc3NldF9pZCI6MTE4NzMzMzE4OTYwNDA1NSwidmlfdXNlY2FzZV9pZCI6MTAxMjMsImR1cmF0aW9uX3MiOjE5MCwidXJsZ2VuX3NvdXJjZSI6Ind3dyJ9&ccb=17-1&_nc_zt=28&oh=00_AfHq9fGiXSTZAi7aawZcHGJGblvnZh0YMwsFiNmvBUMXkQ&oe=68099D17&dl=1",
    "https://video-lga3-2.xx.fbcdn.net/o1/v/t2/f2/m69/AQOAZ1LES6fFIExDpc9KWOa_GgAwLZDm6dX_n4bIjVupvTOkQj5QXSQzO8iYro0Ry5S5twRs2VTmi9lBydwA0foP.mp4?strext=1&_nc_cat=107&_nc_sid=8bf8fe&_nc_ht=video-lga3-2.xx.fbcdn.net&_nc_ohc=kkIdpW8g928Q7kNvwEV-xH0&efg=eyJ2ZW5jb2RlX3RhZyI6Inhwdl9wcm9ncmVzc2l2ZS5GQUNFQk9PSy4uQzIuNTQwLnN2ZV9zZCIsInhwdl9hc3NldF9pZCI6MTE4NzMzMzE4OTYwNDA1NSwidmlfdXNlY2FzZV9pZCI6MTAxMjMsImR1cmF0aW9uX3MiOjE5MCwidXJsZ2VuX3NvdXJjZSI6Ind3dyJ9&ccb=17-1&_nc_zt=28&oh=00_AfHq9fGiXSTZAi7aawZcHGJGblvnZh0YMwsFiNmvBUMXkQ&oe=68099D17&dl=1",
    "https://video-lga3-2.xx.fbcdn.net/o1/v/t2/f2/m69/AQOAZ1LES6fFIExDpc9KWOa_GgAwLZDm6dX_n4bIjVupvTOkQj5QXSQzO8iYro0Ry5S5twRs2VTmi9lBydwA0foP.mp4?strext=1&_nc_cat=107&_nc_sid=8bf8fe&_nc_ht=video-lga3-2.xx.fbcdn.net&_nc_ohc=kkIdpW8g928Q7kNvwEV-xH0&efg=eyJ2ZW5jb2RlX3RhZyI6Inhwdl9wcm9ncmVzc2l2ZS5GQUNFQk9PSy4uQzIuNTQwLnN2ZV9zZCIsInhwdl9hc3NldF9pZCI6MTE4NzMzMzE4OTYwNDA1NSwidmlfdXNlY2FzZV9pZCI6MTAxMjMsImR1cmF0aW9uX3MiOjE5MCwidXJsZ2VuX3NvdXJjZSI6Ind3dyJ9&ccb=17-1&_nc_zt=28&oh=00_AfHq9fGiXSTZAi7aawZcHGJGblvnZh0YMwsFiNmvBUMXkQ&oe=68099D17&dl=1",
    # "আপনার ভিডিও লিঙ্ক ১",
    # "আপনার ভিডিও লিঙ্ক ২",
    # "আপনার ভিডিও লিঙ্ক ৩",
    # "আপনার ভিডিও লিঙ্ক ৪",
    # "আপনার ভিডিও লিঙ্ক ৫",
]
VIDEO_DIR = "videos"
HLS_DIR = os.path.join("static", "hls")
PLAYLIST_FILE = "playlist.txt" # FFmpeg কনক্যাটেনেশনের জন্য প্লেলিস্ট

# ডিরেক্টরিগুলো তৈরি করুন যদি না থাকে
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(HLS_DIR, exist_ok=True)

# লগিং সেটআপ
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- গ্লোবাল ভেরিয়েবল ---
ffmpeg_process = None
available_videos = [] # ডাউনলোড হওয়া ভিডিও ফাইলের পাথ
lock = threading.Lock() # গ্লোবাল ভেরিয়েবল অ্যাক্সেস নিয়ন্ত্রণের জন্য
stop_event = threading.Event() # থ্রেড বন্ধ করার জন্য সিগন্যাল

# --- ফ্লাস্ক অ্যাপ ---
app = Flask(__name__)

@app.route('/')
def index():
    """প্রধান HTML পেজ পরিবেশন করে।"""
    return render_template('index.html')

# স্ট্যাটিক ফাইল (HLS সেগমেন্ট) পরিবেশনের জন্য ফ্লাস্ক ডিফল্ট রুট ব্যবহার করবে
# /static/hls/...

# --- হেল্পার ফাংশন ---
def download_video(url, index):
    """একটি ভিডিও ডাউনলোড করে।"""
    filename = os.path.join(VIDEO_DIR, f"video_{index}.mp4")
    filepath = os.path.abspath(filename) # সম্পূর্ণ পাথ নিন

    # যদি ফাইল আগে থেকেই থাকে, ডাউনলোড করার দরকার নেই
    if os.path.exists(filepath):
        logging.info(f"ভিডিও {index+1} ({filepath}) আগে থেকেই ডাউনলোড করা আছে।")
        return filepath

    try:
        logging.info(f"ভিডিও {index+1} ডাউনলোড শুরু হচ্ছে: {url}")
        response = requests.get(url, stream=True, timeout=300) # টাইমআউট যোগ করা ভালো
        response.raise_for_status()  # HTTP एरর চেক করুন
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logging.info(f"ভিডিও {index+1} ডাউনলোড সম্পন্ন: {filepath}")
        return filepath
    except requests.exceptions.RequestException as e:
        logging.error(f"ভিডিও {index+1} ডাউনলোড ব্যর্থ ({url}): {e}")
        # আংশিক ডাউনলোড হওয়া ফাইল মুছে ফেলুন (যদি থাকে)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError as rm_err:
                logging.error(f"আংশিক ফাইল মুছতে ব্যর্থ ({filepath}): {rm_err}")
        return None
    except Exception as e:
        logging.error(f"ভিডিও {index+1} ডাউনলোড করার সময় অপ্রত্যাশিত ত্রুটি: {e}")
        if os.path.exists(filepath):
             try:
                os.remove(filepath)
             except OSError as rm_err:
                logging.error(f"ত্রুটির পর ফাইল মুছতে ব্যর্থ ({filepath}): {rm_err}")
        return None

def manage_downloads_and_stream():
    """ভিডিও ডাউনলোড এবং FFmpeg স্ট্রিম ম্যানেজ করে।"""
    global available_videos, ffmpeg_process

    processed_indices = set() # কোন ভিডিওগুলো ইতিমধ্যে প্রসেস করা হয়েছে

    while not stop_event.is_set():
        new_video_added = False
        current_video_paths = []

        for i, url in enumerate(VIDEO_LINKS):
            if i in processed_indices:
                 # যদি আগে প্রসেস করা হয়, পাথ যোগ করুন
                 potential_path = os.path.abspath(os.path.join(VIDEO_DIR, f"video_{i}.mp4"))
                 if potential_path in available_videos:
                     current_video_paths.append(potential_path)
                 continue # পরের লিঙ্কে যান

            video_path = download_video(url, i)

            if video_path:
                with lock:
                    if video_path not in available_videos:
                        available_videos.append(video_path)
                        available_videos.sort() # ফাইল নামের ক্রমানুসারে সাজান
                        new_video_added = True
                        logging.info(f"প্লেলিস্টে যুক্ত হয়েছে: {video_path} (মোট: {len(available_videos)})")
                processed_indices.add(i) # এই ইনডেক্স প্রসেস করা হয়েছে
                current_video_paths.append(video_path)
            else:
                # ডাউনলোড ব্যর্থ হলে কিছুক্ষণ অপেক্ষা করে আবার চেষ্টা করতে পারে
                logging.warning(f"ভিডিও {i+1} ডাউনলোড করা যায়নি, পরে আবার চেষ্টা করা হবে।")
                # এখানে ব্রেক না করে লুপ চলতে থাকবে, পরেরবার আবার চেষ্টা করবে

        # যদি নতুন ভিডিও যোগ হয় বা 처음 স্ট্রিম শুরু করার সময়
        if new_video_added or (not ffmpeg_process and available_videos):
             with lock:
                 # available_videos থেকে বর্তমান পাথগুলো নিন
                 paths_to_stream = list(available_videos)

             if paths_to_stream:
                 logging.info(f"FFmpeg স্ট্রিম আপডেট করা হচ্ছে {len(paths_to_stream)} টি ভিডিও দিয়ে।")
                 stop_ffmpeg_stream() # পুরানো স্ট্রিম বন্ধ করুন (যদি থাকে)
                 start_ffmpeg_stream(paths_to_stream) # নতুন স্ট্রিম শুরু করুন
             else:
                 logging.info("এখনও কোনো ভিডিও স্ট্রিমিংয়ের জন্য উপলব্ধ নেই।")

        # যদি সব ভিডিও প্রসেস করা হয়ে যায়, তাহলে লুপ থেকে বের হয়ে যেতে পারে বা শুধু অপেক্ষা করতে পারে
        if len(processed_indices) == len(VIDEO_LINKS):
            logging.info("সকল ভিডিও লিঙ্ক প্রসেস করা হয়েছে। ডাউনলোড ম্যানেজার এখন নিষ্ক্রিয় থাকবে।")
            break # লুপ থেকে বের হয়ে যান

        # যদি কোনো নতুন ভিডিও যোগ না হয় এবং সবগুলো ডাউনলোড না হয়ে থাকে, কিছুক্ষণ অপেক্ষা করুন
        if not new_video_added and len(processed_indices) < len(VIDEO_LINKS):
             time.sleep(30) # ৩০ সেকেন্ড পর আবার চেক করুন

    logging.info("ডাউনলোড এবং স্ট্রীম ম্যানেজমেন্ট থ্রেড শেষ হচ্ছে।")


def start_ffmpeg_stream(video_files):
    """প্রদত্ত ভিডিও ফাইলগুলো ব্যবহার করে FFmpeg স্ট্রিম শুরু করে।"""
    global ffmpeg_process
    if not video_files:
        logging.warning("স্ট্রিমিংয়ের জন্য কোনো ভিডিও ফাইল নেই।")
        return

    # 1. FFmpeg concat demuxer এর জন্য প্লেলিস্ট ফাইল তৈরি করুন
    playlist_content = ""
    for video_path in video_files:
        # ফাইলের পাথগুলো সঠিকভাবে ফরম্যাট করুন (স্পেস বা বিশেষ অক্ষর থাকলে)
        # 'safe 0' অপশন ব্যবহার করলে এটি সাধারণত প্রয়োজন হয় না, তবে ভালো অভ্যাস
        escaped_path = video_path.replace("'", "'\\''") # সিঙ্গেল কোট এস্কেপ করুন
        playlist_content += f"file '{escaped_path}'\n"

    try:
        with open(PLAYLIST_FILE, "w", encoding='utf-8') as f:
            f.write(playlist_content)
        logging.info(f"{PLAYLIST_FILE} তৈরি হয়েছে {len(video_files)} টি ভিডিওর জন্য।")
    except IOError as e:
        logging.error(f"প্লেলিস্ট ফাইল ({PLAYLIST_FILE}) লিখতে ব্যর্থ: {e}")
        return

    # HLS ডিরেক্টরি পরিষ্কার করুন (আগের সেগমেন্ট মুছে ফেলুন)
    logging.info(f"HLS ডিরেক্টরি পরিষ্কার করা হচ্ছে: {HLS_DIR}")
    for filename in os.listdir(HLS_DIR):
        if filename.endswith(('.m3u8', '.ts')):
            try:
                os.remove(os.path.join(HLS_DIR, filename))
            except OSError as e:
                logging.error(f"পুরানো HLS ফাইল মুছতে ত্রুটি ({filename}): {e}")

    # 2. FFmpeg কমান্ড তৈরি করুন
    ffmpeg_cmd = [
        'ffmpeg',
        '-re',                      # নেটিভ ফ্রেম রেটে ইনপুট পড়ুন (লাইভের জন্য গুরুত্বপূর্ণ)
        '-f', 'concat',             # concat demuxer ব্যবহার করুন
        '-safe', '0',               # প্লেলিস্টে অনিরাপদ পাথ ব্যবহারের অনুমতি দিন
        '-stream_loop', '-1',       # প্লেলিস্টটি অসীমভাবে লুপ করুন
        '-i', PLAYLIST_FILE,        # ইনপুট প্লেলিস্ট ফাইল
        '-map', '0',                # ইনপুট থেকে সব স্ট্রিম ম্যাপ করুন (ভিডিও, অডিও)
        '-c', 'copy',               # কোডেক কপি করুন (দ্রুত, কম সিপিইউ ব্যবহার; যদি ভিডিওগুলো সামঞ্জস্যপূর্ণ হয়)
                                    # অসামঞ্জস্যপূর্ণ হলে রি-এনকোড করুন: '-c:v libx264 -preset veryfast -crf 23 -c:a aac -b:a 128k'
        '-f', 'hls',                # আউটপুট ফরম্যাট HLS
        '-hls_time', '4',           # প্রতিটি সেগমেন্টের সময়কাল (সেকেন্ড)
        '-hls_list_size', '5',      # প্লেলিস্টে সেগমেন্টের সংখ্যা
        '-hls_flags', 'delete_segments+append_list', # পুরানো সেগমেন্ট মুছুন, নতুন যোগ করুন
        '-hls_segment_filename', os.path.join(HLS_DIR, 'segment%03d.ts'), # সেগমেন্ট ফাইলের নাম প্যাটার্ন
        os.path.join(HLS_DIR, 'live.m3u8') # মাস্টার প্লেলিস্ট ফাইলের নাম
    ]

    logging.info(f"FFmpeg চালু হচ্ছে: {' '.join(ffmpeg_cmd)}")
    try:
        # stderr লগ করার জন্য পাইপ ব্যবহার করুন
        ffmpeg_process = subprocess.Popen(
            ffmpeg_cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL, # stdout উপেক্ষা করুন
            text=True, # stderr টেক্সট হিসাবে ডিকোড করুন
            bufsize=1, # লাইন বাফার্ড
            universal_newlines=True
        )
        # FFmpeg এর আউটপুট লগ করার জন্য একটি ডেমন থ্রেড চালু করুন
        stderr_thread = threading.Thread(target=log_ffmpeg_output, args=(ffmpeg_process.stderr,), daemon=True)
        stderr_thread.start()
        logging.info(f"FFmpeg প্রসেস শুরু হয়েছে (PID: {ffmpeg_process.pid})। {len(video_files)} টি ভিডিও লুপে স্ট্রিমিং হচ্ছে।")

    except FileNotFoundError:
        logging.error("ffmpeg কমান্ড পাওয়া যায়নি। FFmpeg ইনস্টল করা আছে এবং PATH এ আছে কিনা নিশ্চিত করুন।")
        ffmpeg_process = None
    except Exception as e:
        logging.error(f"FFmpeg চালু করতে ব্যর্থ: {e}")
        ffmpeg_process = None

def log_ffmpeg_output(pipe):
    """FFmpeg এর stderr থেকে আউটপুট পড়ে এবং লগ করে।"""
    try:
        for line in iter(pipe.readline, ''):
            # লগিং লেভেল ডিবাগ রাখুন যাতে বেশি ভার্বোস না হয়
            logging.debug(f"FFmpeg: {line.strip()}")
        pipe.close()
    except Exception as e:
        logging.error(f"FFmpeg আউটপুট পড়তে ত্রুটি: {e}")
    logging.info("FFmpeg আউটপুট মনিটরিং থ্রেড শেষ হয়েছে।")


def stop_ffmpeg_stream():
    """চলমান FFmpeg প্রসেসটি সাবধানে বন্ধ করে।"""
    global ffmpeg_process
    if ffmpeg_process and ffmpeg_process.poll() is None:
        logging.info(f"FFmpeg প্রসেস (PID: {ffmpeg_process.pid}) বন্ধ করার চেষ্টা চলছে...")
        try:
            # প্রথমে SIGTERM পাঠান যাতে নিজে থেকে বন্ধ হতে পারে
            ffmpeg_process.terminate()
            # নির্দিষ্ট সময় পর্যন্ত অপেক্ষা করুন
            ffmpeg_process.wait(timeout=10)
            logging.info(f"FFmpeg প্রসেস (PID: {ffmpeg_process.pid}) বন্ধ হয়েছে।")
        except subprocess.TimeoutExpired:
            logging.warning(f"FFmpeg প্রসেস (PID: {ffmpeg_process.pid}) নিজে থেকে বন্ধ হয়নি। SIGKILL পাঠানো হচ্ছে।")
            ffmpeg_process.kill()
            ffmpeg_process.wait() # kill করার পর অপেক্ষা করুন
            logging.info(f"FFmpeg প্রসেস (PID: {ffmpeg_process.pid}) kill করা হয়েছে।")
        except Exception as e:
             logging.error(f"FFmpeg প্রসেস বন্ধ করতে ত্রুটি: {e}")
        ffmpeg_process = None
    elif ffmpeg_process:
         # যদি প্রসেস আগে থেকেই বন্ধ হয়ে গিয়ে থাকে
         logging.info("FFmpeg প্রসেস আগে থেকেই বন্ধ ছিল।")
         ffmpeg_process = None


# --- সিগন্যাল হ্যান্ডলার ---
def signal_handler(signum, frame):
    """অ্যাপ্লিকেশন বন্ধ করার সিগন্যাল হ্যান্ডেল করে।"""
    logging.info("বন্ধ করার সিগন্যাল পাওয়া গেছে। অ্যাপ্লিকেশন বন্ধ করা হচ্ছে...")
    stop_event.set() # ব্যাকগ্রাউন্ড থ্রেডকে বন্ধ হতে বলুন
    stop_ffmpeg_stream() # FFmpeg বন্ধ করুন
    # প্রয়োজনে অন্যান্য রিসোর্স ক্লিনআপ করুন
    # sys.exit(0) # প্রস্থান করুন (Flask এটি নিজে হ্যান্ডেল করতে পারে)

signal.signal(signal.SIGINT, signal_handler)  # Ctrl+C হ্যান্ডেল করুন
signal.signal(signal.SIGTERM, signal_handler) # ডকার স্টপ হ্যান্ডেল করুন

# --- প্রধান এক্সিকিউশন ---
if __name__ == '__main__':
    logging.info("লাইভ স্ট্রিম অ্যাপ্লিকেশন চালু হচ্ছে...")

    # ভিডিও ডাউনলোড এবং স্ট্রিমিং ম্যানেজ করার জন্য ব্যাকগ্রাউন্ড থ্রেড চালু করুন
    stream_manager_thread = threading.Thread(target=manage_downloads_and_stream, daemon=True)
    stream_manager_thread.start()

    # ফ্লাস্ক ডেভেলপমেন্ট সার্ভার চালু করুন
    # প্রোডাকশনের জন্য Gunicorn বা uWSGI ব্যবহার করুন
    logging.info("ফ্লাস্ক সার্ভার http://0.0.0.0:80 তে চালু হচ্ছে")
    # debug=False এবং use_reloader=False থ্রেড এবং সাবপ্রসেসের সাথে ব্যবহারের জন্য গুরুত্বপূর্ণ
    app.run(host='0.0.0.0', port=80, debug=False, use_reloader=False)

    # অ্যাপ বন্ধ হওয়ার সময় (যদিও ডেমন থ্রেড এবং app.run() এটিকে জটিল করে তোলে)
    logging.info("ফ্লাস্ক সার্ভার বন্ধ হয়েছে। রিসোর্স ক্লিনআপ করা হচ্ছে...")
    stop_event.set() # নিশ্চিত করুন যে থ্রেড বন্ধ হওয়ার সিগন্যাল পেয়েছে
    stop_ffmpeg_stream()
    if stream_manager_thread.is_alive():
        stream_manager_thread.join(timeout=5) # থ্রেড শেষ হওয়ার জন্য অপেক্ষা করুন
    logging.info("অ্যাপ্লিকেশন বন্ধ হয়েছে।")
