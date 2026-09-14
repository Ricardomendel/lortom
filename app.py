from flask import Flask, render_template, request, session, redirect, url_for, send_file
from flask_socketio import join_room, leave_room, send, SocketIO, emit, disconnect
from dotenv import load_dotenv
import os
import random
import html
import requests
from string import ascii_uppercase, digits
from flask_cors import CORS
import PyPDF2
from io import BytesIO
import unicodedata

load_dotenv()

app = Flask(__name__)
CORS(app)
app.config["FLASK_DEBUG"] = os.environ.get("FLASK_DEBUG")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="eventlet")

GOOGLE_TRANSLATE_API_KEY = os.environ.get("GOOGLE_TRANSLATE_API_KEY")
GOOGLE_TRANSLATE_URL = "https://translation.googleapis.com/language/translate/v2"

rooms = {} #Dictionary to store the list of rooms
files = {} #Dictionary to store the list of files

def safe_translate(text, dest):
    """Translate text via the official Google Cloud Translation API, falling
    back to the original text on failure instead of crashing the whole
    broadcast loop (one bad translation shouldn't stop every other room
    member from getting the message).

    We previously used the unofficial `googletrans` package, which scrapes
    Google Translate's web frontend rather than calling a real API. Google
    silently declined to translate requests coming from Render's datacenter
    IP range (returning the input text unchanged, with no error) -- a known
    failure mode for that kind of unofficial client in cloud environments.
    This uses the real, supported API instead.

    Logs every call at WARNING level (not just failures) with the input and
    output side by side, so a production issue is immediately visible in the
    logs. WARNING is used deliberately: Python's root logger defaults to
    WARNING, so this stays visible even if INFO-level logs are being
    swallowed."""
    if not GOOGLE_TRANSLATE_API_KEY:
        app.logger.warning("[translate] GOOGLE_TRANSLATE_API_KEY is not set; returning original text")
        return text
    try:
        response = requests.post(
            GOOGLE_TRANSLATE_URL,
            params={"key": GOOGLE_TRANSLATE_API_KEY},
            json={"q": text, "target": dest, "format": "text"},
            timeout=10,
        )
        response.raise_for_status()
        result = html.unescape(response.json()["data"]["translations"][0]["translatedText"])
        app.logger.warning(f"[translate] dest={dest!r} in={text[:60]!r} out={result[:60]!r}")
        return result
    except Exception as e:
        app.logger.warning(f"[translate] dest={dest!r} in={text[:60]!r} FAILED: {e}")
        return text

GOOGLE_SPEECH_TO_TEXT_URL = "https://speech.googleapis.com/v1/speech:recognize"
GOOGLE_TEXT_TO_SPEECH_URL = "https://texttospeech.googleapis.com/v1/text:synthesize"

# Cloud Speech-to-Text and Text-to-Speech need a full BCP-47 locale (e.g. "es-ES"),
# not the bare language code our language picker uses (e.g. "es").
SPEECH_LANG_MAP = {
    'en': 'en-US', 'es': 'es-ES', 'fr': 'fr-FR', 'de': 'de-DE', 'it': 'it-IT', 'pt': 'pt-PT', 'ak': 'ak-GH',
    'ru': 'ru-RU', 'ja': 'ja-JP', 'ko': 'ko-KR', 'zh-CN': 'zh-CN', 'zh-TW': 'zh-TW',
    'ar': 'ar-SA', 'hi': 'hi-IN', 'nl': 'nl-NL', 'pl': 'pl-PL', 'tr': 'tr-TR', 'vi': 'vi-VN',
    'th': 'th-TH', 'id': 'id-ID', 'sv': 'sv-SE', 'da': 'da-DK', 'fi': 'fi-FI', 'no': 'nb-NO',
    'el': 'el-GR', 'he': 'he-IL', 'cs': 'cs-CZ', 'ro': 'ro-RO', 'hu': 'hu-HU', 'uk': 'uk-UA',
    'bg': 'bg-BG', 'hr': 'hr-HR', 'sk': 'sk-SK', 'ca': 'ca-ES', 'ms': 'ms-MY', 'tl': 'fil-PH',
    'bn': 'bn-BD', 'ta': 'ta-IN', 'te': 'te-IN', 'ml': 'ml-IN', 'mr': 'mr-IN', 'gu': 'gu-IN',
    'kn': 'kn-IN', 'pa': 'pa-IN', 'ur': 'ur-PK', 'fa': 'fa-IR', 'sr': 'sr-RS', 'sl': 'sl-SI',
    'lt': 'lt-LT', 'lv': 'lv-LV', 'et': 'et-EE', 'is': 'is-IS', 'sw': 'sw-KE', 'af': 'af-ZA',
    'am': 'am-ET', 'az': 'az-AZ', 'eu': 'eu-ES', 'be': 'be-BY', 'bs': 'bs-BA', 'gl': 'gl-ES',
    'ka': 'ka-GE', 'km': 'km-KH', 'lo': 'lo-LA', 'mk': 'mk-MK', 'mn': 'mn-MN', 'ne': 'ne-NP',
    'si': 'si-LK', 'so': 'so-SO', 'uz': 'uz-UZ', 'zu': 'zu-ZA', 'xh': 'xh-ZA', 'cy': 'cy-GB',
    'ga': 'ga-IE', 'mt': 'mt-MT', 'my': 'my-MM',
}

