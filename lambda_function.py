import os
import json
import subprocess
import urllib3
import boto3
import zipfile
from botocore.exceptions import ClientError
from botocore.config import Config
from datetime import datetime
import logging


REGION_NAME = "us-east-1"
S3_YT_VIDEOS_BUCKET_NAME = "yt-downloaded-videos"
S3_COOKIES_BUCKET_NAME = "yt-cookies"
S3_COOKIES_KEY = "youtube_cookies.txt"
YT_DLP_PATH = "/opt/bin/yt-dlp"
FFMPEG_PATH = "/opt/bin/ffmpeg"
BOT_SECRET_NAME = "Telegram-bot-token"
BOT_SECRET_KEY = "bot_token"
DYNAMODB = boto3.resource('dynamodb')
MESSAGES_TABLE = DYNAMODB.Table('telegram_messages')

HELP_MESSAGE = """
📚 Available commands:

/start - Start the bot
/list - List all your videos on the server
/delete filename.zip - Delete a specific video
/help - Display this help

To download a YouTube video:
"[URL] [resolution]"

Available resolutions:
• low - low quality (240p)
• medium - medium quality (480p)
• high - high quality (720p)
• veryhigh - very high quality (1080p)
• mp3 - audio only (MP3 format)

Example: "https://www.youtube.com/watch?v=example medium"
    """

FORMATS = {
    "low": "bestvideo[height<=240][ext=mp4]+bestaudio",
    "medium": "bestvideo[height<=480][ext=mp4]+bestaudio",
    "high": "bestvideo[height<=720][ext=mp4]+bestaudio",
    "veryhigh": "bestvideo[height<=1080][ext=mp4]+bestaudio",
    "mp3": "bestaudio"}

WORKING_DIR = "/tmp"  # AWS Lambda has write permissions in /tmp
os.makedirs(WORKING_DIR, exist_ok=True)
HTTP = urllib3.PoolManager()
logger = logging.getLogger()
logger.setLevel(logging.INFO)
cloudwatch = boto3.client('cloudwatch')


def get_secret_bot_token():
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=REGION_NAME)

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=BOT_SECRET_NAME)
    except ClientError as e:
        logger.error(f"Error retrieving bot token: {e}")
        raise e

    # Convert SecretString to a dictionary
    secret_string = get_secret_value_response['SecretString']
    secret = json.loads(secret_string)

    return secret[BOT_SECRET_KEY]


def send_message(chat_id, message):
    url = f"https://api.telegram.org/bot{get_secret_bot_token()}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    encoded_data = json.dumps(data).encode('utf-8')
    HTTP.request('POST', url, body=encoded_data, headers={'Content-Type': 'application/json'})


def save_message_to_dynamodb(chat_id, message_text, first_name=None, last_name=None):
    """
    Save the user's message to DynamoDB.
    """
    try:
        item = {
            'chat_id': f"{str(chat_id)}_{first_name}_{last_name}",
            'timestamp': datetime.utcnow().isoformat(),
            'message': message_text,
            'user_info': {
                'first_name': first_name,
                'last_name': last_name
            }
        }
        MESSAGES_TABLE.put_item(Item=item)
        logger.info(f"Message saved to DynamoDB for chat_id: {chat_id}")
    except Exception as e:
        logger.error(f"Error saving message to DynamoDB: {e}")


def get_message_history(chat_id, first_name, last_name, limit=25):
    """
    Retrieve the user's message history from DynamoDB, sorted by timestamp.
    """
    try:
        response = MESSAGES_TABLE.query(
            KeyConditionExpression='chat_id = :chat_id',
            ExpressionAttributeValues={':chat_id': f"{str(chat_id)}_{first_name}_{last_name}"},
            Limit=limit,
            ScanIndexForward=False  # Get most recent first
        )
        return response['Items']
    except Exception as e:
        logger.error(f"Error retrieving message history from DynamoDB: {e}")
        return None


