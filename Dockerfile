# পাইথন বেস ইমেজ ব্যবহার করুন
FROM python:3.9-slim

# ওয়ার্কিং ডিরেক্টরি সেট করুন
WORKDIR /app

# FFmpeg এবং অন্যান্য প্রয়োজনীয় টুল ইনস্টল করুন
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    wget \
 && rm -rf /var/lib/apt/lists/*

# requirements ফাইল কপি করুন
COPY requirements.txt .

# পাইথন লাইব্রেরীগুলো ইনস্টল করুন
RUN pip install --no-cache-dir -r requirements.txt

# অ্যাপ্লিকেশন কোড কপি করুন
COPY . .

# পোর্ট 80 এক্সপোজ করুন (ওয়েব সার্ভারের জন্য)
EXPOSE 80

# ভিডিও এবং HLS আউটপুটের জন্য ডিরেক্টরি তৈরি করুন
RUN mkdir -p /app/videos /app/static/hls

# কন্টেইনার চালু হলে অ্যাপ্লিকেশন রান করার কমান্ড
# প্রোডাকশনের জন্য gunicorn ব্যবহার করা ভালো, এখানে সহজ রাখার জন্য সরাসরি পাইথন ব্যবহার করা হচ্ছে
CMD ["python", "app.py"]
