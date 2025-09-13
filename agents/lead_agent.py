# agents/lead_agent.py
import asyncio
import re
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from loguru import logger
import unicodedata

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None

from config.settings import settings
from database.database import db_manager
from services.whatsapp_service import whatsapp_service
from services.calendar_service import calendar_service
from utils.helpers import rag

# ---------- Utilitários ----------
def _strip_emoji(s: str) -> str:
    return "".join(ch for ch in s if not unicodedata.category(ch).startswith("So"))

def _normalize(s: str) -> str:
    s = _strip_emoji(s or "")
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s\-:@.]+", "", s)
    return re.sub(r"\s+", " ", s).strip()

def _is_email(text: str) -> bool:
    """Detecta se o texto é um email"""
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(email_pattern, text.strip()))

# Mapeamento de respostas de botões
_BTN_MAP = {
    "gostei muito": "feedback_positivo",
    "amei": "feedback_positivo", 
    "muito bom": "feedback_positivo",
    "gostei": "feedback_bom",
    "foi ok": "feedback_neutro",
    "ok": "feedback_neutro",
    "legal": "feedback_neutro",
    "mais ou menos": "feedback_neutro",
    "nao gostei": "feedback_negativo",
    "não gostei": "feedback_negativo",
    "ruim": "feedback_negativo",
    "tenho muito interesse": "interesse_alto",
    "tenho interesse": "interesse_medio",
    "talvez futuramente": "interesse_futuro", 
    "nao tenho interesse": "sem_interesse",
    "não tenho interesse": "sem_interesse",
    "sim quero uma reuniao": "aceita_reuniao",
    "sim, quero uma reuniao": "aceita_reuniao",
    "agendar 15 min": "aceita_reuniao",
    "prefiro whatsapp": "prefere_whatsapp",
    "falo por whatsapp": "prefere_whatsapp",
    "enviem por email": "prefere_email",
    "prefiro email": "prefere_email",
    "sem tempo agora": "sem_tempo",
}

def map_label_to_id(label: str) -> Optional[str]:
    if not label:
        return None
    key = _normalize(label)
    if key in _BTN_MAP:
        return _BTN_MAP[key]
    for k, v in _BTN_MAP.items():
        if _normalize(k) in key or key in _normalize(k):
            return v
    return None

def infer_feedback_from_free_text(text: str) -> Optional[str]:
    t = _normalize(text)
    positives = ("gostei", "otimo", "ótimo", "excelente", "amei", "muito bom", "maravilho", "aprendi")
    negatives = ("nao gostei", "não gostei", "ruim", "horrivel", "horrível", "pessimo", "péssimo", "decepcion")
    neutral = ("ok", "legal", "bom", "interessante", "mais ou menos", "neutro")
    if any(p in t for p in positives):
        return "feedback_positivo"
    if any(n in t for n in negatives):
        return "feedback_negativo"
    if any(nu in t for nu in neutral):
        return "feedback_neutro"
    return None


