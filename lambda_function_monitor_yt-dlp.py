import os
import subprocess
import json
import boto3
import logging
import urllib
import tempfile
import zipfile


REGION_NAME = "us-east-1"
ACCOUNT_ID = 'XXXXXXXXXXXX'
S3_COOKIES_BUCKET_NAME = "yt-cookies"
S3_COOKIES_KEY = "youtube_cookies.txt"
YT_DLP_PATH = "/opt/bin/yt-dlp"
FFMPEG_PATH = "/opt/bin/ffmpeg"
FUNCTIONS_TO_UPDATE = [
        "yt_dl_bot_lambda_function",
        "Monitor_yt-dlp"
    ]

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

def check_ytdlp_version():

    # Get the current yt-dlp version
    try:
        command = [YT_DLP_PATH, "--version"]
        process = subprocess.run(command, capture_output=True, text=True)
        current_ytdlp_version = process.stdout.strip()
    except Exception as e:
        logger.error(f"Error in check_ytdlp_version: {str(e)}", exc_info=True)

    # Get the last yt-dlp version
    try:
        # GitHub API endpoint for latest release
        url = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        
        with urllib.request.urlopen(url) as response:
            data = json.loads(response.read().decode())
            
        # Extract version from tag_name (e.g., "2024.01.07" from tag)
        tag_name = data.get("tag_name", "")
        
        # Remove 'v' prefix if present
        last_ytdlp_version = tag_name.lstrip('v').strip()
                
    except Exception as e:
        logger.error(f"Error in check_ytdlp_version: {str(e)}", exc_info=True)

    return current_ytdlp_version, last_ytdlp_version


def update_ytdlp_layer(last_ytdlp_version):

    # Configuration
    LAYER_NAME = "yt-dlp-layer"
    COMPATIBLE_RUNTIMES = ["python3.9"]
    DESCRIPTION = f"Automatic update {last_ytdlp_version}"
    
    # Initialize AWS clients
    lambda_client = boto3.client('lambda')
    
    try:
        
        # Create temporary directory
        with tempfile.TemporaryDirectory() as temp_dir:
            layer_dir = os.path.join(temp_dir, "yt-dlp-layer")
            bin_dir = os.path.join(layer_dir, "bin")
            
            # Create directory structure
            os.makedirs(bin_dir, exist_ok=True)
            logger.info(f"Created directory structure at {layer_dir}")
            
            # Download yt-dlp
            ytdlp_path = os.path.join(bin_dir, "yt-dlp")
            logger.info("Downloading yt-dlp...")

            url = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"

            try:
                with urllib.request.urlopen(url) as response, open(ytdlp_path, 'wb') as out_file:
                    chunk_size = 8192
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        out_file.write(chunk)
            except urllib.error.URLError as e:
                logger.error(f"Failed to download yt-dlp: {e}")
                raise

            # Make executable
            os.chmod(ytdlp_path, 0o755)
            
            # Create zip file
            zip_path = os.path.join(temp_dir, "yt-dlp-layer.zip")
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Add the bin directory and its contents
                for root, dirs, files in os.walk(layer_dir):
                    for file in files:
                        file_path = os.path.join(root, file)
                        # Calculate the archive name relative to layer_dir
                        arcname = os.path.relpath(file_path, layer_dir)
                        zipf.write(file_path, arcname)
            
            
            # Read zip file content
            with open(zip_path, 'rb') as f:
                zip_content = f.read()
                        
            # Upload new layer version
            logger.info(f"Publishing new version of layer {LAYER_NAME}...")
            layer_response = lambda_client.publish_layer_version(
                LayerName=LAYER_NAME,
                Description=DESCRIPTION,
                Content={'ZipFile': zip_content},
                CompatibleRuntimes=COMPATIBLE_RUNTIMES
            )

            new_layer_version = layer_response['Version']    
            logger.info(f"Published new layer version: {new_layer_version}")
            return new_layer_version
        
    except Exception as e:
        logger.error(f"Error in update_ytdlp_layer: {str(e)}", exc_info=True)
        return
    

def link_ytdlp_layer(new_layer_version):

    yt_dlp_layer_arn = f"arn:aws:lambda:{REGION_NAME}:{ACCOUNT_ID}:layer:yt-dlp-layer:{new_layer_version}"
    lambda_client = boto3.client('lambda')

    try:
        logger.info(f"Linking new layer version {new_layer_version} to functions...")
        for function_name in FUNCTIONS_TO_UPDATE:
            # 1. Get current layers
            config = lambda_client.get_function_configuration(FunctionName=function_name)
            current_layers = [layer['Arn'] for layer in config.get('Layers', [])]

            # 2. Remove old yt-dlp layer (optional, in case you're replacing)
            current_layers = [arn for arn in current_layers if "yt-dlp-layer" not in arn]

            # 3. Add the new yt-dlp layer
            updated_layers = current_layers + [yt_dlp_layer_arn]

            # 4. Update function config
            lambda_client.update_function_configuration(
                FunctionName=function_name,
                Layers=updated_layers
            )

        logger.info("Linked new yt-dlp layer to all functions without removing other layers.")
    except Exception as e:
        logger.error(f"Error in link_ytdlp_layer: {str(e)}", exc_info=True)


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

    current_ytdlp_version, last_ytdlp_version = check_ytdlp_version()

    logger.info(f"Current yt-dlp version: '{current_ytdlp_version}'")
    logger.info(f"Last yt-dlp version: '{last_ytdlp_version}'")

    if current_ytdlp_version != last_ytdlp_version:
        logger.info(f"Updating yt-dlp from {current_ytdlp_version} to {last_ytdlp_version}")
        new_layer_version = update_ytdlp_layer(last_ytdlp_version)
        link_ytdlp_layer(new_layer_version)
    else:
        logger.info("yt-dlp is up to date.")

    url = event.get("url")
    resolution = event.get("resolution", "low")

    file_path = download_video(url, resolution)

    if file_path:
        logger.info(f"Video downloaded successfully: {file_path}")
        return {'statusCode': 200, 'body': json.dumps('Video downloaded')}
    else:
        logger.error(f"Error in process_video_download for url: {url}")
        raise  Exception(f"Error in download process for url: {url}")