def zip_file(file_path):
    """
    Create a zip file from the downloaded video
    """
    file_name = os.path.basename(file_path)
    zip_file_path = os.path.join(WORKING_DIR, f"{os.path.splitext(file_name)[0]}.zip")

    try:
        with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, arcname=file_name)
        logger.info(f"Successfully zipped file: {zip_file_path}")
        return zip_file_path
    except Exception as e:
        logger.error(f"Error zipping file: {e}")
        return None


def get_s3_key(chat_id, file_name, first_name=None, last_name=None):
    """
    Generate the S3 key using chat_id, first_name, and last_name as folder structure
    """
    # Create a folder name with available user info
    folder_parts = [str(chat_id)]
    if first_name:
        folder_parts.append(first_name)
    if last_name:
        folder_parts.append(last_name)

    folder_name = "_".join(folder_parts)
    return f"{folder_name}/{file_name}"


def upload_file_to_s3(file_path, chat_id, first_name=None, last_name=None):
    s3 = boto3.client('s3')

    file_name = os.path.basename(file_path)
    s3_key = get_s3_key(chat_id, file_name, first_name, last_name)

    try:
        s3.upload_file(file_path, S3_YT_VIDEOS_BUCKET_NAME, s3_key)
        logger.info(f"Successfully uploaded to S3: {s3_key}")
    except ClientError as e:
        logger.error(f"Error uploading file to S3: {e}")
        return None

    return s3_key


def generate_url(s3_key):
    s3 = boto3.client('s3', config=Config(signature_version='s3v4'))
    try:
        url = s3.generate_presigned_url('get_object',
                                        Params={'Bucket': S3_YT_VIDEOS_BUCKET_NAME, 'Key': s3_key},
                                        ExpiresIn=86400)  # 24 hours
        return url
    except ClientError as e:
        logger.error(f"Error generating presigned URL: {e}")
        return None


def send_video_or_link(chat_id, file_path, first_name=None, last_name=None):
    file_name = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)  # Convert to MB
    logger.info(f"File size: {file_size_mb:.2f} MB")

    # If the file size is less than 50MB, send it directly
    if file_size_mb < 50:
        logger.info(f"File is {file_size_mb:.2f}MB, sending directly")
        if file_name.endswith('.mp3'):
            url = f"https://api.telegram.org/bot{get_secret_bot_token()}/sendAudio"
            with open(file_path, 'rb') as audio:
                audio_data = audio.read()
            fields = {"chat_id": str(chat_id), "audio": (file_name, audio_data, "audio/mp3")}
        else:
            url = f"https://api.telegram.org/bot{get_secret_bot_token()}/sendVideo"
            with open(file_path, 'rb') as video:
                video_data = video.read()
            fields = {"chat_id": str(chat_id), "video": (file_name, video_data, "video/mp4")}

        response = HTTP.request('POST', url, fields=fields)
        logger.info(f"Response of the POST request: {response.data}")

    # If the file size is 50MB or more, zip it, upload to S3 and send the link
    else:
        logger.info(f"File is {file_size_mb:.2f}MB, zipping, uploading to S3 and sending link")

        media = "audio/music" if file_name.endswith('.mp3') else "video"

        zip_file_path = zip_file(file_path)
        s3_key = upload_file_to_s3(zip_file_path, chat_id, first_name, last_name)
        if s3_key:
            file_url = generate_url(s3_key)
            if file_url:
                msg = f"Here's your {media} (as a zip file) 🍿\n\n{file_url}"
                send_message(chat_id, msg)
                logger.info(f"{media} uploaded to S3 and link sent to user")
            else:
                logger.error("Failed to generate pre-signed URL")
                send_message(chat_id, "Sorry, there was an error creating the download URL 🥲")

            os.remove(zip_file_path)
        else:
            logger.error(f"Failed to upload {media} to S3")
            send_message(chat_id, f"Sorry, there was an error sending the {media} to the server 🥲")

    os.remove(file_path)


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


