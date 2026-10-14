"""Ask Agent - Ollama on wilma"""
import json
import urllib.request
from datetime import date

OLLAMA_URL = "http://localhost:11434"
OLLAMA_MODEL = "hermes3:8b"

def ollama_generate(prompt):
    payload = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=payload, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read()).get("response", "")

async def ask_agent_ollama(question, user_id, username="unknown"):
    today = date.today()
    prompt = f"You are a theme park data analyst. Today is {today}.\\nQuestion: {question}\\nAnswer:"
    answer = ollama_generate(prompt)
    return answer[:1900] if len(answer) > 1900 else answer