class LeadAgent:
    """Agente de IA para converter leads pós-seminário"""

    def __init__(self):
        api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
        self.anthropic = Anthropic(api_key=api_key) if (Anthropic and api_key) else None
        self.model = getattr(settings, "CLAUDE_MODEL", "claude-3-5-sonnet-latest")

    # ========================= ENTRADA PRINCIPAL =========================
    async def handle_message(self, phone: str, message: str, user_name: str = "Cliente") -> str:
        raw = (message or "").strip()
        logger.info(f"[handle_message] {phone} -> '{raw}'")

        # 1) Verifica se é resposta de botão/ID conhecido
        if raw.startswith(('feedback_', 'interesse_', 'aceita_', 'prefere_', 'sem_', 'horario_')):
            return await self._handle_button_response(phone, raw, user_name)

        # 2) Mapeia texto para ID de botão
        mapped = map_label_to_id(raw)
        if mapped:
            logger.info(f"[map_label_to_id] '{raw}' -> '{mapped}'")
            return await self._handle_button_response(phone, mapped, user_name)

        # 3) Detecta email - PRIORIDADE ALTA
        if _is_email(raw):
            return await self._handle_email_provided(phone, raw, user_name)

        # 4) Inferir feedback de texto livre
        inferred = infer_feedback_from_free_text(raw)
        if inferred:
            logger.info(f"[infer_feedback] '{raw}' -> '{inferred}'")
            return await self.handle_feedback_response(phone, inferred, user_name)

        # 5) Conversa livre com contexto melhorado
        return await self._handle_free_conversation(phone, raw, user_name)

    # ========================= TRATAMENTO DE EMAIL =========================
    async def _handle_email_provided(self, phone: str, email: str, user_name: str) -> str:
        """Trata quando usuário fornece um email"""
        try:
            logger.info(f"📧 Email recebido de {phone}: {email}")
            
            # Salva o email no histórico
            db_manager.save_chat_message(phone=phone, role="user", message=f"email:{email}")

            # Verifica o contexto com histórico mais amplo
            chat_history = self._get_chat_history_safe(phone, limit=12)
            
            # MELHORIA: Verifica se tem contexto de reunião aceita
            if self._has_meeting_accepted_context(chat_history):
                logger.info(f"🎯 Contexto de reunião aceita detectado para {phone}")
                return await self._process_scheduling_with_email(phone, user_name, email)
            else:
                logger.info(f"ℹ️ Contexto geral de email para {phone}")
                # Contexto geral de email fornecido
                msg = f"Perfeito, {user_name}! Salvei seu email: {email}\n\nVou te enviar as informações por lá também. Te retorno em breve! 😊"
                await whatsapp_service.send_text_message(phone, msg)
                db_manager.update_lead_status(phone, "EMAIL_PROVIDED")
                return "Email salvo - contexto geral"

        except Exception as e:
            logger.error(f"❌ Erro ao processar email {phone}: {e}")
            await whatsapp_service.send_text_message(phone, "Email recebido! Vou organizar isso pra você.")
            return "Erro ao processar email"

    def _has_meeting_accepted_context(self, chat_history: List[Dict]) -> bool:
        """Verifica se há contexto de reunião aceita recentemente - MELHORADO"""
        logger.info(f"🔍 Verificando contexto de reunião aceita")
        
        for i, msg in enumerate(chat_history[-10:]):  # últimas 10 mensagens
            content = msg.get("message", "").lower()
            role = msg.get("role", "")
            
            logger.debug(f"Msg {i}: {role} -> {content[:50]}...")
            
            # Busca por indicadores de reunião aceita
            meeting_indicators = [
                "meeting_pref:aceita_reuniao",
                "sim, quero uma reunião",
                "agendar 20–30 min",
                "qual seu e-mail para eu adiantar",
                "deixa eu verificar nossa agenda",
                "te passo horários",
                "waiting_email_for_scheduling"
            ]
            
            for indicator in meeting_indicators:
                if indicator in content:
                    logger.info(f"✅ Indicador encontrado: {indicator}")
                    return True
        
        logger.info(f"❌ Nenhum contexto de reunião aceita encontrado")
        return False

    async def _process_scheduling_with_email(self, phone: str, user_name: str, email: str) -> str:
        """Processa agendamento quando email é fornecido"""
        try:
            logger.info(f"📅 Iniciando processo de agendamento para {phone}")
            
            # Busca horários disponíveis
            available_slots = calendar_service.get_available_slots(days_ahead=7, duration_minutes=30)
            
            if not available_slots:
                msg = f"Ótimo, {user_name}! Email salvo: {email}\n\nEstou organizando nossa agenda e te retorno com os horários disponíveis em poucos minutos!"
                await whatsapp_service.send_text_message(phone, msg)
                logger.warning(f"⚠️ Nenhum horário disponível encontrado")
                return "Nenhum horário disponível"

            logger.info(f"🗓️ {len(available_slots)} horários encontrados")

            # Envia horários disponíveis
            sections = [{"title": "🗓️ Horários disponíveis", "rows": []}]
            
            for i, slot in enumerate(available_slots[:5], 1):
                sections[0]["rows"].append({
                    "title": f"📅 {slot['datetime_str']}",
                    "description": "Reunião de 30 minutos",
                    "rowId": f"horario_{i}",
                })

            success = await whatsapp_service.send_list_message(
                phone=phone,
                button_text="Escolher Horário",
                text=f"Perfeito, {user_name}! Email salvo: {email}\n\n🗓️ Horários disponíveis para nossa reunião:",
                title="Quando é melhor para você?",
                description="Escolha o horário ideal",
                sections=sections,
            )

            if success:
                # Salva os slots E o email para referência futura
                db_manager.save_chat_message(phone=phone, role="system", 
                                           message=f"available_slots:{available_slots[:5]}")
                db_manager.save_chat_message(phone=phone, role="system", 
                                           message=f"email_saved:{email}")
                db_manager.update_lead_status(phone, "SCHEDULING")
                logger.info(f"✅ Horários enviados com sucesso para {phone}")
                return "Horários enviados com email salvo"
            else:
                await whatsapp_service.send_text_message(phone, "Te retorno com os horários em instantes!")
                logger.error(f"❌ Falha ao enviar lista de horários")
                return "Erro ao enviar horários"

        except Exception as e:
            logger.error(f"❌ Erro no processo de agendamento: {e}")
            await whatsapp_service.send_text_message(phone, "Salvei seu email! Te retorno com os horários.")
            return "Erro no agendamento"

    # ========================= HISTÓRICO SEGURO =========================
    def _get_chat_history_safe(self, phone: str, limit: int = 10) -> List[Dict[str, str]]:
        """Recupera histórico de forma segura"""
        try:
            history_raw = db_manager.get_chat_history(phone, limit=limit)
            history_safe = []
            for m in history_raw or []:
                try:
                    role = getattr(m, "role", None)
                    message = getattr(m, "message", None) 
                    history_safe.append({"role": role or "", "message": message or ""})
                except Exception as e:
                    logger.warning(f"Erro ao acessar mensagem do histórico: {e}")
                    break
            return history_safe
        except Exception as e:
            logger.error(f"Erro ao recuperar histórico: {e}")
            return []

    # ========================= BOTÕES =========================
    async def _handle_button_response(self, phone: str, response_id: str, user_name: str) -> str:
        if response_id.startswith('feedback_'):
            return await self.handle_feedback_response(phone, response_id, user_name)
        elif response_id.startswith('interesse_'):
            return await self.handle_interest_response(phone, response_id, user_name)
        elif response_id in ['aceita_reuniao', 'prefere_whatsapp', 'prefere_email', 'sem_tempo']:
            return await self.handle_meeting_response(phone, response_id, user_name)
        elif response_id.startswith('horario_'):
            return await self._handle_time_selection(phone, response_id, user_name)
        else:
            return await self._handle_free_conversation(phone, f"Selecionou: {response_id}", user_name)

    # ========================= CONVERSA LIVRE =========================
    async def _handle_free_conversation(self, phone: str, message: str, user_name: str) -> str:
        try:
            # Cria lead se não existir
            if not db_manager.get_lead_by_phone(phone):
                db_manager.create_lead(phone=phone, name=user_name, source="pos_seminario")

            db_manager.save_chat_message(phone=phone, role="user", message=message)

            # Recupera histórico seguro
            chat_history = self._get_chat_history_safe(phone, limit=8)
            
            # Determina estágio da conversa
            stage = self._determine_conversation_stage_improved(chat_history, message)
            
            # Gera resposta contextual
            response = await self._generate_improved_response(
                message=message,
                user_name=user_name,
                phone=phone,
                stage=stage,
                chat_history=chat_history,
            )

            db_manager.save_chat_message(phone=phone, role="assistant", message=response)
            await whatsapp_service.send_text_message(phone, response)
            return response

        except Exception as e:
            logger.error(f"❌ Erro na conversa livre {phone}: {e}")
            fallback = "Deixa eu organizar as informações aqui e já te retorno!"
            await whatsapp_service.send_text_message(phone, fallback)
            return fallback

    def _determine_conversation_stage_improved(self, chat_history: List[Dict], current_message: str) -> str:
        """Determina estágio da conversa de forma melhorada"""
        
        # Se histórico vazio, é inicial
        if not chat_history:
            return "inicial"

        # Analisa últimas mensagens para contexto
        recent_messages = [msg.get("message", "") for msg in chat_history[-6:]]
        recent_context = " ".join(recent_messages).lower()

        # Verifica se acabou de fornecer email
        if _is_email(current_message):
            return "email_fornecido"

        # Contextos específicos baseados no histórico
        if "email:" in recent_context:
            return "pos_email"
        elif "meeting_pref:aceita_reuniao" in recent_context:
            return "pos_aceite_reuniao" 
        elif "meeting_pref:" in recent_context:
            return "pos_reuniao"
        elif "interesse:" in recent_context:
            return "pos_interesse"
        elif "feedback:" in recent_context:
            return "pos_feedback"
        elif any("PerguntaFeedback:" in msg for msg in recent_messages):
            return "pos_feedback_pergunta"

        return "conversa_livre"

    async def _generate_improved_response(self, message: str, user_name: str, phone: str,
                                        stage: str, chat_history: List[Dict]) -> str:
        """Gera resposta melhorada baseada no contexto"""
        
        # Respostas diretas para estágios específicos
        if stage == "email_fornecido":
            return f"Email recebido, {user_name}! Vou organizar as informações e te retorno."
        
        if stage == "pos_aceite_reuniao":
            return "Te passo os horários disponíveis em instantes!"

        # Para outros estágios, usa prompt melhorado
        return await self._call_llm_with_improved_prompt(message, user_name, stage, chat_history)

    async def _call_llm_with_improved_prompt(self, message: str, user_name: str, 
                                           stage: str, chat_history: List[Dict]) -> str:
        """Chama LLM com prompt melhorado e mais restritivo"""
        
        if not self.anthropic:
            return self._get_fallback_response(stage, user_name)

        # Contexto de conversa recente
        conversation_context = ""
        for msg in reversed(chat_history[-4:]):
            role = "Cliente" if msg.get("role") == "user" else "Nat"
            conversation_context += f"{role}: {msg.get('message','')}\n"

        system_prompt = f"""Você é a Nat, consultora do CENAT falando por WhatsApp.

SITUAÇÃO ATUAL: {stage}
CONVERSA RECENTE:
{conversation_context}

REGRAS OBRIGATÓRIAS:
- MÁXIMO 2 linhas de resposta
- Seja natural e brasileira
- NÃO invente informações
- NÃO mencione outros seminários
- Mantenha foco: converter para reunião
- Se perguntarem preços: "te explico na reunião"
- Use poucos emojis

CONTEXTO: Lead pós-seminário que precisa ser convertido em reunião comercial.

Responda APENAS à mensagem atual, sem fugir do assunto."""

        try:
            async def _call_anthropic():
                try:
                    resp = self.anthropic.messages.create(
                        model=self.model,
                        max_tokens=100,  # REDUZIDO para evitar respostas longas
                        temperature=0.5,  # REDUZIDO para mais precisão
                        system=system_prompt,
                        messages=[{"role": "user", "content": f"Cliente disse: {message}"}],
                    )
                    parts = getattr(resp, "content", []) or []
                    texts = [p.text for p in parts if getattr(p, "type", "") == "text"]
                    return ("\n".join(texts)).strip()
                except Exception as e:
                    logger.error(f"Erro LLM: {e}")
                    return ""

            result = await asyncio.wait_for(_call_anthropic(), timeout=8)
            return result or self._get_fallback_response(stage, user_name)

        except asyncio.TimeoutError:
            logger.warning("LLM timeout")
            return self._get_fallback_response(stage, user_name)

    def _get_fallback_response(self, stage: str, user_name: str) -> str:
        """Respostas fallback por estágio"""
        fallbacks = {
            "inicial": f"Oi {user_name}! Como você achou nosso seminário?",
            "pos_feedback": "Legal! Te mostro as opções de pós-graduação?",  
            "pos_interesse": "Perfeito! Prefere conversar por aqui ou agendar uns minutinhos?",
            "pos_reuniao": "Tranquilo! Qualquer coisa me chama que organizamos.",
            "email_fornecido": f"Email salvo, {user_name}! Te retorno com as informações.",
            "conversa_livre": "Entendi. Posso te explicar melhor sobre as oportunidades?",
        }
        return fallbacks.get(stage, "Deixa eu organizar isso pra você!")

    # ========================= RESTO DOS MÉTODOS (mantidos iguais) =========================
    async def start_post_seminar_campaign(self, phone: str, name: str, seminario_nome: str = None) -> bool:
        """Inicia campanha pós-seminário"""
        try:
            db_manager.create_lead(phone=phone, name=name, source="pos_seminario")
            ok = await self._send_feedback_question(phone, name, seminario_nome)
            if ok:
                db_manager.save_chat_message(phone=phone, role="assistant", message="PerguntaFeedback: enviada")
                db_manager.log_interaction(phone=phone, interaction_type="pos_seminario_inicio", 
                                         message_sent="Pergunta sobre satisfação com botões")
                db_manager.update_lead_status(phone, "CONTACTED")
                logger.info(f"✅ Campanha pós-seminário iniciada: {name} ({phone})")
            return ok
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar campanha pós-seminário {phone}: {e}")
            return False

    async def _send_feedback_question(self, phone: str, name: str, seminario_nome: str = None) -> bool:
        """Envia pergunta inicial sobre satisfação"""
        if not seminario_nome:
            try:
                seminario_info = rag.get_current_seminario()
                seminario_nome = seminario_info.get("nome", "")
            except Exception:
                seminario_nome = ""

        sections = [{
            "title": "🎯 O que você achou do seminário?",
            "rows": [
                {"title": "😊 Gostei muito!", "description": "Foi ótimo, aprendi bastante", "rowId": "feedback_positivo"},
                {"title": "👍 Gostei", "description": "Atendeu minhas expectativas", "rowId": "feedback_bom"},
                {"title": "😐 Mais ou menos", "description": "Poderia ser melhor", "rowId": "feedback_neutro"},
                {"title": "👎 Não gostei", "description": "Não atendeu minhas expectativas", "rowId": "feedback_negativo"},
            ],
        }]

        return await whatsapp_service.send_list_message(
            phone=phone,
            button_text="Avaliar Seminário", 
            text=f"Oi {name}! Aqui é a Nat, da equipe CENAT.\n\n"
                 f"Vi que você participou do nosso seminário"
                 f"{f' de {seminario_nome}' if seminario_nome else ''}.\n\n"
                 "💬 *Pode responder pelos botões ou conversar comigo livremente!*",
            title="Como foi sua experiência?",
            description="Sua opinião é muito importante para nós!",
            sections=sections,
        )

    async def handle_feedback_response(self, phone: str, response_id: str, user_name: str = "Cliente") -> str:
        """Trata resposta de feedback"""
        if not db_manager.get_lead_by_phone(phone):
            db_manager.create_lead(phone=phone, name=user_name, source="pos_seminario")

        db_manager.save_chat_message(phone=phone, role="user", message=f"feedback:{response_id}")

        if response_id in ["feedback_positivo", "feedback_bom", "feedback_neutro"]:
            await self._send_discount_offer(phone, user_name)
            db_manager.update_lead_status(phone, "INTERESTED")
            return "Oferta de desconto enviada"

        # Feedback negativo
        msg = (f"Obrigada pelo retorno, {user_name}! 🙏\n"
               "Posso te mandar um material resumido e, se fizer sentido, "
               "te explico outras opções que combinam mais com você.")
        await whatsapp_service.send_text_message(phone, msg)
        return "Feedback negativo - enviada alternativa"

    async def _send_discount_offer(self, phone: str, name: str) -> bool:
        """Envia oferta de desconto"""
        sections = [{
            "title": "🎓 Interesse em Pós-Graduação",
            "rows": [
                {"title": "🤩 Tenho muito interesse!", "description": "Quero saber tudo", "rowId": "interesse_alto"},
                {"title": "🤔 Tenho interesse", "description": "Quero mais detalhes", "rowId": "interesse_medio"},  
                {"title": "🤷 Talvez futuramente", "description": "Não é prioridade agora", "rowId": "interesse_futuro"},
                {"title": "😅 Não tenho interesse", "description": "Não pretendo agora", "rowId": "sem_interesse"},
            ],
        }]

        first = name.split()[0] if name else "Você"
        return await whatsapp_service.send_list_message(
            phone=phone,
            button_text="Meu Interesse",
            text=(f"Que bom que gostou, {first}! 🎉\n\n"
                  "Participantes do seminário têm **5% de desconto** nas pós.\n"
                  "💬 *Use os botões ou me mande uma mensagem!*"),
            title="Quer saber mais sobre a pós?", 
            description="Aproveite o desconto exclusivo para participantes",
            sections=sections,
        )

    async def handle_interest_response(self, phone: str, response_id: str, user_name: str = "Cliente") -> str:
        """Trata resposta sobre interesse"""
        db_manager.save_chat_message(phone=phone, role="user", message=f"interesse:{response_id}")

        if response_id in ["interesse_alto", "interesse_medio"]:
            await self._send_meeting_proposal(phone, user_name)
            db_manager.update_lead_status(phone, "QUALIFIED")
            return "Proposta de reunião enviada"

        if response_id == "interesse_futuro":
            msg = (f"Tranquilo, {user_name}! Vou te avisar quando abrirmos novas turmas. 😉\n"
                   "Se mudar de ideia antes, é só me chamar.")
            await whatsapp_service.send_text_message(phone, msg)
            db_manager.update_lead_status(phone, "FUTURE_INTEREST")
            return "Interesse futuro registrado"

        # Sem interesse
        msg = (f"Sem problema, {user_name}! Obrigada por participar do seminário. 🙌\n"
               "Se precisar de algo ou mudar de ideia, me chama por aqui.")
        await whatsapp_service.send_text_message(phone, msg)
        db_manager.update_lead_status(phone, "LOST")
        return "Sem interesse - agradecimento"

    async def _send_meeting_proposal(self, phone: str, name: str) -> bool:
        """Propõe reunião"""
        sections = [{
            "title": "📞 Conversa com nossa equipe",
            "rows": [
                {"title": "🤝 Sim, quero uma reunião!", "description": "Agendar 20–30 min", "rowId": "aceita_reuniao"},
                {"title": "💬 Prefiro WhatsApp", "description": "Explicar por aqui", "rowId": "prefere_whatsapp"},
                {"title": "📧 Enviem por e-mail", "description": "Receber por e-mail", "rowId": "prefere_email"},
                {"title": "⏰ Não tenho tempo agora", "description": "Fica pra depois", "rowId": "sem_tempo"},
            ],
        }]

        return await whatsapp_service.send_list_message(
            phone=phone,
            button_text="Como Prefere",
            text=(f"Perfeito, {name}! 🎯\n\nPara garantir seu desconto e te explicar direitinho:"),
            title="Como você prefere continuar?",
            description="Escolha a forma mais confortável",
            sections=sections,
        )

    async def handle_meeting_response(self, phone: str, response_id: str, user_name: str = "Cliente") -> str:
        """Trata resposta sobre reunião"""
        db_manager.save_chat_message(phone=phone, role="user", message=f"meeting_pref:{response_id}")

        if response_id == "aceita_reuniao":
            msg = f"Deixa eu verificar nossa agenda, {user_name}! Em alguns minutos te passo horários. Qual seu e-mail para eu adiantar sua ficha?"
            await whatsapp_service.send_text_message(phone, msg)
            db_manager.update_lead_status(phone, "WAITING_EMAIL_FOR_SCHEDULING")
            return "Aguardando email para agendamento"

        if response_id == "prefere_whatsapp":
            msg = f"Ótimo, {user_name}! Te explico por aqui e te mando os próximos passos. 👍"
            await whatsapp_service.send_text_message(phone, msg)
            db_manager.update_lead_status(phone, "TRANSFERRED_WHATSAPP")
            return "WhatsApp preferido"

        if response_id == "prefere_email":
            msg = "Perfeito! Me passa seu melhor e-mail para eu enviar as informações. 📧"
            await whatsapp_service.send_text_message(phone, msg)
            db_manager.update_lead_status(phone, "WAITING_EMAIL")
            return "Aguardando e-mail"

        # sem_tempo
        msg = "Tranquilo! Quando surgir um tempinho, me chama que agendamos rapidinho. 😉"
        await whatsapp_service.send_text_message(phone, msg)
        db_manager.update_lead_status(phone, "FUTURE_MEETING")
        return "Sem tempo"

    async def _handle_time_selection(self, phone: str, response_id: str, user_name: str) -> str:
        """Trata seleção de horário"""
        try:
            chat_history = self._get_chat_history_safe(phone, limit=10)
            
            # Busca slots salvos no histórico
            available_slots = None
            saved_email = None
            
            for msg in reversed(chat_history):
                content = msg.get("message", "")
                if msg.get("role") == "system":
                    if "available_slots:" in content:
                        import ast
                        slots_str = content.replace("available_slots:", "")
                        available_slots = ast.literal_eval(slots_str)
                    elif "email_saved:" in content:
                        saved_email = content.replace("email_saved:", "")

            if not available_slots:
                await whatsapp_service.send_text_message(phone, "Deixa eu verificar os horários de novo!")
                return "Verificando horários novamente"

            slot_number = int(response_id.replace("horario_", ""))
            slot_index = slot_number - 1
            
            if slot_index >= len(available_slots):
                await whatsapp_service.send_text_message(phone, "Ops, esse horário não está mais disponível.")
                return "Horário indisponível"

            selected_slot = available_slots[slot_index]
            meeting_ok = await self._schedule_meeting(phone, user_name, selected_slot, saved_email)
            
            if meeting_ok:
                start = selected_slot['datetime_str']
                msg = (f"Pronto! ✅\n\nAgendado para {start}.\n"
                       "Te enviei o convite por email. Até lá! 😊")
                await whatsapp_service.send_text_message(phone, msg)
                db_manager.update_lead_status(phone, "SCHEDULED")
                return "Reunião agendada com sucesso"

            await whatsapp_service.send_text_message(phone, "Deu um probleminha para agendar. Vou tentar de novo!")
            return "Erro no agendamento"

        except Exception as e:
            logger.error(f"❌ Erro ao processar seleção: {e}")
            await whatsapp_service.send_text_message(phone, "Deixa eu organizar isso melhor e já te retorno!")
            return "Erro ao processar horário"

    async def _schedule_meeting(self, phone: str, user_name: str, selected_slot: Dict, email: str = None) -> bool:
        """Agenda reunião no Google Calendar"""
        try:
            start_time = selected_slot.get("start")
            if not isinstance(start_time, datetime):
                start_time = datetime.fromisoformat(str(start_time))
            
            end_time = start_time + timedelta(minutes=30)

            meeting_result = calendar_service.schedule_lead_meeting(
                lead_name=user_name,
                lead_phone=phone,
                lead_email=email,
                preferred_time=start_time,
            )
            
            if meeting_result:
                db_manager.save_calendar_event(
                    phone=phone,
                    google_event_id=meeting_result['event_id'],
                    title=f"Reunião - {user_name}",
                    start_time=start_time,
                    end_time=end_time,
                    attendee_email=email,
                )
                logger.info(f"📅 Reunião agendada: {user_name} - {start_time}")
                return True
            return False
            
        except Exception as e:
            logger.error(f"❌ Erro ao agendar reunião: {e}")
            return False

    # ========================= UTILITÁRIOS =========================
    async def process_excel_leads(self, excel_file_path: str) -> Dict:
        try:
            import pandas as pd
            df = pd.read_excel(excel_file_path)
            results = {"processed": 0, "errors": []}
            for _, row in df.iterrows():
                try:
                    name = str(row.get('Nome', row.get('name', 'Cliente'))).strip()
                    phone = str(row.get('Telefone', row.get('phone', ''))).strip()
                    email = str(row.get('Email', row.get('email', ''))).strip() if ('Email' in row or 'email' in row) else None
                    if phone and name:
                        db_manager.create_lead(phone=phone, name=name, email=email, source="seminario_excel")
                        results["processed"] += 1
                        logger.info(f"📊 Lead importado: {name} ({phone})")
                except Exception as e:
                    results["errors"].append(f"{row.get('Nome', row.get('name', '?'))}: {e}")
            logger.info(f"✅ Importação concluída: {results['processed']} leads")
            return results
        except Exception as e:
            logger.error(f"❌ Erro ao processar Excel: {e}")
            return {"processed": 0, "errors": [str(e)]}

    async def start_campaign_batch(self, leads: List[Dict], seminario_nome: str = None) -> Dict:
        results = {"sent": 0, "errors": []}
        for lead_data in leads:
            try:
                phone = lead_data.get('phone', lead_data.get('telefone', ''))
                name = lead_data.get('name', lead_data.get('nome', 'Cliente'))
                if phone and name:
                    ok = await self.start_post_seminar_campaign(phone, name, seminario_nome)
                    if ok:
                        results["sent"] += 1
                    else:
                        results["errors"].append(f"Falha ao enviar para {name} ({phone})")
                    await asyncio.sleep(getattr(settings, "DELAY_BETWEEN_MESSAGES", 0.6))
            except Exception as e:
                results["errors"].append(f"{lead_data.get('name', '???')}: {e}")
        logger.info(f"📊 Campanha finalizada: {results['sent']} enviadas, {len(results['errors'])} erros")
        return results


# Instância global
lead_agent = LeadAgent()