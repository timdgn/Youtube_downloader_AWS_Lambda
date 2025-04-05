import os
import subprocess
import json
import boto3
import logging


REGION_NAME = "us-east-1"
S3_COOKIES_BUCKET_NAME = "yt-cookies"
S3_COOKIES_KEY = "youtube_cookies.txt"
YT_DLP_PATH = "/opt/bin/yt-dlp"
FFMPEG_PATH = "/opt/bin/ffmpeg"

FORMATS = {
    "low": "bestvideo[height<=240][ext=mp4]+bestaudio",
    "medium": "bestvideo[height<=480][ext=mp4]+bestaudio",
    "high": "bestvideo[height<=720][ext=mp4]+bestaudio",
    "veryhigh": "bestvideo[height<=1080][ext=mp4]+bestaudio",
    "mp3": "bestaudio"}

WORKING_DIR = "/tmp"  # AWS Lambda has write permissions in /tmp
os.makedirs(WORKING_DIR, exist_ok=True)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def download_video(url, resolution):
    try:
        cookie_file = os.path.join(WORKING_DIR, "cookie.txt")
        output_path = os.path.join(WORKING_DIR, "%(title)s.%(ext)s")

        s3 = boto3.client('s3')
        s3.download_file(S3_COOKIES_BUCKET_NAME, S3_COOKIES_KEY, cookie_file)

        format_string = FORMATS.get(resolution, FORMATS["medium"])

        command_download = [
            YT_DLP_PATH,
            "--cookies", cookie_file,
            "--output", output_path,
            "--format", format_string]

        if resolution == "mp3":
            command_download.extend([
                "--extract-audio",
                "--audio-format", "mp3"])
        else:
            command_download.extend([
                "--ffmpeg-location", FFMPEG_PATH,
                "--merge-output-format", "mp4"])

        command_download.append(url)

        logger.info(f"Executing command: {' '.join(command_download)}")
        process = subprocess.run(command_download, capture_output=True, text=True)
        logger.info(f"yt-dlp stdout: {process.stdout}")

        if process.returncode != 0:
            raise Exception(f"yt-dlp failed with return code {process.returncode}: {process.stderr}")

        if process.returncode == 0:
            if resolution == "mp3":
                for file in os.listdir(WORKING_DIR):
                    if file.endswith(".mp3"):
                        return os.path.join(WORKING_DIR, file)
            else:
                for file in os.listdir(WORKING_DIR):
                    if file.endswith(".mp4"):
                        return os.path.join(WORKING_DIR, file)
    except Exception as e:
        logger.error(f"Error in download_video: {str(e)}", exc_info=True)
        return None


def lambda_handler(event, context):

    url = event.get("url")
    resolution = event.get("resolution", "low")

    file_path = download_video(url, resolution)

    if file_path:
        logger.info(f"Video downloaded successfully: {file_path}")
        return {'statusCode': 200, 'body': json.dumps('Video downloaded')}
    else:
        logger.error(f"Error in process_video_download for url: {url}")
        raise  Exception(f"Error in download process for url: {url}")