def speech_lang_tag(code):
    return SPEECH_LANG_MAP.get(code, code)

def stt_encoding_for_mime(mime):
    mime = (mime or '').lower()
    if 'ogg' in mime:
        return 'OGG_OPUS'
    if 'mp3' in mime or 'mpeg' in mime:
        return 'MP3'
    if 'webm' in mime:
        return 'WEBM_OPUS'
    # Browsers that support none of our requested codecs (notably Safari, which
    # records audio/mp4) fall through here. Cloud Speech-to-Text's sync
    # recognize API has no MP4/AAC encoding at all, so guessing WEBM_OPUS for
    # it would just make Google silently fail to decode the audio (0s billed,
    # empty results) -- return None so the caller can report this precisely
    # instead of spending an API call on a request that can't work.
    return None

def speech_to_text(audio_b64, mime, lang_code):
    """Transcribes a short audio clip via Cloud Speech-to-Text. Returns None on
    failure or silence -- both are expected/frequent (most VAD-segmented clips
    from a live mic are background noise), so this doesn't raise."""
    if not GOOGLE_TRANSLATE_API_KEY:
        app.logger.warning("[speech-to-text] GOOGLE_TRANSLATE_API_KEY is not set")
        return None
    encoding = stt_encoding_for_mime(mime)
    if not encoding:
        app.logger.warning(f"[speech-to-text] lang={lang_code!r} mime={mime!r} has no supported Cloud STT encoding, skipping")
        return None
    try:
        response = requests.post(
            GOOGLE_SPEECH_TO_TEXT_URL,
            params={"key": GOOGLE_TRANSLATE_API_KEY},
            json={
                "config": {
                    "encoding": encoding,
                    "languageCode": lang_code,
                },
                "audio": {"content": audio_b64},
            },
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results", [])
        if not results:
            # totalBilledTime tells us whether Google actually received/processed
            # real audio (a few seconds billed) or effectively got nothing (~0s),
            # which distinguishes "genuinely quiet/no speech" from a corrupt or
            # truncated audio blob never reaching Google as valid content.
            billed = payload.get("totalBilledTime", "?")
            app.logger.warning(
                f"[speech-to-text] lang={lang_code!r} succeeded but recognized no speech "
                f"(empty results, totalBilledTime={billed}, audio_bytes={len(audio_b64)})"
            )
            return None
        # Google's JSON mapping omits fields left at their default value, so a
        # low-confidence alternative can arrive with no "transcript" key at all
        # (an empty transcript) rather than transcript="". Treat that the same
        # as "no speech recognized" instead of letting it raise a KeyError.
        alternatives = results[0].get("alternatives", [])
        transcript = alternatives[0].get("transcript", "") if alternatives else ""
        if not transcript:
            app.logger.warning(f"[speech-to-text] lang={lang_code!r} succeeded but transcript was empty")
            return None
        app.logger.warning(f"[speech-to-text] lang={lang_code!r} transcript={transcript[:60]!r}")
        return transcript
    except Exception as e:
        app.logger.warning(f"[speech-to-text] lang={lang_code!r} FAILED: {e}")
        return None

def text_to_speech(text, lang_code):
    """Synthesizes natural speech via Cloud Text-to-Speech. Returns base64-encoded
    MP3 audio, or None on failure."""
    if not GOOGLE_TRANSLATE_API_KEY or not text:
        return None
    try:
        response = requests.post(
            GOOGLE_TEXT_TO_SPEECH_URL,
            params={"key": GOOGLE_TRANSLATE_API_KEY},
            json={
                "input": {"text": text},
                "voice": {"languageCode": lang_code, "ssmlGender": "NEUTRAL"},
                "audioConfig": {"audioEncoding": "MP3"},
            },
            timeout=15,
        )
        if not response.ok:
            # Capture Google's actual error body (e.g. "unsupported voice/language")
            # instead of just the generic HTTP status -- requests' exception message
            # alone doesn't include it, and that detail is the difference between a
            # transient failure and a language Cloud TTS has no voice for at all.
            app.logger.warning(f"[text-to-speech] lang={lang_code!r} FAILED: {response.status_code} {response.text[:300]}")
            return None
        return response.json()["audioContent"]
    except Exception as e:
        app.logger.warning(f"[text-to-speech] lang={lang_code!r} FAILED: {e}")
        return None

def generate_unique_code(length):
    while True:
        code = "".join(random.choice(ascii_uppercase) for _ in range(length))
        if code not in rooms:
            break
    return code

def generate_unique_id(length=8):
    return ''.join(random.choice(ascii_uppercase + digits) for _ in range(length))

def new_room(owner_id=None):
    return {"members": [], "messages": [], "call_participants": set(), "owner_id": owner_id}

@app.route("/", methods=["POST", "GET"])
def main():
    return render_template("main.html")


@app.route("/home", methods=["POST", "GET"])
def home():
    session.clear()
    if request.method == "POST":
        name = request.form.get("name")
        code = request.form.get("code")
        language = request.form.get("language")
        join = request.form.get("join", False)
        create = request.form.get("create", False)

        if not name:
            return render_template("home.html", error="Please enter a name!", code=code, name=name)

        if join != False and not code:
            return render_template("home.html", error="Please enter a room code!", code=code, name=name)

        user_id = generate_unique_id()

        room = code
        if create != False:
            room = generate_unique_code(4)
            rooms[room] = new_room(owner_id=user_id)
        elif code not in rooms:
            return render_template("home.html", error="Room does not exist.", code=code, name=name)

        session["room"] = room
        session["name"] = name
        session["language"] = language
        session["user_id"] = user_id
        return redirect(url_for("room"))

    return render_template("home.html")

@app.route("/room")
def room():
    room = session.get("room")
    if room is None or session.get("name") is None or room not in rooms:
        return redirect(url_for("home"))

    is_owner = session.get("user_id") == rooms[room].get("owner_id")
    return render_template("room.html", code=room, is_owner=is_owner)

@app.route('/help')
def help_page():
    return render_template('help.html')

def find_message_by_id(room, message_id):
    if not message_id or room not in rooms:
        return None
    return next((m for m in rooms[room]["messages"] if m.get("id") == message_id), None)

def build_reply_preview(room, reply_to_id, dest_language):
    """Looks up the original message being replied to and translates just its
    preview into the recipient's language -- always from the true original text,
    never from an already-translated copy, so quoted replies don't degrade
    through a second round of translation."""
    original = find_message_by_id(room, reply_to_id)
    if not original:
        return None
    return {
        "name": original["name"],
        "message": safe_translate(original["message"], dest_language),
    }

@socketio.on("message")
def handle_message(data):
    room = session.get("room")
    if room not in rooms:
        return

    sender_name = session.get("name")
    original_message = data["data"]
    is_voice = bool(data.get("is_voice"))
    reply_to_id = data.get("reply_to_id")

    message_id = generate_unique_id()
    content = {
        "id": message_id,
        "name": sender_name,
        "message": original_message,
        "reply_to_id": reply_to_id,
    }
    rooms[room]["messages"].append(content)

    for member in rooms[room]["members"]:
        user_language = member["language"]
        translated_message = safe_translate(original_message, user_language)

        translated_content = {
            "id": message_id,
            "name": sender_name,
            "message": translated_message,
            "is_voice": is_voice,
            "reply_to": build_reply_preview(room, reply_to_id, user_language),
        }

        emit("message", translated_content, room=member["sid"])
        print(f"Sent to {member['name']} ({user_language}): {translated_message}")

@socketio.on("voice_audio")
def handle_voice_audio(data):
    """One VAD-segmented utterance from a live call: transcribe it, translate the
    transcript per recipient, synthesize speech in their language, and send back
    audio only -- no text is ever shown or stored for call audio."""
    room = session.get("room")
    if room not in rooms:
        return

    sender_name = session.get("name")
    sender_language = session.get("language")
    sender_sid = request.sid

    audio_b64 = data.get("content")
    mime = data.get("mime", "audio/webm")
    if not audio_b64:
        return

    # Confirms an utterance actually reached the server at all -- if this
    # line never appears in the logs during a real call, the mic/VAD
    # pipeline in the browser never sent anything, which points at the
    # client (mic permissions, voice-activity threshold never tripping in
    # a noisy room, etc.) rather than the server or the Google APIs.
    app.logger.warning(f"[voice_audio] received from {sender_name!r}, {len(audio_b64)} b64 chars, mime={mime!r}")

    transcript = speech_to_text(audio_b64, mime, speech_lang_tag(sender_language))
    if not transcript or not transcript.strip():
        return

    recipients = [m for m in rooms[room]["members"] if m["sid"] != sender_sid]
    if not recipients:
        app.logger.warning("[voice_audio] no other members in the room to send translated audio to")
        return

    for member in recipients:
        translated_text = safe_translate(transcript, member["language"])
        audio_content = text_to_speech(translated_text, speech_lang_tag(member["language"]))
        if not audio_content:
            app.logger.warning(f"[voice_audio] text_to_speech returned nothing for {member['name']!r}, skipping")
            continue

        emit("call_audio", {"name": sender_name, "audio": audio_content}, room=member["sid"])
        app.logger.warning(f"[voice_audio] sent translated audio to {member['name']!r}")

@socketio.on("connect")
def connect(auth):
    room = session.get("room")
    name = session.get("name")
    language = session.get("language")
    user_id = session.get("user_id")
    sid = request.sid
    if not room or not name:
        return

    if room not in rooms:
        rooms[room] = new_room()

    join_room(room)

    # Add user to the room
    rooms[room]["members"].append({"name": name, "language": language, "sid": sid, "user_id": user_id})

    # Notify all clients in the room about the new user list
    emit("update_users", {"users": rooms[room]["members"]}, room=room)

    # Send existing messages to the newly connected user
    for message in rooms[room]["messages"]:
        translated_message = safe_translate(message["message"], language)
        emit("message", {
            "id": message.get("id"),
            "name": message["name"],
            "message": translated_message,
            "reply_to": build_reply_preview(room, message.get("reply_to_id"), language),
        }, room=sid)

    # Broadcast a message that the user has joined the room
    for member in rooms[room]["members"]:
        user_language = member["language"]
        connect_message = safe_translate(f"{name} has entered the room", user_language)
        emit("message", {"name": name, "message": connect_message}, room=member["sid"])


@socketio.on("disconnect")
def handle_disconnect():
    room = session.get("room")
    name = session.get("name")
    sid = request.sid

    if room in rooms:
        # Remove the user from the room
        rooms[room]["members"] = [member for member in rooms[room]["members"] if member["sid"] != sid]

        # Check if the room is now empty
        if len(rooms[room]["members"]) == 0:
            # Clean up the files associated with this room
            if room in files:
                for file_path in files[room]:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                del files[room]
            del rooms[room]
        else:
            # Update the remaining users with the new user list
            emit("update_users", {"users": rooms[room]["members"]}, room=room)

            # Broadcast a message that the user has left the room
            for member in rooms[room]["members"]:
                user_language = member["language"]
                disconnect_message = safe_translate(f"{name} has left the room", user_language)
                emit("message", {"name": name, "message": disconnect_message}, room=member["sid"])

            # If they dropped off mid-call, let the other participants know so
            # their UI updates instead of thinking a silent participant is still there.
            if room in rooms and sid in rooms[room]["call_participants"]:
                rooms[room]["call_participants"].discard(sid)
                emit("call_left", {"name": name}, room=room)

@socketio.on("kick_user")
def handle_kick_user(data):
    """Lets the room's creator remove another member. Forcing their socket to
    disconnect reuses all the normal disconnect cleanup (member list, call
    state, "has left" message) -- kicking isn't a separate code path."""
    room = session.get("room")
    user_id = session.get("user_id")
    if room not in rooms or rooms[room].get("owner_id") != user_id:
        return

    target_user_id = data.get("target_user_id")
    target_member = next((m for m in rooms[room]["members"] if m["user_id"] == target_user_id), None)
    if not target_member or target_member["user_id"] == user_id:
        return

    emit("kicked", {}, room=target_member["sid"])
    disconnect(sid=target_member["sid"])


@socketio.on("call_invite")
def handle_call_invite():
    """Caller starts ringing everyone else currently in the room."""
    room = session.get("room")
    name = session.get("name")
    if room not in rooms or not name:
        return
    rooms[room]["call_participants"].add(request.sid)
    emit("call_invite", {"name": name}, room=room, include_self=False)


@socketio.on("call_accept")
def handle_call_accept():
    """A callee picked up; they join the call and everyone hears about it."""
    room = session.get("room")
    name = session.get("name")
    if room not in rooms or not name:
        return
    rooms[room]["call_participants"].add(request.sid)
    emit("call_accepted", {"name": name}, room=room)


@socketio.on("call_decline")
def handle_call_decline():
    """A callee declined; only the caller(s) need to know."""
    room = session.get("room")
    name = session.get("name")
    if room not in rooms or not name:
        return
    emit("call_declined", {"name": name}, room=room, include_self=False)


@socketio.on("call_leave")
def handle_call_leave():
    """Someone hung up; if they were the last one in the call, it's over for everyone."""
    room = session.get("room")
    name = session.get("name")
    if room not in rooms or not name:
        return
    rooms[room]["call_participants"].discard(request.sid)
    emit("call_left", {"name": name}, room=room)
import unicodedata

def clean_text(text):
    # Normalize the text to decompose special characters into simpler forms
    text = unicodedata.normalize('NFKD', text)
    
    # Replace common bullet points and special characters
    replacements = {
        '•': '-',  # Bullet point replacement
        '–': '-',  # En-dash replacement
        '—': '-',  # Em-dash replacement
        '�': '',   # Unknown character replacement
        # Add more replacements as needed
    }

    # Perform replacements
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)

    # Strip excessive whitespace and normalize spaces
    text = ' '.join(text.split())
    
    return text

