import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from anthropic import Anthropic
from loguru import logger

from config.settings import settings
from database.database import db_manager
from services.whatsapp_service import whatsapp_service
from services.calendar_service import calendar_service
from utils.helpers import rag

class LeadAgent:
    """Agente de IA para agendar reuniões com leads do seminário - COM RAG"""
    
    def __init__(self):
        self.anthropic = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self.model = settings.CLAUDE_MODEL
        
        # Contexto genérico - informações vêm do RAG
        self.context = """
OBJETIVO: Agendar reuniões com leads interessados na pós-graduação

FLUXO NATURAL:
1. Gostou do seminário? 
2. Oferece desconto 5% na pós
3. Propõe conversa de 20 min
4. Mostra horários disponíveis
5. Agenda e finaliza

LINGUAGEM: Natural, brasileira, consultiva
"""
    
    async def start_active_campaign(self, phone: str, name: str, seminario_nome: str = None) -> bool:
        """Inicia campanha ativa pós-seminário"""
        try:
            # Cria/atualiza lead no banco
            lead = db_manager.create_lead(phone=phone, name=name, source="seminario_dh")
            
            # Busca informações do seminário atual via RAG
            seminario_info = rag.get_current_seminario()
            seminario_nome_final = seminario_nome or seminario_info['nome']
            
            # Mensagem inicial natural e genérica
            message = f"""Oi {name}!

Aqui é Nat, da equipe CENAT. Vi que você participou do nosso seminário de {seminario_nome_final}.

E aí, o que achou? Gostou?"""
            
            success = await whatsapp_service.send_text_message(phone, message)
            
            if success:
                db_manager.log_interaction(
                    phone=phone,
                    interaction_type="campanha_ativa_inicio",
                    message_sent=message
                )
                
                db_manager.update_lead_status(phone, "CONTACTED")
                logger.info(f"✅ Campanha iniciada: {name} ({phone})")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar campanha {phone}: {e}")
            return False
    
    async def handle_response(self, phone: str, message: str, user_name: str = "Cliente") -> str:
        """Processa resposta do lead de forma natural"""
        try:
            # Busca lead
            lead = db_manager.get_lead_by_phone(phone)
            if not lead:
                lead = db_manager.create_lead(phone=phone, name=user_name, source="seminario_dh")
            
            # Salva mensagem do usuário
            db_manager.save_chat_message(phone=phone, role="user", message=message)
            
            # Determina resposta baseada no fluxo
            response = await self._determine_response(phone, message, user_name, lead)
            
            # Salva resposta do agente
            db_manager.save_chat_message(phone=phone, role="assistant", message=response)
            
            return response
            
        except Exception as e:
            logger.error(f"❌ Erro ao processar resposta de {phone}: {e}")
            return f"Ops, tive um probleminha aqui! Me dá uns minutinhos?"
    
    async def _determine_response(self, phone: str, message: str, user_name: str, lead) -> str:
        """Determina resposta natural baseada no fluxo"""
        
        # Busca histórico para entender onde estamos
        chat_history = db_manager.get_chat_history(phone, limit=4)
        message_count = len([msg for msg in chat_history if msg.role == "user"])
        
        message_lower = message.lower().strip()
        
        # FLUXO 1: Feedback do seminário
        if message_count == 1:
            if any(word in message_lower for word in ['sim', 'gostei', 'adorei', 'legal', 'bom', 'ótimo', 'otimo', 'amei']):
                response = f"""Que bom! 

Olha, tenho uma notícia boa: quem participou do seminário tem 5% de desconto nas nossas pós-graduações.

Você teria interesse em saber mais sobre isso?"""
                
                db_manager.update_lead_status(phone, "INTERESTED")
                return response
                
            elif any(word in message_lower for word in ['não', 'nao', 'ruim', 'fraco', 'não gostei']):
                response = f"""Poxa, que pena...

O que você sentiu que poderia ter sido melhor? Às vezes conseguimos suprir essas lacunas nas nossas pós-graduações."""
                
                return response
            
            else:
                # Resposta ambígua - Claude responde com contexto do RAG
                return await self._generate_contextual_response(message, user_name, "feedback_seminario")
        
        # FLUXO 2: Interesse na pós
        elif message_count == 2:
            if any(word in message_lower for word in ['sim', 'quero', 'tenho interesse', 'me interessa', 'claro']):
                response = f"""Perfeito!

Pra eu te explicar direitinho como funcionam nossas pós-graduações e garantir seu desconto, que tal conversarmos uns 20 minutinhos?

Pode ser?"""
                
                db_manager.update_lead_status(phone, "QUALIFIED")
                return response
                
            elif any(word in message_lower for word in ['não', 'nao', 'sem interesse', 'não quero']):
                response = f"""Tranquilo!

Obrigada por ter participado do seminário. Se mudar de ideia, me chama aqui!"""
                
                db_manager.update_lead_status(phone, "LOST")
                return response
                
            else:
                return await self._generate_contextual_response(message, user_name, "interesse_pos")
        
        # FLUXO 3: Agendamento da conversa
        elif message_count == 3:
            if any(word in message_lower for word in ['sim', 'pode', 'vamos', 'claro', 'ok', 'tudo bem']):
                return await self._send_available_times(phone, user_name)
                
            elif any(word in message_lower for word in ['não', 'nao', 'sem tempo', 'não posso']):
                response = f"""Sem problema!

Se conseguir um tempinho depois, me avisa. Tô aqui!"""
                
                db_manager.update_lead_status(phone, "LOST")
                return response
                
            else:
                return await self._generate_contextual_response(message, user_name, "agendamento")
        
        # FLUXO 4+: Escolha de horário
        elif message_count >= 4:
            return await self._process_time_selection(phone, message, user_name)
        
        # Fallback
        else:
            return await self._generate_contextual_response(message, user_name, "conversa_livre")
    
    async def _send_available_times(self, phone: str, user_name: str) -> str:
        """Envia horários disponíveis de forma natural"""
        try:
            available_slots = calendar_service.get_available_slots(days_ahead=7, duration_minutes=30)
            
            if not available_slots:
                return "Deixa eu dar uma olhada na agenda e já te falo os horários!"
            
            # Mensagem natural com horários
            response = "Ótimo! Tenho esses horários livres:\n\n"
            
            for i, slot in enumerate(available_slots[:5], 1):
                response += f"{i} - {slot['datetime_str']}\n"
            
            response += "\nQual funciona melhor pra você?"
            
            # Salva slots para referência
            db_manager.save_chat_message(
                phone=phone,
                role="system", 
                message=f"available_slots:{available_slots[:5]}"
            )
            
            return response
            
        except Exception as e:
            logger.error(f"❌ Erro ao buscar horários: {e}")
            return "Deixa eu ver minha agenda e já te falo!"
    
    async def _process_time_selection(self, phone: str, message: str, user_name: str) -> str:
        """Processa escolha de horário"""
        try:
            # Busca slots salvos
            chat_history = db_manager.get_chat_history(phone, limit=10)
            available_slots = None
            
            for msg in reversed(chat_history):
                if msg.role == "system" and "available_slots:" in msg.message:
                    import ast
                    slots_str = msg.message.replace("available_slots:", "")
                    available_slots = ast.literal_eval(slots_str)
                    break
            
            if not available_slots:
                return "Deixa eu verificar os horários de novo!"
            
            # Identifica escolha
            message_lower = message.lower().strip()
            
            selected_slot = None
            if message_lower in ['1', '2', '3', '4', '5']:
                slot_index = int(message_lower) - 1
                if slot_index < len(available_slots):
                    selected_slot = available_slots[slot_index]
            
            if not selected_slot:
                return await self._generate_contextual_response(message, user_name, "escolha_horario")
            
            # Agenda a reunião
            meeting_result = await self._schedule_meeting(phone, user_name, selected_slot)
            
            if meeting_result:
                db_manager.update_lead_status(phone, "SCHEDULED")
                
                response = f"""Pronto! ✅

Agendado pra {selected_slot['datetime_str']}.

Acabei de mandar um convite com o link da reunião no seu email.

Te espero lá! Vai ser ótimo conversar sobre as pós-graduações e garantir seu desconto.

Até mais! 😊"""
                
                return response
            else:
                return "Opa, deu um probleminha pra agendar. Deixa eu tentar de novo!"
            
        except Exception as e:
            logger.error(f"❌ Erro ao processar seleção: {e}")
            return "Deixa eu organizar isso melhor e te retorno!"
    
    async def _schedule_meeting(self, phone: str, user_name: str, selected_slot: Dict) -> bool:
        """Agenda reunião no Google Calendar"""
        try:
            lead = db_manager.get_lead_by_phone(phone)
            
            start_time = datetime.fromisoformat(selected_slot['start'].isoformat() if hasattr(selected_slot['start'], 'isoformat') else str(selected_slot['start']))
            end_time = start_time + timedelta(minutes=30)
            
            meeting_result = calendar_service.schedule_lead_meeting(
                lead_name=user_name,
                lead_phone=phone,
                lead_email=lead.email if lead else None,
                preferred_time=start_time
            )
            
            if meeting_result:
                # Salva evento no banco
                db_manager.save_calendar_event(
                    phone=phone,
                    google_event_id=meeting_result['event_id'],
                    title=f"Reunião - {user_name}",
                    start_time=start_time,
                    end_time=end_time,
                    attendee_email=lead.email if lead else None
                )
                
                logger.info(f"📅 Reunião agendada: {user_name} - {start_time}")
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"❌ Erro ao agendar reunião: {e}")
            return False
    
    async def _generate_contextual_response(self, message: str, user_name: str, context: str) -> str:
        """Gera resposta natural usando Claude + RAG"""
        
        # Busca contexto relevante no RAG baseado na mensagem
        rag_context = rag.search_context(message + " " + context)
        
        system_prompt = f"""Você é uma consultora do CENAT conversando por WhatsApp. Seu objetivo é agendar uma reunião de 20-30 min com pessoas que participaram de seminários.

CONTEXTO DO RAG:
{rag_context}

SITUAÇÃO ATUAL: {context}

JEITO DE FALAR:
- Natural, como uma pessoa real conversando
- Brasileiro, casual mas profissional
- Não repetir o nome toda hora
- Emojis com moderação 
- Respostas curtas (1-2 linhas máximo)
- Consultiva, nunca insistente

OBJETIVO: Agendar reunião para explicar pós-graduações e desconto de 5%

REGRAS:
- Use as informações do RAG quando relevante
- Foco apenas no agendamento da conversa
- Se a pessoa não quiser, respeitar
- Linguagem brasileira casual do dia a dia"""

        try:
            response = self.anthropic.messages.create(
                model=self.model,
                max_tokens=80,
                temperature=0.8,
                system=system_prompt,
                messages=[
                    {"role": "user", "content": message}
                ]
            )
            
            return response.content[0].text.strip()
            
        except Exception as e:
            logger.error(f"❌ Erro Claude: {e}")
            return "Deixa eu te retornar melhor sobre isso!"
    
    async def process_excel_leads(self, excel_file_path: str) -> Dict:
        """Processa planilha Excel com leads do seminário"""
        try:
            import pandas as pd
            
            # Lê planilha
            df = pd.read_excel(excel_file_path)
            
            results = {"processed": 0, "errors": []}
            
            # Processa cada lead
            for _, row in df.iterrows():
                try:
                    name = str(row.get('Nome', row.get('name', 'Cliente'))).strip()
                    phone = str(row.get('Telefone', row.get('phone', ''))).strip()
                    email = str(row.get('Email', row.get('email', ''))).strip() if 'Email' in row or 'email' in row else None
                    
                    if name and phone:
                        # Cria lead no banco
                        lead = db_manager.create_lead(
                            phone=phone,
                            name=name,
                            email=email,
                            source="seminario_dh_excel"
                        )
                        
                        results["processed"] += 1
                        logger.info(f"📊 Lead importado: {name} ({phone})")
                    
                except Exception as e:
                    results["errors"].append(f"Erro linha {len(results['errors']) + 1}: {str(e)}")
            
            logger.info(f"✅ Importação concluída: {results['processed']} leads")
            return results
            
        except Exception as e:
            logger.error(f"❌ Erro ao processar Excel: {e}")
            return {"processed": 0, "errors": [str(e)]}
    
    async def start_campaign_batch(self, leads: List[Dict], seminario_nome: str = None) -> Dict:
        """Inicia campanha para lote de leads"""
        results = {"sent": 0, "errors": []}
        
        # Busca nome do seminário se não fornecido
        if not seminario_nome:
            seminario_info = rag.get_current_seminario()
            seminario_nome = seminario_info['nome']
        
        for lead_data in leads:
            try:
                phone = lead_data.get('phone', lead_data.get('telefone', ''))
                name = lead_data.get('name', lead_data.get('nome', 'Cliente'))
                
                if phone and name:
                    success = await self.start_active_campaign(phone, name, seminario_nome)
                    if success:
                        results["sent"] += 1
                    else:
                        results["errors"].append(f"Falha ao enviar para {name} ({phone})")
                    
                    # Delay entre envios
                    await asyncio.sleep(settings.DELAY_BETWEEN_MESSAGES)
                
            except Exception as e:
                results["errors"].append(f"Erro processando lead: {str(e)}")
        
        logger.info(f"📊 Campanha finalizada: {results['sent']} enviadas, {len(results['errors'])} erros")
        return results

# Instância global
lead_agent = LeadAgent()