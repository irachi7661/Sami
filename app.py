import os
import subprocess
import threading
import time
import signal
import requests
import hashlib
from flask import Flask, render_template, send_from_directory, abort, request, redirect, url_for, flash, jsonify
from flask_cors import CORS
from collections import deque
import traceback
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

DEFAULT_VIDEO_URL = "https://www.dropbox.com/scl/fi/2w5ai1fda804zfruoj8yn/assets_staytuned0.ts?rlkey=jixrs4b1v3keu4q6hpebmbw5v&st=b1teebao&raw=1"
DEFAULT_VIDEO_FILENAME = "default_video.ts"

VIDEO_DIR = "videos"
STREAM_OUTPUT_DIR = "stream_output"
HLS_OUTPUT_FILE = os.path.join(STREAM_OUTPUT_DIR, "stream.m3u8")

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

os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)

def ensure_dropbox_raw_param(url):
    try:
        if not url or not (url.startswith('http://') or url.startswith('https://')):
            return url

        parsed_url = urlparse(url)

        if parsed_url.netloc.lower() == 'www.dropbox.com' or parsed_url.netloc.lower() == 'dropbox.com':
            query_params = parse_qs(parsed_url.query)

            if not ('raw' in query_params and query_params['raw'] == ['1']):
                query_params['raw'] = ['1']
                new_query = urlencode(query_params, doseq=True)
                modified_url = urlunparse((
                    parsed_url.scheme,
                    parsed_url.netloc,
                    parsed_url.path,
                    parsed_url.params,
                    new_query,
                    parsed_url.fragment
                ))
                return modified_url
            else:
                 return url
        else:
            return url
    except Exception as e:
        # Error logging removed as requested
        return url


def get_safe_filename(url):
    try:
        parsed_url = urlparse(url)
        path_part = parsed_url.path
        base_name = os.path.basename(path_part)
        _, ext = os.path.splitext(base_name)

        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]

        if not ext or len(ext) > 5:
             ext = '.mp4'

        if ext.lower() not in ['.mp4', '.ts', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.m3u8']:
             ext = '.mp4'

        return f"video_{hashed_url}{ext}"
    except Exception as e:
        # Error logging removed as requested
        hashed_url = hashlib.sha1(url.encode()).hexdigest()[:10]
        return f"video_{hashed_url}.mp4"


def download_video(url, output_filename):
    filepath = os.path.join(VIDEO_DIR, output_filename)
    try:
        if os.path.exists(filepath):
            try:
                if os.path.getsize(filepath) > 0:
                    return filepath
                # else: print removed
            except OSError as e:
                 # print removed
                 pass # Continue to download

        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, stream=True, timeout=60, headers=headers, allow_redirects=True)
        response.raise_for_status()

        content_type = response.headers.get('content-type', '').lower()
        problematic_types = ['text/html', 'application/json']
        is_likely_video = 'video' in content_type or 'mpegurl' in content_type or 'octet-stream' in content_type or not any(ptype in content_type for ptype in problematic_types)

        if not is_likely_video:
             if 'dropbox.com' in url and 'raw=1' not in url:
                 # print removed
                 pass

        with open(filepath, "wb") as f:
            downloaded_size = 0
            for chunk in response.iter_content(chunk_size=8192 * 4):
                if stop_event.is_set():
                    if os.path.exists(filepath): os.remove(filepath)
                    return None
                if chunk:
                    f.write(chunk)
                    downloaded_size += len(chunk)

        if downloaded_size == 0:
             if os.path.exists(filepath): os.remove(filepath)
             return None

        return filepath

    except requests.exceptions.Timeout:
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except requests.exceptions.SSLError as e:
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except requests.exceptions.RequestException as e:
        if os.path.exists(filepath): os.remove(filepath)
        return None
    except Exception as e:
        if os.path.exists(filepath): os.remove(filepath)
        return None

def stop_ffmpeg_stream():
    global current_ffmpeg_process
    with stream_lock:
        process_to_stop = current_ffmpeg_process
        if process_to_stop and process_to_stop.poll() is None:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/F', '/PID', str(process_to_stop.pid)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    process_to_stop.terminate()
                    try:
                        process_to_stop.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process_to_stop.kill()
                        process_to_stop.wait()
            except Exception as e:
                # Error logging removed
                pass
        # elif process_to_stop: print removed
        # else: print removed

        if current_ffmpeg_process == process_to_stop:
             current_ffmpeg_process = None


