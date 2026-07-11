"""CAPHY Phase 8 - Voice control.  Run:  python run_voice.py

Say one of:  arm system  /  disarm system  /  stop siren  /  system status
Disarm asks 'Are you sure?' - answer 'yes' or 'no'.  Press Ctrl+C to stop.
"""
import os
from datetime import datetime

import config
from voice.commands import CommandInterpreter
from storage.database import Database


def log_command(db, action):
    db.conn.execute(
        "INSERT INTO voice_commands(user_id, command_text, language, confirmed, timestamp) "
        "VALUES(1,?,?,1,?)", (action, "en", datetime.now().isoformat(timespec="seconds")))
    db.conn.commit()


def apply(action, db, voice):
    if action == "arm":
        db.conn.execute("UPDATE settings SET armed=1 WHERE setting_id=1")
        db.conn.commit()
    elif action == "disarm":
        db.conn.execute("UPDATE settings SET armed=0 WHERE setting_id=1")
        db.conn.commit()
    elif action == "status":
        s = db.conn.execute("SELECT armed FROM settings WHERE setting_id=1").fetchone()
        voice.say(f"System is {'armed' if s['armed'] else 'disarmed'}, "
                  f"{db.count_alerts()} alerts recorded.")
    # 'stop_siren' actually silences the siren in Phase 10 (integration)
    log_command(db, action)


def main():
    if not os.path.isdir(config.VOSK_MODEL_PATH):
        print(f"[CAPHY] Vosk model not found at '{config.VOSK_MODEL_PATH}'.")
        print("Download 'vosk-model-small-en-us-0.15', unzip it, and put the folder there.")
        return

    from voice.engine import VoskVoice   # imported here so the message above shows first
    voice = VoskVoice(config.VOSK_MODEL_PATH, config.VOICE_SAMPLE_RATE)
    ci = CommandInterpreter()
    db = Database(config.DB_PATH)
    print("[CAPHY] Voice ready. Commands: arm system / disarm system / stop siren / system status.")

    try:
        for text in voice.listen():
            print(f"heard: {text}")
            r = ci.interpret(text)
            if r is None:
                continue
            voice.say(r.speak)
            if r.action:
                apply(r.action, db, voice)
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
        print("\n[CAPHY] Voice stopped.")


if __name__ == "__main__":
    main()