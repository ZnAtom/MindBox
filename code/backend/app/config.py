from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Settings:
    api_url: str
    api_key: str
    model: str
    verify_tls: bool = True
    attempts: int = 4
    timeout: float = 120

    @classmethod
    def load(cls):
        load_dotenv(ROOT / '.env')
        return cls(os.getenv('MIND_BOX_API_URL', ''), os.getenv('MIND_BOX_API_KEY', ''),
                   os.getenv('MIND_BOX_MODEL', 'deepseek-flash'),
                   os.getenv('MIND_BOX_VERIFY_TLS', 'true').lower() == 'true',
                   max(1, min(6, int(os.getenv('MIND_BOX_API_ATTEMPTS', '4')))),
                   float(os.getenv('MIND_BOX_API_TIMEOUT', '120')))
