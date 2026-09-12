from flask import Flask, render_template, request, session, redirect, url_for, send_file
from flask_socketio import join_room, leave_room, send, SocketIO, emit
from dotenv import load_dotenv
import os
import random
from string import ascii_uppercase, digits
import googletrans
from googletrans import Translator

# googletrans==3.1.0a0 ships a hardcoded language list that predates Google
# Translate's 2022 addition of Akan and other languages; the live API supports
# it, so the client-side validation just needs to be told about it.
googletrans.LANGUAGES['ak'] = 'akan'
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
translator = Translator()

rooms = {} #Dictionary to store the list of rooms
files = {} #Dictionary to store the list of files

def safe_translate(text, dest):
    """Translate text, falling back to the original on failure instead of
    crashing the whole broadcast loop (one bad translation shouldn't stop
    every other room member from getting the message). Logs the real error
    so it's visible in the server logs (e.g. Render's Logs tab) instead of
    silently showing up as "translation didn't happen"."""
    try:
        return translator.translate(text, src='auto', dest=dest).text
    except Exception as e:
        app.logger.error(f"Translation to '{dest}' failed: {e}")
        return text

def generate_unique_code(length):
    while True:
        code = "".join(random.choice(ascii_uppercase) for _ in range(length))
        if code not in rooms:
            break
    return code

def generate_unique_id(length=8):
    return ''.join(random.choice(ascii_uppercase + digits) for _ in range(length))

def new_room():
    return {"members": [], "messages": [], "call_participants": set()}

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

        room = code
        if create != False:
            room = generate_unique_code(4)
            rooms[room] = new_room()
        elif code not in rooms:
            return render_template("home.html", error="Room does not exist.", code=code, name=name)

        session["room"] = room
        session["name"] = name
        session["language"] = language
        session["user_id"] = generate_unique_id()
        return redirect(url_for("room"))

    return render_template("home.html")

@app.route("/room")
def room():
    room = session.get("room")
    if room is None or session.get("name") is None or room not in rooms:
        return redirect(url_for("home"))

    return render_template("room.html", code=room)

@app.route('/help')
def help_page():
    return render_template('help.html')

@socketio.on("message")
def handle_message(data):
    room = session.get("room")
    if room not in rooms:
        return

    sender_name = session.get("name")
    original_message = data["data"]
    is_voice = bool(data.get("is_voice"))

    content = {
        "name": sender_name,
        "message": original_message
    }
    rooms[room]["messages"].append(content)

    for member in rooms[room]["members"]:
        user_language = member["language"]
        translated_message = safe_translate(original_message, user_language)

        translated_content = {
            "name": sender_name,
            "message": translated_message,
            "is_voice": is_voice
        }

        emit("message", translated_content, room=member["sid"])
        print(f"Sent to {member['name']} ({user_language}): {translated_message}")

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
        emit("message", {"name": message["name"], "message": translated_message}, room=sid)

    # Broadcast a message that the user has joined the room
    for member in rooms[room]["members"]:
        user_language = member["language"]
        connect_message = safe_translate(f"{name} has entered the room", user_language)
        emit("message", {"name": name, "message": connect_message}, room=member["sid"])


@socketio.on("disconnect")
def disconnect():
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