# YouTube Downloader for Telegram

A Telegram bot powered by AWS Lambda that allows users to download YouTube videos in different qualities and send them directly via Telegram or through a presigned S3 download link for larger files.

## 🌟 Features

- Download YouTube videos in 4 different resolutions (240p, 480p, 720p, 1080p)
- Direct sending of videos under 50 MB via Telegram
- Automatic storage on AWS S3 and generation of presigned links for videos over 50 MB
- YouTube cookies management to access age-restricted content
- Commands to list and delete stored videos

## 📋 Available Commands

- `/start` - Start the bot
- `/list` - List all videos stored in the S3 bucket
- `/delete filename.mp4` - Delete a specific video from the S3 bucket
- `/help` - Display help with all available commands

To download a video, simply send:
```
[YouTube URL] [resolution]
```

## 🏞️ Available resolutions:

- `low` (240p)
- `medium` (480p)
- `high` (720p)
- `veryhigh` (1080p)

## 📝 Example:

```
https://www.youtube.com/watch?v=example medium
```

## 🏗️ Architecture

The bot is built with:
- AWS Lambda for code execution
- AWS Lambda layers for yt-dlp and FFmpeg
- AWS S3 for vides and cookies storage
- AWS Secrets Manager for securely managing the Telegram bot token
- AWS API Gateway for the Telegram webhook
- AWS IAM for managing permissions

## 🔧 Prerequisites

- An AWS account
- A Telegram bot (created via @BotFather)

## 🚀 Deployment

### 👷 Lambda function

Create a Lambda function (Adapt the GLOBAL VARIABLES in the beginning of the file)

### 🛠️ Add layers containing yt-dlp and FFmpeg

To create an AWS Lambda layer for yt-dlp, follow these steps:

1. Create a new directory for the layer:
   ```bash
   mkdir yt-dlp-layer && cd yt-dlp-layer
   ```

2. Create a `bin` directory inside it:
   ```bash
   mkdir -p bin
   ```

3. Download `yt-dlp`:
   ```bash
   curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o bin/yt-dlp
   ```

4. Make the downloaded file executable:
   ```bash
   chmod +x bin/yt-dlp
   ```

5. Zip the layer:
   ```bash
   zip -r yt-dlp-layer.zip bin
   ```

6. Upload the zip file to AWS Lambda as a new layer and assign it to your Lambda function.

For the FFmpeg layer, follow the steps of this tutorial [here](https://virkud-sarvesh.medium.com/building-ffmpeg-layer-for-a-lambda-function-a206f36d3edc) but instead of storing in S3, upload the final zip file to AWS Lambda as a new layer and assign it to your Lambda function.

Note that when creating a layer, you need to select "Compatible runtimes" as your python version you are using across your AWS services for this project.

### 🌐 API Gateway

1. Create a new HTTP API
2. Add a new integration with your Lambda function
3. Choose "Method" as ANY for simplicity, the "Resource path" like "/my_api", and "Integration target" as your Lambda function name
4. Keep Stage name ad "$default" and "Auto-deployed" selected
5. Get webhook info by using this url https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo
6. Add webhook by using this url https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=<API_GATEWAY_URL>
7. (Optional) Delete webhook by using this url https://api.telegram.org/bot<BOT_TOKEN>/deleteWebhook

### 🪣 S3 bucket for Cookies and stored youtube videos

Yt-dlp sometimes needs cookies to work

1. Export your youtube cookies with a Chrome extention like "Get cookies.txt LOCALLY"
2. Create a new bucket in your AWS account
3. Upload the youtube cookies .txt file to the bucket
4. Adapt the lambda_function.py file to use the bucket name and file key (i.e. the path in the bucket)

Create an S3 bucket to store downloaded videos that are larger than 50MB.

### 🛡️ IAM Permissions

Configure IAM permissions to access S3 ("s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:DeleteObject") and Secrets Manager ("secretsmanager:GetSecretValue"). You can do it in the Lambda function Configuration > Permissions > Click on the Role name > Add permissions > Create inline policy > Add the required permissions.

The S3 policy looks like this:
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Sid": "S3Access",
			"Effect": "Allow",
			"Action": "s3:ListBucket",
			"Resource": "arn:aws:s3:::yt-downloaded-videos"
		},
		{
			"Sid": "S3ObjectAccessDlVideos",
			"Effect": "Allow",
			"Action": [
				"s3:PutObject",
				"s3:GetObject",
				"s3:DeleteObject"
			],
			"Resource": [
				"arn:aws:s3:::yt-downloaded-videos/*"
			]
		},
		{
			"Sid": "S3ObjectAccessCookies",
			"Effect": "Allow",
			"Action": [
				"s3:GetObject"
			],
			"Resource": [
				"arn:aws:s3:::yt-cookies/youtube_cookies.txt"
			]
		}
	]
}
```

The Secrets Manager policy looks like this:
```json
{
	"Version": "2012-10-17",
	"Statement": [
		{
			"Sid": "VisualEditor0",
			"Effect": "Allow",
			"Action": "secretsmanager:GetSecretValue",
			"Resource": "arn:aws:secretsmanager:*:*:secret:Telegram-bot-token-*"
		}
	]
}
```

## 📝 Notes

- Files larger than 50MB are automatically stored on S3 and shared via a presigned link (valid for 1 hour), because Telegram API has a file size limit of 50MB
- Debug using CloudWatch Log groups and Lambda function logs located in the Monitoring tab