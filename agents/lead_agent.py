# agents/lead_agent.py
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict, List
from loguru import logger
import unicodedata
import re

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None  # opcional

from config.settings import settings
from database.database import db_manager
from services.whatsapp_service import whatsapp_service
from services.calendar_service import calendar_service
from utils.helpers import rag

# ---------- Normalização / Mapeamento de rótulos ----------
_BTN_MAP = {
    # feedback
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
    # interesse
    "tenho muito interesse": "interesse_alto",
    "tenho interesse": "interesse_medio",
    "talvez futuramente": "interesse_futuro",
    "nao tenho interesse": "sem_interesse",
    "não tenho interesse": "sem_interesse",
    # reunião/preferência
    "sim quero uma reuniao": "aceita_reuniao",
    "sim, quero uma reuniao": "aceita_reuniao",
    "agendar 15 min": "aceita_reuniao",
    "prefiro whatsapp": "prefere_whatsapp",
    "falo por whatsapp": "prefere_whatsapp",
    "enviem por email": "prefere_email",
    "prefiro email": "prefere_email",
    "sem tempo agora": "sem_tempo",
}

def _strip_emoji(s: str) -> str:
    return "".join(ch for ch in s if not unicodedata.category(ch).startswith("So"))

def _normalize(s: str) -> str:
    s = _strip_emoji(s or "")
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9\s\-:]+", "", s)
    return re.sub(r"\s+", " ", s).strip()

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
    neutral   = ("ok", "legal", "bom", "interessante", "mais ou menos", "neutro")
    if any(p in t for p in positives):
        return "feedback_positivo"
    if any(n in t for n in negatives):
        return "feedback_negativo"
    if any(nu in t for nu in neutral):
        return "feedback_neutro"
    return None