@socketio.on('pdf_file')
def handle_pdf_file(data):
    room = session.get("room")
    if room not in rooms:
        return

    file_content = data['content']
    filename = data['filename']

    # Convert PDF to TXT
    pdf_file = BytesIO(file_content)
    pdf_reader = PyPDF2.PdfReader(pdf_file)
    text_content = ""

    # Extract and clean text from each page, ignoring images and handling special characters
    for page_num in range(len(pdf_reader.pages)):
        page = pdf_reader.pages[page_num]
        if page is not None:
            page_text = page.extract_text()
            if page_text:
                cleaned_text = clean_text(page_text)
                text_content += cleaned_text + "\n\n"  # Add page breaks

    if not text_content:
        print(f"No text found in PDF: {filename}")
        return  # Skip translation if no text was extracted

    # Translate the TXT file content for each user
    for member in rooms.get(room, {}).get("members", []):
        user_language = member["language"]
        translated_text = safe_translate(text_content, user_language)
        translated_txt_file = BytesIO(translated_text.encode('utf-8'))
        translated_txt_filename = f"{filename.replace('.pdf', f'_{user_language}.txt')}"

        # Save the translated TXT file
        translated_txt_file_path = os.path.join('static', translated_txt_filename)
        with open(translated_txt_file_path, 'wb') as f:
            f.write(translated_txt_file.getvalue())

        # Notify room with the translated TXT file URL
        file_url = url_for('static', filename=translated_txt_filename)
        content = {
            "name": session.get("name"),
            "file_url": file_url,
            "filename": translated_txt_filename
        }
        emit("pdf_message", content, room=member["sid"])
        print(f"Sent translated TXT file {translated_txt_filename} to {member['name']}")


def notify_server_error(room, message):
    for member in rooms.get(room, {}).get("members", []):
        user_language = member["language"]
        translated_message = safe_translate(message, user_language)
        emit("server_error", {"message": translated_message}, room=member["sid"])

if __name__ == "__main__":
    try:
        port = int(os.environ.get('PORT', 5000))
        socketio.run(app, host='0.0.0.0', port=port)
    except Exception as e:
        for room in rooms:
            notify_server_error(room, "Server encountered an issue. Please leave the room.")
        print("Server encountered an issue:", e)