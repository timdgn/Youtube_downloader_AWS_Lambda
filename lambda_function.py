import os
import json
import subprocess
import urllib3
import boto3
import zipfile
from botocore.exceptions import ClientError
from botocore.config import Config

REGION_NAME = "us-east-1"
S3_YT_VIDEOS_BUCKET_NAME = "yt-downloaded-videos"
S3_COOKIES_BUCKET_NAME = "yt-cookies"
S3_COOKIES_KEY = "youtube_cookies.txt"
YT_DLP_PATH = "/opt/bin/yt-dlp"
FFMPEG_PATH = "/opt/bin/ffmpeg"
BOT_SECRET_NAME = "Telegram-bot-token"
BOT_SECRET_KEY = "bot_token"

HELP_MESSAGE = """
    📚 Commandes disponibles:

    /list - Lister toutes les vidéos dans le bucket S3
    /delete nom_du_fichier.zip - Supprimer une vidéo spécifique
    /help - Afficher cette aide

    Pour télécharger une vidéo YouTube:
    [URL] [résolution]

    Résolutions disponibles: low, medium, high, veryhigh
    Exemple: https://www.youtube.com/watch?v=example medium
    """
FORMATS = {
    "low": "bestvideo[height<=240][ext=mp4]+bestaudio",
    "medium": "bestvideo[height<=480][ext=mp4]+bestaudio",
    "high": "bestvideo[height<=720][ext=mp4]+bestaudio",
    "veryhigh": "bestvideo[height<=1080][ext=mp4]+bestaudio"}

WORKING_DIR = "/tmp"  # AWS Lambda has write permissions in /tmp
os.makedirs(WORKING_DIR, exist_ok=True)
HTTP = urllib3.PoolManager()


def get_secret_bot_token():
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=REGION_NAME)

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=BOT_SECRET_NAME)
    except ClientError as e:
        raise e

    # Convert SecretString to a dictionary
    secret_string = get_secret_value_response['SecretString']
    secret = json.loads(secret_string)

    return secret[BOT_SECRET_KEY]


