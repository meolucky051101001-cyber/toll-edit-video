import os
import subprocess
import glob

dataset_dir = r"C:\Users\admin\.gemini\antigravity\scratch\voice_training\dataset"

# Find all mp4 files
mp4_files = glob.glob(os.path.join(dataset_dir, "*.mp4"))

for mp4_file in mp4_files:
    wav_file = mp4_file.rsplit('.', 1)[0] + '.wav'
    print(f"Converting {os.path.basename(mp4_file)} to wav...")
    
    # Use ffmpeg to convert video to audio (pcm_s16le is standard wav format)
    command = [
        "ffmpeg", "-y", "-i", mp4_file,
        "-vn", # no video
        "-acodec", "pcm_s16le", # audio codec
        "-ar", "44100", # sample rate
        "-ac", "2", # channels
        wav_file
    ]
    
    try:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Optional: delete the mp4 file after successful conversion to save space
        # os.remove(mp4_file)
    except subprocess.CalledProcessError as e:
        print(f"Error converting {mp4_file}")

print("All done!")
