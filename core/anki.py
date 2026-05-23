import requests

ANKI_URL = "http://127.0.0.1:8765"
DECK_NAME = "My_Daily_English"
MODEL_NAME = "English_White_Method"


def anki(action, **params):
    r = requests.post(ANKI_URL, json={"action": action, "version": 6, "params": params})
    result = r.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect: {result['error']}")
    return result["result"]