def list_s3_videos(chat_id, first_name=None, last_name=None):
    """
    List all videos in the S3 bucket for the specific chat_id
    """
    s3 = boto3.client('s3')
    try:
        prefix = f"{chat_id}"
        if first_name:
            prefix += f"_{first_name}"
        if last_name:
            prefix += f"_{last_name}"
        prefix += "/"
        response = s3.list_objects_v2(Bucket=S3_YT_VIDEOS_BUCKET_NAME, Prefix=prefix)
        if 'Contents' in response:
            videos = []
            for obj in response['Contents']:
                key = obj['Key']
                # Extract just the filename (without the chat_id/ prefix)
                file_name = key.split('/', 1)[1]
                size_mb = obj['Size'] / (1024 * 1024)  # Convert to MB
                videos.append(f"{file_name} ({size_mb:.2f} MB)")
            return videos
        else:
            return []
    except ClientError as e:
        logger.error(f"Error listing S3 objects: {e}")
        return None


def delete_s3_video(chat_id, file_name, first_name=None, last_name=None):
    """
    Delete a specific video from the S3 bucket for the specific chat_id
    """
    s3 = boto3.client('s3')
    s3_key = get_s3_key(chat_id, file_name, first_name, last_name)

    try:
        # Check if the file exists
        response = s3.list_objects_v2(Bucket=S3_YT_VIDEOS_BUCKET_NAME, Prefix=s3_key)
        file_exists = 'Contents' in response and len(response['Contents']) > 0

        if not file_exists:
            return False

        # The file exists, we can delete it
        s3.delete_object(Bucket=S3_YT_VIDEOS_BUCKET_NAME, Key=s3_key)
        return True
    except ClientError as e:
        print(f"*** Error deleting S3 object: {e}")
        return False


def send_cloudwatch_dl_error(chat_id):
    """
    Send a metric to CloudWatch to track download errors
    """

    cloudwatch.put_metric_data(
        Namespace='YTDownloader_app',
        MetricData=[
            {
                'MetricName': 'DownloadError',
                'Value': 1,
                'Unit': 'Count'
            },
        ]
    )

    msg = "🤖 Download failed, I need to be updated, my admin Tim has been notified 🔔"
    send_message(chat_id, msg)


def process_video_download(chat_id, url, resolution, first_name=None, last_name=None):
    """
    Function to handle the video download process asynchronously
    """

    logger.info(f"Starting video download for chat_id: {chat_id}, url: {url}, resolution: {resolution}")

    send_message(chat_id, "Download in progress, please wait... 🔄")
    file_path = download_video(url, resolution)

    if file_path:
        file_name = os.path.basename(file_path)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        msg = f"""Sending "{file_name}" in {resolution} resolution ({file_size_mb:.2f} MB), coming soon... 📲"""
        send_message(chat_id, msg)
        send_video_or_link(chat_id, file_path, first_name, last_name)
    else:
        logger.error(f"Error in process_video_download for chat_id: {chat_id}, url: {url}, resolution: {resolution}")
        send_cloudwatch_dl_error(chat_id)


def invoke_lambda_async(payload):
    """
    Invoke the same Lambda function asynchronously to process the video download
    """
    lambda_client = boto3.client('lambda')
    lambda_client.invoke(
        FunctionName=os.environ.get('AWS_LAMBDA_FUNCTION_NAME'),
        InvocationType='Event',  # Asynchronous invocation
        Payload=json.dumps(payload)
    )


