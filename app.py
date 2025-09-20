import os
import uuid
import asyncio
import aiofiles
import httpx
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import telegram
from telegram import InputFile
import telethon
from telethon import TelegramClient
from telethon.sessions import StringSession
import logging
from typing import Optional
from datetime import datetime, timedelta
import math

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="File Uploader API", version="2.0.0")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "your_bot_token_here")
CHANNEL_ID = os.getenv("CHANNEL_ID", "-1001234567890")
API_ID = int(os.getenv("API_ID", "12345678"))
API_HASH = os.getenv("API_HASH", "your_api_hash_here")
UPLOAD_DIR = os.getenv("UPLOAD_DIR", "/tmp/uploads")
MAX_FILE_SIZE = 6 * 1024 * 1024 * 1024  # 6GB
CHUNK_SIZE = 2000 * 1024 * 1024  # 2GB

# Initialize clients
bot = telegram.Bot(token=BOT_TOKEN)
telegram_client = None

# File extensions
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.3gp'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma'}

# Ensure upload directory exists
os.makedirs(UPLOAD_DIR, exist_ok=True)

def format_file_size(size_bytes):
    """Format file size in human readable format without external dependencies"""
    if size_bytes == 0:
        return "0 Bytes"
    
    size_names = ["Bytes", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_names[i]}"

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    global telegram_client
    
    # Initialize Telegram client
    try:
        session_string = os.getenv("SESSION_STRING", "")
        if session_string:
            telegram_client = TelegramClient(
                StringSession(session_string), 
                API_ID, 
                API_HASH
            )
            await telegram_client.start()
            logger.info("✅ Telegram client initialized")
    except Exception as e:
        logger.warning(f"❌ Telegram client initialization failed: {e}")
    
    # Start cleanup task
    asyncio.create_task(cleanup_task())

async def cleanup_task():
    """Background task to clean up temporary files"""
    while True:
        try:
            await cleanup_old_files()
            await asyncio.sleep(300)  # Run every 5 minutes
        except Exception as e:
            logger.error(f"Cleanup task error: {e}")
            await asyncio.sleep(60)

async def cleanup_old_files():
    """Clean up files older than 1 hour"""
    try:
        for filename in os.listdir(UPLOAD_DIR):
            filepath = os.path.join(UPLOAD_DIR, filename)
            if os.path.isfile(filepath):
                file_age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(filepath))
                if file_age > timedelta(hours=1):
                    os.remove(filepath)
                    logger.info(f"🧹 Cleaned up file: {filename}")
    except Exception as e:
        logger.error(f"Cleanup error: {e}")

# Health check endpoint
@app.get("/health")
async def health_check():
    """Comprehensive health check"""
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "services": {
            "api": "online",
            "telegram_bot": "unknown",
            "storage": "online" if os.path.exists(UPLOAD_DIR) else "offline"
        },
        "version": "2.0.0"
    }
    
    # Check Telegram bot
    try:
        await bot.get_me()
        health_status["services"]["telegram_bot"] = "online"
    except Exception as e:
        health_status["services"]["telegram_bot"] = f"offline: {str(e)}"
        health_status["status"] = "degraded"
    
    return health_status

@app.get("/")
async def home():
    """Serve the upload interface"""
    html_content = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>File Uploader</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 800px; margin: 0 auto; padding: 20px; }
            .upload-container { border: 2px dashed #ccc; padding: 40px; text-align: center; margin: 20px 0; }
            .progress { margin: 20px 0; }
            .btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 5px; cursor: pointer; }
        </style>
    </head>
    <body>
        <h1>File Uploader to Telegram</h1>
        <div class="upload-container">
            <input type="file" id="fileInput" multiple>
            <button class="btn" onclick="uploadFile()">Upload File</button>
            <div class="progress" id="progress" style="display: none;">
                <progress value="0" max="100"></progress>
                <span id="progressText">0%</span>
            </div>
        </div>
        <script>
            async function uploadFile() {
                const fileInput = document.getElementById('fileInput');
                const progress = document.getElementById('progress');
                const progressBar = progress.querySelector('progress');
                const progressText = document.getElementById('progressText');
                
                if (fileInput.files.length === 0) {
                    alert('Please select a file');
                    return;
                }
                
                const formData = new FormData();
                formData.append('file', fileInput.files[0]);
                
                progress.style.display = 'block';
                
                try {
                    const response = await fetch('/upload', {
                        method: 'POST',
                        body: formData,
                    });
                    
                    const result = await response.json();
                    
                    if (result.success) {
                        alert('Upload successful! Download URL: ' + result.download_url);
                    } else {
                        alert('Upload failed: ' + result.error);
                    }
                } catch (error) {
                    alert('Upload error: ' + error.message);
                } finally {
                    progress.style.display = 'none';
                }
            }
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Upload file to Telegram"""
    try:
        # Generate unique filename
        file_ext = os.path.splitext(file.filename)[1] if file.filename else ''
        temp_filename = f"{uuid.uuid4()}{file_ext}"
        temp_filepath = os.path.join(UPLOAD_DIR, temp_filename)
        
        # Save file temporarily
        async with aiofiles.open(temp_filepath, 'wb') as out_file:
            content = await file.read()
            await out_file.write(content)
        
        file_size = len(content)
        
        # Upload to Telegram
        result = await upload_to_telegram(temp_filepath, file.filename, file_size)
        
        # Clean up
        try:
            os.remove(temp_filepath)
        except:
            pass
        
        return result
        
    except Exception as e:
        logger.error(f"Upload error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

async def upload_to_telegram(filepath: str, filename: str, filesize: int):
    """Upload file to Telegram channel"""
    try:
        human_size = format_file_size(filesize)
        caption = f"📁 {filename}\n💾 Size: {human_size}"
        
        # For small files (<2GB)
        if filesize <= CHUNK_SIZE:
            with open(filepath, 'rb') as f:
                message = await bot.send_document(
                    chat_id=CHANNEL_ID,
                    document=InputFile(f, filename=filename),
                    caption=caption
                )
        else:
            # For large files, use telethon if available
            if telegram_client:
                message = await telegram_client.send_file(
                    CHANNEL_ID,
                    file=filepath,
                    caption=caption
                )
            else:
                # Fallback to chunking
                raise HTTPException(400, "Large file support requires Telegram client setup")
        
        file_id = message.document.file_id if hasattr(message, 'document') else None
        
        return {
            "success": True,
            "filename": filename,
            "filesize": filesize,
            "file_size_formatted": human_size,
            "file_id": file_id,
            "message": "File uploaded successfully"
        }
        
    except Exception as e:
        logger.error(f"Telegram upload error: {e}")
        raise HTTPException(500, f"Telegram upload failed: {str(e)}")

@app.get("/metrics")
async def metrics():
    """Metrics endpoint"""
    metrics_data = {
        "uploads_total": 0,
        "uploads_failed": 0,
        "storage_usage_bytes": 0,
        "active_connections": 0
    }
    
    # Count files in upload directory
    try:
        upload_files = os.listdir(UPLOAD_DIR)
        metrics_data["storage_usage_bytes"] = sum(
            os.path.getsize(os.path.join(UPLOAD_DIR, f)) for f in upload_files
        )
    except:
        pass
    
    return JSONResponse(content=metrics_data)

@app.get("/api/status")
async def api_status():
    """API status information"""
    return {
        "status": "operational",
        "version": "2.0.0",
        "services": {
            "telegram_bot": "active",
            "file_upload": "active",
            "health_checks": "active"
        },
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
