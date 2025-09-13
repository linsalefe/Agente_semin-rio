import os
from typing import Dict, ClassVar
from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

# Carregar variáveis de ambiente
load_dotenv()

class Settings(BaseSettings):
    # Claude API  
    ANTHROPIC_API_KEY: str = ""
    CLAUDE_MODEL: str = "claude-sonnet-4-20250514"
    
    # Mega API WhatsApp
    MEGA_API_BASE_URL: str = "https://apistart03.megaapi.com.br"
    MEGA_API_TOKEN: str = ""
    MEGA_INSTANCE_ID: str = ""
    
    # Database
    DATABASE_URL: str = "sqlite:///cenat_leads.db"
    
    # Google Calendar
    GOOGLE_CALENDAR_CREDENTIALS_PATH: str = "credentials.json"
    GOOGLE_CALENDAR_ID: str = ""
    
    # Agent Settings
    BATCH_SIZE: int = 10
    DELAY_BETWEEN_MESSAGES: int = 30
    MAX_RETRIES: int = 3
    WEBHOOK_URL: str = ""
    
    # Anti-loop
    IGNORE_FROM_ME: bool = True
    DEDUP_TTL: float = 12.0
    
    # Lead Status (ClassVar para não ser campo do modelo)
    LEAD_STATUS: ClassVar[Dict[str, str]] = {
        "NEW": "novo",
        "CONTACTED": "contatado", 
        "QUALIFIED": "qualificado",
        "SCHEDULED": "agendado",
        "CONVERTED": "convertido",
        "LOST": "perdido"
    }
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    
    def validate_settings(self):
        """Valida configurações obrigatórias"""
        required_settings = [
            "ANTHROPIC_API_KEY",
            "MEGA_API_TOKEN", 
            "MEGA_INSTANCE_ID"
        ]
        
        missing = []
        for setting in required_settings:
            if not getattr(self, setting):
                missing.append(setting)
        
        if missing:
            raise ValueError(f"Configurações obrigatórias não definidas: {', '.join(missing)}")
        
        return True

# Instância global das configurações
settings = Settings()