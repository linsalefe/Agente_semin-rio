import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from config.settings import settings
from database.models import Base, Lead, Interaction, ChatMessage, ScheduledAction, CalendarEvent
from loguru import logger

class DatabaseManager:
    def __init__(self):
        self.engine = create_engine(settings.DATABASE_URL, echo=False)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        self.init_database()
    
    def init_database(self):
        """Inicializa o banco de dados"""
        try:
            Base.metadata.create_all(bind=self.engine)
            logger.info("✅ Banco de dados inicializado")
        except Exception as e:
            logger.error(f"❌ Erro ao inicializar banco: {e}")
    
    @contextmanager
    def get_session(self):
        """Context manager para sessões do banco"""
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Erro na sessão do banco: {e}")
            raise
        finally:
            session.close()
    
    # =================== LEADS ===================
    def create_lead(self, phone: str, name: str, email: str = None, source: str = "seminario") -> Lead:
        """Cria um novo lead"""
        with self.get_session() as session:
            # Verifica se já existe
            existing = session.query(Lead).filter(Lead.phone == phone).first()
            if existing:
                return existing
            
            lead = Lead(
                phone=phone,
                name=name,
                email=email,
                source=source,
                status="NEW"
            )
            session.add(lead)
            session.flush()
            logger.info(f"📝 Lead criado: {name} ({phone})")
            return lead
    
    def get_lead_by_phone(self, phone: str) -> Optional[Lead]:
        """Busca lead por telefone"""
        with self.get_session() as session:
            return session.query(Lead).filter(Lead.phone == phone).first()
    
    def update_lead_status(self, phone: str, status: str) -> bool:
        """Atualiza status do lead"""
        with self.get_session() as session:
            lead = session.query(Lead).filter(Lead.phone == phone).first()
            if lead:
                lead.status = status
                lead.updated_at = datetime.now()
                logger.info(f"📊 Status atualizado: {phone} -> {status}")
                return True
            return False
    
    def get_leads_by_status(self, status: str) -> List[Lead]:
        """Busca leads por status"""
        with self.get_session() as session:
            return session.query(Lead).filter(Lead.status == status).all()
    
    def get_leads_for_initial_contact(self, limit: int = 10) -> List[Lead]:
        """Busca leads novos para contato inicial"""
        with self.get_session() as session:
            return session.query(Lead).filter(
                Lead.status == "NEW"
            ).limit(limit).all()
    
    # =================== CHAT MESSAGES ===================
    def save_chat_message(self, phone: str, role: str, message: str, intent: str = None):
        """Salva mensagem no histórico"""
        with self.get_session() as session:
            lead = session.query(Lead).filter(Lead.phone == phone).first()
            if not lead:
                logger.warning(f"Lead não encontrado para salvar mensagem: {phone}")
                return
            
            chat_msg = ChatMessage(
                lead_id=lead.id,
                role=role,
                message=message,
                intent=intent
            )
            session.add(chat_msg)
            
            # Atualiza última interação do lead
            lead.last_interaction = datetime.now()
            if intent:
                lead.last_intent = intent
    
    def get_chat_history(self, phone: str, limit: int = 10) -> List[ChatMessage]:
        """Recupera histórico de chat"""
        with self.get_session() as session:
            lead = session.query(Lead).filter(Lead.phone == phone).first()
            if not lead:
                return []
            
            return session.query(ChatMessage).filter(
                ChatMessage.lead_id == lead.id
            ).order_by(ChatMessage.timestamp.desc()).limit(limit).all()
    
    # =================== SCHEDULED ACTIONS ===================
    def schedule_action(self, phone: str, action_type: str, scheduled_for: datetime, 
                       message_template: str = None) -> bool:
        """Agenda uma ação para o lead"""
        with self.get_session() as session:
            lead = session.query(Lead).filter(Lead.phone == phone).first()
            if not lead:
                return False
            
            action = ScheduledAction(
                lead_id=lead.id,
                action_type=action_type,
                message_template=message_template,
                scheduled_for=scheduled_for
            )
            session.add(action)
            logger.info(f"📅 Ação agendada: {action_type} para {phone} em {scheduled_for}")
            return True
    
    def get_pending_actions(self) -> List[Tuple[ScheduledAction, Lead]]:
        """Busca ações pendentes para execução"""
        with self.get_session() as session:
            return session.query(ScheduledAction, Lead).join(
                Lead, ScheduledAction.lead_id == Lead.id
            ).filter(
                ScheduledAction.executed == False,
                ScheduledAction.scheduled_for <= datetime.now(),
                ScheduledAction.attempt_number <= ScheduledAction.max_attempts
            ).all()
    
    def mark_action_executed(self, action_id: int, success: bool = True):
        """Marca ação como executada"""
        with self.get_session() as session:
            action = session.query(ScheduledAction).filter(ScheduledAction.id == action_id).first()
            if action:
                if success:
                    action.executed = True
                    action.executed_at = datetime.now()
                else:
                    action.attempt_number += 1
                logger.info(f"✅ Ação {action_id} marcada como {'executada' if success else 'tentativa falhada'}")
    
    # =================== INTERACTIONS ===================
    def log_interaction(self, phone: str, interaction_type: str, message_sent: str = None, 
                       response_received: str = None) -> bool:
        """Registra uma interação"""
        with self.get_session() as session:
            lead = session.query(Lead).filter(Lead.phone == phone).first()
            if not lead:
                return False
            
            interaction = Interaction(
                lead_id=lead.id,
                interaction_type=interaction_type,
                message_sent=message_sent,
                response_received=response_received,
                status="completed"
            )
            session.add(interaction)
            return True
    
    # =================== CALENDAR EVENTS ===================
    def save_calendar_event(self, phone: str, google_event_id: str, title: str, 
                           start_time: datetime, end_time: datetime, 
                           attendee_email: str = None) -> bool:
        """Salva evento do calendário"""
        with self.get_session() as session:
            lead = session.query(Lead).filter(Lead.phone == phone).first()
            if not lead:
                return False
            
            event = CalendarEvent(
                lead_id=lead.id,
                google_event_id=google_event_id,
                title=title,
                start_time=start_time,
                end_time=end_time,
                attendee_email=attendee_email
            )
            session.add(event)
            logger.info(f"📅 Evento salvo: {title} para {phone}")
            return True
    
    # =================== STATS ===================
    def get_conversion_stats(self) -> Dict:
        """Estatísticas de conversão"""
        with self.get_session() as session:
            total = session.query(Lead).count()
            contacted = session.query(Lead).filter(Lead.status != "NEW").count()
            qualified = session.query(Lead).filter(Lead.status == "QUALIFIED").count()
            scheduled = session.query(Lead).filter(Lead.status == "SCHEDULED").count()
            converted = session.query(Lead).filter(Lead.status == "CONVERTED").count()
            
            return {
                "total_leads": total,
                "contacted": contacted,
                "qualified": qualified,
                "scheduled": scheduled,
                "converted": converted,
                "contact_rate": (contacted / total * 100) if total > 0 else 0,
                "conversion_rate": (converted / total * 100) if total > 0 else 0
            }

# Instância global
db_manager = DatabaseManager()