def start_ffmpeg_stream(video_path, loop=False):
    global current_ffmpeg_process

    abs_video_path = os.path.abspath(video_path)
    if not os.path.exists(abs_video_path):
        return None

    stop_ffmpeg_stream()
    # time.sleep removed

    try:
        if os.path.exists(STREAM_OUTPUT_DIR):
             deleted_count = 0
             for f in os.listdir(STREAM_OUTPUT_DIR):
                 if f.endswith('.ts') or f.endswith('.m3u8'):
                     try:
                         os.remove(os.path.join(STREAM_OUTPUT_DIR, f))
                         deleted_count += 1
                     except OSError as e:
                         # Error logging removed
                         pass
             # if deleted_count > 0: print removed
        else:
             os.makedirs(STREAM_OUTPUT_DIR, exist_ok=True)
    except Exception as e:
        # Error logging removed
        pass


    ffmpeg_command_base = [
        'ffmpeg',
        '-re',
    ]

    if loop:
        ffmpeg_command_base.extend(['-stream_loop', '-1'])

    ffmpeg_command_base.extend(['-i', abs_video_path])

    ffmpeg_command_options = [
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-b:a', '128k',
        '-ac', '2',
        '-ar', '44100',
        '-err_detect', 'ignore_err',
        '-ignore_unknown',
        '-f', 'hls',
        '-hls_time', '4',
        '-hls_list_size', '6',
        '-hls_flags', 'delete_segments+omit_endlist+program_date_time',
        '-hls_segment_filename', os.path.join(STREAM_OUTPUT_DIR, 'segment%05d.ts'),
        HLS_OUTPUT_FILE
    ]

    ffmpeg_command = ffmpeg_command_base + ffmpeg_command_options

    # print command removed

    try:
        # Redirect stderr to DEVNULL to suppress FFmpeg messages
        process = subprocess.Popen(ffmpeg_command, stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

        # stderr logging thread removed

        with stream_lock:
            current_ffmpeg_process = process
        return process

    except FileNotFoundError:
        with stream_lock: current_ffmpeg_process = None
        return None
    except Exception as e:
        with stream_lock: current_ffmpeg_process = None
        return None

def stream_manager():
    global currently_playing_url, default_video_path, current_ffmpeg_process

    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    temp_default_path = download_video(modified_default_url, DEFAULT_VIDEO_FILENAME)
    if temp_default_path:
         default_video_path = temp_default_path
    # else: print removed

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

                modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                if ffmpeg_is_running:
                    if current_url_snapshot != modified_default_url_snapshot and video_queue:
                        next_url_in_queue_raw = video_queue[0]
                        next_url_in_queue_modified = ensure_dropbox_raw_param(next_url_in_queue_raw)

                        if next_url_in_queue_modified != predownload_attempted_for_url:
                            next_filename = get_safe_filename(next_url_in_queue_modified)
                            downloaded_path = download_video(next_url_in_queue_modified, next_filename)
                            # if downloaded_path: print removed
                            # else: print removed
                            predownload_attempted_for_url = next_url_in_queue_modified

                    elif current_url_snapshot == modified_default_url_snapshot and video_queue:
                        stop_default_and_process_queue = True
                        predownload_attempted_for_url = None

                    else:
                        if current_url_snapshot != modified_default_url_snapshot and not video_queue:
                            predownload_attempted_for_url = None
                        pass

                else:
                    predownload_attempted_for_url = None
                    if current_ffmpeg_process and current_ffmpeg_process.poll() is not None:
                        modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                        if current_url_snapshot and current_url_snapshot != modified_default_url_snapshot:
                             played_today.add(current_url_snapshot)
                        current_ffmpeg_process = None
                        currently_playing_url = None

                    if video_queue:
                        raw_url_from_queue = video_queue.popleft()
                        play_url = ensure_dropbox_raw_param(raw_url_from_queue)
                        filename = get_safe_filename(play_url)
                        next_video_path = download_video(play_url, filename)
                        if not next_video_path:
                            play_url = None
                            currently_playing_url = None
                        else:
                             loop_default = False
                             currently_playing_url = play_url

                    elif default_video_path:
                        modified_default_url_snapshot = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
                        # if current_url_snapshot != modified_default_url_snapshot: print removed
                        next_video_path = default_video_path
                        play_url = modified_default_url_snapshot
                        loop_default = True
                        currently_playing_url = play_url

                    else:
                        # if current_url_snapshot: print removed
                        currently_playing_url = None
                        pass

            if stop_default_and_process_queue:
                stop_ffmpeg_stream()
                time.sleep(0.5)
                continue

            if next_video_path and play_url:
                started_process = start_ffmpeg_stream(next_video_path, loop=loop_default)
                if not started_process:
                     with stream_lock:
                         if currently_playing_url == play_url:
                             currently_playing_url = None

            if ffmpeg_is_running:
                 time.sleep(1)
            elif not next_video_path:
                 time.sleep(3)
            else:
                 time.sleep(0.5)

        except Exception as e:
             # traceback.print_exc() removed
             try:
                 stop_ffmpeg_stream()
             except Exception as stop_err:
                  # print removed
                  pass
             with stream_lock:
                 currently_playing_url = None
                 predownload_attempted_for_url = None
             time.sleep(5)

    stop_ffmpeg_stream()


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
        if is_ffmpeg_running and video_queue:
            next_in_queue_raw = video_queue[0]
            status_detail = f" | এরপর কিউতে: {next_in_queue_raw[:50]}..."

    modified_default_url = ensure_dropbox_raw_param(DEFAULT_VIDEO_URL)
    if is_ffmpeg_running:
        mode = "[ভিডিও কপি]" if current_url_snapshot != modified_default_url else "(লুপ)"
        if current_url_snapshot == modified_default_url:
            current_status = f"ডিফল্ট ভিডিও চলছে {mode}{status_detail}"
        elif current_url_snapshot:
            current_status = f"চলছে: {current_url_snapshot[:80]}... {mode}{status_detail}"
        else:
            current_status = "একটি ভিডিও চলছে (URL অজানা)"
    else:
        current_status = "⭕ কোনো ভিডিও চলছে না"
        if video_queue:
             current_status += f" | প্লে করার অপেক্ষায়: {video_queue[0][:50]}..."

    return render_template('admin.html',
                           queue=queue_snapshot,
                           current_status=current_status,
                           played=played_snapshot)

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
                    flash(f'"{url_to_add[:50]}..." সফলভাবে কিউতে যোগ করা হয়েছে।', 'success')
            return redirect(url_for('admin_panel'))
        else:
            flash('অবৈধ URL! অনুগ্রহ করে http:// বা https:// দিয়ে শুরু হওয়া একটি URL দিন।', 'error')
    else:
        flash('URL খালি রাখা যাবে না।', 'error')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_queue', methods=['POST'])
def clear_queue_form():
    with stream_lock:
        if video_queue:
            video_queue.clear()
            flash('ভিডিও কিউ সফলভাবে খালি করা হয়েছে।', 'success')
        else:
             flash('ভিডিও কিউ আগে থেকেই খালি ছিল।', 'info')
    return redirect(url_for('admin_panel'))

@app.route('/admin/clear_played', methods=['POST'])
def clear_played_form():
    with stream_lock:
        if played_today:
            played_today.clear()
            flash("'আজকে চালানো হয়েছে' তালিকা খালি করা হয়েছে।", 'success')
        else:
             flash("'আজকে চালানো হয়েছে' তালিকা আগে থেকেই খালি ছিল।", 'info')
    return redirect(url_for('admin_panel'))


@app.route('/add', methods=['GET'])
def add_video_api():
    url_from_request = request.args.get('link', '').strip()
    if not url_from_request:
        return jsonify({'status': 'error', 'message': 'Missing "link" parameter.'}), 400

    if not (url_from_request.startswith('http://') or url_from_request.startswith('https://')):
        return jsonify({'status': 'error', 'message': 'Invalid URL format. Must start with http:// or https://', 'url': url_from_request}), 400

    url_to_add = ensure_dropbox_raw_param(url_from_request)

    with stream_lock:
        if url_to_add in video_queue:
            return jsonify({'status': 'warning', 'message': 'Video already in queue.', 'url': url_to_add, 'original_url': url_from_request}), 200
        else:
            video_queue.append(url_to_add)
            return jsonify({'status': 'success', 'message': 'Video added to queue.', 'url': url_to_add, 'original_url': url_from_request}), 200

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
                 return jsonify({'status': 'error', 'message': 'Cannot delete the currently playing video.', 'url': url_to_delete, 'original_url': url_from_request}), 403

            try:
                video_queue.remove(url_to_delete)
                return jsonify({'status': 'success', 'message': 'Video removed from queue.', 'url': url_to_delete, 'original_url': url_from_request}), 200
            except ValueError:
                return jsonify({'status': 'error', 'message': 'Video not found in queue.', 'url': url_to_delete, 'original_url': url_from_request}), 404


@app.route('/stream/<path:filename>')
def stream(filename):
    stream_abs_path = os.path.abspath(STREAM_OUTPUT_DIR)
    safe_base = os.path.normpath(stream_abs_path)
    file_abs_path = os.path.normpath(os.path.join(safe_base, filename))

    if not file_abs_path.startswith(safe_base):
        abort(403)

    if not os.path.isfile(file_abs_path):
        abort(404)

    try:
        response = send_from_directory(safe_base, filename, conditional=True)
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        return response

    except FileNotFoundError:
         abort(404)
    except Exception as e:
        # Error logging removed
        abort(500)

def signal_handler(sig, frame):
    if stop_event.is_set():
        return
    stop_event.set()
    stop_ffmpeg_stream()
    exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    manager_thread = threading.Thread(target=stream_manager, name="StreamManager", daemon=True)
    manager_thread.start()

    host = '0.0.0.0'
    port = 5000

    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)
    except Exception as e:
        # traceback.print_exc() removed
        pass # Suppress Flask run error print
    finally:
        if not stop_event.is_set():
            stop_event.set()

        if manager_thread.is_alive():
            manager_thread.join(timeout=10)
            # if manager_thread.is_alive(): print removed

        stop_ffmpeg_stream()