def send_message(chat_id, message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": chat_id, "text": message}
    encoded_data = json.dumps(data).encode('utf-8')
    HTTP.request('POST', url, body=encoded_data, headers={'Content-Type': 'application/json'})


def zip_file(file_path):
    """
    Create a zip file from the downloaded video
    """
    file_name = os.path.basename(file_path)
    zip_file_path = os.path.join(WORKING_DIR, f"{os.path.splitext(file_name)[0]}.zip")

    try:
        with zipfile.ZipFile(zip_file_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(file_path, arcname=file_name)
        print(f"*** Successfully zipped file: {zip_file_path}")
        return zip_file_path
    except Exception as e:
        print(f"*** Error zipping file: {e}")
        return None


def upload_video_to_s3(file_path):
    s3 = boto3.client('s3')

    file_name = os.path.basename(file_path)
    try:
        s3.upload_file(file_path, S3_YT_VIDEOS_BUCKET_NAME, file_name)
        print(f"*** Successfully uploaded to S3: {file_name}")
    except ClientError as e:
        print(f"*** Error uploading file to S3: {e}")
        return None

    return file_name


def generate_presigned_url(file_name):
    s3 = boto3.client('s3', config=Config(signature_version='s3v4'))
    try:
        url = s3.generate_presigned_url('get_object',
                                        Params={'Bucket': S3_YT_VIDEOS_BUCKET_NAME, 'Key': file_name},
                                        ExpiresIn=86400)  # 24 hours
        return url
    except ClientError as e:
        print(f"*** Error generating presigned URL: {e}")
        return None


def send_video_or_link(chat_id, file_path):
    file_name = os.path.basename(file_path)
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)  # Convert to MB
    print(f"*** File size: {file_size_mb:.2f} MB")

    # If the file size is less than 50MB, send it directly as a video
    if file_size_mb < 50:
        print(f"*** File is {file_size_mb:.2f}MB, using sendVideo")
        url_video = f"https://api.telegram.org/bot{BOT_TOKEN}/sendVideo"
        with open(file_path, 'rb') as video:
            video_data = video.read()
        fields = {"chat_id": str(chat_id), "video": (file_name, video_data, "video/mp4")}
        response = HTTP.request('POST', url_video, fields=fields)
        print(f"*** Response of the POST request: {response.data}")

    # If the file size is 50MB or more, zip it, upload to S3 and send the link
    else:
        print(f"*** File is {file_size_mb:.2f}MB, zipping, uploading to S3 and sending link")

        # Zip the file
        zip_file_path = zip_file(file_path)

        # Upload the zipped file to S3
        zip_file_name = upload_video_to_s3(zip_file_path)
        if zip_file_name:
            # Generate a pre-signed URL
            file_url = generate_presigned_url(zip_file_name)
            if file_url:
                msg = f"Et voici ta vidéo (en fichier zip) 🍿\n\n{file_url}"
                send_message(chat_id, msg)
                print("*** Video uploaded to S3 and link sent to user")
            else:
                print("*** Failed to generate pre-signed URL")
                send_message(chat_id, "Désolé, il y a une erreur lors de la création de l'URL de téléchargement 🥲")

            # Clean up the zip file
            os.remove(zip_file_path)
        else:
            print("*** Failed to upload video to S3")
            send_message(chat_id, "Désolé, il y a une erreur lors de l'envoi de la vidéo au serveur 🥲")

    # Clean up after sending the video
    os.remove(file_path)


def download_video(url, resolution):
    cookie_file = os.path.join(WORKING_DIR, "cookie.txt")
    output_path = os.path.join(WORKING_DIR, "%(title)s.%(ext)s")

    s3 = boto3.client('s3')
    s3.download_file(S3_COOKIES_BUCKET_NAME, S3_COOKIES_KEY, cookie_file)

    format_string = FORMATS.get(resolution, FORMATS["medium"])

    command_download = [
        YT_DLP_PATH,
        "--cookies", cookie_file,
        "--output", output_path,
        "--format", format_string,
        "--ffmpeg-location", FFMPEG_PATH,
        "--merge-output-format", "mp4",
        url]

    print("*** Executing command:", " ".join(command_download))
    process = subprocess.run(command_download, capture_output=True, text=True)
    print(process.stdout)

    if process.returncode == 0:
        print("*** Download successful")
        for file in os.listdir(WORKING_DIR):
            if file.endswith(".mp4"):
                return os.path.join(WORKING_DIR, file)
        print("*** No MP4 file found")
    else:
        print("*** Error downloading")
        print(f"*** Error downloading with stderr: {process.stderr}")

    return None


def list_s3_videos():
    """
    List all videos in the S3 bucket
    """
    s3 = boto3.client('s3')
    try:
        response = s3.list_objects_v2(Bucket=S3_YT_VIDEOS_BUCKET_NAME)
        if 'Contents' in response:
            videos = []
            for obj in response['Contents']:
                key = obj['Key']
                size_mb = obj['Size'] / (1024 * 1024)  # Convert to MB
                videos.append(f"{key} ({size_mb:.2f} MB)")
            return videos
        else:
            return []
    except ClientError as e:
        print(f"*** Error listing S3 objects: {e}")
        return None


def delete_s3_video(file_name):
    """
    Delete a specific video from the S3 bucket
    """
    s3 = boto3.client('s3')
    try:
        # Vérifier si le fichier existe
        response = s3.list_objects_v2(Bucket=S3_YT_VIDEOS_BUCKET_NAME, Prefix=file_name)
        file_exists = 'Contents' in response and len(response['Contents']) > 0

        if not file_exists:
            return False

        # Le fichier existe, on peut le supprimer
        s3.delete_object(Bucket=S3_YT_VIDEOS_BUCKET_NAME, Key=file_name)
        return True
    except ClientError as e:
        print(f"*** Error deleting S3 object: {e}")
        return False


def process_video_download(chat_id, url, resolution):
    """
    Function to handle the video download process asynchronously
    """

    send_message(chat_id, "Téléchargement en cours, ça arrive ... 🔄")
    file_path = download_video(url, resolution)

    if file_path:
        file_name = os.path.basename(file_path)
        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
        msg = f"Je t'envoie '{file_name}' en résolution {resolution} ({file_size_mb:.2f} MB), ça arrive... 📲"
        send_message(chat_id, msg)
        send_video_or_link(chat_id, file_path)
    else:
        send_message(chat_id, "Échec du téléchargement 🤕 Réessaye !")


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

    global BOT_TOKEN
    BOT_TOKEN = get_secret_bot_token()
    print(f"*** Bot Token : {BOT_TOKEN}")

    print(f"*** Event : {event}")

    # Check if this is an async video processing invocation
    if event.get('type') == 'process_video':
        chat_id = event.get('chat_id')
        url = event.get('url')
        resolution = event.get('resolution')
        process_video_download(chat_id, url, resolution)
        return {'statusCode': 200, 'body': json.dumps('Video processing completed')}

    # Regular webhook handling
    body = json.loads(event.get('body', '{}'))
    print(f"*** Body : {body}")

    try:
        chat_id = body['message']['chat']['id']
        message_text = body['message']['text']
    except KeyError:
        try:
            chat_id = body['edited_message']['chat']['id']
            message_text = body['edited_message']['text']
        except KeyError:
            return {'statusCode': 200, 'body': json.dumps('Invalid message format')}

    # strip message_text
    message_text = message_text.strip()
    print(f"*** Message Text : {message_text}")

    # Command: /list - List all videos in S3 bucket
    if message_text.startswith('/list'):
        videos = list_s3_videos()
        if videos:
            message = "📋 Vidéos disponibles:\n\n"
            for i, video in enumerate(videos, 1):
                message += f"{i} - {video}\n\n"
            send_message(chat_id, message)
        else:
            send_message(chat_id, "Aucune vidéo disponible, rien, nada 🧹")
        return {'statusCode': 200, 'body': json.dumps('List command processed')}

    # Command: /delete filename.zip - Delete a specific video
    elif message_text.startswith('/delete'):
        parts = message_text.split(maxsplit=1)  # keep maxsplit=1 because filenames can have spaces

        if len(parts) > 1:
            file_name = parts[1].strip()
            success = delete_s3_video(file_name)
            if success:
                send_message(chat_id, f"✅ Vidéo '{file_name}' supprimée, c'est ciao 🫡")
                return {'statusCode': 200, 'body': json.dumps('Delete command processed')}
            else:
                send_message(chat_id, f"❌ Impossible de supprimer '{file_name}', vérifie le nom du fichier 🧐")
                return {'statusCode': 200, 'body': json.dumps('Delete command processed')}
        else:
            send_message(chat_id, "❌ Indique le nom du fichier à supprimer, par exemple /delete Video.zip")
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
            send_message(chat_id, "Même pas la peine d'y penser 🤨")
            return {'statusCode': 200, 'body': json.dumps('Invalid URL')}

        if resolution not in FORMATS.keys():
            send_message(chat_id, HELP_MESSAGE)
            return {'statusCode': 200, 'body': json.dumps('Invalid resolution')}

        # Invoke the same Lambda function asynchronously to process the video
        payload = {
            'type': 'process_video',
            'chat_id': chat_id,
            'url': url,
            'resolution': resolution
        }
        invoke_lambda_async(payload)

        return {'statusCode': 200, 'body': json.dumps('Video download request received')}