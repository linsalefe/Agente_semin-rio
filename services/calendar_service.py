import os
import json
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from loguru import logger
from config.settings import settings

class CalendarService:
    """Serviço para integração com Google Calendar"""
    
    # Escopos necessários para o Google Calendar
    SCOPES = ['https://www.googleapis.com/auth/calendar']
    
    def __init__(self):
        self.service = None
        self.calendar_id = settings.GOOGLE_CALENDAR_ID
        self.credentials_path = settings.GOOGLE_CALENDAR_CREDENTIALS_PATH
        self.token_path = "token.json"
        self._authenticate()
    
    def _authenticate(self):
        """Autentica com Google Calendar"""
        creds = None
        
        # Verifica se já existe token salvo
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, self.SCOPES)
        
        # Se não há credenciais válidas, faz o fluxo de autenticação
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:
                    logger.error(f"Erro ao renovar token: {e}")
                    creds = None
            
            if not creds:
                if not os.path.exists(self.credentials_path):
                    logger.error(f"Arquivo de credenciais não encontrado: {self.credentials_path}")
                    return
                
                try:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        self.credentials_path, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                except Exception as e:
                    logger.error(f"Erro na autenticação: {e}")
                    return
            
            # Salva as credenciais para próximas execuções
            if creds:
                with open(self.token_path, 'w') as token:
                    token.write(creds.to_json())
        
        try:
            self.service = build('calendar', 'v3', credentials=creds)
            logger.info("✅ Google Calendar conectado com sucesso")
        except Exception as e:
            logger.error(f"❌ Erro ao conectar Google Calendar: {e}")
    
    def get_available_slots(self, days_ahead: int = 7, duration_minutes: int = 60) -> List[Dict]:
        """Busca horários disponíveis para agendamento"""
        if not self.service:
            logger.error("Google Calendar não está conectado")
            return []
        
        # Horários comerciais: 9h às 18h, seg-sex
        business_hours = {
            'start': 9,  # 9h
            'end': 18,   # 18h
            'weekdays': [0, 1, 2, 3, 4]  # seg-sex (0=segunda)
        }
        
        available_slots = []
        
        try:
            for day_offset in range(1, days_ahead + 1):
                target_date = datetime.now() + timedelta(days=day_offset)
                
                # Só dias úteis
                if target_date.weekday() not in business_hours['weekdays']:
                    continue
                
                # Busca eventos do dia
                start_of_day = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
                end_of_day = target_date.replace(hour=23, minute=59, second=59, microsecond=0)
                
                events_result = self.service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=start_of_day.isoformat() + 'Z',
                    timeMax=end_of_day.isoformat() + 'Z',
                    singleEvents=True,
                    orderBy='startTime'
                ).execute()
                
                events = events_result.get('items', [])
                
                # Gera slots disponíveis
                current_time = target_date.replace(hour=business_hours['start'], minute=0, second=0, microsecond=0)
                end_time = target_date.replace(hour=business_hours['end'], minute=0, second=0, microsecond=0)
                
                while current_time + timedelta(minutes=duration_minutes) <= end_time:
                    slot_end = current_time + timedelta(minutes=duration_minutes)
                    
                    # Verifica se o slot está livre
                    is_free = True
                    for event in events:
                        event_start = datetime.fromisoformat(event['start'].get('dateTime', event['start'].get('date')).replace('Z', '+00:00'))
                        event_end = datetime.fromisoformat(event['end'].get('dateTime', event['end'].get('date')).replace('Z', '+00:00'))
                        
                        # Converte para timezone local se necessário
                        if event_start.tzinfo:
                            event_start = event_start.replace(tzinfo=None)
                        if event_end.tzinfo:
                            event_end = event_end.replace(tzinfo=None)
                        
                        # Verifica conflito
                        if (current_time < event_end and slot_end > event_start):
                            is_free = False
                            break
                    
                    if is_free:
                        available_slots.append({
                            'start': current_time,
                            'end': slot_end,
                            'date_str': current_time.strftime('%d/%m/%Y'),
                            'time_str': current_time.strftime('%H:%M'),
                            'datetime_str': current_time.strftime('%d/%m/%Y às %H:%M')
                        })
                    
                    current_time += timedelta(minutes=30)  # Slots a cada 30 min
            
            logger.info(f"🗓️ Encontrados {len(available_slots)} horários disponíveis")
            return available_slots[:10]  # Limita a 10 slots
            
        except HttpError as e:
            logger.error(f"❌ Erro ao buscar agenda: {e}")
            return []
    
    def create_event(self, title: str, description: str, start_time: datetime, 
                    end_time: datetime, attendee_email: str = None, 
                    attendee_phone: str = None) -> Optional[str]:
        """Cria evento no calendário"""
        if not self.service:
            logger.error("Google Calendar não está conectado")
            return None
        
        # Prepara descrição com informações do lead
        full_description = f"{description}\n\n"
        if attendee_phone:
            full_description += f"📱 Telefone: {attendee_phone}\n"
        if attendee_email:
            full_description += f"📧 Email: {attendee_email}\n"
        
        full_description += f"\n🎯 Origem: Seminário DH e Populações Vulnerabilizadas"
        full_description += f"\n⏰ Agendado via: Agente de IA CENAT"
        
        event = {
            'summary': title,
            'description': full_description,
            'start': {
                'dateTime': start_time.isoformat(),
                'timeZone': 'America/Sao_Paulo',
            },
            'end': {
                'dateTime': end_time.isoformat(),
                'timeZone': 'America/Sao_Paulo',
            },
            'attendees': [],
            'reminders': {
                'useDefault': False,
                'overrides': [
                    {'method': 'email', 'minutes': 24 * 60},  # 1 dia antes
                    {'method': 'popup', 'minutes': 60},       # 1 hora antes
                ],
            },
            'conferenceData': {
                'createRequest': {
                    'requestId': f"cenat-{int(datetime.now().timestamp())}",
                    'conferenceSolutionKey': {'type': 'hangoutsMeet'}
                }
            }
        }
        
        # Adiciona participante se tiver email
        if attendee_email:
            event['attendees'].append({'email': attendee_email})
        
        try:
            event = self.service.events().insert(
                calendarId=self.calendar_id, 
                body=event,
                conferenceDataVersion=1
            ).execute()
            
            event_id = event.get('id')
            meet_link = event.get('conferenceData', {}).get('entryPoints', [{}])[0].get('uri', '')
            
            logger.info(f"📅 Evento criado: {title} - {start_time.strftime('%d/%m/%Y %H:%M')}")
            
            return {
                'event_id': event_id,
                'meet_link': meet_link,
                'calendar_link': event.get('htmlLink', ''),
                'start_time': start_time,
                'end_time': end_time
            }
            
        except HttpError as e:
            logger.error(f"❌ Erro ao criar evento: {e}")
            return None
    
    def schedule_lead_meeting(self, lead_name: str, lead_phone: str, 
                            lead_email: str = None, preferred_time: datetime = None) -> Optional[Dict]:
        """Agenda reunião específica para lead"""
        
        # Se não especificou horário, pega o próximo disponível
        if not preferred_time:
            available_slots = self.get_available_slots(days_ahead=14)
            if not available_slots:
                logger.error("Nenhum horário disponível encontrado")
                return None
            preferred_time = available_slots[0]['start']
        
        # Duração padrão de 60 minutos
        end_time = preferred_time + timedelta(minutes=60)
        
        title = f"Reunião Comercial - {lead_name}"
        description = f"""Reunião comercial com lead do Seminário DH e Populações Vulnerabilizadas.

Lead: {lead_name}
Interesse: Seminário Online CENAT
Status: Qualificado para reunião

Objetivos da reunião:
- Apresentar detalhes do seminário
- Esclarecer dúvidas
- Finalizar inscrição
- Identificar outras necessidades"""
        
        result = self.create_event(
            title=title,
            description=description,
            start_time=preferred_time,
            end_time=end_time,
            attendee_email=lead_email,
            attendee_phone=lead_phone
        )
        
        if result:
            logger.info(f"🎯 Reunião agendada para {lead_name}: {preferred_time.strftime('%d/%m/%Y %H:%M')}")
        
        return result
    
    def get_upcoming_events(self, days_ahead: int = 7) -> List[Dict]:
        """Lista próximos eventos"""
        if not self.service:
            return []
        
        try:
            now = datetime.now().isoformat() + 'Z'
            future = (datetime.now() + timedelta(days=days_ahead)).isoformat() + 'Z'
            
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=now,
                timeMax=future,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            
            formatted_events = []
            for event in events:
                start = event['start'].get('dateTime', event['start'].get('date'))
                formatted_events.append({
                    'id': event['id'],
                    'title': event.get('summary', 'Sem título'),
                    'start': start,
                    'description': event.get('description', ''),
                    'attendees': event.get('attendees', [])
                })
            
            return formatted_events
            
        except HttpError as e:
            logger.error(f"❌ Erro ao listar eventos: {e}")
            return []
    
    def format_available_times_message(self, available_slots: List[Dict]) -> str:
        """Formata horários disponíveis para mensagem WhatsApp"""
        if not available_slots:
            return "❌ Não encontrei horários disponíveis nos próximos dias. Vou verificar outras opções!"
        
        message = "🗓️ **HORÁRIOS DISPONÍVEIS PARA REUNIÃO:**\n\n"
        
        for i, slot in enumerate(available_slots[:5], 1):
            message += f"{i}️⃣ {slot['datetime_str']}\n"
        
        message += f"\nEscolha um dos números acima ou me informe outro horário de sua preferência! 😊"
        
        return message

# Instância global
calendar_service = CalendarService()