class LeadAgent:
    """Agente de IA para converter leads pós-seminário com botões + conversação livre"""

    def __init__(self):
        api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
        self.anthropic = Anthropic(api_key=api_key) if (Anthropic and api_key) else None
        self.model = getattr(settings, "CLAUDE_MODEL", "claude-3-5-sonnet-latest")

    # ========================= ENTRADA PRINCIPAL =========================
    async def handle_message(self, phone: str, message: str, user_name: str = "Cliente") -> str:
        raw = (message or "").strip()
        logger.info(f"[handle_message] {phone} -> '{raw}'")

        # 1) se já é ID esperado
        if raw.startswith(('feedback_', 'interesse_', 'aceita_', 'prefere_', 'sem_', 'horario_')):
            return await self._handle_button_response(phone, raw, user_name)

        # 2) mapear rótulo → id
        mapped = map_label_to_id(raw)
        if mapped:
            logger.info(f"[map_label_to_id] '{raw}' -> '{mapped}'")
            return await self._handle_button_response(phone, mapped, user_name)

        # 3) texto livre que parece feedback
        inferred = infer_feedback_from_free_text(raw)
        if inferred:
            logger.info(f"[infer_feedback] '{raw}' -> '{inferred}'")
            return await self.handle_feedback_response(phone, inferred, user_name)

        # 4) conversa livre
        return await self._handle_free_conversation(phone, raw, user_name)

    # ========================= BOTÕES =========================
    async def _handle_button_response(self, phone: str, response_id: str, user_name: str) -> str:
        if response_id.startswith('feedback_'):
            return await self.handle_feedback_response(phone, response_id, user_name)
        if response_id.startswith('interesse_'):
            return await self.handle_interest_response(phone, response_id, user_name)
        if response_id in ['aceita_reuniao', 'prefere_whatsapp', 'prefere_email', 'sem_tempo']:
            return await self.handle_meeting_response(phone, response_id, user_name)
        if response_id.startswith('horario_'):
            return await self._handle_time_selection(phone, response_id, user_name)
        return await self._handle_free_conversation(phone, f"Selecionou: {response_id}", user_name)

    # ========================= SNAPSHOT DE HISTÓRICO =========================
    def _snapshot_history(self, history_raw: List) -> List[Dict[str, str]]:
        """
        Converte objetos ORM (potencialmente desanexados) em uma lista de dicts.
        QUALQUER erro de sessão é engolido e retornamos o que for possível.
        """
        snap: List[Dict[str, str]] = []
        try:
            for m in history_raw or []:
                try:
                    role = getattr(m, "role", None)
                    message = getattr(m, "message", None)
                except Exception as e:
                    logger.error(f"[history] ORM desanexado ao acessar atributos: {e}")
                    return snap  # devolve o que já deu certo
                snap.append({"role": role or "", "message": message or ""})
        except Exception as e:
            logger.error(f"[history] Falha ao materializar histórico: {e}")
        return snap

    # ========================= CONVERSA LIVRE =========================
    async def _handle_free_conversation(self, phone: str, message: str, user_name: str) -> str:
        try:
            # cria lead se não existir (NÃO usamos campos do ORM depois!)
            if not db_manager.get_lead_by_phone(phone):
                db_manager.create_lead(phone=phone, name=user_name, source="pos_seminario")

            db_manager.save_chat_message(phone=phone, role="user", message=message)

            chat_history_raw = db_manager.get_chat_history(phone, limit=6)
            chat_history = self._snapshot_history(chat_history_raw)
            stage = self._determine_conversation_stage(chat_history)

            response = await self._generate_contextual_response(
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
            fallback = "Ops, tive um probleminha aqui! Me dá uns segundinhos?"
            await whatsapp_service.send_text_message(phone, fallback)
            return fallback

    def _determine_conversation_stage(self, chat_history: List[Dict[str, str]]) -> str:
        if not chat_history:
            return "inicial"

        # marcador que enviamos após a 1ª pergunta
        if any((m.get("role") == "assistant" and "PerguntaFeedback:" in (m.get("message") or ""))
               for m in chat_history[-6:]):
            return "pos_feedback_pergunta"

        user_msgs = [m for m in chat_history if m.get("role") == "user"]
        last_two = [m.get("message", "") for m in user_msgs[-2:]]
        if any('feedback:' in msg for msg in last_two):
            return "pos_feedback"
        if any('interesse:' in msg for msg in last_two):
            return "pos_interesse"
        if any('meeting_pref:' in msg for msg in last_two):
            return "pos_reuniao"
        return "conversa_livre"

    async def _generate_contextual_response(self, message: str, user_name: str, phone: str,
                                            stage: str, chat_history: List[Dict[str, str]]) -> str:
        rag_context = rag.search_context(f"{message} {stage}")

        conversation_context = ""
        for msg in reversed(chat_history[-4:]):
            role = "Humano" if msg.get("role") == "user" else "Assistente"
            conversation_context += f"{role}: {msg.get('message','')}\n"

        system_prompt = f"""Você é a Nat, consultora do CENAT conversando por WhatsApp com {user_name}.

CONTEXTO DO RAG:
{rag_context}

SITUAÇÃO ATUAL: {stage}
HISTÓRICO RECENTE:
{conversation_context}

OBJETIVO PRINCIPAL: Converter leads pós-seminário em reuniões comerciais.

ESTRATÉGIA POR ETAPA:
- inicial: Perguntar sobre o seminário
- pos_feedback_pergunta/pos_feedback: Oferecer desconto e checar interesse
- pos_interesse: Propor reunião com comercial
- pos_reuniao: Facilitar agendamento
- conversa_livre: Responder e conduzir ao agendamento

JEITO DE FALAR:
- Natural, brasileira, consultiva; emojis moderados
- Respostas curtas (máx. 3 linhas)
- Se perguntarem preços, direcione para reunião
- Não invente nada fora do RAG
"""

        if not self.anthropic:
            return self._fallback_by_stage(stage, user_name)

        async def _call_anthropic():
            try:
                def _inner():
                    resp = self.anthropic.messages.create(
                        model=self.model,
                        max_tokens=180,
                        temperature=0.7,
                        system=system_prompt,
                        messages=[{"role": "user", "content": f"MENSAGEM ATUAL: {message}"}],
                    )
                    parts = getattr(resp, "content", []) or []
                    texts = [p.text for p in parts if getattr(p, "type", "") == "text"]
                    return ("\n".join(texts)).strip() or ""
                return await asyncio.to_thread(_inner)
            except Exception as e:
                logger.error(f"LLM erro: {e}")
                return ""

        try:
            result = await asyncio.wait_for(_call_anthropic(), timeout=10)
            return result or self._fallback_by_stage(stage, user_name)
        except asyncio.TimeoutError:
            logger.warning("LLM timeout")
            return self._fallback_by_stage(stage, user_name)

    def _fallback_by_stage(self, stage: str, user_name: str) -> str:
        fallbacks = {
            "inicial": f"Oi {user_name}! Como você achou nosso seminário?",
            "pos_feedback_pergunta": "Perfeito! Quer que eu te mostre as opções com desconto?",
            "pos_feedback": "Legal! Quer conhecer as opções que combinam com você?",
            "pos_interesse": "Maravilha. Prefere WhatsApp ou já agendamos 15 min?",
            "pos_reuniao": "Fechado. Se surgir um tempinho, me chama que agendamos rapidinho.",
            "conversa_livre": "Entendi. Posso te passar as opções e garantir um descontinho?",
        }
        return fallbacks.get(stage, "Me conta um pouco mais pra eu te ajudar melhor!")

    # ========================= FLUXO =========================
    async def start_post_seminar_campaign(self, phone: str, name: str, seminario_nome: str = None) -> bool:
        try:
            db_manager.create_lead(phone=phone, name=name, source="pos_seminario")
            ok = await self._send_feedback_question(phone, name, seminario_nome)
            if ok:
                db_manager.save_chat_message(phone=phone, role="assistant", message="PerguntaFeedback: enviada")
                db_manager.log_interaction(
                    phone=phone,
                    interaction_type="pos_seminario_inicio",
                    message_sent="Pergunta sobre satisfação com botões",
                )
                db_manager.update_lead_status(phone, "CONTACTED")
                logger.info(f"✅ Campanha pós-seminário iniciada: {name} ({phone})")
            return ok
        except Exception as e:
            logger.error(f"❌ Erro ao iniciar campanha pós-seminário {phone}: {e}")
            return False

    async def _send_feedback_question(self, phone: str, name: str, seminario_nome: str = None) -> bool:
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
        if not db_manager.get_lead_by_phone(phone):
            db_manager.create_lead(phone=phone, name=user_name, source="pos_seminario")

        db_manager.save_chat_message(phone=phone, role="user", message=f"feedback:{response_id}")

        if response_id in ["feedback_positivo", "feedback_bom", "feedback_neutro"]:
            await self._send_discount_offer(phone, user_name)
            db_manager.update_lead_status(phone, "INTERESTED")
            return "Oferta de desconto enviada"

        msg = (
            f"Obrigada pelo retorno, {user_name}! 🙏\n"
            "Posso te mandar um material resumido do seminário e, se fizer sentido, "
            "te explico as trilhas de pós que mais combinam com você."
        )
        await whatsapp_service.send_text_message(phone, msg)
        return "Feedback negativo - enviada alternativa"

    async def _send_discount_offer(self, phone: str, name: str) -> bool:
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
        db_manager.save_chat_message(phone=phone, role="user", message=f"interesse:{response_id}")

        if response_id in ["interesse_alto", "interesse_medio"]:
            await self._send_meeting_proposal(phone, user_name)
            db_manager.update_lead_status(phone, "QUALIFIED")
            return "Proposta de reunião enviada"

        if response_id == "interesse_futuro":
            msg = (
                f"Tranquilo, {user_name}! Vou te avisar quando abrirmos novas turmas. 😉\n"
                "Se mudar de ideia antes, é só me chamar."
            )
            await whatsapp_service.send_text_message(phone, msg)
            db_manager.update_lead_status(phone, "FUTURE_INTEREST")
            return "Interesse futuro registrado"

        msg = (
            f"Sem problema, {user_name}! Obrigada por participar do seminário. 🙌\n"
            "Se precisar de algo ou mudar de ideia, me chama por aqui."
        )
        await whatsapp_service.send_text_message(phone, msg)
        db_manager.update_lead_status(phone, "LOST")
        return "Sem interesse - agradecimento"

    async def _send_meeting_proposal(self, phone: str, name: str) -> bool:
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
        db_manager.save_chat_message(phone=phone, role="user", message=f"meeting_pref:{response_id}")

        if response_id == "aceita_reuniao":
            return await self._send_available_times(phone, user_name)

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

        msg = "Tranquilo! Quando surgir um tempinho, me chama que agendamos rapidinho. 😉"
        await whatsapp_service.send_text_message(phone, msg)
        db_manager.update_lead_status(phone, "FUTURE_MEETING")
        return "Sem tempo"

    async def _send_available_times(self, phone: str, user_name: str) -> str:
        try:
            available_slots = calendar_service.get_available_slots(days_ahead=7, duration_minutes=30)
            if not available_slots:
                msg = (f"Deixa eu verificar nossa agenda, {user_name}! Em alguns minutos te passo horários. "
                       "Qual seu e-mail para eu adiantar sua ficha?")
                await whatsapp_service.send_text_message(phone, msg)
                return "Verificando horários"

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
                text=f"Ótimo, {user_name}! 📅\n\nTemos estes horários livres:",
                title="Qual horário é melhor para você?",
                description="Escolha o que funciona melhor",
                sections=sections,
            )
            if success:
                db_manager.save_chat_message(phone=phone, role="system",
                                             message=f"available_slots:{available_slots[:5]}")
            return "Horários enviados"
        except Exception as e:
            logger.error(f"❌ Erro ao buscar horários: {e}")
            await whatsapp_service.send_text_message(phone, "Já organizo nossa agenda e te retorno com os horários!")
            return "Erro ao buscar horários"

    async def _handle_time_selection(self, phone: str, response_id: str, user_name: str) -> str:
        try:
            # lê histórico cru e tira snapshot seguro
            chat_history_raw = db_manager.get_chat_history(phone, limit=10)
            chat_history = self._snapshot_history(chat_history_raw)

            available_slots = None
            for msg in reversed(chat_history):
                if msg.get("role") == "system" and "available_slots:" in (msg.get("message") or ""):
                    import ast
                    slots_str = msg["message"].replace("available_slots:", "")
                    available_slots = ast.literal_eval(slots_str)
                    break

            if not available_slots:
                await whatsapp_service.send_text_message(phone, "Deixa eu verificar os horários de novo!")
                return "Verificando horários novamente"

            slot_number = int(response_id.replace("horario_", ""))
            slot_index = slot_number - 1
            if slot_index >= len(available_slots):
                await whatsapp_service.send_text_message(phone, "Ops, esse horário não está mais disponível.")
                return "Horário indisponível"

            selected_slot = available_slots[slot_index]
            meeting_ok = await self._schedule_meeting(phone, user_name, selected_slot)
            if meeting_ok:
                start = selected_slot['datetime_str']
                msg = (f"Pronto! ✅\n\nAgendado para {start}.\n"
                       "Acabei de enviar o convite no seu e-mail. Até lá! 😊")
                await whatsapp_service.send_text_message(phone, msg)
                db_manager.update_lead_status(phone, "SCHEDULED")
                return "Reunião agendada com sucesso"

            await whatsapp_service.send_text_message(phone, "Deu um probleminha para agendar. Vou tentar de novo!")
            return "Erro no agendamento"

        except Exception as e:
            logger.error(f"❌ Erro ao processar seleção: {e}")
            await whatsapp_service.send_text_message(phone, "Deixa eu organizar isso melhor e já te retorno!")
            return "Erro ao processar horário"

    async def _schedule_meeting(self, phone: str, user_name: str, selected_slot: Dict) -> bool:
        """Agenda no Google Calendar (sem ler campos do ORM)."""
        try:
            start_time = selected_slot.get("start")
            if not isinstance(start_time, datetime):
                try:
                    start_time = datetime.fromisoformat(str(start_time))
                except Exception:
                    start_time = datetime.utcnow()
            end_time = start_time + timedelta(minutes=30)

            meeting_result = calendar_service.schedule_lead_meeting(
                lead_name=user_name,
                lead_phone=phone,
                lead_email=None,  # mantenha None para evitar ORM desprendido
                preferred_time=start_time,
            )
            if meeting_result:
                db_manager.save_calendar_event(
                    phone=phone,
                    google_event_id=meeting_result['event_id'],
                    title=f"Reunião - {user_name}",
                    start_time=start_time,
                    end_time=end_time,
                    attendee_email=None,
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
