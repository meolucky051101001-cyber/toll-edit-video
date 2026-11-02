import os
import subprocess
import sys

def download_audio(urls, output_dir="dataset"):
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    for i, url in enumerate(urls):
        print(f"Downloading {i+1}/{len(urls)}: {url}")
        
        # Use yt-dlp to download and extract audio as wav
        command = [
            "yt-dlp",
            "-x",
            "--audio-format", "wav",
            "--audio-quality", "0",
            "-o", f"{output_dir}/%(title)s_%(id)s.%(ext)s",
            url
        ]
        
        try:
            subprocess.run(command, check=True)
            print(f"Success: {url}\n")
        except subprocess.CalledProcessError as e:
            print(f"Error downloading {url}: {e}\n")

if __name__ == "__main__":
    # Add your URLs to this list, or read them from a file
    urls = [
        # "https://www.youtube.com/watch?v=...",
    ]
    
    # Alternatively read from urls.txt if it exists
    if os.path.exists("urls.txt"):
        with open("urls.txt", "r", encoding="utf-8") as f:
            urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            
    if not urls:
        print("Please add URLs to urls.txt or directly to the script.")
        sys.exit(1)
        
    download_audio(urls)
