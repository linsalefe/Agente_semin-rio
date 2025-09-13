from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship
from datetime import datetime

Base = declarative_base()

class Lead(Base):
    __tablename__ = "leads"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    phone = Column(String(20), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    email = Column(String(100))
    source = Column(String(50), default="seminario")  # seminário, site, indicação, etc
    status = Column(String(20), default="NEW")  # NEW, CONTACTED, QUALIFIED, SCHEDULED, CONVERTED, LOST
    first_contact = Column(DateTime, default=datetime.now)
    last_interaction = Column(DateTime, default=datetime.now)
    last_intent = Column(String(50))
    notes = Column(Text)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # Relacionamentos
    interactions = relationship("Interaction", back_populates="lead")
    chat_messages = relationship("ChatMessage", back_populates="lead")

class Interaction(Base):
    __tablename__ = "interactions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    interaction_type = Column(String(50), nullable=False)  # whatsapp, email, call, meeting
    status = Column(String(20), default="pending")  # pending, completed, failed
    message_sent = Column(Text)
    response_received = Column(Text)
    response_time_minutes = Column(Integer)
    created_at = Column(DateTime, default=datetime.now)
    scheduled_for = Column(DateTime)
    completed_at = Column(DateTime)
    
    # Relacionamento
    lead = relationship("Lead", back_populates="interactions")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    role = Column(String(10), nullable=False)  # user, assistant
    message = Column(Text, nullable=False)
    intent = Column(String(50))
    timestamp = Column(DateTime, default=datetime.now)
    
    # Relacionamento
    lead = relationship("Lead", back_populates="chat_messages")

class ScheduledAction(Base):
    __tablename__ = "scheduled_actions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    action_type = Column(String(50), nullable=False)  # initial_contact, follow_up, reminder
    message_template = Column(String(100))
    scheduled_for = Column(DateTime, nullable=False)
    executed = Column(Boolean, default=False)
    executed_at = Column(DateTime)
    attempt_number = Column(Integer, default=1)
    max_attempts = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.now)

class CalendarEvent(Base):
    __tablename__ = "calendar_events"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    google_event_id = Column(String(100))
    title = Column(String(200), nullable=False)
    description = Column(Text)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    attendee_email = Column(String(100))
    status = Column(String(20), default="scheduled")  # scheduled, completed, cancelled
    created_at = Column(DateTime, default=datetime.now)