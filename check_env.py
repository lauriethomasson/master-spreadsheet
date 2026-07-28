import os

from env_utils import load_dotenv

load_dotenv()

for name in ["GEMINI_API_KEY", "GOOGLE_GEOCODING_API_KEY"]:
    print(f"{name}: {'loaded' if os.environ.get(name) else 'missing'}")