def lambda_handler(event, context):
    print(f"*** Bot Token : {get_secret_bot_token()}")
    print(f"*** Event : {event}")

    # Check if this is an async video processing invocation
    if event.get('type') == 'process_video':
        chat_id = event.get('chat_id')
        first_name = event.get('first_name')
        last_name = event.get('last_name')
        url = event.get('url')
        resolution = event.get('resolution')
        process_video_download(chat_id, url, resolution, first_name, last_name)
        return {'statusCode': 200, 'body': json.dumps('Video processing completed')}

    # Regular webhook handling
    body = json.loads(event.get('body', '{}'))
    print(f"*** Body : {body}")

    try:
        chat_id = body['message']['chat']['id']
        message_text = body['message']['text']
        first_name = body['message']['chat'].get('first_name')
        last_name = body['message']['chat'].get('last_name')
    except KeyError:
        try:
            chat_id = body['edited_message']['chat']['id']
            message_text = body['edited_message']['text']
            first_name = body['edited_message']['chat'].get('first_name')
            last_name = body['edited_message']['chat'].get('last_name')
        except KeyError:
            return {'statusCode': 200, 'body': json.dumps('Invalid message format')}

    # Strip message_text
    message_text = message_text.strip()
    logger.info(f"Message Text: {message_text}")

    # Save the message to DynamoDB before processing
    save_message_to_dynamodb(chat_id, message_text, first_name, last_name)

    # Command: /history - Show message history
    if message_text.startswith('/history'):
        limit = 25
        messages = get_message_history(chat_id, first_name, last_name, limit)
        if messages is None:
            send_message(chat_id, "Sorry, there was an error retrieving your message history 🥲")
        elif messages:
            response = f"📜 Your recent messages (up to {limit}):\n\n"
            for i, msg in enumerate(messages, 1):
                timestamp = msg['timestamp']
                text = msg['message']
                response += f"{i} - [{timestamp}] {text}\n\n"
            send_message(chat_id, response)
        else:
            send_message(chat_id, "No message history found 📭")
        return {'statusCode': 200, 'body': json.dumps('History command processed')}

    # Command: /list - List all videos in S3 bucket for this user
    elif message_text.startswith('/list'):
        videos = list_s3_videos(chat_id, first_name, last_name)
        if videos:
            message = "📋 Your available videos:\n\n"
            for i, video in enumerate(videos, 1):
                message += f"{i} - {video}\n\n"
            send_message(chat_id, message)
        else:
            send_message(chat_id, "No videos available, nothing, nada 🧹")
        return {'statusCode': 200, 'body': json.dumps('List command processed')}

    # Command: /delete filename.zip - Delete a specific video
    elif message_text.startswith('/delete'):
        parts = message_text.split(maxsplit=1)  # keep maxsplit=1 because filenames can have spaces

        if len(parts) > 1:
            file_name = parts[1].strip()
            success = delete_s3_video(chat_id, file_name, first_name, last_name)
            if success:
                send_message(chat_id, f"""✅ Video "{file_name}" deleted, c'est ciao 🫡""")
                return {'statusCode': 200, 'body': json.dumps('Delete command processed')}
            else:
                send_message(chat_id, f"""❌ Unable to delete "{file_name}", check the filename 🧐""")
                return {'statusCode': 200, 'body': json.dumps('Delete command processed')}
        else:
            send_message(chat_id, "❌ Please specify the filename to delete, for example /delete filename.zip")
        return {'statusCode': 200, 'body': json.dumps('Delete command processed')}

    # Command: /help or /start - Show available commands
    elif message_text.startswith('/help') or message_text.startswith('/start'):
        send_message(chat_id, HELP_MESSAGE)
        return {'statusCode': 200, 'body': json.dumps('Help command processed')}

    # Standard video download command
    else:
        parts = message_text.strip().split()
        if len(parts) != 2:
            send_message(chat_id, HELP_MESSAGE)
            return {'statusCode': 200, 'body': json.dumps('Invalid input')}

        url, resolution = parts[0], parts[1].lower()
        print(f"*** URL : {url}")
        print(f"*** resolution : {resolution}")

        if "dQw4w9WgXcQ" in url:
            send_message(chat_id, "Don't even think about it 🤨")
            return {'statusCode': 200, 'body': json.dumps('Invalid URL')}

        if resolution not in FORMATS.keys():
            send_message(chat_id, HELP_MESSAGE)
            return {'statusCode': 200, 'body': json.dumps('Invalid resolution')}

        # Invoke the same Lambda function asynchronously to process the video
        payload = {
            'type': 'process_video',
            'chat_id': chat_id,
            'first_name': first_name,
            'last_name': last_name,
            'url': url,
            'resolution': resolution}
        invoke_lambda_async(payload)

        return {'statusCode': 200, 'body': json.dumps('Video download request received')}